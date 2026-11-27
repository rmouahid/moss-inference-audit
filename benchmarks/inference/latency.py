"""INF-02 / INF-03 / INF-04: time to first token (TTFT) and total time, streamed, per condition.

Design
- Conditions (model x grounded x prompt size) are interleaved in a seeded random order, so drift over
  time (load, network) spreads across conditions instead of biasing one of them.
- One keep-alive HTTP/1.1 connection, sequential calls: TTFT then excludes TCP/TLS setup, which is
  logged separately when a call had to open a new connection.
- `X-Moss-Cache: off` on every call, so the reply cache cannot answer; `usage.input_tokens > 0` checks it.
- Warm-up calls (first `--warmup` per condition) are kept in the raw file with `warmup: true` and left
  out of the summary.
- With --index, questions come from the INF-08 fixture (answerable ones), the same in both arms.
- Pacing: --min-interval between authenticated starts; a 429 waits its full Retry-After, and one above
  60 s stops the campaign. Rate-limited units are not retried.
- Rejection-path TTFB: every `--floor-every` calls, an unauthenticated POST /v1/responses that the
  server refuses (free). Reported only as context ("how fast does the front door answer"); it is NOT a
  lower bound of server time and nothing is subtracted from TTFT.
- Budget: each call reserves its worst case (prompt + bounded retrieved context + max output) before
  it is sent; the settled cost replaces the reservation. If a reply carries no usage (failed or
  truncated stream), the full reservation is booked as spent and the row is flagged.

TTFT = time from just before the request is written to the first non-empty text delta (see client.py).

Run:  python benchmarks/inference/latency.py --models moss/fastlane-1 --n 100 --warmup 10
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from mossaudit.client import MossHTTP
from mossaudit.config import load_credentials
from mossaudit.ledger import Ledger
from mossaudit.prices import PRICES, RETRIEVAL_USD, definitive_refusal, token_cost, usage_tokens
from mossaudit.runlog import RunLog

QUESTIONS = [
    "In one sentence, what is a hash map?",
    "In one sentence, why is the sky blue?",
    "In one sentence, what does HTTP status 404 mean?",
    "In one sentence, what is a prime number?",
    "In one sentence, what is photosynthesis?",
    "In one sentence, what is a database index?",
    "In one sentence, what is latency?",
    "In one sentence, what does a compiler do?",
]
INSTRUCTIONS = "You are a concise assistant. Answer in one short sentence."
# With --index, both arms ask the fixture's answerable questions (INF-08 data), so the grounded arm
# has something to retrieve and the pair differs only by X-Moss-Retrieve.
_FIX = json.loads((Path(__file__).resolve().parent / "data/fictional_facts.json").read_text())
FIXTURE_QS = [(q["id"], q["question"]) for q in _FIX["questions"] if q["answerable"]]


def filler(tokens: int) -> str:
    """~`tokens` tokens of neutral context (about 0.75 words per token for English prose)."""
    # The opening names the size, so contexts of different sizes share no cached prefix.
    head = f"Context block of about {tokens} tokens. "
    base = ("The archive holds records of shipments, invoices and maintenance logs for the fleet. ")
    words = int(tokens * 0.75)
    reps = max(1, words // len(base.split()))
    return head + base * reps


RETRIEVED_CTX_BOUND = 4000  # tokens of retrieved context we budget per grounded call


def call_reserve(model: str, grounded: bool, ctx: int, max_out: int) -> float:
    p = PRICES[model]
    inp = ctx + 200 + (RETRIEVED_CTX_BOUND if grounded else 0)
    return (inp * p.input + max_out * p.output) / 1e6 + (RETRIEVAL_USD if grounded else 0)


def estimate(conds: list[tuple[str, bool, int, str]], n_total: int, max_out: int) -> float:
    per = sum(call_reserve(m, g, c, max_out) for m, g, c, _ in conds)
    return per / len(conds) * n_total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["moss/fastlane-1"])
    ap.add_argument("--index", help="name this index on every call; plain arm sends Retrieve: off")
    ap.add_argument("--ctx", nargs="+", type=int, default=[0], help="extra context tokens")
    ap.add_argument("--prefix", nargs="+", choices=["repeat", "fresh"], default=["repeat"],
                    help="randomized condition; fresh = unique opening line, no prefix-cache reuse")
    ap.add_argument("--n", type=int, default=50, help="measured units (one call per condition)")
    ap.add_argument("--warmup", type=int, default=5, help="warm-up units, logged apart")
    ap.add_argument("--max-output", type=int, default=64)
    ap.add_argument("--floor-every", type=int, default=10)
    ap.add_argument("--cold", action="store_true", help="open a new connection for every call")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--endpoint", choices=["responses", "chat"], default="responses")
    ap.add_argument("--campaign", default="inf02_latency")
    ap.add_argument("--min-interval", type=float, default=1.0,
                    help="seconds between request starts (docs: 120 req/min sustained, burst 10)")
    a = ap.parse_args()

    # A condition is (model, grounded, ctx). A "unit" runs every condition once on the same question,
    # in a random order, so conditions are paired and time drift hits them equally.
    conds = [(m, g, c, pf) for m in a.models for g in ([False, True] if a.index else [False])
             for c in a.ctx for pf in (a.prefix if any(a.ctx) else ["repeat"])]
    rng = random.Random(a.seed)
    schedule: list[tuple[int, bool, tuple[str, bool, int, str]]] = []
    for unit in range(a.warmup + a.n):
        order = conds[:]
        rng.shuffle(order)
        schedule += [(unit, unit < a.warmup, c) for c in order]

    creds = load_credentials()
    ledger = Ledger()
    est = estimate(conds, len(schedule), a.max_output)
    ledger.guard(est, a.campaign)
    params = vars(a) | {"conditions": [list(c) for c in conds], "estimate_usd": round(est, 4)}
    contexts = {c: filler(c) for c in a.ctx}
    http = MossHTTP(creds)
    with RunLog(a.campaign, params) as log, MossHTTP(None) as anon:
        next_start = 0.0
        for i, (unit, warmup, (model, grounded, ctx, prefix)) in enumerate(schedule):
            reserve = call_reserve(model, grounded, ctx, a.max_output)
            rkey = f"{log.run_id}:{i}"
            ledger.reserve(a.campaign, rkey, reserve)
            if a.floor_every and i % a.floor_every == 0:
                f = anon.call("POST", "/responses", body={"input": "x"},
                              tag={"rejection_path": True, "i": i})
                log.call(f)
            if a.cold:
                http.close()
                http = MossHTTP(creds)
            qid, q = (FIXTURE_QS[unit % len(FIXTURE_QS)] if a.index
                      else (f"g{unit % len(QUESTIONS)}", QUESTIONS[unit % len(QUESTIONS)]))
            user = q
            if ctx:
                opening = f"Record set {rng.getrandbits(64):016x}.\n" if prefix == "fresh" else ""
                user = opening + contexts[ctx] + "\n\nQuestion: " + q
            headers = {"X-Moss-Cache": "off"}
            if a.index:
                headers["X-Moss-Indexes"] = a.index
                if not grounded:
                    headers["X-Moss-Retrieve"] = "off"
            else:
                headers["X-Moss-Retrieve"] = "off"  # no saved default index can leak in
            if a.endpoint == "responses":
                path = "/responses"
                body = {"model": model, "instructions": INSTRUCTIONS, "input": user,
                        "stream": True, "max_output_tokens": a.max_output}
            else:
                path = "/chat/completions"
                body = {"model": model, "stream": True, "max_tokens": a.max_output,
                        "messages": [{"role": "system", "content": INSTRUCTIONS},
                                     {"role": "user", "content": user}]}
            tag = {"qid": qid, "model": model, "grounded": grounded, "ctx": ctx, "prefix": prefix,
                   "warmup": warmup, "unit": unit, "i": i, "cold": a.cold}
            # Pace authenticated starts below the documented per-project limit (the gate sits right
            # before the call, after the optional probe); a rate-limited unit is never retried.
            time.sleep(max(0.0, next_start - time.monotonic()))
            next_start = time.monotonic() + a.min_interval
            rec = http.call("POST", path, body=body, headers=headers, tag=tag)
            log.call(rec)
            stop = None
            if rec.status == 429:  # honour Retry-After in full before the next request
                try:
                    wait = float(rec.headers.get("retry-after") or a.min_interval)
                except ValueError:
                    wait = a.min_interval
                if wait > 60:
                    stop = f"Retry-After {wait:.0f}s > 60s: campaign stopped"
                next_start = max(next_start, time.monotonic() + wait)
            toks = usage_tokens(rec.usage)
            inp, out, cached = toks or (0, 0, 0)
            billed_model = rec.model if rec.model in PRICES else model  # reply model, also on streams
            if toks:
                cost = token_cost(billed_model, inp, out, cached) + (RETRIEVAL_USD if grounded else 0)
                ledger.charge(a.campaign, cost, key=rkey, model=billed_model,
                              outcome=rec.stream_outcome)
            elif definitive_refusal(rec.status, rec.error, rec.n_text_deltas, rec.body):
                ledger.release(rkey)
            else:
                ledger.charge(a.campaign, reserve, key=rkey, model=model, usage_missing=True,
                              outcome=rec.stream_outcome, status=rec.status)
            print(f"{i:4d} {model:28} g={int(grounded)} ctx={ctx:6d} st={rec.status} "
                  f"ttft={rec.t_first_token_ms} total={rec.t_end_ms} end={rec.stream_outcome} "
                  f"in={inp} out={out} "
                  f"cached={cached} retr={rec.headers.get('x-moss-retrieve-ms')}")
            if stop:
                log.note(stopped=stop, i=i)
                print(stop)
                break
    http.close()
    print(f"spent so far: ${ledger.spent():.4f}")


if __name__ == "__main__":
    main()
