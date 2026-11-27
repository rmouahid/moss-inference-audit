"""INF-01: smoke test through every documented client path, one tiny call each.

Paths: raw HTTP (Responses non-stream, Responses stream, Chat stream), the OpenAI Python SDK pointed at
Moss (Responses + Chat), and the Moss Python SDK (`MossInference.responses.create`). Each call uses
`max_output_tokens` 16, reply cache off, retrieval off, and reserves its cost in the ledger first.
Output: results/processed/inf01_smoke.json (one row per path: ok, latency, model, usage, notes).

Run:  python benchmarks/inference/smoke.py
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from typing import Any

from mossaudit.client import MossHTTP
from mossaudit.config import BASE_URL, RESULTS_PROCESSED, load_credentials
from mossaudit.jsonsafe import json_safe
from mossaudit.ledger import Ledger
from mossaudit.prices import PRICES, definitive_refusal, token_cost, usage_tokens
from mossaudit.runlog import RunLog

FL = "moss/fastlane-1"
INSTR = "Reply with the single word OK."
HDRS = {"X-Moss-Cache": "off", "X-Moss-Retrieve": "off"}
RESERVE = 0.002


def _usage_cost(model: str | None, u: dict[str, Any] | None) -> float | None:
    toks = usage_tokens(u)
    if not toks or model not in PRICES:
        return None
    return token_cost(model, *toks)


def main() -> None:
    creds = load_credentials()
    ledger = Ledger()
    ledger.guard(RESERVE * 8, "INF-01 smoke")
    rows: list[dict[str, Any]] = []
    with RunLog("inf01_smoke", {"model": FL}) as log, MossHTTP(creds) as http:

        def book(name: str, model: str | None, usage: Any, refused: bool) -> float:
            u = usage if isinstance(usage, dict) else None
            cost = _usage_cost(model, u)
            key = f"{log.run_id}:{name}"
            if cost is not None:
                ledger.charge("INF-01", cost, key=key, path=name)
            elif refused:
                ledger.release(key)
                cost = 0.0
            else:  # no priced usage and not a clear refusal: book the hold, reconcile later
                ledger.charge("INF-01", RESERVE, key=key, path=name, unpriced=True)
                cost = RESERVE
            return cost

        def raw(name: str, path: str, body: dict[str, Any]) -> None:
            ledger.reserve("INF-01", f"{log.run_id}:{name}", RESERVE)
            rec = http.call("POST", path, body=body, headers=HDRS, tag={"path": name})
            log.call(rec)
            model = rec.model
            streamed = body.get("stream", False)
            expected = ("done" if path.endswith("/chat/completions") else "completed")
            ok = (rec.status == 200 and bool(rec.output_text)
                  and (not streamed or rec.stream_outcome == expected))
            rows.append({"path": name, "ok": ok, "status": rec.status, "model": model,
                         "text": rec.output_text[:40], "ttft_ms": rec.t_first_token_ms,
                         "total_ms": rec.t_end_ms, "outcome": rec.stream_outcome,
                         "usage": rec.usage, "request_id": rec.request_id,
                         "cost_usd": book(name, model, rec.usage,
                                          definitive_refusal(rec.status, rec.error, rec.n_text_deltas, rec.body)),
                         "error": rec.error or (rec.body.get("error") if isinstance(rec.body, dict)
                                                else None)})

        raw("raw_responses", "/responses",
            {"model": FL, "instructions": INSTR, "input": "Say OK.", "max_output_tokens": 16})
        raw("raw_responses_stream", "/responses",
            {"model": FL, "instructions": INSTR, "input": "Say OK.", "max_output_tokens": 16,
             "stream": True})
        raw("raw_chat_stream", "/chat/completions",
            {"model": FL, "max_tokens": 16, "stream": True,
             "stream_options": {"include_usage": True},
             "messages": [{"role": "system", "content": INSTR},
                          {"role": "user", "content": "Say OK."}]})

        def sdk(name: str, fn: Any) -> None:
            ledger.reserve("INF-01", f"{log.run_id}:{name}", RESERVE)
            t0 = time.perf_counter()
            try:
                model, text, usage, rid = fn()
                ok, err = bool(text), None
            except Exception as exc:  # noqa: BLE001 - the failure IS the result
                model = text = usage = rid = None
                ok, err = False, "".join(traceback.format_exception_only(exc)).strip()[:500]
            ms = round((time.perf_counter() - t0) * 1000, 1)
            usage = json_safe(usage)
            cost = book(name, model, usage, refused=False)  # book before anything that may fail
            row = {"path": name, "ok": ok, "model": model, "text": (text or "")[:40],
                   "total_ms": ms, "usage": usage, "request_id": rid, "cost_usd": cost,
                   "error": err}
            rows.append(row)
            try:
                log.note(**row)
            except (TypeError, ValueError) as exc:
                log.note(path=name, log_error=repr(exc))

        from openai import OpenAI

        oai = OpenAI(base_url=BASE_URL, api_key=creds.bearer, max_retries=0,
                     default_headers=HDRS)

        def oai_responses():
            r = oai.responses.create(model=FL, instructions=INSTR, input="Say OK.",
                                     max_output_tokens=16)
            return r.model, r.output_text, json_safe(r.usage), None

        def oai_chat():
            r = oai.chat.completions.create(
                model=FL, max_tokens=16,
                messages=[{"role": "system", "content": INSTR},
                          {"role": "user", "content": "Say OK."}])
            return (r.model, r.choices[0].message.content, json_safe(r.usage), None)

        sdk("openai_sdk_responses", oai_responses)
        sdk("openai_sdk_chat", oai_chat)

        def moss_sdk():
            from moss import MossInference

            async def go():
                async with MossInference(creds.project_id, creds.project_key, max_retries=0,
                                         default_headers=HDRS) as client:
                    r = await client.responses.create(
                        model=FL, instructions=INSTR, input="Say OK.", max_output_tokens=16)
                    return (getattr(r, "model", None), r.output_text,
                            json_safe(getattr(r, "usage", None)),
                            getattr(r, "request_id", None))

            return asyncio.run(go())

        sdk("moss_sdk_responses", moss_sdk)
    for r in rows:
        print(f"{'OK ' if r['ok'] else 'FAIL'} {r['path']:24} model={r.get('model')} "
              f"text={r.get('text')!r} total={r.get('total_ms')}ms cost=${r['cost_usd']:.6f} "
              f"{r.get('error') or ''}"[:220])
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    (RESULTS_PROCESSED / "inf01_smoke.json").write_text(json.dumps(rows, indent=2, default=str))
    print(f"exposure now ${ledger.spent():.4f}")


if __name__ == "__main__":
    main()
