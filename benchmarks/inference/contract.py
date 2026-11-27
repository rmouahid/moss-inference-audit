"""INF-10 / INF-11: error and request contract of the Inference API, case by case against the docs.

Each case states what the docs say should happen (status and, when documented, error code) and the docs
page that says it. Most cases are refused before any model runs, so they cost nothing; the few that reach
a model use tiny prompts and `max_output_tokens` 16, and their cost is recorded in the ledger.

Run:  python benchmarks/inference/contract.py [--only NAME ...] [--index faq]
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any

from mossaudit.client import MossHTTP
from mossaudit.config import RESULTS_PROCESSED, load_credentials
from mossaudit.ledger import Ledger
from mossaudit.prices import PRICES, RETRIEVAL_USD, definitive_refusal, token_cost, usage_tokens
from mossaudit.runlog import RunLog

FL = "moss/fastlane-1"
D_COMPAT = "inference/openai-compatibility"
D_RESP = "api-reference/v1/inference/responses"
D_CHAT = "api-reference/v1/inference/chat-completions"
D_ERR = "inference/errors-and-limits"
D_MODELS = "inference/models"
D_RETR = "inference/retrieval"


@dataclass
class Case:
    name: str
    path: str
    body: Any = None
    expect_status: int | None = None  # None = undocumented, we only observe
    expect_code: str | None = None
    doc: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None
    auth: str = "ok"  # ok | none | bad_key | no_colon
    note: str = ""
    check: str | None = None  # name of a reply-body predicate in CHECKS, applied on 2xx


CHECKS: dict[str, Any] = {
    "store_false": lambda b: isinstance(b, dict) and b.get("store") is False,
    "model_is_fastlane": lambda b: isinstance(b, dict) and b.get("model") == FL,
}


def response_body(**overrides: Any) -> dict[str, Any]:
    """Minimal valid Responses body plus overrides."""
    return {"model": FL, "instructions": "Reply with the single word OK.", "input": "Say OK.",
            "max_output_tokens": 16, **overrides}


def chat_body(**overrides: Any) -> dict[str, Any]:
    """Minimal valid Chat Completions body plus overrides."""
    return {"model": FL, "messages": [{"role": "user", "content": "Say OK."}],
            "max_tokens": 16, **overrides}


def cases(index: str | None) -> list[Case]:
    big_prompt = "lorem " * 300_000  # ~300k tokens, > 262,144-token fastlane window, ~1.8 MB (< 4 MB)
    test_cases = [
        # --- authentication ---
        Case("auth_missing", "/responses", response_body(), 401, None, D_ERR, auth="none"),
        Case("auth_wrong_key", "/responses", response_body(), 401, None, D_ERR, auth="bad_key"),
        Case("auth_no_colon", "/responses", response_body(), 401, None, D_ERR, auth="no_colon"),
        Case("auth_missing_invalid_body", "/responses", {}, None, None, D_ERR, auth="none",
             note="precedence of auth vs validation is undocumented: observe only"),
        Case("auth_bogus_creds_invalid_body", "/chat/completions", {}, None, None, D_ERR,
             auth="no_colon_bogus", note="F6: bogus id:key + bad body -> 400 before 401?"),
        Case("models_no_auth", "/models", None, None, None, D_MODELS, method="GET", auth="none",
             note="docs always send the bearer token"),
        Case("models_retrieve_one", f"/models/{FL}", None, None, None, D_MODELS, method="GET",
             note="OpenAI clients call models.retrieve(id); not documented by Moss"),
        Case("method_get_responses", "/responses", None, None, None, D_RESP, method="GET"),
        # --- request shape, Responses ---
        Case("resp_missing_input", "/responses", {"model": FL}, 400, None, D_RESP),
        Case("resp_input_number", "/responses", response_body(input=42), 400, None, D_RESP),
        Case("resp_input_empty_string", "/responses", response_body(input=""), None, None, D_RESP,
             note="empty input: undocumented"),
        Case("resp_input_empty_list", "/responses", response_body(input=[]), None, None, D_RESP),
        Case("resp_bad_role", "/responses", response_body(input=[{"role": "tool", "content": "x"}]),
             400, None, D_RESP, note="roles: user, system, developer, assistant"),
        Case("resp_unknown_field", "/responses", response_body(foo=1), 400,
             "unsupported_parameter", D_RESP),
        Case("resp_previous_response_id", "/responses", response_body(previous_response_id="resp_x"),
             400, "unsupported_parameter", D_COMPAT),
        Case("resp_background_true", "/responses", response_body(background=True), 400,
             "unsupported_parameter", D_RESP),
        Case("resp_include_nonempty", "/responses",
             response_body(include=["reasoning.encrypted_content"]), 400,
             "unsupported_parameter", D_RESP),
        Case("resp_truncation_auto", "/responses", response_body(truncation="auto"), 400,
             "unsupported_parameter", D_RESP),
        Case("resp_background_false_ok", "/responses", response_body(background=False), 200, None,
             D_RESP, note="documented as accepted"),
        Case("resp_include_empty_ok", "/responses", response_body(include=[]), 200, None, D_RESP),
        Case("resp_truncation_disabled_ok", "/responses", response_body(truncation="disabled"),
             200, None, D_RESP),
        Case("resp_instructions_list", "/responses", response_body(instructions=["a"]), 400,
             None, D_RESP),
        Case("resp_verbosity_low", "/responses", response_body(text={"verbosity": "low"}), 400,
             "unsupported_parameter", D_RESP),
        Case("resp_reasoning_summary", "/responses", response_body(reasoning={"summary": "auto"}), 400,
             "unsupported_parameter", D_RESP),
        Case("resp_tool_web_search", "/responses", response_body(tools=[{"type": "web_search"}]), 400,
             "unsupported_parameter", D_RESP),
        Case("resp_input_file_part", "/responses",
             response_body(input=[{"role": "user", "content": [{"type": "input_file", "file_id": "f"}]}]),
             400, "unsupported_parameter", D_RESP),
        Case("resp_store_ignored", "/responses", response_body(store=True), 200, None, D_RESP,
             note="store is ignored; reply store must be false", check="store_false"),
        Case("resp_null_field_is_unset", "/responses", response_body(temperature=None),
             200, None, D_RESP),
        Case("resp_malformed_json", "/responses", None, 400, None, D_ERR, raw=b'{"input": "x",'),
        Case("resp_text_plain_ctype", "/responses", None, None, None, D_ERR,
             raw=json.dumps(response_body()).encode(), headers={"Content-Type": "text/plain"}),
        Case("resp_body_over_4mb", "/responses", None, 413, None, D_ERR,
             raw=json.dumps(response_body(input="a" * (4 * 1024 * 1024 + 10))).encode()),
        Case("resp_context_length_exceeded", "/responses", response_body(input=big_prompt), 400,
             "context_length_exceeded", D_MODELS),
        # --- models ---
        Case("model_unknown", "/responses", response_body(model="moss/does-not-exist"), 400,
             "model_not_found", D_ERR),
        Case("model_bare_vendor_name", "/responses", response_body(model="gpt-6-luna"), 400,
             "missing_provider_key", D_ERR),
        Case("model_alias_moss", "/responses", response_body(model="moss"), 200, None, D_MODELS,
             note="reply model must read moss/fastlane-1", check="model_is_fastlane"),
        Case("model_omitted", "/responses", {"input": "Say OK.", "max_output_tokens": 16}, 200,
             None, D_RESP),
        # --- refused parameters on the Moss key (models#parameters-refused-on-the-moss-key) ---
        Case("openai_seed_chat", "/chat/completions", chat_body(model="openai/gpt-6-luna", seed=1), 400,
             "unsupported_parameter", D_MODELS,
             note="Chat passes unknown fields through, so this isolates the vendor-side refusal"),
        Case("openai_stop_chat", "/chat/completions", chat_body(model="openai/gpt-6-luna", stop=["x"]),
             400, "unsupported_parameter", D_MODELS),
        Case("anthropic_temperature", "/responses",
             response_body(model="anthropic/claude-haiku-5.5", temperature=0.2),
             400, "unsupported_parameter", D_MODELS),
        Case("anthropic_temperature_1_is_unset", "/responses",
             response_body(model="anthropic/claude-haiku-5.5", temperature=1), 200, None, D_MODELS),
        Case("anthropic_json_object", "/responses",
             response_body(model="anthropic/claude-haiku-5.5", text={"format": {"type": "json_object"}}),
             400, "unsupported_parameter", D_MODELS),
        Case("fastlane_seed", "/responses", response_body(seed=1), 400, "unsupported_parameter", D_RESP,
             note="seed is not a Responses field: 'any other field' rule"),
        # --- Chat Completions ---
        Case("chat_missing_messages", "/chat/completions", {"model": FL}, 400, None, D_CHAT),
        Case("chat_empty_messages", "/chat/completions", chat_body(messages=[]), None, None, D_CHAT),
        Case("chat_unknown_field", "/chat/completions", chat_body(foo=1), None, None, D_CHAT,
             note="docs: 'Other OpenAI fields: passed to the model'"),
        Case("chat_n_2", "/chat/completions", chat_body(n=2), None, None, D_CHAT,
             note="docs: one choice"),
        Case("chat_tools_not_list", "/chat/completions", chat_body(tools={"type": "function"}),
             400, None, D_CHAT),
        # --- retrieval headers ---
        Case("indexes_33", "/responses", response_body(), 400, None, D_RETR,
             headers={"X-Moss-Indexes": ",".join(f"i{n}" for n in range(33))}),
        Case("index_unknown_name", "/responses", response_body(), None, None, D_RETR,
             headers={"X-Moss-Indexes": "index-that-does-not-exist-9f3c"},
             note="unknown name: undocumented (the 403 rule is for another project's index, "
                  "which we do not test)"),
        Case("retrieve_header_garbage", "/responses", response_body(), None, None, D_COMPAT,
             headers={"X-Moss-Retrieve": "maybe"}),
    ]
    if index:
        test_cases += [
            Case("index_ok", "/responses", response_body(input="Do you ship to Canada?"),
                 200, None, D_RETR, headers={"X-Moss-Indexes": index}),
            Case("index_ok_retrieve_off", "/responses", response_body(input="Do you ship to Canada?"),
                 200, None, D_RETR, headers={"X-Moss-Indexes": index, "X-Moss-Retrieve": "off"}),
            Case("index_duplicate_names", "/responses", response_body(input="Do you ship to Canada?"),
                 None, None, D_RETR, headers={"X-Moss-Indexes": f"{index},{index}"}),
            Case("index_with_spaces", "/responses", response_body(input="Do you ship to Canada?"),
                 None, None, D_RETR, headers={"X-Moss-Indexes": f" {index} , "}),
        ]
    return test_cases


def run(selected: list[str] | None, index: str | None) -> None:
    creds = load_credentials()
    ledger = Ledger()
    todo = [case for case in cases(index) if not selected or case.name in selected]
    # Worst case if every model-reaching case is accepted: one 300k-token fastlane prompt ($0.075)
    # plus ~25 tiny calls.
    ledger.guard(0.10, "INF-10 contract")
    rows = []
    clients = {
        "ok": MossHTTP(creds),
        "none": MossHTTP(None),
        "bad_key": MossHTTP(type(creds)(creds.project_id, "wrong-key-000")),
        "no_colon": MossHTTP(None, bearer_override="moss_dummy_not_a_real_key"),
        "no_colon_bogus": MossHTTP(None, bearer_override="x:y"),
    }
    with RunLog("inf10_contract", {"n_cases": len(todo), "index": index}) as log:
        for case in todo:
            big = (len(json.dumps(case.body or {})) > 100_000
                   or (case.raw is not None and len(case.raw) > 100_000))
            reserve = 0.08 if big else 0.003
            reservation_key = f"{log.run_id}:{case.name}"
            ledger.reserve("INF-10", reservation_key, reserve)
            headers = {"X-Moss-Cache": "off", **case.headers}
            if "X-Moss-Indexes" not in headers:
                headers.setdefault("X-Moss-Retrieve", "off")
            rec = clients[case.auth].call(
                case.method, case.path, body=case.body, headers=headers, raw_content=case.raw,
                tag={"case": case.name},
            )
            code = typ = msg = None
            if isinstance(rec.body, dict) and isinstance(rec.body.get("error"), dict):
                err = rec.body["error"]
                code, typ, msg = err.get("code"), err.get("type"), err.get("message")
            usage = rec.usage or {}
            model = (rec.body or {}).get("model") if isinstance(rec.body, dict) else None
            cost = 0.0
            toks = usage_tokens(usage)
            if rec.status == 200 and toks and model in PRICES:
                inp, out, cached = toks
                retrieved = "X-Moss-Indexes" in headers and headers.get("X-Moss-Retrieve") != "off"
                cost = token_cost(model, inp, out, cached) + (RETRIEVAL_USD if retrieved else 0)
                ledger.charge("INF-10", cost, key=reservation_key, case=case.name, model=model)
            elif definitive_refusal(rec.status, rec.error, rec.n_text_deltas, rec.body):
                ledger.release(reservation_key)
            else:
                # 2xx without priced usage, 5xx, transport error: book the full hold, reconcile later
                cost = reserve
                ledger.charge("INF-10", cost, key=reservation_key, case=case.name, model=model,
                              unpriced=True, status=rec.status)
            status_ok = case.expect_status is None or rec.status == case.expect_status
            code_ok = case.expect_code is None or case.expect_code in (code, typ)
            check_ok = (case.check is None or rec.status != 200 or bool(CHECKS[case.check](rec.body)))
            if rec.error:
                verdict = "TRANSPORT"
            elif case.expect_status is None:
                verdict = "observe"
            else:
                verdict = "PASS" if status_ok and code_ok and check_ok else "MISMATCH"
            rows.append({
                "case": case.name,
                "expected": f"{case.expect_status or '?'} {case.expect_code or ''}".strip(),
                "observed": f"{rec.status} {code or ''}".strip(), "type": typ, "verdict": verdict,
                "message": (msg or (rec.output_text[:60] if rec.status == 200 else str(rec.body)[:80])),
                "reply_model": model, "content_type": rec.headers.get("content-type"),
                "request_id": rec.request_id, "check": case.check,
                "check_passed": None if case.check is None or rec.status != 200 else check_ok,
                "doc": case.doc, "note": case.note,
                "cost_usd": round(cost, 6),
            })
            log.call(rec)
            print(f"{verdict:8} {case.name:34} exp={rows[-1]['expected']:28} obs={rows[-1]['observed']}")
    for client in clients.values():
        client.close()
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    out = RESULTS_PROCESSED / "inf10_contract.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--index")
    args = ap.parse_args()
    run(args.only, args.index)
