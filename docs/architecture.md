# Architecture

The harness is a small Python package (`src/mossaudit/`) plus one script per experiment. Each
experiment follows the same path:

```
benchmark script ──► MossHTTP.call() ──► Moss API
      │                    │
      │ reserve()          │ CallRecord (timings, headers, usage, frames)
      ▼                    ▼
  Ledger (results/ledger.jsonl)   RunLog (results/raw/<run>.jsonl)
                                        │
                     scripts/redact.py  ▼
                                  results/logs/<run>.jsonl   (published)
                                        │
           scripts/analyze_latency.py   ▼
                                  results/processed/*.json   (numbers in README/findings)
```

## Package `src/mossaudit/`

| Module | Role |
|---|---|
| `config.py` | Reads `MOSS_PROJECT_ID` / `MOSS_PROJECT_KEY` from `.env`; holds the base URL, the budget cap (`MOSS_BUDGET_CAP_USD`, default $8) and output paths. Reruns write to `results/reruns/` unless `MOSS_AUDIT_PUBLISH=1`. |
| `client.py` | Raw `httpx` client. One HTTP exchange per call, with a clock started right before `send`. It parses Server-Sent Events by hand, so it knows when the headers, the first byte, the first event and the first text delta arrive (TTFT). |
| `ledger.py` | Append-only spend ledger. Every paid send first reserves a worst-case amount; the check and the write run under a file lock. Exposure above the cap refuses the send. |
| `tasks.py` | `TaskRunner` for the Tasks API: create with an `Idempotency-Key`, poll until terminal, cancel, and book the final cost once. An ambiguous create is recovered by resending the same key and body, which Moss documents as returning the original task. The deliberate duplicate sends of the idempotency test (I1) are never resent; they are reconciled by hand. |
| `runlog.py` | Writes one JSON Lines file per run: run metadata on the first line, then one line per call. |
| `prices.py` | Documented prices, used for pre-run estimates and for checking reported costs. |
| `stats.py` | Percentiles (linear interpolation, like numpy) and Wilson score intervals. |
| `jsonsafe.py` | Turns SDK reply objects into plain JSON so they can be logged like raw HTTP replies. |

## Experiments

| Script | What it measures | Card |
|---|---|---|
| `benchmarks/inference/smoke.py` | The six client paths work (raw HTTP, OpenAI SDK, Moss SDK) | — |
| `benchmarks/inference/contract.py` | 50 error and edge cases against the documented contract | INF-A4–A6 |
| `benchmarks/inference/latency.py` | TTFT, plain vs grounded, vendor route, prompt size and prefix cache; paired and in random order | INF-A1 |
| `benchmarks/inference/grounding.py` | Answers on 30 invented facts + 10 absent ones, search on vs off | — |
| `benchmarks/inference/cache.py` | Reply cache behaviour | INF-A2, A3 |
| `benchmarks/inference/tools.py` | Tool calling, two rounds, raw HTTP and SDK | — |
| `benchmarks/inference/streaming.py` | Event order and shape of streamed replies | — |
| `benchmarks/tasks/smoke.py` | One task end to end (TSK-01) | TSK-A4 |
| `benchmarks/tasks/campaign.py` | Accuracy ladder, caps, cancel, idempotency, files | TSK-A1–A3, A5 |
| `benchmarks/tasks/validation.py` | 24 request-validation cases | TSK-A6 |

Offline scripts (no credits): `scripts/redact.py` (publishable logs + leak check),
`scripts/analyze_latency.py` (latency summaries), `scripts/check_crawl.py` (32-book crawl score),
`scripts/reconcile.py` (close ledger holds after a portal check), `scripts/gen_api_docs.py`
(this folder's reference). `scripts/make_index.py` creates the grounding index and spends a little.

## Design choices

- **Raw HTTP for timing.** SDKs buffer and retry, which hides when the first token really
  arrives. The SDKs are still tested for correctness in `smoke.py` and `tools.py`.
- **Ground truth we control.** Invented facts for grounding; for tasks, the expected answer is
  extracted from the fixture page at run time and the page hash is logged.
- **Reserve before spending.** Prices per call are tiny, but a task can run until its cap. Every
  paid send first reserves an estimated worst case, so the local estimate of exposure stays under
  the cap even if the process dies mid-run. These are estimates, not a guarantee on the final bill.
- **Logs first, numbers second.** Summaries are recomputed from the logs by the scripts; the few
  figures that come from a manual check (for example the portal comparison) say so in their card.

## Tests

`pytest -q` runs the offline tests (no network, no credits): ledger arithmetic and locking, the SSE
parser on crafted streams, and the task runner against a mocked API.
