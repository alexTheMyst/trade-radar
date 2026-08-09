"""Tests for the golden-set classifier eval harness.

classify_headline() returns a Signal on every path — never a
ClassificationResult — so grade_case must grade on signal.pillar/
signal.direction with [parse_failure] title detection. These tests pin that
contract with real Signal objects (the live API run is an operator action).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

import pytest

from signal_system.evals.classifier_eval import (
    grade_case,
    load_cases,
    load_eval_thesis,
    run_eval,
)
from signal_system.models import Signal


def _signal(**overrides) -> Signal:
    defaults = dict(
        ticker=None, score=0.9, severity="INFORMATIONAL", agent="news_classifier",
        timestamp=datetime(2026, 1, 1, tzinfo=ZoneInfo("America/New_York")),
        alert_id="a", title="case",
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _case(cid: int, expected_pillar="monetary_policy", expected_direction="positive"):
    return {"id": cid, "headline": f"headline {cid}", "expected_pillar": expected_pillar,
            "expected_direction": expected_direction}


def test_load_eval_thesis_passes_staleness_gate(tmp_path):
    """The frozen thesis must load without a staleness error."""
    thesis_path = tmp_path / "thesis-eval.yaml"
    thesis_path.write_text(
        "review_due: 2099-01-01\n"
        "pillars:\n"
        "  - name: monetary_policy\n"
        "    description: Fed\n"
        "    positive_signals: [rate cut]\n"
        "    negative_signals: [rate hike]\n"
    )
    thesis, version_hash = load_eval_thesis(thesis_path)
    assert thesis.review_due.year == 2099
    assert len(version_hash) == 64


def test_load_cases_reads_set_and_cases(tmp_path):
    path = tmp_path / "golden.yaml"
    path.write_text(
        yaml.safe_dump({"set_version": 1, "cases": [_case(1)]})
    )
    cases, version = load_cases(path)
    assert version == 1
    assert len(cases) == 1


def test_grade_case_pillar_match_direction_match():
    signal = _signal(pillar="monetary_policy", direction="positive")
    assert grade_case(_case(1), signal) == (True, "")


def test_grade_case_pillar_mismatch():
    signal = _signal(pillar="ai_capex", direction="positive")
    passed, reason = grade_case(_case(1), signal)
    assert passed is False
    assert "pillar" in reason


def test_grade_case_off_thesis_expected_null():
    case = _case(2, expected_pillar=None, expected_direction=None)
    signal = _signal(pillar=None, direction="neutral")
    assert grade_case(case, signal) == (True, "")


def test_grade_case_off_thesis_wrong_when_classified():
    case = _case(2, expected_pillar=None, expected_direction=None)
    signal = _signal(pillar="monetary_policy", direction="neutral")
    passed, reason = grade_case(case, signal)
    assert passed is False
    assert "off-thesis" in reason


def test_grade_case_direction_checked_only_when_non_null():
    case = _case(3, expected_pillar="monetary_policy", expected_direction=None)
    signal = _signal(pillar="monetary_policy", direction="positive")
    assert grade_case(case, signal) == (True, "")  # direction unchecked


def test_grade_case_direction_mismatch_fails():
    case = _case(4, expected_pillar="monetary_policy", expected_direction="negative")
    signal = _signal(pillar="monetary_policy", direction="positive")
    passed, reason = grade_case(case, signal)
    assert passed is False
    assert "direction" in reason


def test_grade_case_parse_failure_prefix_fails():
    case = _case(5)
    signal = _signal(title="[parse_failure] bad schema", pillar=None, direction=None)
    passed, reason = grade_case(case, signal)
    assert passed is False
    assert "parse" in reason


def test_run_eval_accuracy_math(monkeypatch, tmp_path, capsys):
    """run_eval calls classify_headline with the eval contract and prints accuracy."""
    from signal_system.evals import classifier_eval as ce

    cases = [_case(1), _case(2, expected_pillar=None, expected_direction=None)]
    monkeypatch.setattr(ce, "load_cases", lambda *a, **k: (cases, 1))
    monkeypatch.setattr(ce, "load_eval_thesis", lambda *a, **k: (object(), "hash"))
    monkeypatch.setattr(ce, "build_system_prompt", lambda thesis: "SYS")

    def fake_classify(**kwargs):
        # Headline 2 is off-thesis → null pillar; headline 1 is monetary_policy.
        headline = kwargs["headline_dict"]["headline"]
        if "2" in headline:
            return _signal(pillar=None, direction="neutral", title="[off_thesis] x")
        return _signal(pillar="monetary_policy", direction="positive", title="[monetary_policy] x")

    captured_calls = []

    def record(**kwargs):
        captured_calls.append(kwargs)
        return fake_classify(**kwargs)

    monkeypatch.setattr(ce, "classify_headline", record)

    run_eval(ce.EVAL_DIR)

    out = capsys.readouterr().out
    assert "Accuracy: 2/2" in out
    assert all(c["ticker"] is None for c in captured_calls)
    assert all(c["weights"] == {} for c in captured_calls)
    assert all(c["llm_job"] == "eval_classifier" for c in captured_calls)


def test_run_eval_exception_does_not_crash(monkeypatch, capsys):
    """A failing classify_headline call must not take down the readout (exit-0 contract)."""
    from signal_system.evals import classifier_eval as ce

    cases = [_case(1), _case(2, expected_pillar=None, expected_direction=None)]
    monkeypatch.setattr(ce, "load_cases", lambda *a, **k: (cases, 1))
    monkeypatch.setattr(ce, "load_eval_thesis", lambda *a, **k: (object(), "hash"))
    monkeypatch.setattr(ce, "build_system_prompt", lambda thesis: "SYS")
    monkeypatch.setattr(
        ce, "classify_headline",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )

    rc = run_eval(ce.EVAL_DIR)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Accuracy: 0/2" in out
    assert "exception" in out
