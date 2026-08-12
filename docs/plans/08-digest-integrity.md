# Plan 08 — Digest Integrity

**Type:** Fix · **Priority:** P0 · **Est. effort:** ~1 day

## Problem

The digest is the only surface the operator reads. Its counts do not match the database,
and the guard that exists to prevent exactly that cannot detect it.

### Evidence — 2026-08-10 news-morning

Digest said: `Monitoring: 389 (off-thesis 17, volume-cap 339, reconciled 5)`

```sql
SELECT count(*) FROM signals
WHERE date(timestamp)='2026-08-10' AND routing_status='MONITORING';
-- 385
```

| Bucket | Digest | Database |
|---|---|---|
| MONITORING total | 389 | **385** |
| off_thesis | 17 | **15** |
| volume_cap | 339 | 339 |
| reconciled | 5 | 5 |
| below-threshold on-thesis | *no bucket exists* | **26** |

The breakdown sums to 361 against a stated total of 389. Twenty-eight items are unaccounted
for on the face of the message.

### Cause 1 — silent insert drops

```python
def _persist_monitoring_signal(signal: Signal) -> None:
    repository.insert_signal(signal, routing_status="MONITORING")   # returns bool, discarded
```

`repository.insert_signal()` uses `INSERT OR IGNORE` and returns `True` only on a real
insert — the docstring says so explicitly. The caller throws it away, then
`monitoring_total += 1` unconditionally. Any `alert_id` collision is counted but not
stored. Four rows lost on 2026-08-10, two of them `off_thesis` (which explains 17 vs 15).

`alert_id` collisions are expected, not exceptional: `compute_alert_id` is a content hash
over `(ticker, date, rule, agent)`, so the same headline surfacing twice in one window
collides by design. The bug is counting a no-op as a write.

### Cause 2 — a bucket that does not exist

```python
monitoring_breakdown: dict[str, int] = {"off_thesis": 0, "parse_failure": 0}
```

…later joined by `volume_cap` and `reconciled`. But `_classify_kept_headlines` increments
`monitoring_total` for **every** signal with `severity == "MONITORING"`, and only
categorises those whose title starts with `[off_thesis]` or `[parse_failure]`.

Signals that matched a pillar but fell below the confidence threshold have no title prefix
and no bucket. There were 26 on 2026-08-10 — real, on-thesis, classified content, including
`gold: Barrick and Newmont Reach Agreement Regarding Nevada Gold Mines Joint Venture` at
0.55 against a held gold pillar. They are invisible in the digest by construction.

### Cause 3 — the guard is self-referential

```python
def validate_digest_payload(payload, *, scanned_tickers, expected_counts, delivered_signals):
    ...
    if payload.status_counts != normalized_expected:
        raise RuntimeError(...)
```

`expected_counts` **is** `status_counts` — the in-memory dictionary that rendered the
digest. The function asserts a number equals itself. It never touches the database.

The docstring says *"Fail closed if digest content diverges from the persisted run
outcomes."* It does not check persisted outcomes. This is the most consequential defect in
the plan, because it is the control that was supposed to catch the other two.

## Goals

1. Every count in the digest is read back from the database after persistence.
2. The monitoring breakdown sums to the monitoring total, always.
3. A silently-dropped insert is an error, not a rounding difference.

## Non-goals

- Changing what gets classified or capped. That is Plan 09.
- Changing routing. That is Plan 06.

---

## Design

### 8a. Make dropped inserts loud

```python
def _persist_monitoring_signal(signal: Signal) -> bool:
    inserted = repository.insert_signal(signal, routing_status="MONITORING")
    if not inserted:
        logger.info("monitoring signal deduped on alert_id: %s", signal.alert_id[:12])
    return inserted
```

Count only actual inserts. Track deduped-on-write separately and report it — a rising dedup
count is a signal in itself (it means the window is re-fetching the same articles).

### 8b. Exhaustive breakdown

Replace prefix-sniffing with an explicit classification at emit time. Add a
`monitoring_reason` column to `signals` and set it on every MONITORING signal:

| Reason | Meaning |
|---|---|
| `off_thesis` | No pillar matched |
| `volume_cap` | Over the newest-N cap, never classified |
| `parse_failure` | Schema parse failed |
| `below_threshold` | Pillar matched, confidence under the routing floor ← **new** |
| `reconciled` | Lost direction reconciliation |
| `cooldown` | Sentinel repeat inside the 5-day window |

Title prefixes stay for human readability; the column becomes the source of truth for
counting. `weekly-synthesis` should read the column rather than parsing prefixes — the
current title-prefix taxonomy in `CLAUDE.md` is load-bearing string matching and it will
drift.

Add a build-time assertion:

```python
assert sum(monitoring_breakdown.values()) == monitoring_total, (
    f"breakdown {monitoring_breakdown} does not sum to {monitoring_total}"
)
```

A breakdown that does not reconcile is a bug, and the job should fail rather than send a
digest that quietly does not add up.

### 8c. Make the guard read the database

New repository method:

```python
def count_signals_by_routing_status(run_date: date) -> dict[str, int]:
    """Counts by routing_status for a given ET date. Source of truth for digest validation."""
```

Rewrite `validate_digest_payload` to take a `db_counts` argument obtained from that method
*after* `persist_routed_signals()` and *before* the digest is sent, and compare the digest
against **that**, not against `status_counts`.

Where the two disagree, the database wins and the job fails loud. That ordering matters: a
digest that undercounts is a lie; a job that refuses to send is a page.

Extend the same treatment to the monitoring breakdown, which is currently unvalidated
entirely.

### 8d. Digest wording

Report the breakdown exhaustively, and name the dedup count:

```
Monitoring: 385 (off-thesis 15, volume-cap 339, below-threshold 26, reconciled 5)
Deduped on write: 4
```

Every number on that line must come from a database read.

---

## Testing

- **Unit:** `_persist_monitoring_signal` returns `False` on a duplicate `alert_id` and the
  total does not increment.
- **Unit:** breakdown assertion raises when a reason bucket is missing. Construct a
  MONITORING signal with an unmapped `monitoring_reason` and assert the job fails.
- **Unit:** `validate_digest_payload` raises when `db_counts` disagrees with the payload —
  the test that would have caught 2026-08-10.
- **Integration:** run `news-morning` against a fixture feed containing a deliberate
  duplicate headline; assert digest total equals `SELECT count(*)` exactly.
- **Regression:** the zero-alert path still emits `"Scanned N tickers, 0 alerts"`.

## Acceptance criteria

- [ ] Digest MONITORING total equals `SELECT count(*) ... WHERE routing_status='MONITORING'`
      for the run date, every run.
- [ ] Breakdown sums to the total, enforced by assertion.
- [ ] `monitoring_reason` populated on every new MONITORING row.
- [ ] `validate_digest_payload` takes a DB-derived argument and has a test proving it fails
      on divergence.
- [ ] Deduped-on-write count is reported.
