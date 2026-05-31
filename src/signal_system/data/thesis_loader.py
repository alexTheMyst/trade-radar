"""Load and validate the operator-maintained thesis.yaml taxonomy.

thesis.yaml is gitignored — operators maintain it locally and copy from thesis.example.yaml.
This module is imported by the News Classifier (Phase 3) at job startup.

Security note: uses yaml.safe_load exclusively — yaml.load is not used here because it
allows arbitrary Python object construction (threat T-01-01).
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError, model_validator  # noqa: F401 — re-exported for callers


class ThesisStaleError(RuntimeError):
    """Raised when thesis.yaml review_due date is in the past.

    Extends RuntimeError so it propagates through the heartbeat context manager
    and trips the /fail ping — stale thesis must abort the job loudly.
    """


class Pillar(BaseModel):
    """One investment thesis pillar with associated signals and tickers."""

    name: str
    description: str
    tickers: list[str] = []
    positive_signals: list[str] = []
    negative_signals: list[str] = []
    holdings_exposed: list[str] = []
    threshold_event: str | None = None
    keywords: list[str] = []

    @model_validator(mode="after")
    def _require_signals(self) -> "Pillar":
        if not self.positive_signals and not self.negative_signals:
            raise ValueError(
                f"Pillar '{self.name}' must have at least one positive_signal or negative_signal"
            )
        return self


class Thesis(BaseModel):
    """Top-level thesis.yaml schema — operator-maintained taxonomy."""

    review_due: date
    pillars: list[Pillar]


def load_thesis(path: Path | str) -> tuple[Thesis, str]:
    """Load, validate, and return the thesis at *path*.

    Args:
        path: Path to thesis.yaml (absolute or relative to cwd).

    Returns:
        (thesis, version_hash) where version_hash is the SHA-256 hex digest
        of the raw file bytes — used to stamp signals for outcome tracking.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ThesisStaleError: if thesis.review_due < today (ET date).
        pydantic.ValidationError: if the YAML does not match the Thesis schema.
    """
    p = Path(path)
    raw = p.read_bytes()  # raises FileNotFoundError if missing
    version_hash = hashlib.sha256(raw).hexdigest()

    data = yaml.safe_load(raw)  # safe_load only — never yaml.load
    thesis = Thesis.model_validate(data)  # Pydantic v2 API

    today_et = date.today()
    if thesis.review_due < today_et:
        raise ThesisStaleError(
            f"thesis.yaml review_due is {thesis.review_due.isoformat()} "
            f"(today: {today_et.isoformat()}). Update thesis.yaml before running."
        )

    return thesis, version_hash
