"""Offline tests (MOCK transport, no network, no credits)."""

import json

import httpx

from mossaudit.client import MossHTTP
from mossaudit.config import Credentials


def _sse(events: list[str]) -> bytes:
    return "".join(f"data: {e}\n\n" for e in events).encode()


def test_responses_stream_ttft_and_final_usage():
    events = [
        json.dumps({"type": "response.created"}),
        json.dumps({"type": "response.output_text.delta", "delta": ""}),
        json.dumps({"type": "response.output_text.delta", "delta": "Hel"}),
        json.dumps({"type": "response.output_text.delta", "delta": "lo"}),
        json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 3}}}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer p:k"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-moss-request-id": "r1"},
            content=_sse(events),
        )

    with MossHTTP(Credentials("p", "k"), transport=httpx.MockTransport(handler)) as c:
        rec = c.call("POST", "/responses", {"input": "hi", "stream": True})
    assert rec.status == 200
    assert rec.output_text == "Hello"
    assert rec.n_text_deltas == 2  # the empty delta does not count as the first token
    assert rec.t_first_token_ms is not None and rec.t_first_token_ms >= rec.t_first_event_ms
    assert rec.usage == {"input_tokens": 3}
    assert rec.headers["x-moss-request-id"] == "r1"
    assert "authorization" not in json.dumps(rec.headers).lower()


def test_chat_stream_usage_chunk_and_done():
    events = [
        json.dumps({"object": "chat.completion.chunk", "choices": [{"delta": {"content": "Yes"}}]}),
        json.dumps({"object": "chat.completion.chunk", "choices": [], "usage": {"total_tokens": 5}}),
        "[DONE]",
    ]
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(events))
    )
    with MossHTTP(None, transport=transport) as c:
        rec = c.call("POST", "/chat/completions", {"stream": True})
    assert rec.output_text == "Yes"
    assert rec.usage == {"total_tokens": 5}
    assert rec.event_types[-1] == "[DONE]"


def test_non_stream_error_body_parsed():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"code": "unsupported_parameter"}})
    )
    with MossHTTP(None, transport=transport) as c:
        rec = c.call("POST", "/responses", {"input": "x", "seed": 1})
    assert rec.status == 400
    assert rec.body["error"]["code"] == "unsupported_parameter"


class _Slow(httpx.SyncByteStream):
    """Yields chunks with pauses, to emulate an SSE frame split across TCP reads (MOCK)."""

    def __init__(self, parts: list[tuple[float, bytes]]):
        self.parts = parts

    def __iter__(self):
        import time

        for pause, chunk in self.parts:
            time.sleep(pause)
            yield chunk


def _stream_client(parts: list[tuple[float, bytes]]) -> MossHTTP:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                 stream=_Slow(parts)))
    return MossHTTP(None, transport=transport)


def test_split_frame_is_dated_at_its_delimiter():
    delta = json.dumps({"type": "response.output_text.delta", "delta": "Hi"})
    done = json.dumps({"type": "response.completed", "response": {"usage": {"output_tokens": 1}}})
    parts = [(0.0, f"data: {delta[:10]}".encode()), (0.15, f"{delta[10:]}\n\n".encode()),
             (0.0, f"data: {done}\n\n".encode())]
    with _stream_client(parts) as c:
        rec = c.call("POST", "/responses", {"stream": True})
    assert rec.t_first_byte_ms < 100  # the first bytes came at once
    assert rec.t_first_token_ms >= 140  # but the event was only complete after the pause
    assert rec.stream_outcome == "completed" and rec.t_terminal_ms is not None


def test_multiline_data_field_is_joined():
    parts = [(0.0, b'data: {"type": "response.output_text.delta",\ndata:  "delta": "ok"}\n\n')]
    with _stream_client(parts) as c:
        rec = c.call("POST", "/responses", {"stream": True})
    assert rec.output_text == "ok"
    assert rec.stream_outcome == "eof_no_terminal"  # no terminal event was sent


def test_chat_error_frame_without_object_is_kept():
    err = json.dumps({"error": {"message": "upstream failed", "code": "upstream_error"}})
    parts = [(0.0, f"data: {err}\n\n".encode())]
    with _stream_client(parts) as c:
        rec = c.call("POST", "/chat/completions", {"stream": True})
    assert rec.stream_outcome == "error_event"
    assert rec.body["error"]["code"] == "upstream_error"


def test_early_stop_is_labelled():
    d = json.dumps({"type": "response.output_text.delta", "delta": "a"})
    parts = [(0.0, f"data: {d}\n\ndata: {d}\n\n".encode())]
    with _stream_client(parts) as c:
        rec = c.call("POST", "/responses", {"stream": True}, stop_after_first_token=True)
    assert rec.stream_outcome == "closed_early" and rec.n_text_deltas == 1


def test_request_id_falls_back_to_body():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": {"code": "x", "request_id": "rid9"}}))
    with MossHTTP(None, transport=transport) as c:
        rec = c.call("POST", "/tasks", {})
    assert rec.request_id == "rid9"


def test_terminal_without_delimiter_is_discarded():
    done = json.dumps({"type": "response.completed", "response": {"usage": {"output_tokens": 1}}})
    parts = [(0.0, f"data: {done}".encode())]  # no blank line: the event never completed
    with _stream_client(parts) as c:
        rec = c.call("POST", "/responses", {"stream": True})
    assert rec.stream_outcome == "eof_no_terminal"
    assert rec.event_types == ["<incomplete-frame>"] and rec.usage is None


def test_done_on_responses_is_not_a_clean_end():
    with _stream_client([(0.0, b"data: [DONE]\n\n")]) as c:
        rec = c.call("POST", "/responses", {"stream": True})
    assert rec.stream_outcome == "unexpected_done"


def test_smoke_plain_serializes_nested_sdk_objects():
    import dataclasses
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "smoke", pathlib.Path(__file__).parents[2] / "benchmarks/inference/smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    @dataclasses.dataclass
    class Details:
        cached_tokens: int

    @dataclasses.dataclass
    class Usage:
        input_tokens: int
        output_tokens: int
        input_tokens_details: Details

    out = smoke.plain(Usage(3, 1, Details(0)))
    json.dumps(out)
    assert out["input_tokens_details"] == {"cached_tokens": 0}


def test_chat_stream_model_is_kept():
    events = [json.dumps({"object": "chat.completion.chunk", "model": "moss/fastlane-1",
                          "choices": [{"delta": {"content": "Y"}}]}), "[DONE]"]
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                 content=_sse(events)))
    with MossHTTP(None, transport=transport) as c:
        rec = c.call("POST", "/chat/completions", {"stream": True})
    assert rec.model == "moss/fastlane-1" and rec.stream_outcome == "done"
