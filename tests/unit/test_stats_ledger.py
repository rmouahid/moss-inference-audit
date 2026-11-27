"""Offline tests of stats, prices, ledger reservations and the Tasks safety rules (MOCK, no network)."""

import json
import math

import httpx
import pytest

from mossaudit.client import MossHTTP
from mossaudit.ledger import BudgetExceeded, Ledger
from mossaudit.prices import token_cost
from mossaudit.stats import percentile, wilson
from scripts.analyze_latency import run_summary


def test_percentile_matches_numpy_linear():
    xs = [1, 2, 3, 4, 10]
    assert percentile(xs, 50) == 3
    assert percentile(xs, 90) == pytest.approx(7.6)


def test_wilson_bounds():
    lo, hi = wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60


def test_latency_pairs_within_model_and_reads_chat_usage(tmp_path):
    records = [{"type": "run_meta", "run_id": "mock", "started_utc": "2026-11-25", "params": {}}]
    for model, grounded, ttft in [
        ("fastlane", False, 100), ("haiku", True, 500),
        ("fastlane", True, 150), ("haiku", False, 300),
    ]:
        records.append({
            "type": "call", "status": 200, "stream_outcome": "done",
            "t_first_token_ms": ttft, "t_end_ms": ttft + 10, "headers": {},
            "tag": {"unit": 0, "model": model, "ctx": 0, "prefix": "repeat", "grounded": grounded},
            "usage": {"prompt_tokens": 12, "completion_tokens": 1,
                      "prompt_tokens_details": {"cached_tokens": 6}},
        })
    log = tmp_path / "latency.jsonl"
    log.write_text("\n".join(json.dumps(record) for record in records))

    summary = run_summary(log)
    paired = summary["paired_grounded_minus_plain_ms"]
    assert (paired["n"], paired["min"], paired["p50"], paired["max"]) == (2, 50, 125, 200)
    for condition in summary["conditions"]:
        assert condition["input_tokens"]["p50"] == 12
        assert condition["cached_tokens"] == [6]


def test_token_cost_fastlane():
    assert token_cost("moss/fastlane-1", 1_000_000, 1_000_000) == pytest.approx(2.0)
    assert token_cost("moss/fastlane-1", 1_000_000, 0, cached_tokens=1_000_000) == pytest.approx(0.03)


def test_reservation_counts_until_settled(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", cap_usd=1.0)
    led.reserve("a", "k1", 0.6)
    with pytest.raises(BudgetExceeded):
        led.reserve("a", "k2", 0.5)  # 0.6 already reserved
    led.charge("a", 0.1, key="k1")  # settles below the reserve
    assert led.spent() == pytest.approx(0.1)
    led.reserve("a", "k2", 0.5)


def test_task_snapshot_keeps_max_and_reserve_covers_gap(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", cap_usd=5.0)
    led.reserve("t", "task-create:x", 1.0)
    led.task_snapshot("t", "task_1", 0.3, key="task-create:x")
    led.task_snapshot("t", "task_1", 0.0, key="task-create:x")  # stale/zero snapshot
    st = led.state()
    assert st["tasks"]["task_1"] == pytest.approx(0.3)
    assert st["exposure"] == pytest.approx(1.0)  # reserve still covers the unknown remainder
    led.task_snapshot("t", "task_1", 1.4, key="task-create:x")  # overshoot beyond the reserve
    assert led.spent() == pytest.approx(1.4)
    led.release("task-create:x")
    assert led.spent() == pytest.approx(1.4)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -0.01])
def test_nonfinite_or_negative_amounts_refused(tmp_path, bad):
    led = Ledger(tmp_path / "l.jsonl", cap_usd=1.0)
    with pytest.raises(ValueError):
        led.reserve("a", "k", bad)
    with pytest.raises(ValueError):
        led.task_snapshot("a", "task_1", bad)


class _Log:
    def __init__(self):
        self.rows = []
        self.run_id = "mock"

    def call(self, rec):
        self.rows.append(("call", rec))

    def note(self, **kv):
        self.rows.append(("note", kv))


def _runner(tmp_path, handler, cap=5.0):
    from mossaudit.tasks import TaskRunner

    led = Ledger(tmp_path / "l.jsonl", cap_usd=cap)
    http = MossHTTP(None, transport=httpx.MockTransport(handler))
    return TaskRunner(http, led, _Log(), "MOCK"), led


def test_ambiguous_create_retries_same_key_then_blocks(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    keys = []

    def handler(req):
        keys.append(req.headers.get("idempotency-key"))
        return httpx.Response(503, json={"error": {"code": "tasks_unavailable"}})

    runner, led = _runner(tmp_path, handler)
    with pytest.raises(tasks.UnresolvedTask):
        runner.create({"goal": "g", "limits": {"max_cost_usd": 0.01, "max_runtime_seconds": 60}})
    assert len(keys) == 3 and len(set(keys)) == 1  # same key on every retry
    assert led.state()["reserved"] > 0  # reservation kept: a task may exist
    with pytest.raises(tasks.UnresolvedTask):  # same runner: blocked
        runner.create({"goal": "again"})
    runner2, _ = _runner(tmp_path, handler)  # a fresh process reading the same ledger: blocked too
    with pytest.raises(tasks.UnresolvedTask):
        runner2.create({"goal": "again"})
    assert len(keys) == 3  # nothing more was sent


def test_late_definitive_refusal_after_ambiguity_keeps_hold(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    replies = iter([httpx.Response(503, json={}),
                    httpx.Response(401, json={"error": {"code": "authentication_error"}})])
    runner, led = _runner(tmp_path, lambda r: next(replies))
    with pytest.raises(tasks.UnresolvedTask):
        runner.create({"goal": "g"})
    assert led.state()["reserved"] > 0


def _terminal_flow(tmp_path, monkeypatch, final_body):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)

    def handler(req):
        if req.method == "POST" and req.url.path == "/v1/tasks":
            return httpx.Response(202, json={"id": "task_5", "status": "queued"})
        return httpx.Response(200, json=final_body)

    runner, led = _runner(tmp_path, handler)
    runner.create({"goal": "g", "limits": {"max_cost_usd": 0.05, "max_runtime_seconds": 120}})
    runner.wait("task_5", poll_s=1, max_wait_s=30)
    return runner, led


def test_terminal_with_cost_swaps_to_traffic_hold(tmp_path, monkeypatch):
    body = {"id": "task_5", "status": "completed", "usage": {"cost": {"total_usd": 0.02}}}
    runner, led = _terminal_flow(tmp_path, monkeypatch, body)
    st = led.state()
    assert list(st["open_reservations"]) == ["traffic:task_5"]
    assert st["exposure"] == pytest.approx(0.02 + 0.05)
    assert not runner.open


def test_terminal_without_cost_keeps_full_hold(tmp_path, monkeypatch):
    from mossaudit.tasks import task_bound

    body = {"id": "task_5", "status": "completed"}
    runner, led = _terminal_flow(tmp_path, monkeypatch, body)
    bound = task_bound({"limits": {"max_cost_usd": 0.05, "max_runtime_seconds": 120}})
    assert led.spent() == pytest.approx(bound)
    assert runner.needs_reconcile == ["task_5"]


def test_corrupt_row_and_duplicate_key_refused(tmp_path):
    p = tmp_path / "l.jsonl"
    led = Ledger(p, cap_usd=1.0)
    led.reserve("a", "k", 0.6)
    with pytest.raises(ValueError):
        led.reserve("a", "k", 0.1)  # same key twice would hide the first hold
    p.write_text(p.read_text() + '{"kind": "charge", "usd": NaN}\n')
    with pytest.raises(ValueError):
        led.reserve("a", "k2", 100)


def test_refused_create_releases_and_open_task_blocks_next(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    replies = iter([httpx.Response(400, json={"error": {"type": "invalid_request_error"}}),
                    httpx.Response(202, json={"id": "task_1", "status": "queued"})])
    runner, led = _runner(tmp_path, lambda r: next(replies))
    runner.create({"goal": ""})
    assert led.spent() == 0
    runner.create({"goal": "g"})
    with pytest.raises(tasks.UnresolvedTask):
        runner.create({"goal": "h"})


def test_wait_deadline_holds_when_every_poll_fails(tmp_path, monkeypatch):
    from mossaudit import tasks

    clock = [0.0]
    monkeypatch.setattr(tasks.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tasks.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.method == "POST" and req.url.path == "/v1/tasks":
            return httpx.Response(202, json={"id": "task_9", "status": "queued"})
        return httpx.Response(500, json={"error": {"type": "api_error"}})

    runner, led = _runner(tmp_path, handler)
    runner.create({"goal": "g"})
    task = runner.wait("task_9", poll_s=10, max_wait_s=60)
    assert task["_trace"][-1].get("gave_up") is True
    assert any(p.endswith("/cancel") for p in calls)  # the safety cancel was sent
    assert "task_9" in runner.open and led.state()["reserved"] > 0  # still unresolved, reserved


@pytest.fixture(autouse=True)
def _intents_in_tmp(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks, "INTENTS_PATH", tmp_path / "intents.jsonl")


def test_usage_tokens_rejects_implausible_counts():
    from mossaudit.prices import usage_tokens

    assert usage_tokens({"input_tokens": 10, "output_tokens": 2}) == (10, 2, 0)
    assert usage_tokens({"input_tokens": True, "output_tokens": 2}) is None
    assert usage_tokens({"input_tokens": -1, "output_tokens": 2}) is None
    assert usage_tokens({"input_tokens": 5, "output_tokens": 2,
                         "input_tokens_details": {"cached_tokens": 6}}) is None
    assert usage_tokens({}) is None


def test_only_pre_execution_refusals_release():
    from mossaudit.prices import definitive_refusal

    err = {"error": {"code": "x"}}
    assert definitive_refusal(400, None, 0, err)
    assert not definitive_refusal(408, None, 0, err)  # timeout: work may have happened
    assert not definitive_refusal(424, None, 0, err)  # failed after a tool call
    assert not definitive_refusal(400, None, 0, None)  # bodyless
    assert not definitive_refusal(400, None, 1, err)  # text was generated


def test_second_runner_blocked_atomically_and_header_normalized(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    seen = []

    def handler(req):
        seen.append(req.headers.get_list("idempotency-key"))
        return httpx.Response(202, json={"id": "task_1", "status": "queued"})

    r1, _led = _runner(tmp_path, handler)
    r1.create({"goal": "g"}, headers={"idempotency-key": "caller-key"})
    assert len(seen[0]) == 1 and seen[0][0] != "caller-key"
    r2, _ = _runner(tmp_path, handler)
    with pytest.raises(tasks.UnresolvedTask):
        r2.create({"goal": "h"})
    assert len(seen) == 1


def test_recover_replays_same_key_and_bytes_with_existing_hold(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    sent = []
    replies = iter([httpx.Response(503, json={})] * 3
                   + [httpx.Response(202, json={"id": "task_7", "status": "queued"},
                                     headers={"Idempotent-Replayed": "true"})])

    def handler(req):
        sent.append((req.headers["idempotency-key"], req.content))
        return next(replies)

    r1, led = _runner(tmp_path, handler)
    with pytest.raises(tasks.UnresolvedTask):
        r1.create({"goal": "g"})
    holds_before = led.open_holds()
    r2, _ = _runner(tmp_path, handler)  # e.g. after a restart
    r2.recover()
    assert len({k for k, _ in sent}) == 1 and len({b for _, b in sent}) == 1
    assert "task_7" in r2.open
    assert led.open_holds() == holds_before  # same hold reused, none added


def test_unpriced_terminal_hold_keeps_task_offset(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    polls = iter([{"id": "task_8", "status": "running", "usage": {"cost": {"total_usd": 0.08}}},
                  {"id": "task_8", "status": "completed"}])

    def handler(req):
        if req.method == "POST":
            return httpx.Response(202, json={"id": "task_8", "status": "queued"})
        return httpx.Response(200, json=next(polls))

    runner, led = _runner(tmp_path, handler)
    body = {"goal": "g", "limits": {"max_cost_usd": 0.05, "max_runtime_seconds": 120}}
    runner.create(body)
    runner.wait("task_8", poll_s=1, max_wait_s=30)
    assert led.spent() == pytest.approx(tasks.task_bound(body))  # not bound + 0.08


def test_duplicate_reserve_rows_on_replay_refused(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text('{"kind": "reserve", "key": "k", "usd": 0.6}\n'
                 '{"kind": "reserve", "key": "k", "usd": 0.1}\n')
    with pytest.raises(ValueError):
        Ledger(p, cap_usd=1.0).spent()


def test_refused_replay_after_restart_keeps_hold(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    replies = iter([httpx.Response(503, json={})] * 3
                   + [httpx.Response(401, json={"error": {"type": "authentication_error"}})])
    r1, led = _runner(tmp_path, lambda r: next(replies))
    with pytest.raises(tasks.UnresolvedTask):
        r1.create({"goal": "g"})
    r2, _ = _runner(tmp_path, lambda r: next(replies))
    report = r2.recover()
    assert report["kept"] and not report["recovered"]
    assert led.open_holds("task-create:")  # a 401 on replay says nothing about the first attempt


def test_expired_intent_is_not_replayed(tmp_path, monkeypatch):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    sent = []

    def handler(req):
        sent.append(1)
        return httpx.Response(503, json={})

    r1, _led = _runner(tmp_path, handler)
    with pytest.raises(tasks.UnresolvedTask):
        r1.create({"goal": "g"})
    n = len(sent)
    real = tasks.time.time
    monkeypatch.setattr(tasks.time, "time", lambda: real() + 25 * 3600)
    report = r1.recover()
    assert len(sent) == n and report["manual"][0]["why"].startswith("idempotency key older")


def test_poll_for_another_task_is_not_accepted(tmp_path, monkeypatch):
    from mossaudit import tasks

    clock = [0.0]
    monkeypatch.setattr(tasks.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tasks.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    def handler(req):
        if req.method == "POST" and req.url.path == "/v1/tasks":
            return httpx.Response(202, json={"id": "task_A", "status": "queued"})
        return httpx.Response(200, json={"id": "task_B", "status": "completed"})

    runner, _led = _runner(tmp_path, handler)
    runner.create({"goal": "g"})
    task = runner.wait("task_A", poll_s=10, max_wait_s=30)
    assert task.get("id") != "task_B" and "task_A" in runner.open


def test_released_key_cannot_be_reserved_again(tmp_path):
    led = Ledger(tmp_path / "l.jsonl", cap_usd=5.0)
    led.reserve("C", "k1", 0.1)
    led.release("k1")
    with pytest.raises(ValueError):
        led.reserve("C", "k1", 0.1)


def _replay_runner(tmp_path, monkeypatch, replies):
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)
    it = iter(replies)
    runner, led = _runner(tmp_path, lambda r: next(it))
    body = {"goal": "g", "limits": {"max_cost_usd": 0.01, "max_runtime_seconds": 60}}
    runner.create(body, idempotency_key="k-idem")
    return runner, led, body


def test_replay_same_id_releases_extra_hold_keeps_original(tmp_path, monkeypatch):
    import json as _json

    first = httpx.Response(202, json={"id": "task_1", "status": "queued"})
    again = httpx.Response(202, json={"id": "task_1", "status": "queued"},
                           headers={"Idempotent-Replayed": "true"})
    runner, led, body = _replay_runner(tmp_path, monkeypatch, [first, again])
    original = runner.open["task_1"]
    runner.replay(_json.dumps(body).encode(), "k-idem", "task_1", 0.07, {})
    assert runner.open["task_1"] == original
    assert list(led.open_holds("task-create:")) == [original]


def test_replay_new_id_stays_held_and_ambiguous_keeps_hold(tmp_path, monkeypatch):
    import json as _json

    from mossaudit import tasks

    first = httpx.Response(202, json={"id": "task_1", "status": "queued"})
    new = httpx.Response(202, json={"id": "task_2", "status": "queued"})
    runner, led, body = _replay_runner(tmp_path, monkeypatch,
                                       [first, new, httpx.Response(503, json={})])
    payload = _json.dumps(body).encode()
    runner.replay(payload, "k-idem", "task_1", 0.07, {})
    assert "task_2" in runner.open and runner.open["task_2"] in led.open_holds()
    with pytest.raises(tasks.UnresolvedTask):
        runner.replay(payload, "k-idem", "task_1", 0.07, {})
    assert len(led.open_holds("task-create:")) == 3  # original, task_2's, ambiguous one


def test_replay_definitive_refusal_releases(tmp_path, monkeypatch):
    first = httpx.Response(202, json={"id": "task_1", "status": "queued"})
    refused = httpx.Response(422, json={"error": {"code": "idempotency_key_reused"}})
    runner, led, _ = _replay_runner(tmp_path, monkeypatch, [first, refused])
    runner.replay(b'{"goal": "other"}', "k-idem", "task_1", 0.07, {})
    assert len(led.open_holds("task-create:")) == 1


def test_unpriced_settle_with_unrounded_bound_frees_create_gate(tmp_path, monkeypatch):
    """A $0.01 / 60 s bound has more than 8 decimals; settling must still re-key the hold."""
    from mossaudit import tasks

    monkeypatch.setattr(tasks.time, "sleep", lambda s: None)

    def handler(req):
        if req.method == "POST":
            return httpx.Response(202, json={"id": "task_9", "status": "queued"})
        return httpx.Response(200, json={"id": "task_9", "status": "completed"})

    runner, led = _runner(tmp_path, handler)
    body = {"goal": "g", "limits": {"max_cost_usd": 0.01, "max_runtime_seconds": 60}}
    runner.create(body)
    runner.wait("task_9", poll_s=1, max_wait_s=30)
    holds = led.open_holds()
    assert list(holds) == ["unpriced:task_9"]  # no task-create: hold left to block the next create
    assert not runner.open
    assert runner.needs_reconcile == ["task_9"]
