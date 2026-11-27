"""Central configuration: credentials, endpoints, budget and output paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")  # before any setting below is read

RESULTS_RAW = ROOT / "results" / "raw"
# Benchmarks write their summaries to results/reruns/ (git-ignored), so a rerun never overwrites
# the published results in results/processed/. Set MOSS_AUDIT_PUBLISH=1 to write there on purpose.
RESULTS_PROCESSED = ROOT / "results" / (
    "processed" if os.environ.get("MOSS_AUDIT_PUBLISH") == "1" else "reruns")
LEDGER_PATH = ROOT / "results" / "ledger.jsonl"

BASE_URL = "https://inference.moss.dev/v1"



def budget_cap_usd() -> float:
    """Spending cap for the ledger: MOSS_BUDGET_CAP_USD, default $8 of the $10 sign-up credits."""
    return float(os.environ.get("MOSS_BUDGET_CAP_USD", "8.0"))


@dataclass(frozen=True)
class Credentials:
    """Moss project id and key, read from `.env`. Never logged."""

    project_id: str
    project_key: str

    @property
    def bearer(self) -> str:
        """Value for `Authorization: Bearer ...` (`project_id:project_key`)."""
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
