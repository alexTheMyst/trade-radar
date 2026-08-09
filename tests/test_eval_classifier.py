"""Tests for the golden-set classifier eval harness.

The harness itself is fully mocked here — the live API run is an operator
action. These pin loading, grading, accuracy math, and the classify_headline
call contract (ticker=None, weights={}, llm_job="eval_classifier").
"""

from __future__ import annotations

import yaml

import pytest

from signal_system.evals.classifier_eval import (
    grade_case,
    load_cases,
    load_eval_thesis,
    run_eval,
)
from signal_system.classifier.news_classifier import ClassificationResult


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
    result = ClassificationResult(pillar_name="monetary_policy", confidence=0.9,
                                  direction="positive", rationale="x")
    assert grade_case(_case(1), result) == (True, "")


def test_grade_case_pillar_mismatch():
    result = ClassificationResult(pillar_name="ai_capex", confidence=0.9,
                                  direction="positive", rationale="x")
    passed, reason = grade_case(_case(1), result)
    assert passed is False
    assert "pillar" in reason


def test_grade_case_off_thesis_expected_null():
    case = _case(2, expected_pillar=None, expected_direction=None)
    result = ClassificationResult(pillar_name=None, confidence=0.4,
                                  direction="neutral", rationale="off")
    assert grade_case(case, result) == (True, "")


def test_grade_case_off_thesis_wrong_when_classified():
    case = _case(2, expected_pillar=None, expected_direction=None)
    result = ClassificationResult(pillar_name="monetary_policy", confidence=0.4,
                                  direction="neutral", rationale="x")
    passed, reason = grade_case(case, result)
    assert passed is False
    assert "off-thesis" in reason


def test_grade_case_direction_checked_only_when_non_null():
    case = _case(3, expected_pillar="monetary_policy", expected_direction=None)
    result = ClassificationResult(pillar_name="monetary_policy", confidence=0.5,
                                  direction="positive", rationale="x")
    assert grade_case(case, result) == (True, "")  # direction unchecked


def test_grade_case_direction_mismatch_fails():
    case = _case(4, expected_pillar="monetary_policy", expected_direction="negative")
    result = ClassificationResult(pillar_name="monetary_policy", confidence=0.5,
                                  direction="positive", rationale="x")
    passed, reason = grade_case(case, result)
    assert passed is False
    assert "direction" in reason


def test_grade_case_parse_failure_prefix_fails():
    case = _case(5)
    result = "parse_failure_marker"
    passed, reason = grade_case(case, result)
    assert passed is False
    assert "parse" in reason


def test_run_eval_accuracy_math(monkeypatch, tmp_path, capsys):
    """run_eval calls classify_headline with the eval contract and prints accuracy."""
    from signal_system import evals as evals_pkg
    from signal_system.evals import classifier_eval as ce

    cases = [_case(1), _case(2, expected_pillar=None, expected_direction=None)]
    monkeypatch.setattr(ce, "load_cases", lambda *a, **k: (cases, 1))
    monkeypatch.setattr(ce, "load_eval_thesis", lambda *a, **k: (object(), "hash"))
    monkeypatch.setattr(ce, "build_system_prompt", lambda thesis: "SYS")

    def fake_classify(**kwargs):
        # Headline 2 is off-thesis → null pillar; headline 1 is monetary_policy.
        headline = kwargs["headline_dict"]["headline"]
        if "2" in headline:
            return ClassificationResult(pillar_name=None, confidence=0.4,
                                        direction="neutral", rationale="off")
        return ClassificationResult(pillar_name="monetary_policy", confidence=0.9,
                                    direction="positive", rationale="ok")

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


def _fake_classify_all_pass(ticker, headline_dict, thesis, thesis_version_hash,
                            system_prompt, weights=None, llm_job="news_classifier"):
    return ClassificationResult(pillar_name="monetary_policy", confidence=0.9,
                                direction="positive", rationale="ok")
