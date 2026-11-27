"""Make a publishable copy of the raw logs: results/raw/*.jsonl -> results/logs/*.jsonl.

Each line is parsed as JSON and walked value by value:
- the Moss project id and project key from .env  -> "<project_id>" / "<project_key>";
- any "Bearer ..." value                         -> "Bearer <redacted>";
- any moss_ + 32 hex token                       -> "<project_key>";
- an `existing_indexes` list (the project's own index names) -> `existing_indexes_count`.

Every redacted copy, and every file already in results/processed/, is then checked (after JSON
decoding) for the id, the key, both token patterns and index names. If anything is found the script
stops before writing a single file.
It checks known credentials and patterns only; it does not scan git history.
results/raw/ itself stays git-ignored.

Run:  python scripts/redact.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from mossaudit.config import load_credentials

ROOT = Path(__file__).resolve().parents[1]
SRC, DST = ROOT / "results/raw", ROOT / "results/logs"
BEARER = re.compile(r"Bearer [^\"\s]+")
KEY = re.compile(r"moss_[0-9a-f]{32}")


def _clean(value: Any, secrets: dict[str, str]) -> Any:
    if isinstance(value, str):
        for secret, label in secrets.items():
            value = value.replace(secret, label)
        return KEY.sub("<project_key>", BEARER.sub("Bearer <redacted>", value))
    if isinstance(value, list):
        return [_clean(v, secrets) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "existing_indexes" and isinstance(v, list):
                out["existing_indexes_count"] = len(v)  # names are private, the count is not
            else:
                out[_clean(k, secrets)] = _clean(v, secrets)
        return out
    return value


def _strings(value: Any) -> list[str]:
    """Every key and string value inside a parsed JSON value (so JSON escapes are decoded)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (k, *_strings(v))]
    return []


def _has_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_has_key(v, key) for v in value.values())
    if isinstance(value, list):
        return any(_has_key(v, key) for v in value)
    return False


def _leaks(values: list[Any], secrets: dict[str, str]) -> list[str]:
    found = set()
    for text in (s for v in values for s in _strings(v)):
        found |= {label for secret, label in secrets.items() if secret in text}
        # Moss's own error text "Bearer project_id:project_key" is a template, not a token.
        if re.search(r"Bearer (?!<redacted>|project_id:project_key)\S{8,}", text):
            found.add("bearer token")
        if KEY.search(text):
            found.add("moss_ key")
    if any(_has_key(v, "existing_indexes") for v in values):
        found.add("index inventory")
    return sorted(found)


def _load(path: Path) -> list[Any]:
    """A .json file is one value; a .jsonl file is one value per line."""
    text = path.read_text()
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return [json.loads(text)]


def main() -> None:
    creds = load_credentials()
    secrets = {creds.project_id: "<project_id>", creds.project_key: "<project_key>"}
    # 1. Build every redacted copy in memory and check it; nothing is written before all pass.
    copies = {f.name: [_clean(row, secrets) for row in _load(f)] for f in sorted(SRC.glob("*.jsonl"))}
    bad = [f"results/logs/{name}: {leaks}" for name, rows in copies.items()
           if (leaks := _leaks(rows, secrets))]
    # 2. Files already published in results/processed/ are checked too (they are not rewritten).
    for f in sorted((ROOT / "results/processed").glob("*.json")):
        if leaks := _leaks(_load(f), secrets):
            bad.append(f"{f.relative_to(ROOT)}: {leaks}")
    if bad:
        sys.exit("leak check failed, nothing written:\n" + "\n".join(bad))
    DST.mkdir(parents=True, exist_ok=True)
    for name, rows in copies.items():
        (DST / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"wrote {len(copies)} redacted files to {DST.relative_to(ROOT)}; leak check passed")


if __name__ == "__main__":
    main()
