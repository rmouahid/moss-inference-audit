"""Tasks campaigns after TSK-01: accuracy ladder, schema vs truth, limits, cancel, idempotency, files.

Fixtures: books.toscrape.com only (public sandbox built for scraping practice). For every case the
ground truth is computed by us at run time from the page bytes (regex), with the SHA-256 of each page
fetched, so a score never depends on what the agent claims.

One task at a time (TaskRunner refuses a create while another task is unresolved). Every create
reserves task_bound(body) first; cancels and waits are bounded (see mossaudit.tasks).

Cases (run with --cases, default: all in this order)
  TSK-02 ladder   L1_fast, L1_balanced   one fact on one page (UPC of "Sharp Objects")
                  L2_fast                one fact among 11 distractors (price of one Travel title)
                  L3_fast, L3_balanced   three facts on one page (UPC, stock count, reviews)
                  L4_fast                2-hop navigation from the home page (Poetry -> a book -> UPC)
                  L5_fast                count across 2 pages (Mystery books priced over 50.00)
  TSK-03'         A1_fast                absent field (ISBN, the page has none): schema allows null
  TSK-09          F1_fast                start_url is a 404 page
  TSK-10          R1_fast, R2_fast       L1_fast repeated twice (with L1_fast: n = 3)
  TSK-04          C1_fast                max_cost_usd 0.01 on a long goal: stop code and overshoot
  TSK-05/12       T1_fast                max_runtime_seconds 60 on a long goal: real runtime vs cap;
                                         files listed WHILE running (expect 409 task_not_finished)
  TSK-06'         X1_fast                cancel once `running` is seen: cancel latency, final cost
                  X2                     cancel of an already finished task (L1_fast): unchanged?
  TSK-07'         I1                     same Idempotency-Key + body replayed (same id, header),
                                         same key + other body (422), concurrent duplicate pair
  TSK-12          files are listed and one file downloaded after every finished task.
  TSK-14          billing arithmetic is checked offline from the saved rows (analysis step).

Run:  python benchmarks/tasks/campaign.py [--cases L1_fast L2_fast ...]
"""

from __future__ import annotations

import argparse
import hashlib
import html as htmlmod
import json
import re
import threading
import time
import uuid
from typing import Any

import httpx

from mossaudit.client import CallRecord, MossHTTP
from mossaudit.config import RESULTS_PROCESSED, load_credentials
from mossaudit.ledger import Ledger
from mossaudit.runlog import RunLog
from mossaudit.tasks import (
    TERMINAL,
    TRAFFIC_ALLOWANCE_USD,
    TaskRunner,
    UnresolvedTask,
    task_bound,
    task_cost,
)

B = "https://books.toscrape.com/"
UA = {"User-Agent": "moss-inference-audit/0.2"}
PAGES: dict[str, dict[str, Any]] = {}


def page(url: str, expect_status: int = 200) -> str:
    """Fetch a fixture page once; any unexpected status stops the run (a failed fetch must never
    look like an absent field or an empty page)."""
    if url not in PAGES:
        r = httpx.get(url, timeout=20, headers=UA)
        PAGES[url] = {"status": r.status_code, "sha256": hashlib.sha256(r.content).hexdigest(),
                      "bytes": len(r.content), "text": r.text}
    if PAGES[url]["status"] != expect_status:
        raise SystemExit(f"fixture {url}: HTTP {PAGES[url]['status']}, expected {expect_status}")
    return PAGES[url]["text"]


def book_url(category_path: str, title: str) -> str:
    h = page(B + category_path)
    for href, t in re.findall(r'<h3><a href="([^"]+)" title="([^"]+)"', h):
        if htmlmod.unescape(t) == title:
            return B + "catalogue/" + href.replace("../", "")
    raise SystemExit(f"fixture changed: {title} not found in {category_path}")


def table(url: str) -> dict[str, str]:
    return {k: v.strip() for k, v in re.findall(r"<th>([^<]+)</th>\s*<td>([^<]+)</td>", page(url))}


def listing(url: str) -> list[tuple[str, str]]:
    h = page(url)
    titles = [htmlmod.unescape(t) for t in re.findall(r'<h3><a href="[^"]+" title="([^"]+)"', h)]
    prices = re.findall(r'<p class="price_color">([^<]+)</p>', h)
    return list(zip(titles, prices, strict=True))


MYSTERY = "catalogue/category/books/mystery_3/index.html"
MYSTERY2 = "catalogue/category/books/mystery_3/page-2.html"
TRAVEL = "catalogue/category/books/travel_2/index.html"
POETRY = "catalogue/category/books/poetry_23/index.html"
LONG_GOAL = ("Open every book in the Mystery category, on all of its pages, one by one, and report "
             "each book's title and UPC.")


def S(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


STR, INT = {"type": "string"}, {"type": "integer"}
NULLSTR = {"type": ["string", "null"]}


def build_cases() -> dict[str, dict[str, Any]]:
    sharp = book_url(MYSTERY, "Sharp Objects")
    t_sharp = table(sharp)
    stock = int(re.search(r"\((\d+) available\)", t_sharp["Availability"]).group(1))  # type: ignore[union-attr]
    travel = listing(B + TRAVEL)
    see_title, see_price = next((t, p) for t, p in travel if t.startswith("See America"))
    maria = book_url(POETRY, "The Black Maria")
    m1, m2 = listing(B + MYSTERY), listing(B + MYSTERY2)
    total = re.search(r"<strong>(\d+)</strong> results", page(B + MYSTERY))
    if not total or len(m1) != 20 or len(m1) + len(m2) != int(total.group(1)) or not m2:
        raise SystemExit("fixture changed: Mystery listing does not add up")
    if len(travel) != 11 or "UPC" not in t_sharp or "UPC" not in table(maria):
        raise SystemExit("fixture changed: Travel listing or book tables")
    mystery_all = m1 + m2
    over50 = sum(1 for _, p in mystery_all if float(p.lstrip("£")) > 50.0)
    attic = B + "catalogue/a-light-in-the-attic_1000/index.html"
    missing = B + "catalogue/this-book-does-not-exist_9999/index.html"
    page(missing, expect_status=404)
    page(attic)  # fixture hash only (ISBN absence is checked by reading the table: no ISBN row)
    t_attic = table(attic)
    if "UPC" not in t_attic or any("isbn" in k.lower() for k in t_attic) or "ISBN" in page(attic):
        raise SystemExit("fixture changed: attic table missing or it now shows an ISBN")

    def base(model: str, goal: str, url: str, schema: dict[str, Any] | None,
             runtime: int = 120, cost: float = 0.05) -> dict[str, Any]:
        b = {"goal": goal, "environment": {"type": "browser", "start_url": url}, "model": model,
             "effort": "low", "limits": {"max_runtime_seconds": runtime, "max_cost_usd": cost},
             "metadata": {"audit": "TSK"}}
        if schema:
            b["output_schema"] = schema
        return b

    l1 = ("Report this book's UPC exactly as shown on the page.", sharp, S({"upc": STR}),
          {"upc": t_sharp["UPC"]})
    l3 = (("Report this book's UPC, the number of copies in stock and the number of reviews, "
           "exactly as shown on the page."), sharp,
          S({"upc": STR, "in_stock": INT, "reviews": INT}),
          {"upc": t_sharp["UPC"], "in_stock": stock, "reviews": int(t_sharp["Number of reviews"])})
    c: dict[str, dict[str, Any]] = {}
    for m in ("fast", "balanced"):
        c[f"L1_{m}"] = {"body": base(f"moss-{m}", l1[0], l1[1], l1[2]), "truth": l1[3]}
        c[f"L3_{m}"] = {"body": base(f"moss-{m}", l3[0], l3[1], l3[2]), "truth": l3[3]}
    c["L2_fast"] = {"body": base("moss-fast", f'Report the price of the book titled "{see_title}" '
                                 "exactly as shown on this page.", B + TRAVEL, S({"price": STR})),
                    "truth": {"price": see_price}}
    c["L4_fast"] = {"body": base("moss-fast", 'Go to the Poetry category, open the book "The Black '
                                 "Maria\" and report its UPC exactly as shown.", B + "index.html",
                                 S({"upc": STR}), runtime=180),
                    "truth": {"upc": table(maria)["UPC"]}}
    c["L5_fast"] = {"body": base("moss-fast", "How many books in the Mystery category cost more "
                                 "than £50.00? The category has more than one page; check all of "
                                 "them.", B + MYSTERY, S({"count": INT}), runtime=240),
                    "truth": {"count": over50}}
    c["A1_fast"] = {"body": base("moss-fast", "Report this book's ISBN. If the page does not show an "
                                 "ISBN, set isbn to null and found to false. Do not guess.", attic,
                                 S({"isbn": NULLSTR, "found": {"type": "boolean"}})),
                    "truth": {"isbn": None, "found": False}}
    c["F1_fast"] = {"body": base("moss-fast", "Report this book's UPC. If the page has no book, set "
                                 "upc to null and found to false. Do not guess.", missing,
                                 S({"upc": NULLSTR, "found": {"type": "boolean"}})),
                    "truth": {"upc": None, "found": False}}
    for r in ("R1_fast", "R2_fast"):
        c[r] = {"body": base("moss-fast", l1[0], l1[1], l1[2]), "truth": l1[3]}
    c["C1_fast"] = {"body": base("moss-fast", LONG_GOAL, B + MYSTERY, None, runtime=300, cost=0.01),
                    "truth": None, "expect": "budget_exceeded"}
    c["T1_fast"] = {"body": base("moss-fast", LONG_GOAL, B + MYSTERY, None, runtime=60, cost=0.05),
                    "truth": None, "expect": "time_limit", "files_while_running": True}
    c["T2_fast"] = {"body": base("moss-fast", "Open every book in the Mystery, Poetry, Travel and "
                                 "Historical Fiction categories, on all of their pages, one by one, "
                                 "and report each book's title and UPC.", B + "index.html", None,
                                 runtime=60, cost=0.05),
                    "truth": None, "expect": "time_limit"}
    c["X1_fast"] = {"body": base("moss-fast", LONG_GOAL, B + MYSTERY, None, runtime=300, cost=0.05),
                    "truth": None, "cancel_on": "running"}
    c["X2"] = {"special": "cancel_finished"}
    c["I1"] = {"special": "idempotency",
               "body": base("moss-fast", l1[0], l1[1], l1[2], runtime=60, cost=0.01),
               "truth": l1[3]}
    return c


def score(truth: dict[str, Any] | None, out: Any) -> dict[str, Any] | None:
    if truth is None:
        return None
    if not isinstance(out, dict):
        return {"all_correct": False, "fields": {k: False for k in truth}, "output_type": str(type(out))}
    norm = {k: (v.strip() if isinstance(v, str) else v) for k, v in out.items()}
    # present, same exact type (so 0 is not False and a missing key is not None), equal value
    fields = {k: k in norm and type(norm[k]) is type(v) and norm[k] == v for k, v in truth.items()}
    return {"all_correct": all(fields.values()), "fields": fields}


def local_schema_ok(schema: dict[str, Any] | None, out: Any) -> bool | None:
    """Minimal independent check of our own schemas (object, required keys, no extras, types)."""
    if not schema:
        return None
    if not isinstance(out, dict) or set(out) != set(schema["required"]):
        return False
    types = {"string": str, "integer": int, "boolean": bool, "null": type(None)}
    for k, spec in schema["properties"].items():
        allowed = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        v = out[k]
        if isinstance(v, bool) and "boolean" not in allowed:
            return False
        if not any(isinstance(v, types[t]) for t in allowed):
            return False
    return True


def files_check(runner: TaskRunner, task_id: str) -> dict[str, Any]:
    lst = runner.http.call("GET", f"/tasks/{task_id}/files", tag={"files": "list"})
    runner.log.call(lst)
    out: dict[str, Any] = {"list_http": lst.status,
                           "n_files": len((lst.body or {}).get("data") or [])
                           if isinstance(lst.body, dict) else None}
    data = (lst.body or {}).get("data") if isinstance(lst.body, dict) else None
    if data:
        path = data[0]["path"]
        r = runner.http._client.get(f"/tasks/{task_id}/files/{path}")  # binary: not via call()
        out.update(download_http=r.status_code, download_bytes=len(r.content),
                   listed_bytes=data[0].get("bytes"),
                   content_type=r.headers.get("content-type"),
                   bytes_match=len(r.content) == data[0].get("bytes"))
        runner.log.note(task_id=task_id, file=path, **{k: out[k] for k in
                                                       ("download_http", "download_bytes",
                                                        "listed_bytes", "content_type")})
    return out


def run_task(runner: TaskRunner, ledger: Ledger, name: str, case: dict[str, Any]) -> dict[str, Any]:
    body = case["body"]
    ledger.guard(task_bound(body), f"{name}")
    rec = runner.create(body, tag={"case": name})
    tid = rec.body.get("id") if isinstance(rec.body, dict) else None
    row: dict[str, Any] = {"case": name, "create_http": rec.status, "task_id": tid,
                           "create_error": (rec.body or {}).get("error")
                           if isinstance(rec.body, dict) else rec.body}
    if not tid:
        return row
    files_running = None
    st = None
    if case.get("files_while_running"):
        for _ in range(20):  # wait until running is seen, then list files once
            g = runner.get(tid, tag={"pre_files": True})
            st = (g.body or {}).get("status") if isinstance(g.body, dict) else None
            if st == "running" or st in TERMINAL:
                break
            time.sleep(2)
        f = runner.http.call("GET", f"/tasks/{tid}/files", tag={"files": "while_running"})
        runner.log.call(f)
        files_running = {"http": f.status, "status_then": st,
                         "error": (f.body or {}).get("error") if isinstance(f.body, dict) else None}
    t_cancel = time.time()
    final = runner.wait(tid, poll_s=3, max_wait_s=body["limits"]["max_runtime_seconds"] + 120,
                        cancel_on_status=case.get("cancel_on"))
    out = (final.get("result") or {}).get("output")
    usage = final.get("usage") or {}
    row.update({
        "model": body["model"], "status": final.get("status"), "error": final.get("error"),
        "cancel_requested": final.get("cancel_requested"),
        "expect": case.get("expect"), "truth": case.get("truth"), "output": out,
        "output_text": (final.get("result") or {}).get("output_text"),
        "score": score(case.get("truth"), out),
        "local_schema_ok": local_schema_ok(body.get("output_schema"), out),
        "verification": final.get("verification"), "usage": usage, "cost_usd": task_cost(final),
        "limits": body["limits"], "trace": final.get("_trace"),
        "timestamps": {k: final.get(k) for k in ("created_at", "started_at", "finished_at")},
        "files_while_running": files_running, "t_wait_start": t_cancel,
    })
    if final.get("status") in TERMINAL and tid not in runner.open:
        row["files"] = files_check(runner, tid)
    return row


def cancel_finished(runner: TaskRunner, rows: list[dict[str, Any]]) -> dict[str, Any]:
    done = next((r for r in rows if r.get("status") == "completed" and r.get("task_id")), None)
    if not done:
        return {"case": "X2", "skipped": "no finished task in this run"}
    tid = done["task_id"]
    before = runner.get(tid, tag={"case": "X2", "before": True}).body
    c = runner.http.call("POST", f"/tasks/{tid}/cancel", tag={"case": "X2"})
    runner.log.call(c)
    after = c.body if isinstance(c.body, dict) else {}
    keys = ("status", "finished_at", "cancel_requested")
    b = before if isinstance(before, dict) else {}
    valid = c.status == 200 and after.get("id") == tid and b.get("id") == tid
    return {"case": "X2", "task_id": tid, "cancel_http": c.status, "valid_reply": valid,
            "before": {k: b.get(k) for k in keys}, "after": {k: after.get(k) for k in keys},
            "unchanged": valid and all(b.get(k) == after.get(k) for k in keys),
            "cost_before": task_cost(b), "cost_after": task_cost(after),
            "error": (after.get("error") if c.status != 200 else None)}


def idempotency(runner: TaskRunner, ledger: Ledger, case: dict[str, Any]) -> dict[str, Any]:
    """TSK-07'. Every send that reuses a key goes through TaskRunner.replay(): a fresh
    `task-create:` hold and intent are written BEFORE the send (create gate, recover() and
    reconcile.py protect it). Any ambiguous outcome raises UnresolvedTask and stops the run with
    the hold kept. A replay that returns a NEW task id is a finding; that task is cancelled at once
    and waited to a terminal state under its own hold."""
    body = case["body"]
    bound = task_bound(body)
    payload = json.dumps(body).encode()
    key = f"audit-idem-{uuid.uuid4()}"
    row: dict[str, Any] = {"case": "I1", "key": key}
    ledger.guard(bound * 3 + TRAFFIC_ALLOWANCE_USD * 3, "I1 idempotency")
    # one attempt only: a retry after an ambiguous reply could itself be a duplicate under one hold
    rec1 = runner.create(body, tag={"case": "I1", "step": "first"}, idempotency_key=key,
                         max_attempts=1, idempotency_test=True)
    t1 = rec1.body.get("id") if isinstance(rec1.body, dict) else None
    row["first"] = {"http": rec1.status, "id": t1,
                    "replayed": rec1.headers.get("idempotent-replayed")}
    if not (rec1.status == 202 and isinstance(t1, str) and t1.startswith("task_")):
        row["stopped"] = "first create not confirmed"
        return row

    def unexpected(rec: CallRecord, step: str) -> str | None:
        tid = rec.body.get("id") if isinstance(rec.body, dict) else None
        if tid and tid != t1 and tid in runner.open:
            runner.cancel(tid, tag={"case": "I1", "step": step, "dup_cleanup": True}, timeout_s=30)
            if tid in runner.open:
                runner.wait(tid, poll_s=3, max_wait_s=120)
            if tid in runner.open:
                raise UnresolvedTask(f"I1 {step}: duplicate {tid} not settled; stopping")
        return tid

    def summary(rec: CallRecord) -> dict[str, Any]:
        b = rec.body if isinstance(rec.body, dict) else {}
        return {"http": rec.status, "id": b.get("id"), "same_id": b.get("id") == t1,
                "replayed": rec.headers.get("idempotent-replayed"), "status": b.get("status"),
                "error": b.get("error")}

    # 1. same key + same body while the first task is live
    rep = runner.replay(payload, key, t1, bound, {"case": "I1", "step": "replay"})
    row["replay"] = summary(rep)
    unexpected(rep, "replay")
    # 2. same key + another body -> documented 422 idempotency_key_reused
    other = json.dumps({**body, "metadata": {"audit": "TSK-07-other"}}).encode()
    ob = runner.replay(other, key, t1, bound, {"case": "I1", "step": "other_body"})
    row["other_body"] = summary(ob)
    unexpected(ob, "other_body")
    # finish the first task, then 3. replay after it finished
    final = runner.wait(t1, poll_s=3, max_wait_s=180)
    row["first_final"] = {"status": final.get("status"), "cost": task_cost(final),
                          "score": score(case["truth"], (final.get("result") or {}).get("output"))}
    if final.get("status") not in TERMINAL or t1 in runner.open or any(
            k.startswith("task-create:") for k in ledger.open_holds()):
        raise UnresolvedTask(f"I1: {t1} or an extra send is not verifiably settled; stopping")
    late = runner.replay(payload, key, t1, bound, {"case": "I1", "step": "replay_after_finish"})
    row["replay_after_finish"] = summary(late)
    unexpected(late, "replay_after_finish")
    # 4. concurrent duplicate pair with a fresh key; each send has its own hold taken first
    key2 = f"audit-idem-{uuid.uuid4()}"
    results: list[CallRecord | BaseException | None] = [None, None]
    barrier = threading.Barrier(2)

    def go(i: int) -> None:
        try:
            with MossHTTP(load_credentials()) as h2:
                barrier.wait(timeout=30)
                results[i] = runner.replay(payload, key2, None, bound,
                                           {"case": "I1", "step": f"pair{i}"}, http=h2)
        except BaseException as exc:  # noqa: BLE001 - recorded, re-raised below
            results[i] = exc

    ths = [threading.Thread(target=go, args=(i,)) for i in (0, 1)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=120)
    recs = [r for r in results if isinstance(r, CallRecord)]
    for r in recs:
        runner.log.call(r)
    ids = [r.body.get("id") if isinstance(r.body, dict) else None for r in recs]
    row["pair"] = {"http": [r.status for r in recs], "ids": ids,
                   "replayed": [r.headers.get("idempotent-replayed") for r in recs],
                   "errors": [repr(r) for r in results if not isinstance(r, CallRecord)],
                   "distinct_tasks": len({i for i in ids if i})}
    if len(recs) == 2 and ids[0] and ids[0] == ids[1]:
        # one task, two holds: the task keeps one, the other is released with the proof
        tid = ids[0]
        keep = runner.open.get(tid)
        for r in recs:
            if r.tag["hold"] != keep:
                ledger.release(r.tag["hold"], note="pair returned one task id", task_id=tid)
    distinct = list(dict.fromkeys(i for i in ids if i))
    for tid in distinct[1:]:  # a second task for one key is a duplicate: cancel it first
        if tid in runner.open:
            runner.cancel(tid, tag={"case": "I1", "step": "pair", "dup_cleanup": True},
                          timeout_s=30)
    for tid in distinct:
        if tid in runner.open:
            runner.wait(tid, poll_s=3, max_wait_s=180)
    errs = [r for r in results if isinstance(r, BaseException) or r is None]
    if errs:  # holds stay open (ambiguous): stop the run here
        row["stopped"] = "pair ambiguous; holds kept, run recover()"
        raise UnresolvedTask(f"I1 pair ambiguous: {errs}")
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="+")
    a = ap.parse_args()
    cases = build_cases()
    names = a.cases or list(cases)
    unknown = set(names) - set(cases)
    if unknown:
        raise SystemExit(f"unknown cases {sorted(unknown)}")
    creds = load_credentials()
    ledger = Ledger()
    rows: list[dict[str, Any]] = []
    out_path = RESULTS_PROCESSED / f"tsk_campaign_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    RESULTS_PROCESSED.mkdir(parents=True, exist_ok=True)
    with RunLog("tsk_campaign", {"cases": names,
                                 "fixtures": {u: {k: v for k, v in p.items() if k != "text"}
                                              for u, p in PAGES.items()}}) as log, \
            MossHTTP(creds) as http, TaskRunner(http, ledger, log, "TSK") as runner:
        for name in names:
            case = cases[name]
            print(f"== {name}")
            if case.get("special") == "cancel_finished":
                row = cancel_finished(runner, rows)
            elif case.get("special") == "idempotency":
                row = idempotency(runner, ledger, case)
            else:
                row = run_task(runner, ledger, name, case)
            rows.append(row)
            out_path.write_text(json.dumps(rows, indent=2, default=str))  # after every case
            print(json.dumps({k: row.get(k) for k in ("case", "status", "error", "cost_usd",
                                                       "score")}, default=str)[:300])
    print(f"wrote {out_path}\nexposure now ${ledger.spent():.4f}")


if __name__ == "__main__":
    main()
