"""TSK-01: one bounded Tasks run end to end (create -> poll -> files -> cost breakdown).

Fixture: one book page on books.toscrape.com (a public sandbox built for scraping practice). We fetch
the page ourselves first, store its SHA-256 and extract the ground truth (UPC + price) with a regex, so
the score does not depend on anything the agent says about the page. The UPC is a random-looking hex
string, which a model cannot plausibly recall or guess.

Limits: moss-fast, effort low, max_cost_usd 0.05, max_runtime_seconds 120. One task only; reconcile it
with the portal (scripts/reconcile.py) before any other Tasks campaign.

Run:  python benchmarks/tasks/smoke.py
"""

from __future__ import annotations

import hashlib
import json
import re

import httpx

from mossaudit.client import MossHTTP
from mossaudit.config import RESULTS_PROCESSED, load_credentials
from mossaudit.ledger import Ledger
from mossaudit.runlog import RunLog
from mossaudit.tasks import TaskRunner, task_bound, task_cost

URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
SCHEMA = {
    "type": "object",
    "properties": {"upc": {"type": "string"}, "price": {"type": "string"}},
    "required": ["upc", "price"],
    "additionalProperties": False,
}


def ground_truth() -> dict[str, str]:
    r = httpx.get(URL, timeout=20, headers={"User-Agent": "moss-inference-audit/0.2"})
    r.raise_for_status()
    html = r.text
    upc = re.search(r"<th>UPC</th>\s*<td>([^<]+)</td>", html)
    price = re.search(r'<p class="price_color">([^<]+)</p>', html)
    if not upc or not price:
        raise SystemExit("fixture page changed: UPC/price not found")
    return {"upc": upc.group(1).strip(), "price": price.group(1).strip(),
            "sha256": hashlib.sha256(r.content).hexdigest(), "bytes": str(len(r.content))}


def main() -> None:
    truth = ground_truth()
    body = {
        "goal": "Open the start page and report the book's UPC and its price exactly as shown "
                "on the page.",
        "environment": {"type": "browser", "start_url": URL},
        "model": "moss-fast",
        "effort": "low",
        "output_schema": SCHEMA,
        "limits": {"max_runtime_seconds": 120, "max_cost_usd": 0.05},
        "metadata": {"audit": "TSK-01"},
    }
    creds = load_credentials()
    ledger = Ledger()
    ledger.guard(task_bound(body), "TSK-01 smoke")
    with RunLog("tsk01_smoke", {"body": body, "truth": truth}) as log, MossHTTP(creds) as http, \
            TaskRunner(http, ledger, log, "TSK-01") as runner:
        rec = runner.create(body, tag={"case": "smoke"})
        print(f"create: http {rec.status} t_headers={rec.t_headers_ms}ms "
              f"replayed={rec.headers.get('idempotent-replayed')} body={str(rec.body)[:200]}")
        task_id = rec.body["id"]
        final = runner.wait(task_id, poll_s=3, max_wait_s=180)
        files = runner.http.call("GET", f"/tasks/{task_id}/files", tag={"files": True})
        log.call(files)
    out = (final.get("result") or {}).get("output") or {}
    row = {
        "task_id": task_id, "status": final.get("status"), "error": final.get("error"),
        "truth": truth, "output": out, "output_text": (final.get("result") or {}).get("output_text"),
        "upc_correct": out.get("upc", "").strip() == truth["upc"],
        "price_correct": out.get("price", "").strip() == truth["price"],
        "verification": final.get("verification"), "usage": final.get("usage"),
        "cost_usd": task_cost(final), "trace": final.get("_trace"),
        "files_http": files.status, "files": files.body,
        "timestamps": {k: final.get(k) for k in ("created_at", "started_at", "finished_at")},
    }
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    (RESULTS_PROCESSED / "tsk01_smoke.json").write_text(json.dumps(row, indent=2, default=str))
    print(json.dumps({k: row[k] for k in ("status", "upc_correct", "price_correct", "cost_usd",
                                          "verification")}, default=str))
    print(f"ledger: {json.dumps(ledger.state()['open_reservations'])}")


if __name__ == "__main__":
    main()
