# Plan 05 — Retire the Wash-Sale Ledger

**Type:** Remove · **Priority:** P2 · **Est. effort:** ~0.5 day · **Coupled with Plan 04**

## Rationale

The `wash_sale` ledger is 0% populated and structurally unable to do its job:

- **Empty.** `SELECT COUNT(*) FROM wash_sale` → **0 rows**. No trade has ever been logged.
- **Depends on manual discipline that isn't happening.** It is only correct if the operator
  runs `log-trade` for **every** buy and sell across **all four** Schwab accounts, from day
  one. That discipline is absent, and a wash-sale tool that is 0% populated is *worse than
  none* — it implies cross-account coverage the operator does not actually have, which is a
  false-safety trap on a tax-compliance surface.
- **Redundant.** Schwab computes wash sales natively per account and provides realized
  gain/loss reporting. The home-grown ledger competes with a system of record the operator
  already trusts for execution.
- **Orphaned by Plan 04.** The ledger's only consumer is the Advisor's verdict engine
  (`wash_sale_caution` on a loss sale, `wash_sale_window` on a buy after a recent sell).
  Once the Advisor is removed, nothing reads the ledger at all.

## Decision framework (choose one)

This plan defaults to **Retire**, but records the fork explicitly because it is a
compliance-adjacent surface:

| Option | When it's right | Action |
|--------|-----------------|--------|
| **A. Retire (default)** | You will rely on Schwab for wash-sale tracking. | Remove `log-trade`, the `wash_sale` repo functions, and stop referencing the ledger. Leave the (empty) table or drop it. |
| **B. Commit** | You want in-system wash-sale flags and will wire an **automatic** trade feed (Schwab export import), not manual logging. | Keep the schema; replace manual `log-trade` with an importer; decouple from the Advisor so flags surface in the digest. This is a *new feature*, not part of this cleanup — spin a separate plan. |

Do **not** keep the status quo (manual `log-trade` feeding a now-deleted Advisor). That is
the worst of both: effort with no reader.

## What references the ledger

```
src/signal_system/state/repository.py   ← insert_wash_sale_trade, get_recent_trades (+ table in init_db)
src/signal_system/__main__.py           ← `log-trade` command: _parse_log_trade, WASH_SALE_WINDOW_DAYS import
src/signal_system/advisor/verdict_engine.py  ← consumer (removed in Plan 04)
src/signal_system/advisor/advisor_agent.py   ← consumer (removed in Plan 04)
src/signal_system/advisor/rationale.py       ← consumer (removed in Plan 04)
tests/test_wash_sale.py                 ← delete (Option A)
```

## Execution steps (Option A — Retire)

Do this **in the same PR as Plan 04** so no dangling references remain.

1. `__main__.py`:
   - Remove the `log-trade` command branch (`_parse_log_trade`, `_cmd_log_trade`) and its
     usage-banner text.
   - Remove the `WASH_SALE_WINDOW_DAYS` import (also removed by Plan 04).
2. `repository.py`:
   - Remove `insert_wash_sale_trade` and `get_recent_trades`.
   - In `init_db()`: stop creating the `wash_sale` table for fresh DBs. **Do not** drop it
     from existing DBs in a migration — leave the (empty) table in place to avoid destructive
     schema changes; it is inert and the weekly backup covers it. Add a code comment marking
     it deprecated.
3. Delete `tests/test_wash_sale.py`.
4. Docs: in `CLAUDE.md` remove the wash-sale ledger from "Known Risks" / architecture and the
   `log-trade` operator command; note in `roadmap.md` that wash-sale tracking is delegated to
   Schwab. Update `README.md` command list.
5. `uv run pytest` green; `grep -rn "wash_sale\|log-trade\|log_trade" src/` returns nothing.

## Data handling

- **Zero rows** means there is no data-loss consideration — nothing to migrate or preserve.
- Leave the empty table for backup/schema stability; a `DROP TABLE` migration is optional and
  higher-risk than the benefit. If a clean schema is desired later, drop it in a dedicated,
  tested migration.

## CLAUDE.md note to update

The current CLAUDE.md states the `wash_sale` table "must have `account` column from day one —
retrofitting across 4 accounts is painful" and lists wash-sale as a known risk. Replace with:
"Wash-sale tracking is delegated to Schwab (system of record per account). The in-system
ledger was retired 2026-08 (never populated; sole consumer was the removed Advisor)."

## Testing

- Suite green after removing `test_wash_sale.py`.
- `test_cli_commands.py` — remove/adjust cases asserting `log-trade`; assert it now routes to
  "Unknown command".
- `init_db()` on a fresh temp DB no longer creates `wash_sale` (Option A) — or, if you keep
  creation for compatibility, assert the table exists but is unreferenced. Pick one and test
  it.
- Import smoke after Plan 04+05: no references to `wash_sale` or `WASH_SALE_WINDOW_DAYS`.

## Risks & mitigations

- **Compliance perception** — removing a "wash-sale" feature sounds like dropping a control.
  Reality: the control was never operative (0 rows) and Schwab provides the real one.
  Document the delegation explicitly so intent is clear.
- **Future desire for in-system flags** → that is Option B, a deliberate new feature with an
  automated feed; this plan doesn't foreclose it, it just stops pretending the manual version
  works.
- **Coupling** — must land with Plan 04; landing 05 alone leaves the Advisor importing a
  deleted `get_recent_trades`.

## Acceptance criteria

- `grep -rn "wash_sale\|log-trade" src/` returns nothing (docs may reference the retirement).
- `uv run pytest` green.
- `CLAUDE.md` reflects Schwab as the wash-sale system of record.
