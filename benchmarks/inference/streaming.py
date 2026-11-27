"""INF-11: streaming grammar against docs/inference/streaming.md, plus one early stop.

Cases (fastlane, reply cache off, retrieval off, 1 s pacing; each logged with its full event list):
  resp_text        Responses stream          -> only documented event types; ends with one
                                                response.completed; no [DONE]
  resp_capped      Responses, max_output 8,
                   long answer asked         -> ends with response.incomplete
  resp_tool        Responses + one tool,
                   tool-worthy prompt        -> function_call_arguments.delta/.done events
  chat_default     Chat stream, no stream_options -> docs: last chunk before [DONE] has empty
                                                choices + usage (OpenAI sends usage only with
                                                include_usage: true) -> observe
  chat_usage_off   Chat, include_usage false -> observe whether usage still arrives
  chat_usage_on    Chat, include_usage true  -> usage chunk then [DONE]
  early_stop       Responses, long answer, client closes after the first text delta -> outcome
                   closed_early. This shows client closure only: server-side cancellation and
                   billing are unverified; the full hold is booked as an allowance.
Chat usage contract (per-frame): default and include_usage true must have exactly one usage chunk,
with empty choices, immediately before a final [DONE]; include_usage false must have none.
resp_capped must end incomplete with incomplete_details.reason max_output_tokens; resp_tool forces
the named function and its argument deltas must rebuild .done and the final call's arguments.
A 429 waits its full Retry-After; above 60 s the run stops.
Run:  python benchmarks/inference/streaming.py
"""

from __future__ import annotations

import json
import time
from typing import Any

from mossaudit.client import MossHTTP
from mossaudit.config import RESULTS_PROCESSED, load_credentials
from mossaudit.ledger import Ledger
from mossaudit.prices import PRICES, definitive_refusal, token_cost, usage_tokens
from mossaudit.runlog import RunLog

FL = "moss/fastlane-1"
HDRS = {"X-Moss-Cache": "off", "X-Moss-Retrieve": "off"}
DOC_EVENTS = {
    "response.created", "response.in_progress", "response.output_item.added",
    "response.output_item.done", "response.content_part.added", "response.content_part.done",
    "response.output_text.delta", "response.output_text.done",
    "response.function_call_arguments.delta", "response.function_call_arguments.done",
    "response.completed", "response.incomplete", "response.failed",
}
TERMINALS = {"response.completed", "response.incomplete", "response.failed"}
LONG = "Write a 300-word story about a lighthouse keeper."
TOOL = {"type": "function", "name": "lookup_order", "description": "Look up an order by its id.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                       "required": ["id"]}}
RESERVE = 0.002


def chat(**kw: Any) -> dict[str, Any]:
    return {"model": FL, "stream": True, "max_tokens": 32,
            "messages": [{"role": "user", "content": "Say hello in five words."}], **kw}


CASES: list[tuple[str, str, dict[str, Any], bool]] = [
    ("resp_text", "/responses", {"model": FL, "input": "Say hello in five words.", "stream": True,
                                 "max_output_tokens": 32}, False),
    ("resp_capped", "/responses", {"model": FL, "input": LONG, "stream": True,
                                   "max_output_tokens": 8}, False),
    ("resp_tool", "/responses", {"model": FL, "input": "Where is my order ZT-90215?",
                                 "tools": [TOOL], "stream": True, "max_output_tokens": 64,
                                 "tool_choice": {"type": "function", "name": "lookup_order"}},
     False),
    ("chat_default", "/chat/completions", chat(), False),
    ("chat_usage_off", "/chat/completions", chat(stream_options={"include_usage": False}), False),
    ("chat_usage_on", "/chat/completions", chat(stream_options={"include_usage": True}), False),
    ("early_stop", "/responses", {"model": FL, "input": LONG, "stream": True,
                                  "max_output_tokens": 400}, True),
]


def resp_grammar(rec: Any, terminal: str) -> list[str]:
    """Problems with a Responses stream's ordered events (empty list = conforms)."""
    ev = [f["t"] for f in rec.frames]
    probs = []
    if not ev or ev[0] != "response.created":
        probs.append(f"first event {ev[:1]}")
    if "[DONE]" in ev:
        probs.append("[DONE] on Responses")
    undoc = sorted({e for e in ev if e not in DOC_EVENTS})
    if undoc:
        probs.append(f"undocumented {undoc}")
    terms = [i for i, e in enumerate(ev) if e in TERMINALS]
    if len(terms) != 1 or terms[0] != len(ev) - 1:
        probs.append(f"terminals at {terms} of {len(ev)}")
    elif ev[-1] != terminal:
        probs.append(f"terminal {ev[-1]} != {terminal}")
    if any(f.get("error") for f in rec.frames):
        probs.append("error frame")
    want = {"response.completed": "completed", "response.incomplete": "incomplete"}.get(terminal)
    b = rec.body
    if not isinstance(b, dict) or b.get("status") != want:
        probs.append(f"terminal payload status {b.get('status') if isinstance(b, dict) else b!r}")
    if rec.error:
        probs.append(f"transport error {rec.error[:80]}")
    return probs


def check(name: str, rec: Any) -> tuple[bool | None, str]:
    """Per-case contract check from the ordered per-frame metadata. None = observe only."""
    fr = rec.frames
    if name.startswith("chat_"):
        probs = []
        kinds = [f["t"] for f in fr]
        if rec.stream_outcome != "done":
            probs.append(f"outcome {rec.stream_outcome}")
        if kinds.count("[DONE]") != 1 or kinds[-1:] != ["[DONE]"]:
            probs.append(f"[DONE] not single and last ({kinds.count('[DONE]')}, last={kinds[-1:]})")
        other = sorted({k for k in kinds if k not in ("chat.completion.chunk", "[DONE]")})
        if other or any(f.get("error") for f in fr):
            probs.append(f"non-chunk or error frames {other}")
        if rec.error:
            probs.append(f"transport error {rec.error[:80]}")
        if not any((f.get("choices") or 0) > 0 for f in fr) or not rec.output_text:
            probs.append("no reply chunk with text")
        usage_at = [i for i, f in enumerate(fr) if f.get("usage")]
        if name != "chat_usage_off" and usage_tokens(rec.usage) is None:
            probs.append(f"usage counts invalid: {str(rec.usage)[:80]}")
        if name == "chat_usage_off":
            if usage_at:
                probs.append(f"usage chunks at {usage_at}")
        elif not (len(usage_at) == 1 and fr[usage_at[0]]["choices"] == 0
                  and usage_at[0] == len(fr) - 2):
            probs.append(f"usage chunks at {usage_at} "
                         f"(choices={[fr[i]['choices'] for i in usage_at]}), frames={len(fr)}")
        return not probs, "; ".join(probs) or "conforms"
    b = rec.body if isinstance(rec.body, dict) else {}
    if name == "resp_text":
        probs = resp_grammar(rec, "response.completed")
        return not probs, "; ".join(probs) or "conforms"
    if name == "resp_capped":
        probs = resp_grammar(rec, "response.incomplete")
        det = b.get("incomplete_details")
        reason = det.get("reason") if isinstance(det, dict) else f"not an object: {det!r}"
        if reason != "max_output_tokens":
            probs.append(f"reason={reason}")
        return not probs, "; ".join(probs) or "conforms"
    if name == "resp_tool":
        probs = resp_grammar(rec, "response.completed")
        idx = [(i, f) for i, f in enumerate(fr) if f["t"].startswith("response.function_call_arg")]
        deltas = [(i, f) for i, f in idx if f["t"].endswith(".delta")]
        dones = [(i, f) for i, f in idx if f["t"].endswith(".done")]
        final = [it for it in (b.get("output") or [])
                 if isinstance(it, dict) and it.get("type") == "function_call"]
        if len(dones) != 1 or not deltas or max(i for i, _ in deltas) > dones[0][0]:
            probs.append(f"{len(deltas)} deltas / {len(dones)} done, order wrong")
        elif len(final) != 1:
            probs.append(f"{len(final)} final calls")
        else:
            ids = {f.get("item_id") for _, f in idx} | {final[0].get("id")}
            joined = "".join(str(f.get("delta") or "") for _, f in deltas)
            if len(ids) != 1 or not all(isinstance(x, str) and x for x in ids):
                probs.append(f"item ids not one shared non-empty string {sorted(map(str, ids))}")
            try:
                parsed = json.loads(joined)
            except ValueError:
                parsed = None
            if parsed != {"id": "ZT-90215"}:
                probs.append(f"rebuilt arguments {joined!r} != fixture")
            if not (joined == dones[0][1].get("arguments") == final[0].get("arguments")):
                probs.append(f"arguments differ {joined!r} / {dones[0][1].get('arguments')!r} / "
                             f"{final[0].get('arguments')!r}")
            if final[0].get("name") != "lookup_order":
                probs.append(f"name {final[0].get('name')}")
        return not probs, "; ".join(probs) or "conforms"
    if name == "early_stop":
        return rec.stream_outcome == "closed_early" and rec.n_text_deltas == 1, \
            f"outcome={rec.stream_outcome} deltas={rec.n_text_deltas} (client closure only)"
    return None, ""


def main() -> None:
    creds = load_credentials()
    ledger = Ledger()
    ledger.guard(RESERVE * len(CASES), "INF-11 streaming")
    rows: list[dict[str, Any]] = []
    with RunLog("inf11_streaming", {"cases": [c[0] for c in CASES]}) as log, MossHTTP(creds) as http:
        wait = 1.0
        for name, path, body, stop in CASES:
            time.sleep(wait)
            wait = 1.0
            key = f"{log.run_id}:{name}"
            ledger.reserve("INF-11", key, RESERVE)
            rec = http.call("POST", path, body=body, headers=HDRS, stop_after_first_token=stop,
                            drain_after_done=path.endswith("/chat/completions"),
                            tag={"case": name})
            log.call(rec)
            toks = usage_tokens(rec.usage)
            if toks and not stop:
                ledger.charge("INF-11", token_cost(rec.model if rec.model in PRICES else FL, *toks),
                              key=key)
            elif definitive_refusal(rec.status, rec.error, rec.n_text_deltas, rec.body):
                ledger.release(key)
            else:  # early stop or missing usage: full hold kept as an allowance, portal later
                ledger.charge("INF-11", RESERVE, key=key, usage_missing=not toks, early_stop=stop,
                              allowance=True)
            if rec.status == 429:  # wait the full Retry-After; above 60 s stop
                try:
                    wait = float(rec.headers.get("retry-after") or 1)
                except ValueError:
                    wait = 1.0
                if wait > 60:
                    log.note(stopped=f"Retry-After {wait:.0f}s > 60s", case=name)
                    break
            try:
                ok, detail = check(name, rec)
            except Exception as exc:  # noqa: BLE001 - a malformed shape is a failed check
                ok, detail = False, f"malformed: {exc!r}"[:200]
            ev = rec.event_types
            types = set(ev) - {"[DONE]"}
            row = {"case": name, "status": rec.status, "outcome": rec.stream_outcome,
                   "n_events": len(ev), "event_types": sorted(types),
                   "undocumented_events": sorted(t for t in types if path == "/responses"
                                                 and t not in DOC_EVENTS),
                   "terminals": [e for e in ev if e in TERMINALS], "done_marker": "[DONE]" in ev,
                   "check": ok, "detail": detail, "frames": rec.frames,
                   "usage": rec.usage, "text": (rec.output_text or "")[:80],
                   "t_first_token_ms": rec.t_first_token_ms, "t_end_ms": rec.t_end_ms,
                   "request_id": rec.request_id}
            rows.append(row)
            print(f"{name:15} st={rec.status} outcome={rec.stream_outcome} events={len(ev)} "
                  f"terminals={row['terminals']} done={row['done_marker']} "
                  f"usage={'yes' if rec.usage else 'no'} undoc={row['undocumented_events']} "
                  f"check={ok} {detail}"[:260])
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    (RESULTS_PROCESSED / "inf11_streaming.json").write_text(json.dumps(rows, indent=2))
    print(f"exposure now ${ledger.spent():.4f}")


if __name__ == "__main__":
    main()
