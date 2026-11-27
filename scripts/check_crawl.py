"""Score the 32-book crawl (task T1) offline against the truth we collected ourselves.

The truth file maps each Mystery book title to the UPC read from its own detail page
(results/processed/tsk_mystery_upc_truth.json, fetched during the campaign). This script reads the
Markdown table in the task's `output_text` and counts exact (title, UPC) matches. No network.

Run:  python scripts/check_crawl.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "results/processed/tsk_campaign_20261125T180020Z.json"
TRUTH = ROOT / "results/processed/tsk_mystery_upc_truth.json"


def table_rows(markdown: str) -> dict[str, str]:
    """{title: upc} from the '| Title | UPC |' rows of a Markdown table."""
    rows = {}
    for line in markdown.splitlines():
        m = re.fullmatch(r"\|\s*(.+?)\s*\|\s*([0-9a-f]{16})\s*\|", line.strip())
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def main() -> None:
    truth = json.loads(TRUTH.read_text())
    t1 = next(r for r in json.loads(CAMPAIGN.read_text()) if r["case"] == "T1_fast")
    got = table_rows(t1["output_text"] or "")
    exact = sum(1 for title, upc in truth.items() if got.get(title) == upc)
    extra = sorted(set(got) - set(truth))
    print(f"T1: {exact}/{len(truth)} exact (title + UPC); {len(got)} rows returned; "
          f"rows not in truth: {extra or 'none'}")


if __name__ == "__main__":
    main()
