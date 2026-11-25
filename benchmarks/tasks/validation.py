"""TSK-08: request validation of POST /v1/tasks, against the Tasks API reference.

Order of a run
1. Control: one known-good create, cancelled at once (a queued cancel). If it is not accepted, the run
   stops: entitlement or gateway failures must not be read as validation verdicts.
2. Rejection cases: each should be refused before a task exists, so it costs nothing. A case that is
   accepted anyway (a real finding) is cancelled at once and waited to a terminal state before the next
   case; its cost is booked. Each create reserves its worst-case bound first (see mossaudit.tasks).
3. Acceptance boundary: goal of exactly 20,000 characters, created then cancelled at once.

The expected `code or type` comes from the reference's error table: 400 -> invalid_request_error,
model_not_found or model_not_enabled; 413 -> invalid_request_error.

Deliberately NOT tested
- start_url values pointing at internal hosts or local files (127.0.0.1, file://, javascript:): if
  accepted they would make Moss's browser touch Moss's own infrastructure, i.e. security testing that
  is out of scope.
- Cost-cap cases (max_cost_usd 0, 0.009, "0.01", 50.01) and max_runtime_seconds 7201: if wrongly
  accepted or silently replaced by the $2 default, the task would run with a bound the audit cannot
  justify (Codex rounds 2-3). The cost cap is exercised only by valid values in TSK-04.

Run:  python benchmarks/tasks/validation.py
"""

from __future__ import annotations

import json
from typing import Any

from mossaudit.client import CallRecord, MossHTTP
from mossaudit.config import RESULTS_PROCESSED, load_credentials
from mossaudit.ledger import Ledger
from mossaudit.runlog import RunLog
from mossaudit.tasks import TRAFFIC_ALLOWANCE_USD, TaskRunner, task_bound

D = "api-reference/v1/inference/tasks#errors"
OK_ENV = {"type": "browser", "start_url": "https://example.com"}
CAP = {"max_runtime_seconds": 60, "max_cost_usd": 0.01}
INVALID = "invalid_request_error"


def T(**kw: Any) -> dict[str, Any]:
    body = {"goal": "Read the page title and stop.", "environment": OK_ENV, "model": "moss-fast",
            "limits": CAP}
    body.update(kw)
    return {k: v for k, v in body.items() if v is not ...}


def lim(**kw: Any) -> dict[str, Any]:
    return {**CAP, **kw}


OVERSIZE = (json.dumps(T()) + " " * (270 * 1024)).encode()  # valid JSON, only the size is wrong

# name, body, expected status (None = undocumented, observe only), expected code-or-type, note
CASES: list[tuple[str, dict[str, Any] | bytes, int | None, str | None, str]] = [
    ("missing_goal", T(goal=...), 400, INVALID, "goal required"),
    ("empty_goal", T(goal=""), None, None, "empty string: undocumented"),
    ("whitespace_goal", T(goal="   "), None, None, "undocumented"),
    ("goal_20001", T(goal="x" * 20_001), 400, INVALID, "goal up to 20,000 characters"),
    ("missing_environment", T(environment=...), 400, INVALID, "environment.type required"),
    ("env_type_desktop", T(environment={"type": "desktop"}), 400, INVALID, "only browser"),
    ("start_url_ftp", T(environment={"type": "browser", "start_url": "ftp://example.com"}), 400,
     INVALID, "http or https"),
    ("start_url_not_url", T(environment={"type": "browser", "start_url": "not a url"}), 400,
     INVALID, "http or https"),
    ("env_unknown_field", T(environment={"type": "browser", "foo": 1}), None, None,
     "unknown top-level fields -> 400; nested: undocumented"),
    ("runtime_59", T(limits=lim(max_runtime_seconds=59)), 400, INVALID, "60-7200"),
    ("runtime_float", T(limits=lim(max_runtime_seconds=60.5)), None, None, "type undocumented"),
    ("limits_unknown_field", T(limits=lim(max_steps=3)), None, None, "undocumented"),
    ("model_unknown", T(model="moss-ultra"), 400, "model_not_found", "lists the names"),
    ("effort_max", T(effort="max"), 400, INVALID, "low, medium or high"),
    ("schema_array", T(output_schema={"type": "array"}), 400, INVALID, "schema type object"),
    ("schema_invalid", T(output_schema={"type": "object", "properties": 5}), None, None,
     "invalid JSON Schema: undocumented"),
    ("schema_over_20000", T(output_schema={"type": "object", "description": "x" * 20_100}), 400,
     INVALID, "up to 20,000 bytes"),
    ("metadata_over_4000", T(metadata={"k": "x" * 4_100}), 400, INVALID, "up to 4,000 bytes"),
    ("metadata_not_object", T(metadata="tag"), 400, INVALID, "an object"),
    ("unknown_top_field", T(priority="high"), 400, INVALID, "unknown top-level field -> 400"),
    ("body_over_256k", OVERSIZE, 413, INVALID, "valid body + JSON whitespace, > 256 KiB"),
    ("malformed_json", b'{"goal": "x",', 400, INVALID, "malformed"),
]
ACCEPT = [("goal_20000", T(goal="x" * 20_000), "at the limit: must be accepted, then cancelled")]


def _err(rec: CallRecord) -> tuple[str | None, str | None, str | None]:
    if isinstance(rec.body, dict) and isinstance(rec.body.get("error"), dict):
        e = rec.body["error"]
        return e.get("code"), e.get("type"), e.get("message")
    return None, None, None


def _created(rec: CallRecord) -> str | None:
    tid = rec.body.get("id") if isinstance(rec.body, dict) else None
    return tid if isinstance(tid, str) and tid.startswith("task_") else None


def main() -> None:
    creds = load_credentials()
    ledger = Ledger()
    # Holds are taken one case at a time; the largest single hold must fit up front.
    def bound(b: dict[str, Any] | bytes) -> float:
        try:
            parsed = json.loads(b) if isinstance(b, bytes) else b
        except ValueError:
            parsed = {}
        return task_bound(parsed if isinstance(parsed, dict) else {})

    worst = max(bound(b) for _, b, *_ in [*CASES, *ACCEPT]) + TRAFFIC_ALLOWANCE_USD * 3
    ledger.guard(worst, "TSK-08 validation (largest single hold + traffic holds)")
    rows: list[dict[str, Any]] = []
    with RunLog("tsk08_validation", {"n_cases": len(CASES) + len(ACCEPT) + 1}) as log, \
            MossHTTP(creds) as http, TaskRunner(http, ledger, log, "TSK-08") as runner:

        expected_id: list[str | None] = [None]

        def run_one(name: str, body: dict[str, Any] | bytes) -> tuple[CallRecord, dict | None]:
            rec = runner.create(body, tag={"case": name})
            tid = _created(rec)
            expected_id[0] = tid
            final = None
            if tid:
                c = runner.cancel(tid, tag={"case": name, "cleanup": True})
                log.note(case=name, cancel_http=c.status,
                         cancel_status=(c.body or {}).get("status") if isinstance(c.body, dict)
                         else None)
                if tid in runner.open:  # not settled by a valid terminal cancel reply
                    final = runner.wait(tid, poll_s=3, max_wait_s=90)
                else:
                    final = c.body if isinstance(c.body, dict) else None
            return rec, final

        def cleaned(final: dict | None) -> bool:
            return bool(final) and final.get("status") in ("cancelled", "completed", "failed") \
                and final.get("id") == expected_id[0] and final.get("id") not in runner.open

        # 1. control: must be a 202 with a queued/running task, then verifiably cancelled
        rec, final = run_one("control_valid", T())
        body = rec.body if isinstance(rec.body, dict) else {}
        if not (rec.status == 202 and _created(rec) and body.get("status") in ("queued", "running")
                and cleaned(final)):
            code, typ, msg = _err(rec)
            raise SystemExit(f"control failed (http {rec.status} {code or typ}: {msg}; "
                             f"final {(final or {}).get('status')}); not running validation cases")
        rows.append({"case": "control_valid", "expected": "202", "observed": str(rec.status),
                     "verdict": "PASS",
                     "created_task": _created(rec),
                     "final_status": (final or {}).get("status"),
                     "final_cost": ((final or {}).get("usage") or {}).get("cost"),
                     "request_id": rec.request_id, "note": "known-good create + queued cancel",
                     "doc": D})

        # 2. rejection cases, 3. acceptance boundary
        for name, body, exp_status, exp_code, note in [*CASES,
                                                       *[(n, b, 202, None, x) for n, b, x in ACCEPT]]:
            rec, final = run_one(name, body)
            code, typ, msg = _err(rec)
            if rec.error:
                verdict = "TRANSPORT"
            elif rec.status in (401, 403):
                verdict = "ACCESS"
            elif exp_status is None:
                verdict = "observe"
            else:
                ok = rec.status == exp_status and (exp_code is None or exp_code in (code, typ))
                if exp_status == 202:  # acceptance case: also needs verified terminal cleanup
                    ok = ok and bool(_created(rec)) and cleaned(final)
                verdict = "PASS" if ok else "MISMATCH"
            rows.append({"case": name, "expected": f"{exp_status or '?'} {exp_code or ''}".strip(),
                         "observed": f"{rec.status} {code or typ or ''}".strip(),
                         "verdict": verdict, "created_task": _created(rec),
                         "final_status": (final or {}).get("status"),
                         "final_cost": ((final or {}).get("usage") or {}).get("cost"),
                         "message": msg, "note": note, "doc": D, "request_id": rec.request_id})
            print(f"{verdict:9} {name:22} exp={rows[-1]['expected']:28} "
                  f"obs={rows[-1]['observed']:28} {(msg or '')[:90]}")
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    (RESULTS_PROCESSED / "tsk08_validation.json").write_text(json.dumps(rows, indent=2))
    print(f"exposure now ${ledger.spent():.4f}")


if __name__ == "__main__":
    main()
