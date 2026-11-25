"""Tasks API harness: bounded create, poll with state-transition timing, cancel, cost ledger.

Safety rules enforced here (Codex round 2):
- Before a create is sent, a worst-case bound for the task is reserved in the ledger, and the
  Idempotency-Key + body are written to the raw log. An ambiguous create (transport error, 5xx, 408,
  429) is retried with the SAME key and body, so it can never start a second task.
- Only one task may be unresolved at a time: `create` refuses while an earlier task is not terminal.
- `wait` enforces its deadline on every poll outcome (errors included, and polls answering for another
  task id count as errors), honours Retry-After, cancels once at `max_wait_s`, and gives up 120 s
  later, leaving the task unresolved with its hold. Every HTTP call inside gets at most 30 s per
  httpx operation (connect/read/write), capped by the time left; this bounds each step, it is not a
  hard wall-clock guarantee for a slowly trickling reply.
- `cleanup()` (also run on context exit, even after an exception) cancels and settles open tasks.
- After a task ends, its hold is swapped (atomically) for a small traffic hold kept until the portal
  Usage page is reconciled (scripts/reconcile.py), because traffic can be billed after the task
  ends. A task that ends without a reported cost keeps its full hold (re-keyed `unpriced:<id>`).
- Across restarts, any open `task-create:` hold in the ledger blocks new creates until recovered.

Polling (`GET /v1/tasks/{id}`) is not billed per the docs; each poll is still logged.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

from .client import CallRecord, MossHTTP
from .config import ROOT
from .ledger import HoldBlocked, Ledger
from .prices import TASK_BROWSER_USD_PER_HOUR, TASK_MODEL_MARKUP, definitive_refusal
from .runlog import RunLog

TERMINAL = {"completed", "failed", "cancelled"}
INTENTS_PATH = ROOT / "results" / "task_intents.jsonl"  # git-ignored: exact create bytes + keys
IDEMPOTENCY_TTL_S = 23 * 3600  # docs: keys are kept 24 h per project; 1 h margin
RETRY_CREATE = {408, 429, 500, 502, 503, 504}
TRAFFIC_ALLOWANCE_USD = 0.05  # per task, until the portal shows settled traffic
OVERSHOOT_FACTOR = 1.5  # max_cost_usd is a target, not a documented hard stop


class UnresolvedTask(RuntimeError):
    pass


def task_cost(task: dict[str, Any]) -> float | None:
    """The task's reported total cost, or None when the body carries no cost at all."""
    usage = task.get("usage") or {}
    cost = usage.get("cost") or {}
    total = cost.get("total_usd") if isinstance(cost, dict) else None
    if total is None:
        total = usage.get("cost_usd")
    return None if total is None else float(total)


def _num(v: Any, default: float, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return default
    return min(max(float(v), lo), hi)


def task_bound(body: dict[str, Any]) -> float:
    """Worst-case cost we are willing to believe for one task created from `body`.

    Model spend: max_cost_usd (default $2) x markup x overshoot factor; browser: the runtime cap at
    $0.02/h; traffic: a fixed allowance. Values the API should refuse are clamped to the documented
    range, so a wrongly accepted value is still budgeted at the range edge.
    """
    raw_limits = body.get("limits")
    limits: dict[str, Any] = raw_limits if isinstance(raw_limits, dict) else {}
    model_cap = _num(limits.get("max_cost_usd"), 2.0, 0.01, 50.0)
    runtime = _num(limits.get("max_runtime_seconds"), 3600.0, 60.0, 7200.0)
    return (model_cap * TASK_MODEL_MARKUP * OVERSHOOT_FACTOR
            + runtime / 3600 * TASK_BROWSER_USD_PER_HOUR + TRAFFIC_ALLOWANCE_USD)


def _retry_after(rec: CallRecord, default: float) -> float:
    try:
        return min(max(float(rec.headers.get("retry-after", default)), 1.0), 60.0)
    except ValueError:
        return default


def _clean_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Caller headers without any spelling of Idempotency-Key (ours must be the only one)."""
    return {k: v for k, v in (headers or {}).items() if k.lower() != "idempotency-key"}


def _save_intent(row: dict[str, Any]) -> None:
    INTENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with INTENTS_PATH.open("a") as f:
        f.write(json.dumps({"ts": time.time(), **row}) + "\n")


def _load_intents() -> list[dict[str, Any]]:
    if not INTENTS_PATH.exists():
        return []
    return [json.loads(x) for x in INTENTS_PATH.read_text().splitlines() if x.strip()]


def _has_usage(task: dict[str, Any]) -> bool:
    return task_cost(task) is not None


class TaskRunner:
    def __init__(self, http: MossHTTP, ledger: Ledger, log: RunLog, campaign: str) -> None:
        self.http, self.ledger, self.log, self.campaign = http, ledger, log, campaign
        self.open: dict[str, str] = {}  # task_id -> ledger reservation key
        self.bounds: dict[str, float] = {}
        self.needs_reconcile: list[str] = []  # tasks that ended without a reported cost
        self.settled: set[str] = set()  # each task id is settled once

    def __enter__(self) -> TaskRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()

    # --- create --------------------------------------------------------------------------------
    def create(self, body: dict[str, Any] | bytes, headers: dict[str, str] | None = None,
               tag: dict[str, Any] | None = None, idempotency_key: str | None = None,
               bound_usd: float | None = None, max_attempts: int = 3,
               idempotency_test: bool = False) -> CallRecord:
        if self.open:
            raise UnresolvedTask(f"refusing a new create while {list(self.open)} is unresolved")
        key = idempotency_key or f"audit-{uuid.uuid4()}"
        if any(i["key"] == key for i in _load_intents()):
            # A recorded intent is bound to its first body; reusing the key would overwrite it.
            raise ValueError(f"Idempotency-Key {key} already has a recorded intent")
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        if bound_usd is None:
            try:
                parsed = json.loads(payload)
            except ValueError:
                parsed = {}
            bound_usd = task_bound(parsed if isinstance(parsed, dict) else {})
        res_key = f"task-create:{key}"
        # Intent first, hold second: an intent without a hold is inert (recover() skips it); a hold
        # without an intent cannot happen, so every hold stays recoverable.
        _save_intent({"key": key, "hold": res_key, "bound_usd": bound_usd, "campaign": self.campaign,
                      "project_id": self.http.project_id, "created": time.time(),
                      "body_b64": base64.b64encode(payload).decode(),
                      "headers": _clean_headers(headers),
                      # recover() never resends an idempotency-test send (manual settlement)
                      "idempotency_test": idempotency_test})
        try:  # the "no other unresolved create" check and the hold are one locked step
            self.ledger.reserve(self.campaign, res_key, bound_usd, block_prefix="task-create:")
        except HoldBlocked as exc:
            raise UnresolvedTask(f"{exc}; run TaskRunner.recover() first") from exc
        self.log.note(event="task_create_intent", idempotency_key=key, bound_usd=bound_usd,
                      body=payload.decode(errors="replace"))
        return self._send_create(payload, key, res_key, bound_usd, headers, tag, max_attempts)

    def recover(self, max_attempts: int = 3) -> dict[str, Any]:
        """Replay unresolved creates with their exact bytes and Idempotency-Key, on the existing hold.

        Same key + same body returns the original task (documented replay, keys live 24 h per
        project), so this never starts a second task. Only a returned task id clears the hold: any
        refusal on replay says nothing about the first attempt, so the hold is kept. Intents that are
        too old or belong to another project are not replayed; they and holds without an intent are
        reported for a manual portal check (scripts/reconcile.py --after-portal-check)."""
        report: dict[str, Any] = {"recovered": [], "kept": [], "manual": []}
        intents = {i["hold"]: i for i in _load_intents()}
        for hold in self.ledger.open_holds("task-create:"):
            intent = intents.get(hold)
            if intent is None:
                report["manual"].append({"hold": hold, "why": "no intent recorded"})
                continue
            if intent.get("idempotency_test"):
                # Resending is exactly what is in doubt for these: never replay them. Read the
                # known task by GET, and settle the rest from the portal.
                report["manual"].append({"hold": hold, "why": "idempotency-test send: check the "
                                         "portal (and GET replay_of)",
                                         "replay_of": intent.get("replay_of")})
                continue
            if intent.get("project_id") != self.http.project_id:
                report["manual"].append({"hold": hold, "why": "intent from another project"})
                continue
            if time.time() - float(intent.get("created", 0)) > IDEMPOTENCY_TTL_S:
                report["manual"].append({"hold": hold, "why": "idempotency key older than 23 h"})
                continue
            payload = base64.b64decode(intent["body_b64"])
            self.log.note(event="task_create_recover", idempotency_key=intent["key"])
            try:
                rec = self._send_create(payload, intent["key"], hold, intent["bound_usd"],
                                        intent.get("headers"), {"recover": True}, max_attempts,
                                        prior_ambiguity=True)
                report["recovered"].append({"hold": hold, "task_id": rec.body.get("id"),
                                            "replayed": rec.headers.get("idempotent-replayed")})
            except UnresolvedTask as exc:
                report["kept"].append({"hold": hold, "why": str(exc)})
        self.log.note(event="task_recover_report", **report)
        return report

    def _send_create(self, payload: bytes, key: str, res_key: str, bound_usd: float,
                     headers: dict[str, str] | None, tag: dict[str, Any] | None,
                     max_attempts: int, prior_ambiguity: bool = False,
                     http: MossHTTP | None = None) -> CallRecord:
        hdrs = {**_clean_headers(headers), "Content-Type": "application/json",
                "Idempotency-Key": key}
        rec: CallRecord | None = None
        ambiguous_seen = prior_ambiguity
        for attempt in range(1, max_attempts + 1):
            rec = (http or self.http).call("POST", "/tasks", raw_content=payload, headers=hdrs,
                                           tag={**(tag or {}), "attempt": attempt})
            self.log.call(rec)
            if rec.error is None and rec.status not in RETRY_CREATE:
                break
            ambiguous_seen = True
            time.sleep(_retry_after(rec, 2.0 * attempt))
        assert rec is not None
        task_id = rec.body.get("id") if isinstance(rec.body, dict) else None
        if rec.status in (200, 201, 202) and isinstance(task_id, str) and task_id.startswith("task_"):
            self.open[task_id] = res_key
            self.bounds[task_id] = bound_usd
            # Links the hold to the task even before any cost is reported (max-per-task rule).
            self.ledger.task_snapshot(self.campaign, task_id, task_cost(rec.body) or 0.0,
                                      key=res_key, status=rec.body.get("status"))
        elif not ambiguous_seen and definitive_refusal(rec.status, rec.error, 0, rec.body):
            self.ledger.release(res_key)  # refused before any task existed
        else:
            # A task may exist. Keep the hold; recover() replays the same key and bytes.
            self.log.note(event="task_create_ambiguous", idempotency_key=key, status=rec.status,
                          error=rec.error)
            raise UnresolvedTask(f"ambiguous create (key {key}); hold kept, run recover()")
        return rec

    # --- idempotency replays (TSK-07') -----------------------------------------------------------
    def replay(self, payload: bytes, key: str, expected_id: str | None, bound_usd: float,
               tag: dict[str, Any], http: MossHTTP | None = None) -> CallRecord:
        """Send a create that reuses `key`, under its own fresh `task-create:` hold and intent.

        The hold is taken before sending, so the create gate, recover() and reconcile.py all protect
        it if the process dies. Outcomes: same id as `expected_id` -> the extra hold is released with
        that proof (the original hold stays); a new id -> it stays open under this hold (the caller
        cancels it); a definitive refusal (e.g. 422) -> released; anything ambiguous -> the hold is
        kept and UnresolvedTask is raised by _send_create."""
        hold = f"task-create:{key}#{uuid.uuid4().hex[:8]}"
        _save_intent({"key": key, "hold": hold, "bound_usd": bound_usd, "campaign": self.campaign,
                      "project_id": self.http.project_id, "created": time.time(),
                      "body_b64": base64.b64encode(payload).decode(), "headers": {},
                      "replay_of": expected_id, "idempotency_test": True})
        self.ledger.reserve(self.campaign, hold, bound_usd)  # no gate: replays overlap on purpose
        prev = self.open.get(expected_id) if expected_id else None
        rec = self._send_create(payload, key, hold, bound_usd, None, tag, max_attempts=1,
                                http=http)
        rec.tag["hold"] = hold
        tid = rec.body.get("id") if isinstance(rec.body, dict) else None
        if tid and tid == expected_id:
            if prev is not None:
                self.open[tid] = prev  # the original hold keeps covering the task
            else:
                self.open.pop(tid, None)
            self.ledger.release(hold, note="replay returned the original task id", task_id=tid)
        return rec

    # --- read / cancel -------------------------------------------------------------------------
    def get(self, task_id: str, tag: dict[str, Any] | None = None,
            timeout_s: float | None = None) -> CallRecord:
        rec = self.http.call("GET", f"/tasks/{task_id}", tag=tag or {}, timeout_s=timeout_s)
        self.log.call(rec)
        return rec

    def cancel(self, task_id: str, tag: dict[str, Any] | None = None,
               timeout_s: float | None = None) -> CallRecord:
        rec = self.http.call("POST", f"/tasks/{task_id}/cancel", tag=tag or {}, timeout_s=timeout_s)
        self.log.call(rec)
        if rec.status == 200 and isinstance(rec.body, dict) and rec.body.get("id") == task_id:
            self._book(rec.body, self.open.get(task_id))
            if rec.body.get("status") in TERMINAL and task_id in self.open:
                self._settle(rec.body)
        return rec

    def _book(self, task: dict[str, Any], key: str | None) -> None:
        cost = task_cost(task)
        if cost is not None:  # a body without cost never lowers known spend (ledger keeps the max)
            self.ledger.task_snapshot(self.campaign, task["id"], cost, key=key,
                                      status=task.get("status"))

    def _settle(self, task: dict[str, Any]) -> None:
        """Terminal task: swap its create hold for a traffic hold, atomically.

        Without a reported cost the full create hold stays until a manual reconciliation."""
        task_id = task["id"]
        key = self.open.pop(task_id, None)
        if key is None:
            return
        if task_id in self.settled:  # a second hold on an already settled task (replay)
            self.ledger.release(key, note=f"{task_id} already settled", task_id=task_id)
            return
        self.settled.add(task_id)
        if not _has_usage(task):
            self.needs_reconcile.append(task_id)
            self.log.note(event="task_settled_without_cost", task_id=task_id, hold_kept=key)
            # Re-key so later creates are not blocked, but the money stays held.
            new_key = f"unpriced:{task_id}"
            self.ledger.swap(self.campaign, key, new_key, self.bounds[task_id])
            # Keep the hold tied to the task, so costs already observed are not counted twice.
            seen = self.ledger.state()["tasks"].get(task_id, 0.0)
            self.ledger.task_snapshot(self.campaign, task_id, seen, key=new_key,
                                      status=task.get("status"))
            return
        self.ledger.swap(self.campaign, key, f"traffic:{task_id}", TRAFFIC_ALLOWANCE_USD)

    # --- wait ----------------------------------------------------------------------------------
    def wait(self, task_id: str, poll_s: float = 3.0, max_wait_s: float = 900,
             cancel_after_s: float | None = None, cancel_on_status: str | None = None
             ) -> dict[str, Any]:
        """Poll until terminal. Returns the last task body plus `_trace` (status transitions)."""
        t0 = time.monotonic()
        give_up = max_wait_s + 120
        trace: list[dict[str, Any]] = []
        last_status = None
        cancel_sent = safety_sent = False
        task: dict[str, Any] = {"id": task_id}

        def nap(seconds: float) -> None:
            time.sleep(max(0.0, min(seconds, give_up - (time.monotonic() - t0))))

        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= give_up:
                trace.append({"t_s": round(elapsed, 2), "gave_up": True})
                break
            if elapsed > max_wait_s and not safety_sent:
                c = self.cancel(task_id, tag={"safety_cancel": True},
                                timeout_s=min(30.0, give_up - elapsed))
                safety_sent = True
                trace.append({"t_s": round(elapsed, 2), "safety_cancel_http": c.status})
                if task_id not in self.open:
                    task = c.body if isinstance(c.body, dict) else task
                    break
            left = give_up - (time.monotonic() - t0)
            rec = self.get(task_id, tag={"poll": True}, timeout_s=min(30.0, left))
            if rec.status != 200 or not isinstance(rec.body, dict) \
                    or rec.body.get("id") != task_id:
                trace.append({"t_s": round(time.monotonic() - t0, 2), "http": rec.status,
                              "error": rec.error, "body": str(rec.body)[:200]})
                nap(_retry_after(rec, poll_s))
                continue
            task = rec.body
            status = task.get("status")
            if status != last_status:
                last_status = status
                trace.append({"t_s": round(time.monotonic() - t0, 2), "status": status,
                              "cost": task_cost(task),
                              "steps": (task.get("usage") or {}).get("steps")})
                print(f"  [{task_id[-6:]}] t={trace[-1]['t_s']:6.1f}s {status} "
                      f"cost=${task_cost(task) or 0:.4f}")
            self._book(task, self.open.get(task_id))
            if status in TERMINAL:
                self._settle(task)
                break
            cost = task_cost(task) or 0.0
            if cost > self.bounds.get(task_id, float("inf")) and not safety_sent:
                c = self.cancel(task_id, tag={"over_bound_cancel": True},
                                timeout_s=min(30.0, give_up - (time.monotonic() - t0)))
                safety_sent = True
                trace.append({"t_s": round(time.monotonic() - t0, 2), "over_bound_cancel": c.status,
                              "cost": cost})
            want_cancel = (cancel_after_s is not None and elapsed >= cancel_after_s) or \
                          (cancel_on_status is not None and status == cancel_on_status)
            if want_cancel and not cancel_sent:
                c = self.cancel(task_id, tag={"requested_cancel": True},
                                timeout_s=min(30.0, give_up - (time.monotonic() - t0)))
                cancel_sent = True
                trace.append({"t_s": round(time.monotonic() - t0, 2), "cancel_http": c.status,
                              "cancel_body": c.body if isinstance(c.body, dict) else None})
                if task_id not in self.open:  # settled by a verified terminal cancel reply
                    task = c.body if isinstance(c.body, dict) else task
                    break
            nap(poll_s)
        task["_trace"] = trace
        self.log.note(task_id=task_id, trace=trace, final_status=task.get("status"),
                      unresolved=task_id in self.open)
        return task

    def cleanup(self) -> None:
        """Cancel and settle any task still open (runs on context exit, also after errors).

        Raises UnresolvedTask if a task is still open afterwards, so the run is reported failed."""
        for task_id in list(self.open):
            try:
                self.cancel(task_id, tag={"cleanup": True}, timeout_s=30)
                if task_id in self.open:
                    self.wait(task_id, poll_s=5, max_wait_s=60)
            except Exception as exc:  # noqa: BLE001 - keep the evidence, keep the hold
                self.log.note(event="cleanup_failed", task_id=task_id, error=repr(exc))
        if self.open:
            raise UnresolvedTask(f"UNRESOLVED after cleanup, holds kept: {list(self.open)}")
