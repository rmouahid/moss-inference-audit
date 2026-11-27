# Inference API: findings

Environment for every card unless stated: client on a Hetzner VPS in Nuremberg (DE), Python 3.12.14,
httpx 0.28.1, moss SDK 1.26.0, openai SDK 3.59.0, endpoint `https://inference.moss.dev/v1`
(resolves to 136.69.65.86; TCP connect from the client 3.6–3.9 ms), model `moss/fastlane-1`,
`X-Moss-Cache: off` unless the card is about the cache, calls sequential and paced
(1 s between starts). All runs on 2026-11-25 UTC. Redacted logs are in `results/logs/` (file named per
card); a reviewer can recompute every number from them.

Classification: confirmed bug / documented limitation / unexpected behaviour / doc defect /
improvement / unconfirmed hypothesis.

---

## INF-A1 · Grounded TTFT stays above 200 ms

| Field | Value |
|---|---|
| Product | Inference API (Responses, streaming, retrieval) |
| Classification | Unexpected behaviour (against a headline claim) |
| Severity | Medium |
| Confidence | Medium-high: replicated in two windows 80 min apart (0/50 and 0/20 grounded calls under 200 ms); one client location, one fixture index |
| Environment | As above; index `audit-facts` (30 short invented facts, `moss-minilm`) |

**Description.** The overview headline is "Sub-200ms time to first token, with retrieval built in."
With the index named and the search on, no measured call reached the first text delta in under
200 ms.

**Repro.** `python scripts/make_index.py`, then
`python benchmarks/inference/latency.py --models moss/fastlane-1 --index audit-facts --n 50 --warmup 10`.
The plain arm and the grounded arm both name the index. The plain arm adds `X-Moss-Retrieve: off`.
Questions come from the fixture, and pairs run in random order.

**Expected.** A typical grounded call has a first token under 200 ms.

**Observed.** These are client time to first text delta. Window 2 had 50 measured pairs (16:56Z,
10 warm-up pairs kept apart). Window 3 had 20 measured pairs (18:17Z, 5 warm-up pairs kept apart):

| arm | p50 | p90 | max | < 200 ms |
|---|---|---|---|---|
| plain, window 2 | 185.3 ms | 303.3 ms | 2,978.6 ms | 34/50 (one of the 34 later hit the output cap: 33/50 if it counts as a miss) |
| grounded, window 2 | 237.7 ms | 924.2 ms | 3,104.3 ms | 0/50 (nominal Wilson 95 %: 0–7 %, calls are not independent given the spike cluster) |
| plain, window 3 | 160.4 ms | 181.4 ms | 182.7 ms | 20/20 |
| grounded, window 3 | 217.8 ms | 231.3 ms | 237.8 ms | 0/20 (fastest 202.7 ms; Wilson 0–16 %) |

- The median paired difference (grounded − plain) is +52.0 ms in window 2 and +50.6 ms in window 3.
- The median search time reported by the server is 10.5 ms in window 2 and 10.2 ms in window 3.
- The median input is 477 (window 2) and 478 (window 3) tokens grounded, against 136 plain.
- The expected fact appeared in 50/50 (window 2) and 20/20 (window 3) grounded replies.
- Eight calls from both arms took 0.7–3.1 s between 16:57:59Z and 16:58:28Z. Nearly all of that wait
  came before the response headers reached the client. The headers cannot tell where on the path the
  wait happened.
- The first grounded search ever observed on this index (the INF-08 pilot, right after the index was
  created) reported `X-Moss-Retrieve-Ms` 1,073.6 ms, against a 10.3 ms median afterwards. The cause
  is not established.
- The 3.7 ms TCP connect figure comes from a separate connection probe, kept in private notes. It
  is not part of these measurements.

**Evidence.**
- `results/logs/inf02_latency_w2_20261125T165632Z_412f54.jsonl` and
  `results/logs/inf02_latency_w3_20261125T181659Z_cb9684.jsonl`
- `results/processed/latency_summary.json`
- Plain baselines for context:
  - window 1 (generic questions): p50 176.6 ms, 44/50 under 200 ms, in `inf02_latency_w1_20261125T164638Z_02a80a.jsonl`;
  - INF-03: p50 162.9 ms, 40/40 under 200 ms, in `inf03_vendor_20261125T170449Z_8b4492.jsonl`.

**Impact.** The "sub-200 ms, grounded" headline was not reproduced on this fixture, from this
client, in these two windows. A voice-agent builder who sized a turn budget on it would have missed
it on every grounded call measured here. The claim did hold for plain calls in some windows.

**Suspected root cause** (hypothesis, not established). These logs cannot split the extra time
between prefill of the longer prompt, the retrieval pipeline outside the reported 10 ms, and
queueing. The spike cluster points at a transient wait before response delivery.

**Proposed solution.**
- Publish the conditions behind the claim: region, prompt size, index size, and percentile.
- Report the full grounded overhead in a header, not only the search time (for example,
  `X-Moss-Queue-Ms` / `X-Moss-Prefill-Ms`).

**Regression test.** Rerun `latency.py --index` in several windows and track grounded p50 and the
share of calls under 200 ms.

**Limitations.**
- Two windows on one day, one client location, a tiny index, and one question set.
- The overview scopes the claim to "supported workloads".
- TTFT is measured client-side and includes the network path.

---

## INF-A2 · Reply-cache hits report fewer output tokens than the original

| Field | Value |
|---|---|
| Product | Inference API, reply cache |
| Classification | Unexpected behaviour (usage-reporting ambiguity: the docs do not specify output tokens on a hit) |
| Severity | Low |
| Confidence | High for the reporting (12 of 12 hits in 8 cohorts); unknown for billing |

**Description.** A reply-cache hit returns the same text as the original reply. Its `usage` reports
0 input tokens, as documented, but always reports fewer output tokens than the original call did
for that identical text.

**Repro.** `python benchmarks/inference/cache.py`

**Expected.** Either the same output-token count as the original, or 0 output tokens, since a hit
is free.

**Observed.** Output tokens, original → hits:

| cohort | original | hits |
|---|---|---|
| A | 2 | 1 (A2 to A5) |
| C | 7 | 5 |
| D | 2 | 1 |
| E | 10 | 8 (E2 and E3) |
| E (stream) | 2 | 1 |
| F | 3 | 1 |
| F (Chat) | 2 | 1 |
| G | 6 | 4 |

The text is identical in every case. Hits answer in 101–131 ms, against 168–255 ms for misses.

**Evidence.** `results/logs/inf05_cache_20261125T170202Z_3858dd.jsonl`

**Impact.**
- A client that estimates cost from `usage` (as cost dashboards do) assigns a cost to hits, although
  the docs call hits free. Actual Moss billing of hits is unverified.
- Token analytics disagree with the reply that was actually served.
- Whether Moss itself bills these tokens is not shown by this run. The amounts are below the
  portal's precision.

**Suspected root cause.** The output count of a hit seems to be recomputed (for example by
re-tokenizing the cached text without the end token) instead of being copied or zeroed.

**Proposed solution.** Report hits with `output_tokens: 0`, or with the original count plus an
explicit flag (see INF-A3). Then state in the pricing docs what a hit bills.

**Regression test.** Send the same request twice with the cache on. Assert that the second reply
reports `input_tokens == 0` and an output count that matches the documented rule.

**Limitations.** The billing effect is not measured, and fastlane is the only model tested.

---

## INF-A3 · Nothing marks a reply as served from the cache

| Field | Value |
|---|---|
| Product | Inference API, reply cache |
| Classification | Improvement |
| Severity | Low |
| Confidence | High |

**Description and observed.** In the 26 captured calls, a hit and a miss returned no cache-specific
header. Both carried `content-type`, `server`, `via`, `x-moss-box`, `x-moss-request-id`,
`x-moss-retrieve-ms` and the six `x-ratelimit-*` headers, and neither carried `Age`, `X-Cache` or a
Moss cache header. The only signal of a hit is `usage.input_tokens == 0`.

**Evidence.** `inf05_cache_20261125T170202Z_3858dd.jsonl`. The client keeps all `x-moss-*`, `age`,
`x-cache*` and `cache-control` headers.

**Impact.** A hit cannot be told apart from a very short prompt without parsing usage. Latency
dashboards then mix hits (about 110 ms) with model calls.

**Proposed solution.** Add `X-Moss-Cache: hit|miss|bypass` to every response.

**Regression test.** Assert the header on a known hit, a known miss and a cache-off call.

---

## INF-A4 · An invalid `X-Moss-Retrieve` value is accepted and a search runs

| Field | Value |
|---|---|
| Product | Inference API, retrieval headers |
| Classification | Improvement (input validation) |
| Severity | Low |
| Confidence | High (one call per value) |

**Repro.** Name the index (`X-Moss-Indexes: audit-facts`) and send `X-Moss-Retrieve` with each of
the values `off`, `OFF`, `maybe` and `on`.

**Expected.** `maybe` returns 400, like other invalid parameters (Moss rejects unknown body fields
with `unsupported_parameter`).

**Observed.**

| Value | Search ran | Input tokens |
|---|---|---|
| `off` | no (retrieve-ms 0.0) | 101 |
| `OFF` | no (retrieve-ms 0.0) | 101 |
| `maybe` | yes (retrieve-ms 10.0) | 437 |
| `on` | yes (retrieve-ms 9.6) | 437 |

In this single call per value, `maybe` behaved like `on`: the search ran and added 336 input tokens.
By the documented price, that is a $0.0005 retrieval, but this run did not verify a posted fee. Other
typos (such as `of`) and the other toggle headers were not tested.

**Evidence.**
- `results/logs/inf10_retrieve_values_20261125T173418Z_2e5a39.jsonl`
- Without an index, the same value returns 200: `inf10_contract_20261125T164207Z_c341a0.jsonl`, case
  `retrieve_header_garbage`.

**Proposed solution.** Accept only `on` and `off` (case-insensitive) and return 400
`invalid_request_error` for anything else. Apply the same rule to `X-Moss-Cache` and
`X-Moss-System-Prompt`.

**Regression test.** A contract case with `X-Moss-Retrieve: maybe` that expects 400.

---

## INF-A5 · An unknown index name is reported as another project's index

| Field | Value |
|---|---|
| Product | Inference API, retrieval |
| Classification | Doc defect / error-message precision |
| Severity | Low |
| Confidence | High |

**Observed.**
- `X-Moss-Indexes: index-that-does-not-exist-9f3c` returns
  `403 permission_error "X-Moss-Indexes names an index outside this project"`.
- Thirty-three nonexistent names return the same 403. The docs specify 400 for more than 32 names,
  and 403 for an index outside the project.

**Evidence.**
- `inf10_contract_20261125T164207Z_c341a0.jsonl`, cases `index_unknown_name` and `indexes_33`.
- Request ids: `b84723ff1b8148fca30774d9a868cb47`, `57b3d01dda074ee7a9eb6ebe68fb48cc`.

**Impact.** A typo in an index name points the developer at permissions instead of the name.

**Proposed solution.** Use a message such as "index unavailable in this project: <name>". It helps
with typos without revealing whether another project has an index of that name. Also document the
order of the checks.

**Limitations.**
- The 33-name case establishes neither the limit nor a general validation order. The 32-name limit is
  untestable on the Developer plan (3 indexes).
- No request named another project's index (out of scope).

---

## INF-A6 · `GET /v1/models/{id}` returns 404

| Field | Value |
|---|---|
| Product | Inference API, OpenAI compatibility |
| Classification | Improvement |
| Severity | Low |
| Confidence | High |

**Observed.**
- `GET /v1/models/moss/fastlane-1` returns 404 with an empty body.
- `GET /v1/models` returned 200 without authentication. The reference documents only this list
  route, and shows it with bearer authentication.
- OpenAI clients expose `models.retrieve(id)`. The slash in the model id may explain the routing;
  this is speculation.

**Evidence.** `inf10_contract_20261125T164207Z_c341a0.jsonl`, case `models_retrieve_one`.

**Proposed solution.** Serve the single-model route (with the slash in the path), or document that
only the list route exists.

---

## Positive results (no card)

- **INF-01:** six client paths (raw HTTP, OpenAI SDK, Moss SDK) worked first time.
- **INF-10:** 36 of 50 cases matched the documentation exactly. 13 undocumented cases were observed
  only, and the one mismatch was inconclusive (INF-A5).
- **INF-09 tool calling:**
  - 14 of 14 correct calls (exact arguments) and 6 of 6 correct non-calls;
  - 14 of 14 second rounds quoted an opaque reference from the tool result;
  - the Moss SDK loop worked 2 of 2.
- **INF-11 streaming:** six protocol checks matched the documented grammar, and one client-side
  early close was observed (server-side stop and billing unverified). Chat sends usage even without
  `stream_options`, as Moss documents (this differs from OpenAI).
- **INF-08 grounding** (json_schema, strict):
  - grounded arm: 29 of 30 correct and 10 of 10 correct abstentions;
  - the same calls with retrieval off returned 4 wrong numbers marked `found: true`;
  - one grounded answer was the literal string `"answer"`, valid against the schema but empty.
- **INF-04 prefix cache:**
  - at about 27.8k input tokens, TTFT is about 0.34 s with a repeated prefix and about 2.0 s fresh;
  - every warm repeat call reported cached tokens at the three tested sizes (800, 6,400 and 27,200
    tokens; multiples of 800).
- **INF-03:** routed through Moss, `anthropic/claude-haiku-5.5` had a TTFT p50 of 480.7 ms against
  162.9 ms for fastlane, in the same run.
- **Reply cache rules observed:**
  - exact text and parameters are part of the key, and so is the endpoint;
  - the streaming mode is not part of the key;
  - `X-Moss-Cache: off` neither reads from nor writes to the cache.
