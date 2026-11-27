"""Make a publishable copy of the raw logs: results/raw/*.jsonl -> results/logs/*.jsonl.

What is replaced, everywhere in each line (also inside error messages):
- the Moss project id and project key from .env  -> "<project_id>" / "<project_key>";
- any "Bearer ..." value                         -> "Bearer <redacted>";
- any moss_ + 32 hex token                       -> "<project_key>";
- the list of the project's pre-existing indexes  -> its count only.
It checks known credentials and patterns only; it does not scan results/processed or git history.
The script then fails if any of those values still appears in the output, so a copy is either
clean or not written. results/raw/ itself stays git-ignored.

Run:  python scripts/redact.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from mossaudit.config import load_credentials

ROOT = Path(__file__).resolve().parents[1]
SRC, DST = ROOT / "results/raw", ROOT / "results/logs"


def _count_only(m: re.Match[str]) -> str:
    names = re.findall(r'"[^"]*"', m.group(1))
    return f'"existing_indexes_count": {len(names)}'


def main() -> None:
    creds = load_credentials()
    secrets = {creds.project_id: "<project_id>", creds.project_key: "<project_key>"}
    if not all(secrets):
        sys.exit("project id/key missing from .env")
    DST.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(SRC.glob("*.jsonl")):
        text = f.read_text()
        for value, label in secrets.items():
            text = text.replace(value, label)
        text = re.sub(r"Bearer [^\"\\s]+", "Bearer <redacted>", text)
        text = re.sub(r"moss_[0-9a-f]{32}", "<project_key>", text)
        # The project's own index inventory is private: keep only how many existed.
        text = re.sub(r'"existing_indexes": \[([^\]]*)\]', _count_only, text)
        leaks = [label for value, label in secrets.items() if value in text]
        if leaks:
            sys.exit(f"{f.name}: still contains {leaks}; nothing written for it")
        (DST / f.name).write_text(text)
        n += 1
    print(f"wrote {n} redacted files to {DST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
