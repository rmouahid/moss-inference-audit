"""INF-05: reply-cache semantics (docs: "A repeated request answered without a model call, free";
a hit "reports 0 prompt tokens"; `X-Moss-Cache: off` skips it).

Every run starts from a fresh nonce in the prompt, so the first call of each base request is a miss.
Calls are sequential and paced (1 s between starts). For each call we keep status, usage, every
response header (to find any hit flag besides the token count), the reply text and timings.

Each comparison gets its own nonce, primed with two identical calls (the second must be a hit), so
earlier probes cannot influence it. Base = Responses, non-stream, fastlane, max_output_tokens 24,
X-Moss-Retrieve: off.
  A  n1: base x5, cache on                   -> A2..A5 expected hits
  B  n1: base, X-Moss-Cache: off             -> must call the model
  C  n2: prime x2, then input + one space    -> exact-text key? (observe)
  D  n3: prime x2, then max_output_tokens 25 -> parameters in the key? (observe)
  E  n4: prime x2, then stream: true         -> non-stream reply reused by a stream? (observe)
     n5: stream x2                           -> stream-to-stream reuse? (observe)
  F  n6: prime x2 on Responses, then the same prompt on Chat x2
                                             -> shared across endpoints? Chat-to-Chat reuse?
  G  n7: X-Moss-Cache: off x3, then cache on x2
                                             -> G4 hit = cache-off calls populate the cache;
                                                G5 hit after a G4 miss = G4 populated it.
A hit is: HTTP 200, a completed reply with non-empty text, and usage input tokens == 0. Every kept
response header is logged (client keeps x-moss-*, age, x-cache*, cache-control) to look for a flag.
A 429 waits its full Retry-After before the next call; above 60 s the run stops.

Run:  python benchmarks/inference/cache.py
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from mossaudit.client import MossHTTP
from mossaudit.config import RESULTS_PROCESSED, load_credentials
from mossaudit.ledger import Ledger
from mossaudit.prices import PRICES, definitive_refusal, token_cost, usage_tokens
from mossaudit.runlog import RunLog

FL = "moss/fastlane-1"
INSTR = "Reply with one short sentence."
MAX_OUT = 24
RESERVE = 0.001
GAP_S = 1.0


def main() -> None:
    creds = load_credentials()
    ledger = Ledger()
    ledger.guard(RESERVE * 30, "INF-05 cache")
    rows: list[dict[str, Any]] = []
    with RunLog("inf05_cache", {"model": FL, "n_calls": 26}) as log, MossHTTP(creds) as http:
        next_start = [0.0]
        stopped: list[str] = []

        def send(step: str, path: str, body: dict[str, Any], cache: bool = True,
                 cohort: str = "") -> dict[str, Any]:
            if stopped:
                return {}
            time.sleep(max(0.0, next_start[0] - time.monotonic()))
            next_start[0] = time.monotonic() + GAP_S
            headers = {"X-Moss-Retrieve": "off"}
            if not cache:
                headers["X-Moss-Cache"] = "off"
            key = f"{log.run_id}:{len(rows)}"
            ledger.reserve("INF-05", key, RESERVE)
            rec = http.call("POST", path, body=body, headers=headers, tag={"step": step})
            log.call(rec)
            toks = usage_tokens(rec.usage)
            if toks:
                cost = token_cost(rec.model if rec.model in PRICES else FL, *toks)
                ledger.charge("INF-05", cost, key=key, step=step)
            elif definitive_refusal(rec.status, rec.error, rec.n_text_deltas, rec.body):
                ledger.release(key)
                cost = 0.0
            else:  # a hit may report usage with zero tokens; usage_tokens keeps it (0 is valid)
                ledger.charge("INF-05", RESERVE, key=key, step=step, usage_missing=True)
                cost = RESERVE
            if rec.status == 429:  # wait the full Retry-After; above 60 s stop the run
                try:
                    wait = float(rec.headers.get("retry-after") or 1)
                except ValueError:
                    wait = 1.0
                if wait > 60:
                    stopped.append(f"{step}: Retry-After {wait:.0f}s > 60s")
                next_start[0] = max(next_start[0], time.monotonic() + wait)
            b = rec.body if isinstance(rec.body, dict) else {}
            completed = (rec.stream_outcome == "completed" if body.get("stream")
                         else b.get("status") == "completed" if path == "/responses"
                         else bool(b.get("choices"))
                         and b["choices"][0].get("finish_reason") in ("stop", "length"))
            row = {"step": step, "cohort": cohort, "path": path, "cache": cache,
                   "status": rec.status, "completed": completed,
                   "input_tokens": toks[0] if toks else None,
                   "output_tokens": toks[1] if toks else None,
                   "cached_tokens": toks[2] if toks else None,
                   "text": rec.output_text, "t_headers_ms": rec.t_headers_ms,
                   "total_ms": rec.t_end_ms, "outcome": rec.stream_outcome,
                   "response_id": (rec.body or {}).get("id") if isinstance(rec.body, dict) else None,
                   "moss_headers": {k: v for k, v in rec.headers.items()
                                    if not k.startswith("x-ratelimit")},
                   "request_id": rec.request_id, "cost_usd": cost}
            rows.append(row)
            print(f"{step:4} {path:18} cache={int(cache)} st={rec.status} in={row['input_tokens']} "
                  f"out={row['output_tokens']} {row['total_ms']}ms {str(row['text'])[:40]!r} "
                  f"{row['moss_headers']}")
            return row

        def base(n: str, **kw: Any) -> dict[str, Any]:
            return {"model": FL, "instructions": INSTR, "input": f"Ref {n}. Name one colour of the sky.",
                    "max_output_tokens": MAX_OUT, **kw}

        def chat(n: str) -> dict[str, Any]:
            return {"model": FL, "max_tokens": MAX_OUT,
                    "messages": [{"role": "system", "content": INSTR},
                                 {"role": "user", "content": f"Ref {n}. Name one colour of the sky."}]}

        nonces = {k: secrets.token_hex(6) for k in ("n1", "n2", "n3", "n4", "n5", "n6", "n7")}
        log.note(nonces=nonces)
        n = nonces
        for k in range(5):
            send(f"A{k + 1}", "/responses", base(n["n1"]), cohort="n1")
        send("B", "/responses", base(n["n1"]), cache=False, cohort="n1")
        send("C1", "/responses", base(n["n2"]), cohort="n2")
        send("C2", "/responses", base(n["n2"]), cohort="n2")
        c3 = base(n["n2"])
        c3["input"] += " "
        send("C3", "/responses", c3, cohort="n2")
        send("D1", "/responses", base(n["n3"]), cohort="n3")
        send("D2", "/responses", base(n["n3"]), cohort="n3")
        send("D3", "/responses", base(n["n3"], max_output_tokens=MAX_OUT + 1), cohort="n3")
        send("E1", "/responses", base(n["n4"]), cohort="n4")
        send("E2", "/responses", base(n["n4"]), cohort="n4")
        send("E3", "/responses", base(n["n4"], stream=True), cohort="n4")
        send("E4", "/responses", base(n["n5"], stream=True), cohort="n5")
        send("E5", "/responses", base(n["n5"], stream=True), cohort="n5")
        send("F1", "/responses", base(n["n6"]), cohort="n6")
        send("F2", "/responses", base(n["n6"]), cohort="n6")
        send("F3", "/chat/completions", chat(n["n6"]), cohort="n6")
        send("F4", "/chat/completions", chat(n["n6"]), cohort="n6")
        for k in range(3):
            send(f"G{k + 1}", "/responses", base(n["n7"]), cache=False, cohort="n7")
        send("G4", "/responses", base(n["n7"]), cohort="n7")
        send("G5", "/responses", base(n["n7"]), cohort="n7")
    firsts: dict[str, Any] = {}
    for r in rows:
        firsts.setdefault(r["cohort"], r["text"])
        r["hit"] = bool(r["status"] == 200 and r["completed"] and r["text"]
                        and r["input_tokens"] == 0)
        r["same_text_as_cohort_first"] = r["text"] == firsts[r["cohort"]]
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    (RESULTS_PROCESSED / "inf05_cache.json").write_text(json.dumps(rows, indent=2))
    print("hits:", [r["step"] for r in rows if r["hit"]], "stopped:", stopped)
    print(f"exposure now ${ledger.spent():.4f}")


if __name__ == "__main__":
    main()
