"""INF-09: function tool calling on Responses, the documented loop (tool-calling.md).

20 prompts on moss/fastlane-1, two function tools (lookup_order, get_weather):
- 8 order prompts   -> expect exactly one lookup_order call with arguments exactly {"id": <id>};
- 6 weather prompts -> expect exactly one get_weather call with arguments exactly {"city": <city>};
- 6 no-tool prompts -> expect no call.
Only a response with status "completed" is scored; anything else is "response_<status>".
A call is accepted only with the right name, a non-empty string call_id, string arguments that parse
to exactly the expected object. Only then do we send round 2 as documented: earlier input + the
reply's output items unchanged + one function_call_output with the same call_id, tools again.
Each tool result carries an opaque reference (e.g. TRK-7Q4M). Round 2 passes only if it completes,
makes no further call, and the final text contains that reference as a whole token, which the model
cannot produce without reading the result. "more_calls" = reached this experiment's 2-round limit
(the documented protocol allows more rounds).
The same validator and dispatch run through the Moss Python SDK on the first 2 order prompts.
Paced 1 s; a 429 waits its full Retry-After, above 60 s the run stops. Reply cache and retrieval off.

Run:  python benchmarks/inference/tools.py
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from mossaudit.client import MossHTTP
from mossaudit.config import RESULTS_PROCESSED, load_credentials
from mossaudit.jsonsafe import json_safe
from mossaudit.ledger import Ledger
from mossaudit.prices import PRICES, definitive_refusal, token_cost, usage_tokens
from mossaudit.runlog import RunLog

FL = "moss/fastlane-1"
INSTR = ("You are the support agent for Acme. Use a tool when it can answer; otherwise answer "
         "directly. When a tool result contains a reference code, quote it exactly.")
HDRS = {"X-Moss-Cache": "off", "X-Moss-Retrieve": "off"}
TOOLS = [
    {"type": "function", "name": "lookup_order", "description": "Look up an order by its id.",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"type": "function", "name": "get_weather", "description": "Current weather for a city.",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                    "required": ["city"]}},
]
ORDERS = ["K7Q-3814", "ZT-90215", "A1B2-77", "QX-4410-M", "RR-0057", "LM-8823", "P-31416", "VX-2207"]
ORDER_REFS = ["TRK-7Q4M", "TRK-B2XW", "TRK-9HJD", "TRK-K3VZ", "TRK-P8RN", "TRK-W5LC", "TRK-F6TY",
              "TRK-M1QG"]
CITIES = ["Brellmouth", "Ostravane", "Kilderwick", "Saffrona", "Thornmere", "Duvallen"]
CITY_REFS = ["WX-4821", "WX-0937", "WX-5564", "WX-7102", "WX-3388", "WX-9045"]
NO_TOOL = ["What is 7 times 8?", "Say hello in French.", "What colour is a ripe banana?",
           "Spell the word cat backwards.", "How many days are in a week?",
           "What is the capital of Italy?"]
MAX_OUT = 120
RESERVE = 0.001


class Stop(Exception):
    pass


def prompts() -> list[dict[str, Any]]:
    out = [{"id": f"o{i}", "input": f"Where is my order {o}? Give me the tracking reference.",
            "tool": "lookup_order", "args": {"id": o},
            "result": f"Order {o} shipped via Quillfast, tracking reference {r}.", "ref": r}
           for i, (o, r) in enumerate(zip(ORDERS, ORDER_REFS, strict=True))]
    out += [{"id": f"w{i}", "input": f"What is the weather in {c} right now? Quote the station code.",
             "tool": "get_weather", "args": {"city": c},
             "result": f"{c}: light rain, station code {r}.", "ref": r}
            for i, (c, r) in enumerate(zip(CITIES, CITY_REFS, strict=True))]
    out += [{"id": f"n{i}", "input": q, "tool": None, "args": None} for i, q in enumerate(NO_TOOL)]
    return out


def check_call(p: dict[str, Any], calls: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Same validator for the raw and SDK paths."""
    if p["tool"] is None:
        return ("correct_no_call" if not calls else "unexpected_call"), None
    if len(calls) != 1:
        return ("no_call" if not calls else f"{len(calls)}_calls"), None
    c = calls[0]
    if c.get("name") != p["tool"]:
        return "wrong_tool", c
    if not isinstance(c.get("call_id"), str) or not c["call_id"]:
        return "missing_call_id", c
    raw = c.get("arguments")
    if not isinstance(raw, str):
        return "non_string_args", c
    try:
        args = json.loads(raw)
    except ValueError:
        return "invalid_args_json", c
    if args != p["args"]:  # exact object: no extra or missing keys
        return "wrong_args", c
    return "correct_call", c


def round2_verdict(p: dict[str, Any], status: str | None, calls: list[Any], text: str | None) -> str:
    if status != "completed":
        return f"response_{status}"
    if calls:
        return "more_calls"
    return ("uses_result" if re.search(rf"(?<![A-Za-z0-9-]){re.escape(p['ref'])}(?![A-Za-z0-9-])",
                                       text or "") else "ignores_result")


def wait_or_stop(next_start: list[float], retry_after: Any) -> None:
    try:
        wait = float(retry_after or 1)
    except (TypeError, ValueError):
        wait = 1.0
    if wait > 60:
        raise Stop(f"Retry-After {wait:.0f}s > 60 s")
    next_start[0] = max(next_start[0], time.monotonic() + wait)


def main() -> None:
    creds = load_credentials()
    ledger = Ledger()
    ledger.guard(RESERVE * 40, "INF-09 tools")
    rows: list[dict[str, Any]] = []
    with RunLog("inf09_tools", {"model": FL, "n_prompts": 20}) as log, MossHTTP(creds) as http:
        next_start = [0.0]

        def pace() -> None:
            time.sleep(max(0.0, next_start[0] - time.monotonic()))
            next_start[0] = time.monotonic() + 1.0

        def post(body: dict[str, Any], tag: dict[str, Any]) -> Any:
            pace()
            key = f"{log.run_id}:{tag['p']}:{tag['round']}"
            ledger.reserve("INF-09", key, RESERVE)
            rec = http.call("POST", "/responses", body=body, headers=HDRS, tag=tag)
            log.call(rec)
            toks = usage_tokens(rec.usage)
            if toks:
                ledger.charge("INF-09", token_cost(rec.model if rec.model in PRICES else FL, *toks),
                              key=key)
            elif definitive_refusal(rec.status, rec.error, rec.n_text_deltas, rec.body):
                ledger.release(key)
            else:
                ledger.charge("INF-09", RESERVE, key=key, usage_missing=True, status=rec.status)
            if rec.status == 429:
                wait_or_stop(next_start, rec.headers.get("retry-after"))
            return rec

        def output_of(rec: Any) -> tuple[str | None, list[dict[str, Any]], list[Any]]:
            b = rec.body if isinstance(rec.body, dict) else {}
            out = b.get("output") if isinstance(b.get("output"), list) else []
            calls = [i for i in out if isinstance(i, dict) and i.get("type") == "function_call"]
            return (b.get("status") if rec.status == 200 else f"http{rec.status}"), calls, out

        try:
            for p in prompts():
                items: list[Any] = [{"role": "user", "content": p["input"]}]
                body = {"model": FL, "instructions": INSTR, "input": items, "tools": TOOLS,
                        "max_output_tokens": MAX_OUT}
                r1 = post(body, {"p": p["id"], "round": 1})
                st1, calls1, out1 = output_of(r1)
                verdict, call = (check_call(p, calls1) if st1 == "completed"
                                 else (f"response_{st1}", None))
                row = {"p": p["id"], "path": "raw", "expected_tool": p["tool"], "round1": verdict,
                       "round1_status": r1.status, "round1_text": r1.output_text, "call": call,
                       "round2": None, "final_text": None}
                if verdict == "correct_call" and call:
                    items2 = items + out1 + [{"type": "function_call_output",
                                              "call_id": call["call_id"], "output": p["result"]}]
                    r2 = post({**body, "input": items2}, {"p": p["id"], "round": 2})
                    st2, calls2, _ = output_of(r2)
                    row["final_text"] = r2.output_text
                    row["round2"] = round2_verdict(p, st2, calls2, r2.output_text)
                rows.append(row)
                print(f"{p['id']:3} raw  r1={verdict:16} r2={row['round2']} "
                      f"{(row['final_text'] or row['round1_text'] or '')[:60]!r}")

            # The same loop through the Moss SDK (documented example), same validator.
            from moss import MossInference

            async def sdk_loop(p: dict[str, Any]) -> dict[str, Any]:
                row: dict[str, Any] = {"p": p["id"], "path": "sdk", "expected_tool": p["tool"]}
                async with MossInference(creds.project_id, creds.project_key, max_retries=0,
                                         default_headers=HDRS) as client:
                    items: list[Any] = [{"role": "user", "content": p["input"]}]
                    for rnd in (1, 2):
                        pace()
                        key = f"{log.run_id}:sdk:{p['id']}:{rnd}"
                        ledger.reserve("INF-09", key, RESERVE)
                        try:
                            resp = await client.responses.create(
                                model=FL, instructions=INSTR, input=items, tools=TOOLS,
                                max_output_tokens=MAX_OUT)
                        except Exception as exc:  # noqa: BLE001 - the failure is the result
                            ledger.charge("INF-09", RESERVE, key=key, usage_missing=True)
                            row[f"round{rnd}"] = f"sdk_error: {repr(exc)[:300]}"
                            log.note(case=p["id"], path="sdk", round=rnd, error=repr(exc)[:300])
                            if getattr(exc, "status", None) == 429:  # moss InferenceError
                                wait_or_stop(next_start, getattr(exc, "retry_after", None))
                            return row
                        u = json_safe(getattr(resp, "usage", None))
                        toks = usage_tokens(u if isinstance(u, dict) else None)
                        ledger.charge("INF-09", token_cost(FL, *toks) if toks else RESERVE,
                                      key=key, usage_missing=not toks)
                        out = list(resp.output)
                        # validate/log the wire dict (.raw); replay the original items unchanged
                        calls = [json_safe(getattr(i, "raw", None) or i) for i in out
                                 if getattr(i, "type", None) == "function_call"]
                        status = getattr(resp, "status", None)
                        log.note(case=p["id"], path="sdk", round=rnd, status=status,
                                 calls=calls, text=resp.output_text, usage=u)
                        if rnd == 1:
                            v, call = (check_call(p, calls) if status == "completed"
                                       else (f"response_{status}", None))
                            row["round1"] = v
                            if v != "correct_call" or not call:
                                return row
                            items += out
                            items.append({"type": "function_call_output",
                                          "call_id": call["call_id"], "output": p["result"]})
                        else:
                            row["round2"] = round2_verdict(p, status, calls, resp.output_text)
                            row["final_text"] = resp.output_text
                return row

            for p in prompts()[:2]:
                r = asyncio.run(sdk_loop(p))
                rows.append(r)
                print(f"{p['id']:3} sdk  {r}"[:160])
        except Stop as exc:
            log.note(stopped=str(exc))
            print(f"stopped: {exc}")
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    (RESULTS_PROCESSED / "inf09_tools.json").write_text(json.dumps(rows, indent=2, default=str))
    raw = [r for r in rows if r["path"] == "raw"]
    tally: dict[str, int] = {}
    for r in raw:
        tally[r["round1"]] = tally.get(r["round1"], 0) + 1
    print("round1:", tally, "round2:", [r["round2"] for r in raw if r["round2"]])
    print(f"exposure now ${ledger.spent():.4f}")


if __name__ == "__main__":
    main()
