# Phase 1: Foundation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-15
**Phase:** 1-Foundation
**Mode:** --auto (all decisions auto-selected as recommended defaults)
**Areas discussed:** Signal dataclass design, Schema migration strategy, thesis.yaml structure, Universe data source, Config additions

---

## Signal Dataclass Design

| Option | Description | Selected |
|--------|-------------|----------|
| Lean dataclass | Only agent-produced fields; routing_status stays in DB | ✓ |
| Fat dataclass | Include routing_status and all DB columns on Signal | |

**Auto-selected:** Lean dataclass — Signal is immutable from agent perspective; router writes routing_status to DB only.
**Notes:** `sub_scores: dict[str, float]` added for per-pillar/per-factor scores. alert_id changed from UUID to SHA-256 content-hash.

---

## Schema Migration Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| ALTER TABLE ADD COLUMN IF NOT EXISTS | Preserves existing rows, idempotent | ✓ |
| DROP and recreate | Simpler but destroys existing data | |
| External migration scripts | Alembic-style versioning | |

**Auto-selected:** ALTER TABLE ADD COLUMN IF NOT EXISTS for existing table; CREATE TABLE IF NOT EXISTS for new tables. Added PRAGMA busy_timeout = 30000 to all connections.

---

## thesis.yaml Structure

| Option | Description | Selected |
|--------|-------------|----------|
| Minimal (name + description + keywords) | Simple, operator-friendly | ✓ |
| Complex (weights, priority, sub-pillars) | More expressive but harder to maintain | |

**Auto-selected:** Minimal schema — `review_due` (ISO date) + `pillars` list (name, description, keywords). Pydantic validates on load. ThesisStaleError propagates through heartbeat.

---

## Universe Data Source

| Option | Description | Selected |
|--------|-------------|----------|
| Static CSV in repo | Operator edits directly, no runtime dependency | ✓ |
| Fetched from Finnhub at runtime | Always current but adds API dependency | |
| Hardcoded in Python | Simple but not operator-maintainable | |

**Auto-selected:** `src/signal_system/data/universe.csv` with `ticker`, `core_holding`, `k1_etf` columns. thesis.yaml gitignored; universe.csv committed to repo.

---

## Config Additions

| Option | Description | Selected |
|--------|-------------|----------|
| Extend config.py with _optional() helper | Follows existing _require() pattern | ✓ |
| Separate config file | More structure but unnecessary complexity | |

**Auto-selected:** Add `_optional(name, default)` to config.py. ANTHROPIC_MODEL is required; THESIS_PATH and DISCOVERY_PHASE are optional with defaults.

---

## Claude's Discretion

- `models.py` location: `src/signal_system/models.py` at package root
- `dataclasses.dataclass(frozen=True)` vs Pydantic for Signal: use frozen dataclass (Pydantic already needed for thesis validation, can be used for Signal too if desired)
- Connection management: connection-per-operation pattern retained

## Deferred Ideas

- SQLite WAL checkpoint + weekly VACUUM cron → v2 (OPS-V2-01)
- Weekly backup script → v2
- Connection pool / context manager for DB connections → out of scope
