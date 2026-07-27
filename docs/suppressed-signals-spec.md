# Trade Radar — Suppressed Signals Digest Section

**Spec date:** 2026-07-22
**Driver:** The 7/22 digest delivered exactly 1 AR + 3 INFO = the full cap, with 2 suppressed. The budget is saturating routinely, which means the cut is being made on **slot count, not merit** — and you currently can't see what got cut.

---

## The concrete failure this fixes

From the 7/22 digest:

```
SCHD  83  ACTION_REQUIRED
AAPL  81  INFORMATIONAL      <-- scored >=80. Should have been AR.
SNOW  76  INFORMATIONAL
RTX   69  INFORMATIONAL
Suppressed: 2                <-- invisible
```

Two distinct problems, and they need different treatment:

1. **AAPL was demoted, not suppressed.** It cleared the ≥80 AR threshold and lost the single AR slot to SCHD by two points. It was delivered, but at the wrong severity, with no indication that a demotion happened. You read it as a routine INFO.
2. **Two signals vanished entirely.** Score unknown, ticker unknown, reason unknown. If either was a held position, the system's core promise — never miss a material event on a holding — silently failed.

---

## Required changes

### A. New digest section

Append after delivered alerts. Always render when count > 0.

```
Suppressed (2)
--------------
NVDA — 74 — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD
CDE  — 61 — INFORMATIONAL — cap_exhausted (INFO 3/3) — HELD
```

Fields: `ticker — score — severity_would_have_been — reason — held_flag`

### B. Distinguish the two suppression reasons

These are not the same event and must not share a label:

| Reason | Meaning | Signal quality |
|---|---|---|
| `outscored` | Lost head-to-head for the AR slot | Genuine merit comparison — working as designed |
| `cap_exhausted` | Slots already full when it arrived | **Arbitrary.** Ordering artifact, not a quality judgment |

`cap_exhausted` at high scores is the one to watch. A 78 dropped because three 60s got there first is a routing bug wearing a policy costume.

### C. Surface demotions in the delivered block

```
AAPL — INFORMATIONAL (demoted from ACTION_REQUIRED, outscored by SCHD 83)
```

### D. Held-position flag inline

`core_holding=true` from `universe.csv` → tag `HELD` on every line, delivered or suppressed. A suppressed signal on a holding is categorically more serious than one on a watchlist name.

### E. Score delta vs prior scan

`+7` / `−3` / `NEW` next to each score. A 76 that was 45 yesterday means something different from a 76 that was 78 yesterday. This is the previously queued improvement and it composes cleanly here.

---

## Query

```sql
SELECT ticker, score, severity, routing_status, suppression_reason
FROM signals
WHERE DATE(timestamp, 'localtime') = DATE('now', 'localtime')
  AND routing_status = 'SUPPRESSED'
ORDER BY score DESC;
```

If `suppression_reason` isn't a column yet, add it — the router currently tags `outscored` in the DB but has nowhere to record cap exhaustion.

```sql
ALTER TABLE signals ADD COLUMN suppression_reason TEXT;
```

---

## Router change

The router must record *why* at the moment of the decision. Today it only tags the head-to-head case.

```java
public RoutingDecision route(Signal s, DailyBudget budget) {
    if (s.severity() == Severity.MONITORING) {
        return RoutingDecision.monitoring();       // bypasses router entirely
    }

    Optional<Signal> incumbent = budget.incumbentFor(s.severity());
    if (incumbent.isPresent() && incumbent.get().score() >= s.score()) {
        return RoutingDecision.suppressed(SuppressionReason.OUTSCORED, incumbent.get());
    }

    if (!budget.hasSlot(s.severity())) {
        // Demote before suppressing — an AR that can't get an AR slot is still
        // worth delivering as INFO if INFO has room. This is the AAPL case.
        Severity demoted = s.severity().demote();
        if (demoted != null && budget.hasSlot(demoted)) {
            return RoutingDecision.delivered(demoted, DemotionReason.SLOT_TAKEN);
        }
        return RoutingDecision.suppressed(SuppressionReason.CAP_EXHAUSTED, null);
    }

    return RoutingDecision.delivered(s.severity(), null);
}
```

Note the ordering: **demote before suppress.** That is what already happened to AAPL implicitly; this makes it explicit and recorded rather than an emergent side effect.

---

## What this deliberately does NOT do

**It does not raise the caps.** The caps exist to protect against alert fatigue, and that's a real constraint worth keeping. The fix is visibility into what the cap cost you, so you can judge whether the cap is set right — from a week of data rather than a hunch. Revisit the numbers after ~10 sessions of suppressed-section output.

---

## Two discrepancies found while writing this

1. **Formula, re-confirmed.** Live digest emits `weights=50/30/20 mom20/mom5/range` — three factors. `signal-system-architecture.md` describes four (technical .35 / fundamental .30 / regime .25 / insider .10); `Trade_Radar_-_System_Overview` describes a different four (momentum 35 / volume 30 / range 25 / news 10). All three disagree. Running code wins.

   Substantive, not just clerical: the live model dropped **volume**. A pure price-momentum composite can't distinguish a real breakout from thin drift — which may be exactly why a dividend ETF (SCHD) topped a 23-name momentum scan.

2. **Language.** `signal-system-architecture.md` says Java. `Trade_Radar_-_System_Overview` describes `smtplib` and `urllib.request` — Python stdlib. One of these documents is describing a system that doesn't exist. The Java sketch above assumes the architecture doc is right; confirm before implementing.

Also stale: Phase A is documented as monitoring-only, but this digest delivered 4 alerts with `Monitoring: 0`. Discovery is fully routed. That line should be struck.
