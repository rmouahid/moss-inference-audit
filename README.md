# Moss Inference: hands-on audit

This is a hands-on technical and functional audit of [Moss Inference](https://docs.moss.dev). It
covers the Inference API (latency, retrieval grounding, caches, tools, streaming, error contract)
and the Tasks API (browser agents: accuracy, limits, cancel, idempotency, files, validation).

Every number below comes from a real call made on 2026-11-25 UTC. The redacted call logs are in
`results/logs/`, and the scripts in this repository recompute the summaries from them. Three pieces
of evidence stay private: the spend ledger, a TCP connect probe and the portal screenshots. They
are labelled as such where they are used.

- **Client:** a VPS in Nuremberg (DE).
- **Account:** the Developer plan with the $10 signup credit.
- **Spend:** $0.48 booked in the private local ledger, including conservative allowances, plus
  $1.05 still held for task traffic charges that Moss had not posted yet. Neither figure is a final
  bill.

## Results at a glance

### Inference API (`moss/fastlane-1` unless stated)

| What | Result |
|---|---|
| Plain TTFT (time to first text delta, streamed, warm connection) | p50 160–185 ms in four runs. Under 200 ms in 20/20, 40/40, 44/50 and 34/50 calls |
| Grounded TTFT (index named, search on) | **0/70 under 200 ms** in two windows (p50 237.7 ms and 217.8 ms, [INF-A1](findings/inference.md)). The median paired TTFT difference over plain is about +51 ms, while the server reports about 10 ms of search |
| Grounding quality (30 answerable + 10 absent invented facts) | Grounded: 29/30 correct and 10/10 correct abstentions. Search off: 4 confident wrong numbers |
| Prefix cache | At about 27.8k input tokens, TTFT is about 0.34 s with a repeated prefix and about 2.0 s with a fresh one |
| Reply cache | Works, with hits in about 110 ms. Hits carry no flag ([INF-A3](findings/inference.md)) and report fewer output tokens than the original ([INF-A2](findings/inference.md)) |
| Vendor route | `anthropic/claude-haiku-5.5` through Moss: TTFT p50 480.7 ms, against 162.9 ms for fastlane in the same run |
| Tool calling | 20/20 correct first rounds and 14/14 second rounds that used the tool result. The Moss SDK loop worked 2/2 |
| Streaming grammar | 6 protocol checks matched the docs. One client-side early close was observed |
| Error contract (50 cases) | 36 matched the docs, 13 undocumented cases were observed and 1 was inconclusive. Small gaps: [INF-A4](findings/inference.md) to [INF-A6](findings/inference.md) |

### Tasks API (browser agents, `moss-fast`, effort `low`)

| What | Result |
|---|---|
| Accuracy on fixture pages with our own ground truth | 11/11 scored tasks correct: one fact (L1, 3 attempts), a fact among distractors, three facts, 2-hop navigation, a count across pages, an absent field (null) and a 404 page (null). L1 and the three-fact case also ran once each on `moss-balanced` |
| Long goal (32 books, title + UPC) | 32/32 exact in 50 s for $0.0126 |
| Cost | $0.005–0.007 per short task on `moss-fast`. `moss-balanced` cost 8–10 times more for the same answers |
| Limits | `budget_exceeded` with a $0.01 model-cost cap, at $0.010012 model / $0.010345 total. `time_limit` at 61.3 s with a 60 s cap. On balanced, a $0.05 cap ended at $0.053 (documented: in-flight calls still bill) |
| Cancel / idempotency / files / validation | Idempotency works as documented, including a concurrent pair. Validation: 24 cases, 0 mismatches. Cancel and files behave as documented. Small gaps: [TSK-A1](findings/tasks.md) to [TSK-A6](findings/tasks.md) |

## Findings

- [`findings/inference.md`](findings/inference.md): INF-A1 to INF-A6, plus the positive results.
- [`findings/tasks.md`](findings/tasks.md): TSK-A1 to TSK-A6, plus the limitations confirmed as
  documented.

Each card has the same fields: classification, severity, confidence, repro, expected, observed,
evidence (log file and request ids), impact, suspected cause, proposed fix, regression test and
limitations.

## Method

- **Logged evidence.** Every figure is computed from a logged call. The raw logs are kept private.
  `scripts/redact.py` publishes copies with the project id and key, any bearer token and the
  project's index inventory removed. It checks known credentials and patterns only.
- **Own ground truth.**
  - Inference grounding uses an index of 30 invented facts
    (`benchmarks/inference/data/fictional_facts.json`), so a model cannot already know them.
  - Tasks fixtures are pages of books.toscrape.com, a sandbox built for scraping practice. Their
    truth is extracted by regex at run time, and the SHA-256 of each page is recorded.
- **Fair latency comparisons.**
  - Arms are interleaved in a seeded random order and paired per unit.
  - Warm-up calls are logged apart.
  - The reply cache is off unless it is the subject of the test.
  - Requests are paced below the documented rate limit.
  - TTFT is timed on raw socket chunks.
- **Budget safety.**
  - Every paid send reserves an estimated worst case in a file-locked ledger before it is sent,
    with a cap of $8. The task estimates are launch allowances, not proven bounds.
  - Tasks run one at a time, except the deliberate concurrent pair in the idempotency case (I1). An
    ambiguous create keeps its reservation until it is recovered with the same Idempotency-Key.
    Ambiguous I1 sends are never resent; they are reconciled by hand.
- **Tooling disclosure.** AI assistants helped build and review the harness.
- **Out of scope.** Security testing, load tests, rate-limit evasion, and internal or `file://`
  URLs in tasks.

## Reproduce

Every command except `pytest` and the two scripts on the last line spends credits. The $8 ledger
cap works on estimated worst-case exposure; it is not a bill.

```bash
uv venv && source .venv/bin/activate && uv pip install -e '.[sdk,dev]'
cp .env.example .env   # fill MOSS_PROJECT_ID, MOSS_PROJECT_KEY, MOSS_AUDIT_CLIENT
pytest -q              # 40 offline tests: ledger, SSE parser, task runner (mocked); no credits
python benchmarks/inference/smoke.py
python benchmarks/inference/contract.py
python scripts/make_index.py
python benchmarks/inference/grounding.py
python benchmarks/inference/latency.py --models moss/fastlane-1 --index audit-facts --n 50 --warmup 10
python benchmarks/inference/cache.py
python benchmarks/inference/tools.py
python benchmarks/inference/streaming.py
python benchmarks/tasks/smoke.py
# checkpoint: compare the TSK-01 cost with the portal task page before running more tasks
python benchmarks/tasks/campaign.py   # case I1 deliberately sends a concurrent duplicate create;
                                      # an ambiguous I1 send needs manual reconciliation
python benchmarks/tasks/validation.py
python scripts/redact.py && python scripts/analyze_latency.py
```

## Layout

```
src/mossaudit/        client (frame-accurate SSE timing), ledger, task runner, prices, stats
benchmarks/inference/ smoke, contract, latency, grounding, cache, tools, streaming
benchmarks/tasks/     smoke, campaign (ladder, limits, cancel, idempotency, files), validation
scripts/              make_index, redact, analyze_latency, reconcile
results/processed/    per-campaign JSON summaries
results/logs/         redacted raw call logs (JSON Lines)
findings/             anomaly cards
```

## Limitations

- **One day, one client location, one account.** Latency results are per window and are not
  general.
- **The fixtures are small and easy.** They test correctness and limits, not hard web tasks.
- **Billing.** Task traffic charges were still `null` when the report was written. The portal check
  covers one task page and agrees at the precision the portal displays.
- **Fixtures.** Accepted validation probes used `example.com` as the start page.
