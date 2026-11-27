"""Raw HTTP client for Moss Inference with byte-level timing.

Why not the SDKs for measurements: an SDK hides when the first byte and the first text delta arrive and
adds its own retries. Here every call is one HTTP exchange. The request is fully built first; the clock
`t0` starts immediately before `send`. All times are `time.perf_counter()` offsets from t0, in ms:

  t_headers       status line + headers received
  t_first_byte    first body chunk received from the socket
  t_first_event   first COMPLETE SSE event (dispatched at its blank-line delimiter)
  t_first_token   first complete event carrying non-empty text (Responses `response.output_text.delta`,
                  Chat `choices[0].delta.content`)  -> the "client time to first text delta" (TTFT)
  t_terminal      the terminal event (Responses completed/incomplete/failed, Chat `[DONE]`)
  t_end           stream closed / body read

Each event is timestamped with the arrival time of the chunk that completed it, so a frame split over
several TCP reads is dated when its last byte arrived. Multi-line `data:` fields are joined per the SSE
spec. `stream_outcome` says how a stream ended: completed | incomplete | failed | done | error_event |
closed_early (we stopped on purpose) | eof_no_terminal | unexpected_done | read_error.
An event missing its blank-line delimiter at EOF is discarded and logged as `<incomplete-frame>`.

httpx's trace extension tells whether the call opened a new connection; `connect_ms` covers TCP connect
only (DNS is separate in httpx 0.28 and not isolated here). Only a whitelist of response headers is
kept and the Authorization header is never logged. Response bodies ARE kept as received, so raw logs
can contain whatever the API sent back: publish them only through scripts/redact.py.
"""

from __future__ import annotations

import codecs
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import BASE_URL, Credentials

KEPT_HEADERS = (
    "x-moss-request-id",
    "x-moss-retrieve-ms",
    "x-moss-box",
    "retry-after",
    "x-should-retry",
    "idempotent-replayed",
    "content-type",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "server",
    "via",
)
# Also kept: any header with one of these prefixes (cache flags, unknown Moss headers). Never
# set-cookie or anything credential-like.
KEPT_PREFIXES = ("x-moss-", "x-cache", "age", "cache-control", "cf-cache-status")
TERMINAL_EVENTS = {"response.completed": "completed", "response.incomplete": "incomplete",
                   "response.failed": "failed"}


@dataclass
class CallRecord:
    """Everything logged about one HTTP exchange: request, status, kept headers, timings and body."""

    ts_utc: str
    method: str
    path: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    new_connection: bool = False
    connect_ms: float | None = None  # TCP connect, when a new connection was opened
    tls_ms: float | None = None
    t_headers_ms: float | None = None
    t_first_byte_ms: float | None = None
    t_first_event_ms: float | None = None
    t_first_token_ms: float | None = None
    t_terminal_ms: float | None = None
    t_end_ms: float | None = None
    n_text_deltas: int = 0
    event_types: list[str] = field(default_factory=list)
    # Per-frame metadata for contract checks: Chat chunks (choices count, usage present,
    # finish_reason), [DONE], and Responses function-call argument events (delta / arguments).
    frames: list[dict[str, Any]] = field(default_factory=list)
    stream_outcome: str | None = None
    output_text: str = ""
    body: Any = None  # parsed JSON body (non-stream) or final response / error object (stream)
    usage: dict[str, Any] | None = None
    model: str | None = None  # model named in the reply (body, terminal event or Chat chunks)
    request_id: str | None = None  # header, or the body's error.request_id when the header is absent
    error: str | None = None  # transport error, not an API error
    tag: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise the record as one JSON line."""
        return json.dumps(asdict(self), ensure_ascii=False)


class MossHTTP:
    """Thin httpx wrapper that sends one request per `call()` and times it at the byte level."""

    def __init__(
        self,
        creds: Credentials | None,
        base_url: str = BASE_URL,
        timeout: float = 120,
        transport: httpx.BaseTransport | None = None,
        bearer_override: str | None = None,
    ):
        # identity encoding: body bytes are timed as they come off the socket, before any decoding
        headers = {"User-Agent": "moss-inference-audit/0.2", "Accept-Encoding": "identity"}
        self.project_id = creds.project_id if creds is not None else None
        if bearer_override is not None:
            headers["Authorization"] = f"Bearer {bearer_override}"
        elif creds is not None:
            headers["Authorization"] = f"Bearer {creds.bearer}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=15),
            transport=transport,
        )

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> MossHTTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        raw_content: bytes | None = None,
        stop_after_first_token: bool = False,
        tag: dict[str, Any] | None = None,
        drain_after_done: bool = False,
        timeout_s: float | None = None,
    ) -> CallRecord:
        """Send one request and return its CallRecord.

        All times are milliseconds after the request is handed to httpx (`t0`, see the module
        docstring). TTFT is `t_first_token_ms`: the first COMPLETE event that carries non-empty text.
        `timeout_s` applies per httpx operation (connect, read, write), not to the whole call.
        With `stop_after_first_token` the stream is closed on purpose after that event
        (`stream_outcome = "closed_early"`); with `drain_after_done` reading continues after
        `[DONE]` to catch trailing frames. An API refusal (4xx/5xx) is a normal record: see
        `rec.status` and `rec.body`. Transport errors and malformed streams are recorded in
        `rec.error` / `rec.stream_outcome` instead of being raised.
        """
        rec = CallRecord(
            ts_utc=datetime.now(UTC).isoformat(timespec="milliseconds"),
            method=method,
            path=path,
            tag=tag or {},
        )
        marks: dict[str, float] = {}

        def trace(name: str, info: dict[str, Any]) -> None:
            marks[name] = time.perf_counter()

        req_headers = dict(headers or {})
        content: bytes | None = raw_content
        if body is not None and raw_content is None:
            content = json.dumps(body).encode()
            req_headers.setdefault("Content-Type", "application/json")
        ext: dict[str, Any] = {"trace": trace}
        if timeout_s is not None:
            ext["timeout"] = httpx.Timeout(max(timeout_s, 1.0)).as_dict()
        req = self._client.build_request(
            method, path, content=content, headers=req_headers, extensions=ext
        )
        resp: httpx.Response | None = None
        t0 = time.perf_counter()

        def ms(t: float) -> float:
            return round((t - t0) * 1000, 2)

        try:
            r = self._client.send(req, stream=True)
            resp = r
            rec.t_headers_ms = ms(time.perf_counter())
            rec.status = r.status_code
            rec.headers = {k.lower(): v for k, v in r.headers.items()
                           if k.lower() in KEPT_HEADERS or k.lower().startswith(KEPT_PREFIXES)}
            ctype = r.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                try:
                    _read_sse(r, rec, t0, stop_after_first_token, drain_after_done)
                except (TypeError, AttributeError, KeyError, IndexError, ValueError) as exc:
                    # A malformed frame shape must not abort the caller before it logs and
                    # settles the call: record it as a failed read instead.
                    rec.error = f"malformed_stream: {type(exc).__name__}: {exc}"[:300]
                    rec.stream_outcome = "read_error"
                    rec.t_end_ms = ms(time.perf_counter())
            else:
                chunks = []
                for chunk in _chunks(r):
                    if rec.t_first_byte_ms is None:
                        rec.t_first_byte_ms = ms(time.perf_counter())
                    chunks.append(chunk)
                raw = b"".join(chunks)
                rec.t_end_ms = ms(time.perf_counter())
                try:
                    rec.body = json.loads(raw)
                except ValueError:
                    rec.body = raw.decode(errors="replace")[:2000]
                if isinstance(rec.body, dict):
                    rec.usage = rec.body.get("usage")
                    rec.output_text = _output_text(rec.body)
                    if isinstance(rec.body.get("model"), str):
                        rec.model = rec.body["model"]
        except httpx.HTTPError as exc:
            rec.error = f"{type(exc).__name__}: {exc}"
            rec.t_end_ms = ms(time.perf_counter())
            if rec.stream_outcome is None and rec.status is not None:
                rec.stream_outcome = "read_error"
        finally:
            if resp is not None:
                resp.close()
        rec.request_id = rec.headers.get("x-moss-request-id") or _body_request_id(rec.body)
        if "connection.connect_tcp.complete" in marks:
            rec.new_connection = True
            rec.connect_ms = round(
                (marks["connection.connect_tcp.complete"] - marks["connection.connect_tcp.started"])
                * 1000, 2)
        if "connection.start_tls.complete" in marks:
            rec.tls_ms = round(
                (marks["connection.start_tls.complete"] - marks["connection.start_tls.started"])
                * 1000, 2)
        return rec


def _chunks(resp: httpx.Response) -> Any:
    """Body chunks as they arrive. Raw bytes when the server honoured identity encoding."""
    if getattr(resp, "_content", None) is not None:  # body already in memory (mock transports)
        return iter([resp.content])
    if resp.headers.get("content-encoding", "identity").lower() in ("", "identity"):
        return resp.iter_raw()
    return resp.iter_bytes()


def _body_request_id(body: Any) -> str | None:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("request_id"):
            return str(err["request_id"])
        if body.get("request_id"):
            return str(body["request_id"])
    return None


def _read_sse(resp: httpx.Response, rec: CallRecord, t0: float, stop_after_first_token: bool,
              drain: bool = False) -> None:
    """Frame-accurate SSE reader: events dispatch at the blank line that ends them."""

    def ms(t: float) -> float:
        return round((t - t0) * 1000, 2)

    texts: list[str] = []
    buf = ""
    event_name: str | None = None
    data_lines: list[str] = []
    stopped = False

    def dispatch(now: float) -> bool:
        """Handle one complete event. Returns True when reading should stop."""
        nonlocal event_name, data_lines
        name, data = event_name, "\n".join(data_lines)
        event_name, data_lines = None, []
        if rec.t_first_event_ms is None:
            rec.t_first_event_ms = ms(now)
        if data == "[DONE]":
            rec.event_types.append("[DONE]")
            rec.frames.append({"t": "[DONE]"})
            rec.t_terminal_ms = ms(now)
            # [DONE] is Chat's terminator; on Responses it is not a documented clean end.
            rec.stream_outcome = rec.stream_outcome or ("done" if chat else "unexpected_done")
            return not drain  # contract mode keeps reading to see what follows [DONE]
        try:
            obj = json.loads(data)
        except ValueError:
            rec.event_types.append("<invalid-json>")
            rec.frames.append({"t": "<invalid-json>"})
            return False
        if not isinstance(obj, dict):
            rec.event_types.append("<non-object>")
            rec.frames.append({"t": "<non-object>"})
            return False
        etype = str(obj.get("type") or name or obj.get("object")
                    or ("error" if "error" in obj else "?"))
        rec.event_types.append(etype)
        ch = obj.get("choices")
        frame: dict[str, Any] = {
            "t": etype,
            "choices": len(ch) if isinstance(ch, list) else None,  # None = field missing
            "usage": bool(obj.get("usage")), "error": bool(obj.get("error")),
            "finish": ch[0].get("finish_reason") if isinstance(ch, list) and ch
            and isinstance(ch[0], dict) else None,
        }
        if etype.startswith("response.function_call_arguments"):
            frame.update(delta=obj.get("delta"), arguments=obj.get("arguments"),
                         item_id=obj.get("item_id"), name=obj.get("name"))
        if etype == "response.output_item.done" and isinstance(obj.get("item"), dict):
            frame.update(item_id=obj["item"].get("id"), item_type=obj["item"].get("type"))
        rec.frames.append(frame)
        delta = ""
        if etype == "response.output_text.delta":
            delta = obj.get("delta") or ""
        elif obj.get("object") == "chat.completion.chunk":
            choices = obj.get("choices") or []
            if choices:
                delta = (choices[0].get("delta") or {}).get("content") or ""
            if obj.get("usage"):
                rec.usage = obj["usage"]
            if isinstance(obj.get("model"), str):
                rec.model = obj["model"]
        if "error" in obj and obj.get("error") and etype not in TERMINAL_EVENTS:
            # Chat error frames carry no `object`; Responses has `type: error`.
            rec.body = obj
            rec.stream_outcome = "error_event"
            rec.t_terminal_ms = ms(now)
        if etype in TERMINAL_EVENTS and not chat:
            rec.body = obj.get("response")
            if isinstance(rec.body, dict):
                rec.usage = rec.body.get("usage")
                if isinstance(rec.body.get("model"), str):
                    rec.model = rec.body["model"]
            rec.stream_outcome = TERMINAL_EVENTS[etype]
            rec.t_terminal_ms = ms(now)
        if delta:
            rec.n_text_deltas += 1
            texts.append(delta)
            if rec.t_first_token_ms is None:
                rec.t_first_token_ms = ms(now)
                if stop_after_first_token:
                    return True
        return False

    chat = rec.path.endswith("/chat/completions")
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    for raw_chunk in _chunks(resp):
        now = time.perf_counter()  # arrival time, taken before any decoding
        if rec.t_first_byte_ms is None:
            rec.t_first_byte_ms = ms(now)
        buf += decoder.decode(raw_chunk)
        while True:
            nl = buf.find("\n")
            if nl < 0:
                break
            line, buf = buf[:nl].rstrip("\r"), buf[nl + 1:]
            if line == "":
                if (data_lines or event_name) and dispatch(now):
                    stopped = True
                    break
                continue
            if line.startswith(":"):
                continue
            fname, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if fname == "event":
                event_name = value
            elif fname == "data":
                data_lines.append(value)
        if stopped:
            break
    if not stopped and (data_lines or buf.strip()):
        # An event without its terminating blank line is incomplete: never dispatched (SSE spec).
        rec.event_types.append("<incomplete-frame>")
    if stopped and rec.stream_outcome is None:
        rec.stream_outcome = "closed_early"
    if rec.stream_outcome is None:
        rec.stream_outcome = "eof_no_terminal"
    rec.t_end_ms = ms(time.perf_counter())
    rec.output_text = "".join(texts)


def _output_text(body: dict[str, Any]) -> str:
    """Text of a non-streamed Responses or Chat Completions body."""
    # Malformed shapes (null text, a number where a list is expected...) give "" instead of a crash,
    # so the call is still logged with its original body.
    if body.get("object") == "chat.completion":
        try:
            content = body["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""
        return content if isinstance(content, str) else ""
    parts = []
    output = body.get("output")
    for item in output if isinstance(output, list) else []:
        if isinstance(item, dict) and item.get("type") == "message":
            content = item.get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text = c.get("text")
                    parts.append(text if isinstance(text, str) else "")
    return "".join(parts)
