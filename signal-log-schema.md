# Signal Log & Measurement

The system's value can only be measured if every signal is logged and every action (or non-action) is recorded. This is the single most important discipline.

## Signal Log Schema

```json
{
  "alert_id": "uuid-v4",
  "timestamp": "2026-05-14T16:30:00-04:00",
  "agent": "DISCOVERY | NEWS_CLASSIFIER",
  "severity": "ACTION_REQUIRED | INFORMATIONAL | MONITORING",
  "ticker": "SMH",
  "title": "SMH composite score crossed 85 with insider accumulation",
  "body": "Detailed reasoning, headline excerpts, scoring breakdown...",
  "suggested_action": "Consider adding 3-5% position",
  "score": 87.3,

  "acted": null,
  "acted_at": null,
  "user_note": null,

  "outcome_price_30d": null,
  "outcome_price_90d": null
}
```

### Field rules

| Field | Filled by | When |
|---|---|---|
| `alert_id` through `score` | System | At signal emission |
| `acted`, `acted_at`, `user_note` | **Operator (manual)** | Within 7 days of alert |
| `outcome_price_30d` | System (cron job) | 30 days after alert |
| `outcome_price_90d` | System (cron job) | 90 days after alert |

If `acted` is still `null` after 7 days, the system should email a reminder. Unresolved signals pollute the hit-rate calculation.

## Per-Signal-Type Measurement

The three signal types are **not commensurable.** Don't compute one aggregate hit rate. Track each separately.

### Mechanical Triggers (Schwab Alerts — outside this system)

The system doesn't manage these, but log them in the same SQLite for unified review.

| Question | Measurement |
|---|---|
| Did you execute? | Binary — `acted` field |
| Was the trigger right? | P&L of the position vs SPY at 30d and 90d |
| Hit rate | (winning positions) / (total triggers fired and acted on) |

### Discovery Agent

| Question | Measurement |
|---|---|
| Did you act within 14 days? | `acted = true`, `acted_at` within window |
| Did the acted-on signals outperform? | Average 90d return of acted basket vs SPY |
| Did the ignored signals underperform? | Average 90d return of ignored basket vs SPY (this is the counterfactual) |
| Is the agent adding alpha? | Acted basket alpha > ignored basket alpha → yes. Otherwise no. |

**The ignored basket is your control group.** If signals you ignored would have outperformed signals you acted on, you're not adding value with your overrides. The system *is* adding value but you're not letting it.

If acted and ignored baskets perform the same vs SPY, the signal has no edge — kill or retune the scoring.

### News Classifier

This one is the trickiest because the output isn't a trade signal — it's an input to a thesis review.

| Question | Measurement |
|---|---|
| Did it flag a thesis change you incorporated? | Manual quarterly count: how many `thesis.yaml` edits were prompted by classifier alerts |
| Did it flag changes that turned out to be noise? | Count of high-delta alerts that you reviewed and dismissed |
| Signal-to-noise ratio | (incorporated changes) / (total ≥\|2\| deltas emitted) |

If after one quarter the classifier has flagged zero changes that you incorporated, **kill it.** It's adding noise, not signal.

## Quarterly Review Process

Calendar event, ~2 hours, every 3 months.

1. **Pull data** — Export `signals` table from SQLite for the quarter
2. **Fill gaps** — Any signals still missing `acted`? Mark them now. (They'll be N/A for hit-rate purposes.)
3. **Compute per-type metrics** — Use the table above for each signal type
4. **Compare to SPY** — Total return of SPY over the same quarter is the bar
5. **Decision per agent:**
   - Acted basket beats SPY by ≥2% → keep, possibly increase position-size confidence
   - Acted basket within ±2% of SPY → keep but flag for next-quarter recheck
   - Acted basket trails SPY by ≥2% → tune scoring weights OR kill
6. **Update thesis.yaml** — Any pillar shifts identified during the quarter

## Anti-Pattern: Don't Let the System Learn From Your Overrides

The system should **not** automatically adjust scoring weights based on which signals you ignored. Reasons:

1. You're a high-conviction discretionary investor with strong priors
2. If the system conforms to your biases, it stops being an outside check
3. Quarterly *operator* review beats continuous *system* adaptation — slower, but more honest

If after quarterly review you want to retune weights, do it manually in config. Document the reason in the commit message.

## Storage & Backup

- SQLite file lives at `./state/signals.db`
- **Daily backup:** copy the file to a timestamped name in `./state/backups/`. Keep 90 days locally, archive older to cloud storage of your choice.
- This is your audit trail. Losing it means losing the ability to measure the system.
