"""Central configuration: credentials, endpoints, budget and output paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
RESULTS_RAW = ROOT / "results" / "raw"
RESULTS_PROCESSED = ROOT / "results" / "processed"
FIGURES = ROOT / "results" / "figures"
LEDGER_PATH = ROOT / "results" / "ledger.jsonl"

BASE_URL = "https://inference.moss.dev/v1"

# Hard cap agreed with the account owner: $8 of the $10 sign-up credits.
BUDGET_CAP_USD = float(os.environ.get("MOSS_BUDGET_CAP_USD", "8.0"))


@dataclass(frozen=True)
class Credentials:
    project_id: str
    project_key: str

    @property
    def bearer(self) -> str:
        return f"{self.project_id}:{self.project_key}"


def load_credentials() -> Credentials:
    """Read MOSS_PROJECT_ID / MOSS_PROJECT_KEY from the environment or a local .env file."""
    load_dotenv(ROOT / ".env")
    pid = os.environ.get("MOSS_PROJECT_ID", "").strip()
    key = os.environ.get("MOSS_PROJECT_KEY", "").strip()
    if not pid or not key:
        raise SystemExit(
            "MOSS_PROJECT_ID and MOSS_PROJECT_KEY must be set (copy .env.example to .env)."
        )
    return Credentials(pid, key)
