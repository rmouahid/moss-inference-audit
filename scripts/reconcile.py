"""Show the ledger state and close holds after checking the portal Usage/Billing pages.

  python scripts/reconcile.py                       # print settled spend, open holds, tasks
  python scripts/reconcile.py --release traffic:task_x [--portal-usd 0.012 --note "Usage 14:05Z"]

`--portal-usd` books the extra amount the portal shows for that one item (for example the traffic
charge of one task) as a settled charge, then releases its hold. It takes exactly one --release key,
so one portal amount can never be booked several times.
"""

from __future__ import annotations

import argparse
import json

from mossaudit.ledger import Ledger


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", nargs="*", default=[])
    ap.add_argument("--portal-usd", type=float)
    ap.add_argument("--note", default="")
    ap.add_argument("--after-portal-check", action="store_true")
    a = ap.parse_args()
    if a.portal_usd is not None and len(a.release) != 1:
        raise SystemExit("--portal-usd needs exactly one --release key (the item that amount is for)")
    led = Ledger()
    for key in a.release:
        if key not in led.open_holds():
            raise SystemExit(f"no open hold {key}")
        if key.startswith("task-create:") and not a.after_portal_check:
            raise SystemExit(f"{key} is an unresolved task create: run TaskRunner.recover() first. "
                             "Only if recover() lists it as 'manual' AND the portal shows no task "
                             "(or its cost), pass --after-portal-check with --note evidence.")
        if a.after_portal_check and not a.note:
            raise SystemExit("--after-portal-check needs --note (what the portal showed, when); "
                             "an empty Usage view is not enough while charges may be pending")
        if a.portal_usd is not None:
            led.charge("reconcile", a.portal_usd, key=key, source="portal", note=a.note)
        else:
            led.release(key, source="portal", note=a.note, after_portal_check=a.after_portal_check)
        print(f"closed {key}")
    st = led.state()
    st.pop("ever_reserved", None)
    print(json.dumps(st, indent=2))


if __name__ == "__main__":
    main()
