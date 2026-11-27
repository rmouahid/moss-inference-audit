"""Append-only spending ledger that reserves money before every paid send.

Row kinds (one JSON object per line, never rewritten):
  reserve  {key, usd}            money set aside before a send; counts until released
  release  {key}                 the reservation is over (call settled, or request refused)
  charge   {usd, key?}           a settled cost (inference: from `usage` and documented prices)
  task     {task_id, usd, key?}  a cumulative cost snapshot of a task; the MAX per task counts,
                                 so a stale or missing (zero) snapshot can never lower known spend

Exposure = charges + max task snapshot per task + whatever part of each open reservation is not
yet covered by the task it belongs to. Every paid send must first `reserve()`; the check and the
reservation happen under an exclusive file lock, so two processes cannot both pass the same check.
Non-finite or negative amounts are refused (a NaN would make every comparison fail open).
"""

from __future__ import annotations

import fcntl
import json
import math
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import LEDGER_PATH, budget_cap_usd


class BudgetExceeded(RuntimeError):
    """A reservation would push exposure above the cap; nothing was sent."""


class HoldBlocked(RuntimeError):
    """A reservation was refused because a blocking hold (e.g. an unresolved task create) is open."""


def _amount(usd: float) -> float:
    usd = float(usd)
    if not math.isfinite(usd) or usd < 0:
        raise ValueError(f"refusing non-finite or negative amount: {usd!r}")
    return usd


class Ledger:
    """File-locked, append-only record of reservations and costs (see the module docstring).

    Three amounts matter: settled dollars (charges + task costs), open holds not yet covered by a
    task cost, and exposure = their sum, which is what the cap is checked against. The lock is a
    POSIX `flock` on the ledger file, so it protects processes on one machine only.
    """

    def __init__(self, path: Path = LEDGER_PATH, cap_usd: float | None = None) -> None:
        self.path = path
        self.cap = _amount(budget_cap_usd() if cap_usd is None else cap_usd)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    # --- reading -------------------------------------------------------------------------------
    def _rows(self) -> list[dict[str, Any]]:
        rows = []
        for n, line in enumerate(self.path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if "usd" in row:
                try:
                    _amount(row["usd"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"ledger line {n} is corrupt ({row['usd']!r}); fix it by hand "
                                     "before spending") from exc
            rows.append(row)
        return rows

    def state(self) -> dict[str, Any]:
        """Recompute charges, task costs, open reservations and total exposure from the file."""
        charges = 0.0
        reserves: dict[str, float] = {}
        task_obs: dict[str, float] = {}
        key_task: dict[str, str] = {}
        ever: set[str] = set()
        for r in self._rows():
            kind = r.get("kind", "task" if r.get("task_id") else "charge")  # v0 rows had no kind
            if kind == "reserve":
                if r["key"] in reserves:
                    raise ValueError(f"ledger: duplicate open reservation {r['key']}; fix by hand")
                reserves[r["key"]] = r["usd"]
                ever.add(r["key"])
            elif kind == "release":
                reserves.pop(r["key"], None)
            elif kind == "charge":
                charges += r["usd"]
            elif kind == "task":
                task_obs[r["task_id"]] = max(task_obs.get(r["task_id"], 0.0), r["usd"])
                if r.get("key") and key_task.setdefault(r["key"], r["task_id"]) != r["task_id"]:
                    raise ValueError(f"ledger: key {r['key']} reassigned to another task")
        uncovered = sum(max(0.0, usd - task_obs.get(key_task.get(k, ""), 0.0))
                        for k, usd in reserves.items())
        settled = charges + sum(task_obs.values())
        return {"settled": settled, "reserved": uncovered, "exposure": settled + uncovered,
                "open_reservations": dict(reserves), "tasks": task_obs, "ever_reserved": ever}

    def spent(self) -> float:
        """Exposure: settled spend plus open reservations (what the cap is checked against)."""
        return self.state()["exposure"]

    # --- writing -------------------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Generator[None]:
        with self.path.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _append(self, row: dict[str, Any]) -> None:
        row = {"ts": time.time(), **row}
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def reserve(self, campaign: str, key: str, usd: float,
                block_prefix: str | tuple[str, ...] | None = None) -> None:
        """Hold `usd` under `key`. With `block_prefix`, refuse (under the same lock) while any open
        hold starts with that prefix: this is how "one unresolved task at a time" is made atomic."""
        usd = _amount(usd)
        with self._locked():
            st = self.state()
            holds = st["open_reservations"]
            if key in holds:
                raise ValueError(f"reservation key {key} is already open")
            if key in st["ever_reserved"]:
                # A released key may still be linked to an old task; reusing it could hide spend.
                raise ValueError(f"reservation key {key} was used before; use a fresh key")
            if block_prefix and any(k.startswith(block_prefix) for k in holds):
                raise HoldBlocked(f"{campaign}: an open {block_prefix}* hold exists: "
                                     f"{[k for k in holds if k.startswith(block_prefix)]}")
            exp = self.spent()
            if exp + usd > self.cap:
                raise BudgetExceeded(
                    f"{campaign}: exposure ${exp:.4f} + reserve ${usd:.4f} > cap ${self.cap:.2f}")
            self._append({"kind": "reserve", "campaign": campaign, "key": key, "usd": round(usd, 8)})

    def swap(self, campaign: str, old_key: str, new_key: str, usd: float) -> bool:
        """Replace one hold by a smaller one in a single locked step (no window without a hold).

        Used when a task ends: its create hold becomes a traffic hold. If the new hold would be larger
        than the old one, the old one is kept, nothing changes and False is returned."""
        usd = round(_amount(usd), 8)  # holds are stored rounded to 8 decimals: compare like with like
        with self._locked():
            st = self.state()
            holds = st["open_reservations"]
            if new_key in holds or new_key in st["ever_reserved"]:
                raise ValueError(f"reservation key {new_key} is already open or was used before")
            if usd > round(holds.get(old_key, 0.0), 8):
                return False
            self._append({"kind": "reserve", "campaign": campaign, "key": new_key,
                          "usd": round(usd, 8)})
            self._append({"kind": "release", "key": old_key})
            return True

    def open_holds(self, prefix: str = "") -> dict[str, float]:
        """Open reservations whose key starts with `prefix`, as {key: usd}."""
        return {k: v for k, v in self.state()["open_reservations"].items() if k.startswith(prefix)}

    def release(self, key: str, **evidence: object) -> None:
        """Close a reservation; `evidence` is stored with the row."""
        with self._locked():
            self._append({"kind": "release", "key": key, **evidence})

    def charge(self, campaign: str, usd: float, key: str | None = None, **extra: object) -> None:
        """Book a settled cost; if `key` is given, its reservation is released in the same step."""
        usd = _amount(usd)
        with self._locked():
            self._append({"kind": "charge", "campaign": campaign, "usd": round(usd, 8), **extra})
            if key:
                self._append({"kind": "release", "key": key})

    def task_snapshot(self, campaign: str, task_id: str, usd: float, key: str | None = None,
                      **extra: object) -> None:
        """Record the cumulative cost of a task; only the highest snapshot per task counts."""
        usd = _amount(usd)
        with self._locked():
            self._append({"kind": "task", "campaign": campaign, "task_id": task_id,
                          "usd": round(usd, 8), "key": key, **extra})

    # --- checks --------------------------------------------------------------------------------
    def guard(self, estimate_usd: float, campaign: str) -> None:
        """Pre-flight sanity check of a whole campaign (each send still reserves on its own)."""
        estimate_usd = _amount(estimate_usd)
        exp = self.spent()
        if exp + estimate_usd > self.cap:
            raise BudgetExceeded(
                f"{campaign}: exposure ${exp:.4f} + estimate ${estimate_usd:.4f} > cap ${self.cap:.2f}")
        print(f"[budget] {campaign}: exposure ${exp:.4f}, estimate ${estimate_usd:.4f}, "
              f"cap ${self.cap}")

