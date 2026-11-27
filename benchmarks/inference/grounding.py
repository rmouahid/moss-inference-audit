"""INF-08: does retrieval ground the answer? Fictional facts, grounded arm vs retrieval-off arm.

Data: benchmarks/inference/data/fictional_facts.json (30 invented facts in index `audit-facts`, 30
answerable questions, 10 questions about entities that are NOT in the index). Every entity and number
is invented, so a correct answer in the plain arm can only be a lucky guess.

Arms (same call, same headers except one): both name `X-Moss-Indexes: audit-facts`; the plain arm
adds `X-Moss-Retrieve: off`. Order of (question, arm) is shuffled with a fixed seed. Reply cache off.
Output uses `text.format` json_schema {answer: string|null, found: boolean} (merges INF-07).

Scoring (exact, no judge model):
- answerable: correct iff found is true and normalize(answer) == expected exactly (normalize: trim,
  casefold, trailing '.', one trailing unit word); every expected value is unique in the fixture;
- unanswerable: correct iff found is false AND answer is null; found:true is a hallucination;
- found:false with a non-null answer (or found:true with null) is "inconsistent"; a Responses status
  other than completed is "response_<status>", never scored.
Also logged: X-Moss-Retrieve-Ms, input tokens per arm (retrieved facts count as input), schema
validity of output_text.

Run:  python benchmarks/inference/grounding.py [--limit N]
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any

from mossaudit.client import MossHTTP
from mossaudit.config import RESULTS_PROCESSED, load_credentials
from mossaudit.ledger import Ledger
from mossaudit.prices import PRICES, RETRIEVAL_USD, definitive_refusal, token_cost, usage_tokens
from mossaudit.runlog import RunLog

DATA = Path(__file__).resolve().parent / "data/fictional_facts.json"
FL = "moss/fastlane-1"
INSTR = ("Answer the question using only the facts you were given. Put only the bare value in answer "
         "(digits only, no unit, no sentence). If those facts do not contain the answer, set found to "
         "false and answer to null. Do not guess.")
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": ["string", "null"]}, "found": {"type": "boolean"}},
    "required": ["answer", "found"],
    "additionalProperties": False,
}
MAX_OUT = 80
RESERVE = (200 + 4000) * PRICES[FL].input / 1e6 + MAX_OUT * PRICES[FL].output / 1e6 + RETRIEVAL_USD


UNITS = {"year", "years", "minute", "minutes", "min", "metre", "metres", "meter", "meters", "m"}


def normalize(ans: str) -> str:
    """Declared normalization: trim, casefold, drop trailing '.', drop one trailing unit word."""
    a = ans.strip().casefold().rstrip(".").strip()
    parts = a.split()
    if len(parts) == 2 and parts[1] in UNITS:
        a = parts[0]
    return a


def score(q: dict[str, Any], parsed: dict[str, Any] | None) -> str:
    if parsed is None:
        return "invalid_output"
    found, ans = parsed.get("found"), parsed.get("answer")
    if found is False:
        abstain = "abstained" if q["answerable"] else "correct_abstain"
        return abstain if ans is None else "inconsistent"  # found:false needs answer:null
    if not isinstance(ans, str):
        return "inconsistent"  # found:true with a null answer
    if q["answerable"]:
        return "correct" if normalize(ans) == q["answer"] else "wrong"
    return "hallucinated"


def parse(text: str | None) -> dict[str, Any] | None:
    try:
        obj = json.loads(text or "")
    except ValueError:
        return None
    ok = (isinstance(obj, dict) and set(obj) == {"answer", "found"}
          and isinstance(obj["found"], bool) and (obj["answer"] is None or isinstance(obj["answer"], str)))
    return obj if ok else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="first N questions only (dry runs)")
    ap.add_argument("--seed", type=int, default=8)
    ap.add_argument("--min-interval", type=float, default=1.0)
    a = ap.parse_args()
    data = json.loads(DATA.read_text())
    qs = data["questions"][: a.limit] if a.limit else data["questions"]
    plan = [(q, arm) for q in qs for arm in ("grounded", "plain")]
    random.Random(a.seed).shuffle(plan)
    creds = load_credentials()
    ledger = Ledger()
    ledger.guard(RESERVE * len(plan), "INF-08 grounding")
    rows: list[dict[str, Any]] = []
    with RunLog("inf08_grounding", {"n_calls": len(plan), "index": data["index"], "seed": a.seed,
                                    "model": FL, "max_output_tokens": MAX_OUT}) as log, \
            MossHTTP(creds) as http:
        next_start = 0.0
        for i, (q, arm) in enumerate(plan):
            time.sleep(max(0.0, next_start - time.monotonic()))
            next_start = time.monotonic() + a.min_interval
            headers = {"X-Moss-Cache": "off", "X-Moss-Indexes": data["index"]}
            if arm == "plain":
                headers["X-Moss-Retrieve"] = "off"
            body = {"model": FL, "instructions": INSTR, "input": q["question"],
                    "max_output_tokens": MAX_OUT,
                    "text": {"format": {"type": "json_schema", "name": "answer", "schema": SCHEMA,
                                        "strict": True}}}
            key = f"{log.run_id}:{i}"
            ledger.reserve("INF-08", key, RESERVE)
            rec = http.call("POST", "/responses", body=body, headers=headers,
                            tag={"q": q["id"], "arm": arm, "i": i})
            log.call(rec)
            toks = usage_tokens(rec.usage)
            model = rec.model if rec.model in PRICES else FL
            if toks:
                cost = token_cost(model, *toks) + (RETRIEVAL_USD if arm == "grounded" else 0)
                ledger.charge("INF-08", cost, key=key, arm=arm)
            elif definitive_refusal(rec.status, rec.error, rec.n_text_deltas, rec.body):
                ledger.release(key)
                cost = 0.0
            else:
                ledger.charge("INF-08", RESERVE, key=key, arm=arm, usage_missing=True,
                              status=rec.status)
                cost = RESERVE
            stop = None
            if rec.status == 429:  # wait the full Retry-After; above 60 s, stop the campaign
                try:
                    wait = float(rec.headers.get("retry-after") or 1)
                except ValueError:
                    wait = 1.0
                if wait > 60:
                    stop = f"Retry-After {wait:.0f}s > 60s: campaign stopped"
                next_start = max(next_start, time.monotonic() + wait)
            resp_status = rec.body.get("status") if isinstance(rec.body, dict) else None
            parsed = parse(rec.output_text) if rec.status == 200 else None
            if rec.status != 200:
                verdict = f"http_{rec.status}"
            elif resp_status != "completed":
                verdict = f"response_{resp_status}"  # e.g. incomplete (max_output_tokens hit)
            else:
                verdict = score(q, parsed)
            rows.append({"q": q["id"], "arm": arm, "answerable": q["answerable"],
                         "response_status": resp_status,
                         "expected": q["answer"], "status": rec.status, "verdict": verdict,
                         "output_text": rec.output_text, "input_tokens": toks[0] if toks else None,
                         "retrieve_ms": rec.headers.get("x-moss-retrieve-ms"),
                         "total_ms": rec.t_end_ms, "request_id": rec.request_id,
                         "cost_usd": cost})
            print(f"{i:3d} {q['id']} {arm:8} st={rec.status} {verdict:16} "
                  f"in={rows[-1]['input_tokens']} retr={rows[-1]['retrieve_ms']} "
                  f"{(rec.output_text or '')[:60]}")
            if stop:
                log.note(stopped=stop, i=i)
                print(stop)
                break
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    (RESULTS_PROCESSED / "inf08_grounding.json").write_text(json.dumps(rows, indent=2))
    for arm in ("grounded", "plain"):
        r = [x for x in rows if x["arm"] == arm]
        c: dict[str, int] = {}
        for x in r:
            c[x["verdict"]] = c.get(x["verdict"], 0) + 1
        print(arm, len(r), c)
    print(f"exposure now ${ledger.spent():.4f}")


if __name__ == "__main__":
    main()
