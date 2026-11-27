# Tasks API: findings

**Environment.** Same client as the Inference findings (Hetzner VPS in Nuremberg, Python 3.12.14,
httpx 0.28.1, moss SDK 1.26.0).

**Runs.** All on 2026-11-25 UTC, at 16:42Z (TSK-01) and between 18:00Z and 18:09Z (campaign). Tasks
were run one at a time on `environment.type: browser`, with `effort: low` and
`limits.max_cost_usd: 0.05` unless stated otherwise.

**Fixtures.** Every page is on books.toscrape.com, a public sandbox built for scraping practice. We
compute ground truth ourselves from the page bytes and keep the SHA-256 of each fetched page in the
run metadata.

**Files.**
- Processed: `results/processed/tsk01_smoke.json`, `tsk_campaign_20261125T180020Z.json`,
  `tsk_campaign_20261125T180753Z.json`.
- Logs (redacted): `results/logs/tsk01_smoke_*`, `results/logs/tsk_campaign_*`.

## What was tested and how it went

| Area | Cases | Result |
|---|---|---|
| Accuracy ladder (TSK-02) | one fact; one fact among 10 distractors (11 titles); three facts; 2-hop navigation from the home page; a count across 2 pages | 5/5 correct (fast); 2/2 correct (balanced, on L1 and L3) |
| Schema vs truth (TSK-03') | an ISBN the page does not show, with a nullable schema | `{"isbn": null, "found": false}`. Moss's `verification.passed` agreed with our local check on every schema case |
| Failure input (TSK-09) | `start_url` returns 404 | `{"upc": null, "found": false}`, status `completed` |
| Reproducibility (TSK-10) | L1 three times | 3/3 correct; cost $0.0047–0.0071, 2–3 steps, 3–7 model calls |
| Cost cap (TSK-04) | $0.01 cap on a 32-book goal | `failed` / `budget_exceeded` at $0.010345 total ($0.010012 model) after 46.5 s |
| Runtime cap (TSK-05) | 60 s cap, 32 books | completed in 50.3 s with 32/32 title + UPC exact, scored offline by `scripts/check_crawl.py` (cap not reached) |
| Runtime cap (TSK-05) | 60 s cap, 4 categories | `failed` / `time_limit` "Stopped after 60 s" at 61.3 s |
| Cancel (TSK-06') | once `running` is seen; on a finished task | running: 200 + `cancel_requested: true`, `cancelled` at the first poll 3.1 s later, 0 steps. Finished: 200, unchanged |
| Idempotency (TSK-07') | replay while running and after finish; same key with another body; concurrent duplicate pair | as documented: same id with `Idempotent-Replayed: true`, 422 `idempotency_key_reused`, one task for the pair |
| Files (TSK-12) | listed while running; listed and downloaded after the end | 409 `task_not_finished` while running; the first listed file downloaded with the listed size on all 14 attempted downloads (X1 had no files; I1 was not checked) |
| Validation (TSK-08) | 24 cases: invalid requests, undocumented edge cases (one of them, a fractional runtime, was accepted), a known-good control and a 20,000-char goal | 24 cases: 18 PASS (including the control and the 20,000-char goal), 6 undocumented cases observed, 0 mismatch; messages name the rule broken |
| Billing (TSK-14) | `usage.cost` arithmetic; portal | portal matches the API for TSK-01 (see TSK-A4) |

Costs are the API-reported totals before traffic, which is still `null`. On `moss-fast`, a short
task cost $0.0047–0.0071. On the two matched cases, `moss-balanced` cost 10.0 times (L1) and 8.3
times (L3) as much, with the same accuracy on these easy pages.

---

## TSK-A1 · The task object has fields that the API reference does not document

| Field | Value |
|---|---|
| Product | Tasks API |
| Classification | Doc defect |
| Severity | Low |
| Confidence | High |

**Observed.**
- Every task body (create, get, cancel) contains `title`, `trace_available`, `video_available`,
  `video_state`, `live_url`, `zero_data_retention`, `content_expires_at`, `content_deleted_at` and
  `webhook`.
- None of these appear in the Tasks API reference or guide (docs snapshot of 2026-11-25). `byok` is
  documented.

**Evidence.** The X1 cancel reply in `tsk_campaign_20261125T180020Z.json`, task
`task_7ed724278e8a41699ed7d859c48d99e1`.

**Impact.**
- `live_url` (a live view?) and `webhook` (notifications instead of polling?) look useful, but they
  cannot be relied on. Both were `null` throughout this run, so their behaviour was not tested.
- The Python SDK already explains `trace_available` and `live_url`; this gap is in the REST
  reference.
- `zero_data_retention` and `content_deleted_at` are compliance-relevant, and their meaning is
  undocumented.

**Proposed solution.** Document every field. Mark experimental ones as such.

**Regression test.** A schema test that diffs a live task object against the documented field list.

---

## TSK-A2 · One API-reported browser charge rounds 61 s down to one minute

| Field | Value |
|---|---|
| Product | Tasks billing |
| Classification | Unexpected behaviour / doc defect |
| Severity | Low (the error is in the customer's favour) |
| Confidence | Medium (one task over the boundary) |

**Expected.** The pricing doc says "Cloud browser time: $0.02 / hour, billed per minute, rounded up".
61 s should therefore bill 2 minutes, which is $0.000667.

**Observed.** T2 (`time_limit`) reports `browser_seconds: 61`, `browser_minutes: 1.01` and
`browser_usd: 0.000333`, which is one minute.

The rounding up does hold below one minute. Sessions of 2, 13, 14 and 49 s are each billed one
minute.

**Evidence.** `tsk_campaign_20261125T180753Z.json`

**Suspected root cause.** Rounding to the nearest minute, or a grace tolerance, instead of a
ceiling.

**Proposed solution.** Align the code and the doc. If a tolerance is intended, document it.

**Limitations.** This is one sample of a charge reported by the API. The final bill and the actual
rounding algorithm are not verified. The amounts are far below any practical impact; the point is
that the reported charge does not follow the documented rule.

---

## TSK-A3 · Failed tasks returned `result: null`, while a CSV file was still downloadable

| Field | Value |
|---|---|
| Product | Tasks API |
| Classification | Improvement |
| Severity | Medium potential impact (scoped to these two failures) |
| Confidence | High |

**Observed.**
Both failures in this run had `result: null`, and each still exposed a downloadable CSV:
- **T2** (`time_limit`): a 12,762-byte `text/csv` file.
- **C1** (`budget_exceeded`, after 6 steps): a 1,686-byte `text/csv` file.

The logs confirm that the files were delivered and give their sizes. They did not keep the file
contents, so whether the CSVs are complete is not verified.

The documented behaviour is that a task which reaches a limit "fails". It says nothing about
partial output.

**Impact.**
- A caller who reads only `result` sees nothing for the money spent up to the limit, and nothing in
  the task object says that partial work may exist in the files.

**Proposed solution.**
- On `time_limit` or `budget_exceeded`, fill `result.output_text` with the agent's last state, or
  add a `partial: true` flag together with the file paths.
- Document that files survive failure.

**Regression test.** Run a capped long task, then assert that `result` or a partial flag is present
whenever files were written.

---

## TSK-A4 · The portal matches the API for TSK-01, and its recording frame looks blank

| Field | Value |
|---|---|
| Product | Portal (task page) |
| Classification | Positive check + unconfirmed hypothesis |
| Severity | — / Low |
| Confidence | High for the numbers; low for the recording |

**Observed.**

| Portal task page | API |
|---|---|
| Spend $0.005 of $0.05 | $0.0053 total ($0.004967 model): both round to $0.005, so the page cannot tell which one it shows |
| Tokens 12,511 | 11,994 + 517 |
| Steps 2 | 2 |
| Time 15 s of 2 min | 15.2 s |
| "Matches the schema" | verification passed |
| Shows the result | same result |

The paused recording frame at 0:09 is an all-white page. The user took screenshots of the portal
at 17:59Z. They are kept private.

**Limitations.**
- A blank frame can be a real moment of the session, for example while the page loads. It needs a
  second look before it becomes a finding.
- This is a task-page check only. The Usage page and traffic billing have not been reconciled yet: traffic stays `null` until a few
  hours after a task ends, as documented.

---

## TSK-A5 · `cancel_requested` is back to false once a task is cancelled

| Field | Value |
|---|---|
| Product | Tasks API |
| Classification | Unexpected behaviour / doc ambiguity |
| Severity | Low |
| Confidence | High (4 of 4 cancelled tasks) |

**Description.** The reference describes `cancel_requested` as "Whether a cancel was requested."

**Observed.** On 4 of 4 tasks (X1, plus the 3 accepted TSK-08 cases):
- the cancel reply and the polls show `status: running` with `cancel_requested: true`;
- the terminal object shows `status: cancelled` with `cancel_requested: false`.

**Evidence.** `tsk08_validation_20261125T180952Z_5fcc85.jsonl`, for example task
`task_60e01ebb5d924ff2a0154da78d51ce0c`, plus X1 in `tsk_campaign_20261125T180020Z.json`.

**Impact.** The SDK describes the field as a pending-cancel flag, which fits what we observed. The
terminal object still says `cancelled`. What is lost is who or what initiated the cancel, which
matters for audit trails.

**Proposed solution.** Keep `cancel_requested: true` after the end, or add `cancelled_by`.

**Regression test.** Cancel a running task, wait for the terminal state and assert
`cancel_requested` is true.

---

## TSK-A6 · Validation messages reveal options the docs do not list

| Field | Value |
|---|---|
| Product | Tasks API + docs |
| Classification | Doc defect |
| Severity | Low |
| Confidence | High |

**Observed.** Error messages, which are otherwise excellent, advertise things the docs do not list.
The run never tested whether these options are accepted:

- `environment` accepts `proxy_country`.
- `start_url` may be "a host such as example.com". The docs say an http or https URL.
- Two model tiers exist that the docs do not list: `moss-balanced-handoff` and `moss-fast-beta`.

The model tiers appear in the `model_not_found` list.

**Evidence.** `tsk08_validation_20261125T180952Z_5fcc85.jsonl`, cases `env_unknown_field`,
`start_url_ftp` and `model_unknown`.

Also observed: `max_runtime_seconds: 60.5` was accepted and echoed back as `60`. Whether the
normalized value is enforced was not tested.

**Proposed solution.** Document these options, or hide them from public error messages if they are
internal.

---

## Documented limitations, confirmed as written (no defect)

- **The cost cap applies to model calls only, and calls in flight still bill.**
  - L1 on `moss-balanced` with a $0.05 cap ended `completed` with the correct answer. Its reported
    model cost of $0.052642 was 5.28 % over the cap, and its total of $0.052975 was 5.95 % over.
  - C1 stopped at $0.010012 model cost against a $0.01 cap.
  - Developers should expect small overshoots.
- **The runtime cap stopped T2 at 61.28 s against 60 s.** A single run cannot establish a general bound.
- **The traffic cost** (`traffic_usd`, `browser_traffic_mb`) is `null` on every task right after the
  end, as the docs warn.
- **Cancelling a running task** returns 200 `running` with `cancel_requested: true`, and the task
  reaches `cancelled` about 3 s later. A browser session that lasted 2 s is billed one minute.
- **Cancelling a queued task was not tested.** The tasks started about 11–21 ms after creation
  (server timestamps). In all four cancel attempts, the cancel found the task already `running`, and
  each cancelled task billed one browser minute ($0.000333).
