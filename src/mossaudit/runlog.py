"""Raw-results writer: one JSONL file per campaign run, first line = run metadata."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import threading
import uuid
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from .client import CallRecord
from .config import RESULTS_RAW


def code_revision() -> str:
    """Short hash of the harness source (src/ and benchmarks/), so a run names the code it used."""
    root = RESULTS_RAW.parents[1]
    h = hashlib.sha256()
    for p in sorted([*root.glob("src/**/*.py"), *root.glob("benchmarks/**/*.py")]):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _versions() -> dict[str, str]:
    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    for pkg in ("httpx", "moss", "openai", "jsonschema"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    return out


def _client_location() -> str:
    from dotenv import load_dotenv

    load_dotenv(RESULTS_RAW.parents[1] / ".env")
    loc = os.environ.get("MOSS_AUDIT_CLIENT", "").strip()
    if not loc:
        raise SystemExit("set MOSS_AUDIT_CLIENT in .env (where the client runs, e.g. "
                         "hetzner-vps-nuremberg-de); every latency claim is scoped to it")
    return loc


class RunLog:
    """Context manager that writes one JSONL file per run under results/raw/."""

    def __init__(self, campaign: str, params: dict[str, Any] | None = None) -> None:
        RESULTS_RAW.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{campaign}_{stamp}_{uuid.uuid4().hex[:6]}"
        self.path: Path = RESULTS_RAW / f"{self.run_id}.jsonl"
        self.campaign = campaign
        meta = {
            "type": "run_meta",
            "campaign": campaign,
            "run_id": self.run_id,
            "code_revision": code_revision(),
            "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            # Where the client ran; a label, not a hostname, so nothing identifying is published.
            "client_location": _client_location(),
            "versions": _versions(),
            "params": params or {},
        }
        self._f = self.path.open("x")  # never overwrite a run
        self._lock = threading.Lock()  # the I1 pair sends from two threads
        self._write(meta)

    def _write(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._lock:
            self._f.write(line)
            self._f.flush()

    def call(self, rec: CallRecord) -> None:
        """Append one call record."""
        self._write({"type": "call", **json.loads(rec.to_json())})

    def note(self, **kv: Any) -> None:
        """Append a free-form note (verdicts, scores, run decisions)."""
        self._write({"type": "note", **kv})

    def close(self) -> None:
        """Write the end marker and close the file."""
        self._write({"type": "run_end", "ended_utc": datetime.now(UTC).isoformat(timespec="seconds")})
        self._f.close()
        print(f"[runlog] {self.path.relative_to(RESULTS_RAW.parents[1])}")

    def __enter__(self) -> RunLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def load(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a run file: (run metadata, call records)."""
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    meta = lines[0]
    return meta, [x for x in lines if x.get("type") == "call"]
