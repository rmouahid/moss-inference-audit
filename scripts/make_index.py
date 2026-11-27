"""Create the INF-08 index `audit-facts` from benchmarks/inference/data/fictional_facts.json.

Uses the documented quickstart path (`MossClient.create_index(name, docs, "moss-minilm")`). The data is
~3 KB of invented facts; Moss Retrieval ingest is $0.03/MB (docs), so we book a $0.01 allowance in the
ledger and reconcile it with the portal later. Refuses to touch an index that already exists.
There is deliberately no delete option: an index with the same name may not be ours. Delete it by
hand in the portal once you have checked it is the one this script created.

Run:  python scripts/make_index.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from moss import DocumentInfo, MossClient

from mossaudit.config import load_credentials
from mossaudit.ledger import Ledger
from mossaudit.runlog import RunLog

DATA = Path(__file__).resolve().parents[1] / "benchmarks/inference/data/fictional_facts.json"
ALLOWANCE = 0.01


async def main() -> None:
    argparse.ArgumentParser(description="Create the audit-facts index.").parse_args()  # no options
    data = json.loads(DATA.read_text())
    name = data["index"]
    creds = load_credentials()
    client = MossClient(creds.project_id, creds.project_key)
    with RunLog("index_create", {"index": name}) as log:
        existing = [getattr(i, "name", None) for i in await client.list_indexes()]
        log.note(existing_indexes_count=len(existing), audit_index_existed=name in existing)
        if name in existing:
            raise SystemExit(f"{name} already exists; not overwriting")
        ledger = Ledger()
        key = f"{log.run_id}:create"
        ledger.reserve("INF-08", key, ALLOWANCE)
        docs = [DocumentInfo(id=d["id"], text=d["text"], metadata=d["metadata"])
                for d in data["documents"]]
        t0 = time.perf_counter()
        try:
            res = await client.create_index(name, docs, "moss-minilm")
        finally:  # the request may have reached Moss: keep the allowance as a charge either way
            ledger.charge("INF-08", ALLOWANCE, key=key, allowance=True, note="index ingest")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        info = await client.get_index(name)
        log.note(create_ms=ms, result=repr(res)[:500], info=repr(info)[:1000])
        print(f"created {name} in {ms} ms: {repr(res)[:200]}\n{repr(info)[:400]}")


if __name__ == "__main__":
    asyncio.run(main())
