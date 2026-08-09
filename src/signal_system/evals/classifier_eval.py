"""Golden-set classifier eval harness.

Run before and after any ANTHROPIC_MODEL change to compare classification
accuracy on a frozen thesis + headline set. Purely offline tooling: NO
heartbeat, NO runs row — exit code 0 always (this is a readout, not a gate).

Paths are anchored at the repo root (this file lives at
src/signal_system/evals/, eval/ is at the repo root). yaml.safe_load only.
"""

from __future__ import annotations

import yaml
from pathlib import Path

from signal_system import config
from signal_system.classifier.news_classifier import (
    ClassificationResult,
    build_system_prompt,
    classify_headline,
)
from signal_system.data.thesis_loader import load_thesis

EVAL_DIR = Path(__file__).parents[3] / "eval"
THESIS_PATH = EVAL_DIR / "thesis-eval.yaml"
GOLDEN_PATH = EVAL_DIR / "golden_headlines.yaml"


def load_eval_thesis(path: Path = THESIS_PATH):
    """Load the frozen eval thesis (review_due 2099 — never stale)."""
    return load_thesis(path)


def load_cases(path: Path = GOLDEN_PATH) -> tuple[list[dict], int]:
    """Load golden cases plus set_version from the golden-set YAML."""
    data = yaml.safe_load(path.read_text())
    return data["cases"], int(data["set_version"])


def grade_case(case: dict, result) -> tuple[bool, str]:
    """Grade one case against a classification result.

    *result* may be a ClassificationResult (happy path) or a MONITORING
    Signal with a [parse_failure] title. Returns (passed, reason).

    A case passes when the pillar matches exactly (including null-for-off-
    thesis) AND the direction matches whenever the case specifies one.
    """
    if not isinstance(result, ClassificationResult):
        return False, "parse_failure (no structured output)"

    expected_pillar = case.get("expected_pillar")
    expected_direction = case.get("expected_direction")

    if result.pillar_name != expected_pillar:
        if expected_pillar is None:
            return False, (
                f"expected off-thesis (null pillar), got {result.pillar_name!r}"
            )
        return False, (
            f"pillar mismatch: expected {expected_pillar!r}, got {result.pillar_name!r}"
        )

    if expected_direction is not None and result.direction != expected_direction:
        return False, (
            f"direction mismatch: expected {expected_direction!r}, got {result.direction!r}"
        )
    return True, ""


def run_eval(eval_dir: Path = EVAL_DIR) -> int:
    """Run the golden-set eval against the live classifier.

    Builds the system prompt once, then calls the real Anthropic API per case
    with ticker=None / weights={} / llm_job='eval_classifier'. Prints per-case
    PASS/FAIL and an accuracy line. Always exits 0 — a readout, not a gate.
    """
    thesis, version_hash = load_eval_thesis(eval_dir / "thesis-eval.yaml")
    cases, set_version = load_cases(eval_dir / "golden_headlines.yaml")
    system_prompt = build_system_prompt(thesis)

    results: list[tuple[dict, bool, str]] = []
    for case in cases:
        signal = classify_headline(
            ticker=None,
            headline_dict={"headline": case["headline"]},
            thesis=thesis,
            thesis_version_hash=version_hash,
            system_prompt=system_prompt,
            weights={},
            llm_job="eval_classifier",
        )
        passed, reason = grade_case(case, signal)
        results.append((case, passed, reason))

    passed_count = sum(1 for _, p, _ in results if p)
    for case, passed, reason in results:
        mark = "PASS" if passed else "FAIL"
        detail = "" if passed else f"  -- {reason}"
        print(f"{mark:4} [{case['id']:>2}] {case['headline'][:80]}{detail}")

    print("")
    print(
        f"Accuracy: {passed_count}/{len(results)} — {config.ANTHROPIC_MODEL} — "
        f"set v{set_version}"
    )
    return 0
