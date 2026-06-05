"""Tests for reconcile_directions — pure function, no DB required.

Coverage:
  T-REC-01: empty input -> ([], [])
  T-REC-02: single signal -> passes through unchanged
  T-REC-03: same-pillar conflict -> highest-score wins, loser in second list
  T-REC-04: margin-guard (<= 0.10 gap) downgrades winner one severity band
  T-REC-05: different pillars, same ticker -> both survive
  T-REC-06: same direction, same pillar -> all survive (no conflict)
  T-REC-07: margin-guard pushes INFORMATIONAL winner to MONITORING -> winner moves to losers
  T-REC-08: neutral direction passes through without reconciliation
  T-REC-09: pillar=None passes through without reconciliation
  T-REC-10: losers carry original alert_id and score (audit trail intact)
"""
import dataclasses
from datetime import datetime, timezone

import pytest

from signal_system.models import Signal, compute_alert_id
from signal_system.reconciler import MARGIN_GUARD, reconcile_directions

_TS = datetime(2026, 6, 5, 10, 0, 0, tzinfo=timezone.utc)


def _sig(
    ticker: str = "AVGO",
    score: float = 0.75,
    severity: str = "INFORMATIONAL",
    direction: str = "positive",
    pillar: str | None = "AI_SPENDING",
    tag: str = "a",
) -> Signal:
    return Signal(
        ticker=ticker,
        score=score,
        severity=severity,
        agent="news_classifier",
        timestamp=_TS,
        alert_id=compute_alert_id(ticker, "2026-06-05", f"rule_{tag}", "news_classifier"),
        title=f"{pillar}: headline {tag}",
        direction=direction,
        pillar=pillar,
    )


# ---------------------------------------------------------------------------
# T-REC-01: empty input
# ---------------------------------------------------------------------------

def test_empty_input():
    winners, losers = reconcile_directions([])
    assert winners == []
    assert losers == []


# ---------------------------------------------------------------------------
# T-REC-02: single signal passes through unchanged
# ---------------------------------------------------------------------------

def test_single_signal_passthrough():
    sig = _sig()
    winners, losers = reconcile_directions([sig])
    assert winners == [sig]
    assert losers == []


# ---------------------------------------------------------------------------
# T-REC-03: same-pillar conflict — highest-score wins
# ---------------------------------------------------------------------------

def test_same_pillar_conflict_highest_score_wins():
    # Gap = 0.15 > MARGIN_GUARD (0.10) so no severity downgrade
    pos = _sig(score=0.85, direction="positive", tag="pos")
    neg = _sig(score=0.70, direction="negative", tag="neg")
    winners, losers = reconcile_directions([pos, neg])
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].alert_id == pos.alert_id
    assert losers[0].alert_id == neg.alert_id


def test_same_pillar_conflict_lower_score_negative_wins():
    # Negative signal has higher score this time
    pos = _sig(score=0.60, direction="positive", tag="pos")
    neg = _sig(score=0.80, direction="negative", tag="neg")
    winners, losers = reconcile_directions([pos, neg])
    assert winners[0].alert_id == neg.alert_id
    assert losers[0].alert_id == pos.alert_id


# ---------------------------------------------------------------------------
# T-REC-04: margin-guard downgrades when gap <= MARGIN_GUARD
# ---------------------------------------------------------------------------

def test_margin_guard_downgrades_action_required():
    # Gap = 0.05 <= 0.10 -> ACTION_REQUIRED -> INFORMATIONAL
    winner_score = 0.85
    loser_score = winner_score - 0.05
    pos = _sig(score=winner_score, severity="ACTION_REQUIRED", direction="positive", tag="pos")
    neg = _sig(score=loser_score, severity="INFORMATIONAL", direction="negative", tag="neg")
    winners, losers = reconcile_directions([pos, neg])
    assert len(winners) == 1
    assert winners[0].alert_id == pos.alert_id
    assert winners[0].severity == "INFORMATIONAL"  # downgraded from ACTION_REQUIRED


def test_no_margin_guard_when_gap_exceeds_threshold():
    # Gap = 0.20 > 0.10 -> no downgrade
    pos = _sig(score=0.90, severity="ACTION_REQUIRED", direction="positive", tag="pos")
    neg = _sig(score=0.70, severity="INFORMATIONAL", direction="negative", tag="neg")
    winners, losers = reconcile_directions([pos, neg])
    assert winners[0].severity == "ACTION_REQUIRED"  # unchanged


def test_margin_guard_exactly_at_threshold_triggers():
    # Gap = exactly MARGIN_GUARD (0.10) -> triggers downgrade
    pos = _sig(score=0.80, severity="ACTION_REQUIRED", direction="positive", tag="pos")
    neg = _sig(score=0.80 - MARGIN_GUARD, severity="INFORMATIONAL", direction="negative", tag="neg")
    winners, losers = reconcile_directions([pos, neg])
    assert winners[0].severity == "INFORMATIONAL"


# ---------------------------------------------------------------------------
# T-REC-05: different pillars on same ticker — both survive
# ---------------------------------------------------------------------------

def test_different_pillars_both_survive():
    sig_a = _sig(ticker="AVGO", pillar="AI_SPENDING", direction="positive", tag="a")
    sig_b = _sig(ticker="AVGO", pillar="MARGIN_COMPRESSION", direction="negative", tag="b")
    winners, losers = reconcile_directions([sig_a, sig_b])
    assert len(winners) == 2
    assert losers == []


# ---------------------------------------------------------------------------
# T-REC-06: same direction, same pillar — all survive
# ---------------------------------------------------------------------------

def test_same_direction_no_conflict():
    sig_a = _sig(score=0.80, direction="positive", tag="a")
    sig_b = _sig(score=0.70, direction="positive", tag="b")
    winners, losers = reconcile_directions([sig_a, sig_b])
    assert len(winners) == 2
    assert losers == []


# ---------------------------------------------------------------------------
# T-REC-07: margin-guard pushes INFORMATIONAL to MONITORING -> moves to losers
# ---------------------------------------------------------------------------

def test_margin_guard_pushes_informational_to_monitoring():
    # INFORMATIONAL winner with tight margin -> downgraded to MONITORING -> moves to losers
    pos = _sig(score=0.75, severity="INFORMATIONAL", direction="positive", tag="pos")
    neg = _sig(score=0.68, severity="INFORMATIONAL", direction="negative", tag="neg")
    # gap = 0.07 <= 0.10 -> INFORMATIONAL -> MONITORING
    winners, losers = reconcile_directions([pos, neg])
    # The winner became MONITORING so it goes to losers too
    winner_ids = {s.alert_id for s in winners}
    loser_ids = {s.alert_id for s in losers}
    assert pos.alert_id not in winner_ids  # pushed to losers
    assert pos.alert_id in loser_ids
    assert neg.alert_id in loser_ids


# ---------------------------------------------------------------------------
# T-REC-08: neutral direction passes through
# ---------------------------------------------------------------------------

def test_neutral_direction_passthrough():
    sig = _sig(direction="neutral", tag="n")
    winners, losers = reconcile_directions([sig])
    assert winners == [sig]
    assert losers == []


# ---------------------------------------------------------------------------
# T-REC-09: pillar=None passes through
# ---------------------------------------------------------------------------

def test_pillar_none_passthrough():
    sig = dataclasses.replace(_sig(), pillar=None)
    winners, losers = reconcile_directions([sig])
    assert winners == [sig]
    assert losers == []


# ---------------------------------------------------------------------------
# T-REC-10: loser retains original score and alert_id (audit trail)
# ---------------------------------------------------------------------------

def test_loser_retains_original_fields():
    # Gap = 0.15 > MARGIN_GUARD so loser is exactly one signal with original fields intact
    pos = _sig(score=0.85, direction="positive", tag="pos")
    neg = _sig(score=0.70, direction="negative", tag="neg")
    _, losers = reconcile_directions([pos, neg])
    assert len(losers) == 1
    assert losers[0].alert_id == neg.alert_id
    assert losers[0].score == 0.70
    assert losers[0].direction == "negative"
    assert losers[0].pillar == "AI_SPENDING"


# ---------------------------------------------------------------------------
# T-REC-11: multi-ticker, multi-pillar mix — only same-pillar-same-ticker conflicts resolved
# ---------------------------------------------------------------------------

def test_multi_ticker_isolation():
    # AVGO/AI_SPENDING: conflict -> reconciled
    avgo_pos = _sig(ticker="AVGO", pillar="AI_SPENDING", score=0.80, direction="positive", tag="avgo_pos")
    avgo_neg = _sig(ticker="AVGO", pillar="AI_SPENDING", score=0.70, direction="negative", tag="avgo_neg")
    # MSFT/AI_SPENDING: no conflict
    msft_pos = _sig(ticker="MSFT", pillar="AI_SPENDING", score=0.75, direction="positive", tag="msft_pos")

    winners, losers = reconcile_directions([avgo_pos, avgo_neg, msft_pos])

    winner_ids = {s.alert_id for s in winners}
    loser_ids = {s.alert_id for s in losers}

    assert avgo_pos.alert_id in winner_ids
    assert avgo_neg.alert_id in loser_ids
    assert msft_pos.alert_id in winner_ids
