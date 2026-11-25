"""Summarize the latency campaigns (INF-02 windows, INF-03, INF-04) from their redacted logs.

For each run: per condition (model, grounded, ctx, prefix), warm-up excluded, the TTFT and total
time distribution (linear-interpolated percentiles), the share of calls with TTFT under 200 ms over
ALL attempted calls (Wilson 95 % interval), outcomes, realized input and cached tokens; for runs with
two arms, the paired difference grounded - plain per unit.
Output: results/processed/latency_summary.json

Run:  python scripts/analyze_latency.py
"""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any

from mossaudit.stats import summary, wilson

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "results/logs"
RUNS = ["inf02_latency_w1_", "inf02_latency_w2_", "inf02_latency_w3_", "inf03_vendor_", "inf04_prompt_size_",
        "inf02_latency_2"]  # the last one is the unpaced pilot (5 rate-limited calls)


def load(prefix: str) -> list[Path]:
    return sorted(p for p in LOGS.glob(f"{prefix}*.jsonl"))


def run_summary(path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    meta = rows[0]
    calls = [r for r in rows if r.get("type") == "call" and not r["tag"].get("rejection_path")]
    groups: dict[tuple, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in calls:
        t = r["tag"]
        if not t.get("warmup"):
            groups[(t["model"], t["grounded"], t["ctx"], t["prefix"])].append(r)
    conds = []
    for (model, grounded, ctx, prefix), c in sorted(groups.items()):
        ok = [r for r in c if r["status"] == 200 and r.get("t_first_token_ms") is not None]
        ttft = [r["t_first_token_ms"] for r in ok]
        under = sum(1 for x in ttft if x < 200)
        usage = [r.get("usage") or {} for r in ok]
        conds.append({
            "model": model, "grounded": grounded, "ctx": ctx, "prefix": prefix,
            "attempted": len(c), "status_counts": collections.Counter(str(r["status"]) for r in c),
            "outcomes": collections.Counter(str(r["stream_outcome"]) for r in c),
            "ttft_ms": summary(ttft), "total_ms": summary([r["t_end_ms"] for r in ok]),
            "under_200ms": under, "under_200ms_wilson95": wilson(under, len(c)),
            "input_tokens": summary([u.get("input_tokens", 0) for u in usage]),
            "cached_tokens": sorted({(u.get("input_tokens_details") or {}).get("cached_tokens", 0)
                                     for u in usage}),
            "retrieve_ms": summary([float(r["headers"].get("x-moss-retrieve-ms") or 0)
                                    for r in ok]),
        })
    out: dict[str, Any] = {"run_id": meta["run_id"], "started_utc": meta["started_utc"],
                           "params": meta["params"], "conditions": conds}
    by_unit: dict[int, dict[bool, float]] = collections.defaultdict(dict)
    for r in calls:
        t = r["tag"]
        if not t.get("warmup") and r["status"] == 200 and r.get("t_first_token_ms") is not None:
            by_unit[t["unit"]][t["grounded"]] = r["t_first_token_ms"]
    diffs = [v[True] - v[False] for v in by_unit.values() if True in v and False in v]
    if diffs:
        out["paired_grounded_minus_plain_ms"] = summary(diffs)
    return out


def main() -> None:
    result = []
    seen: set[Path] = set()
    for prefix in RUNS:
        for p in load(prefix):
            if p not in seen:
                seen.add(p)
                result.append(run_summary(p))
    dst = ROOT / "results/processed/latency_summary.json"
    dst.write_text(json.dumps(result, indent=2, default=str))
    for r in result:
        for c in r["conditions"]:
            print(f"{r['run_id'][:34]:34} {c['model'][:20]:20} g={int(c['grounded'])} "
                  f"ctx={c['ctx']:<6} {c['prefix']:6} n={c['attempted']} "
                  f"p50={c['ttft_ms']['p50']} <200={c['under_200ms']}")
        if "paired_grounded_minus_plain_ms" in r:
            print("   paired diff p50", r["paired_grounded_minus_plain_ms"]["p50"])


if __name__ == "__main__":
    main()
