# Stack Research — Investment Signal System

## Existing Stack (Locked — Do Not Change)

| Component | Choice | Status |
|-----------|--------|--------|
| Language | Python 3.12+ | Locked |
| Package manager | uv | Locked |
| State | SQLite (stdlib sqlite3, WAL mode) | Locked |
| LLM | Anthropic Claude API (pinned Sonnet) via `anthropic` SDK | Locked |
| Market data | Finnhub free-tier via `finnhub-python` | Locked |
| Delivery | Telegram via custom sender (`delivery/telegram_sender.py`) | Locked |
| Heartbeat | healthchecks.io | Locked |
| Runner | Windows Task Scheduler | Locked |
| DB access layer | `state/repository.py` — no raw SQL outside | Locked |

---

## What's Needed for the Next Milestone

### Structured Output — `messages.parse()` (not `instructor`)

**Recommendation:** Use Anthropic SDK's native `client.messages.parse(output_format=PydanticModel)` for structured classification output.

- Returns a typed `ParsedMessage` with `.parsed_output` (Pydantic instance)
- Auto-generates JSON schema from the Pydantic model — no manual boilerplate
- This is SDK-native as of the current `anthropic` SDK version; the older tool-use-with-forced-tool pattern is no longer necessary
- **Do NOT use `instructor`** — it wraps the same SDK calls with an extra dependency and is now redundant

**Confidence:** HIGH — verified against anthropic-sdk-python SDK source

### Rate Limiting — stdlib token bucket (not a library)

**Recommendation:** Implement a simple preemptive token bucket using `time.monotonic()` + `time.sleep()` in `finnhub_client.py`.

```python
# ~10 lines — correct for sequential one-shot jobs
_CALLS_PER_MINUTE = 55  # conservative headroom below 60 limit
_MIN_INTERVAL = 60.0 / _CALLS_PER_MINUTE

class RateLimitedFinnhubClient:
    def __init__(self): self._last_call = 0.0
    def _throttle(self):
        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        self._last_call = time.monotonic()
```

- At 500 tickers/day × 1-2 calls/ticker, the Discovery job takes 9–18 minutes wall time — acceptable
- **Do NOT use `pyrate-limiter`** — it solves concurrent-access problems that don't exist in sequential jobs
- **Do NOT use `asyncio`** — Windows event-loop policy quirks; sequential is correct

**Confidence:** HIGH

### Reactive 429 Handling — `tenacity`

**Recommendation:** Add `tenacity>=8.0` to `pyproject.toml` for Finnhub 429 retry and Claude API transient errors.

**Critical finding:** `finnhub-python` SDK does NOT expose rate-limit headers. The `_handle_response()` method raises `FinnhubAPIException` on non-OK responses but discards the raw `requests.Response` object. `Retry-After` and `X-RateLimit-Remaining` headers are not accessible. Reactive handling must detect by `exc.status_code == 429`.

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

@retry(
    retry=retry_if_exception(lambda e: getattr(e, "status_code", None) == 429),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(5)
)
def _finnhub_call(...): ...
```

**Confidence:** HIGH — verified against tenacity docs

### Prompt Caching — thesis.yaml as cached system prompt

**Recommendation:** Pass thesis.yaml content as a system prompt with `cache_control: {"type": "ephemeral"}` to cache across headlines within a single news-morning job run.

```python
system=[
    {"type": "text", "text": thesis_content,
     "cache_control": {"type": "ephemeral"}}
]
```

- Log `cache_read_input_tokens` and `cache_creation_input_tokens` from `response.usage` to the `llm_calls` table per run
- Token telemetry is free — `ParsedMessage.usage` exposes all four token counts
- Minimum token threshold for cache activation: validate against current Anthropic pricing docs at implementation time (training data says ~1,024 tokens; a real thesis.yaml will likely exceed it)

**Confidence:** HIGH for syntax; MEDIUM for exact minimum token threshold — verify at implementation

### Alert Router — Custom domain logic (not a library)

**Recommendation:** Build a custom router with a staging-table pattern in `routing/alert_router.py`.

Pattern:
1. Agents write to `candidate_signals` staging table
2. Router runs in `BEGIN EXCLUSIVE` transaction
3. Selects top-N by `score DESC, candidate_id ASC` (deterministic tiebreak)
4. Writes final disposition to `signals` with `routing_status` column
5. `candidate_id` must be a content-hash (not UUID) for deterministic rerun behavior

**No library exists for this pattern** — do not look for one.

**Confidence:** HIGH

---

## Dependency Delta

Add to `pyproject.toml` (dev dependencies already updated — pytest is present):

```toml
dependencies = [
    # existing...
    "tenacity>=8.0",
]
```

**Everything else is already present or uses stdlib.**

---

## What NOT to Use

| Rejected | Reason |
|----------|--------|
| `instructor` | SDK now has `messages.parse()` natively; extra dep, no benefit |
| `LangChain`, `CrewAI`, `AutoGen`, `LlamaIndex` | Frameworks own agent lifecycle; conflicts with heartbeat/dispatcher design |
| `asyncio` / `aiohttp` | Windows event-loop policy quirks; sequential jobs don't need concurrency |
| `Celery`, `RQ`, `APScheduler` | Task Scheduler is the orchestrator; no daemons |
| `SQLAlchemy`, `peewee` | Explicitly excluded by CLAUDE.md; `repository.py` is the access layer |
| `pyrate-limiter`, `ratelimit` | Solve concurrent-access problems absent in sequential jobs |
| `Redis` | No network services; SQLite is the shared state |
| `httpx` | `finnhub-python` wraps `requests`; mixing HTTP clients creates confusion |

---

## New SQLite Tables Needed

All schema changes go through `repository.py`. Tables needed before any agent code:

| Table | Purpose |
|-------|---------|
| `candidate_signals` | Router staging; cleared after each routing pass |
| `daily_budget` | Tracks slots used per day per severity; query target for router |
| `wash_sale` | Wash sale tracking with `account` column (4 accounts — day one) |
| `llm_calls` | Token telemetry: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` |

Schema columns to add to existing `signals` table:

| Column | Purpose |
|--------|---------|
| `routing_status` | DELIVERED / MONITORING / SUPPRESSED — router sets this, never severity |
| `model_version` | Pinned model ID string for IC comparability |
| `thesis_version_hash` | SHA256 of thesis.yaml at classification time |
| `signal_price_snapshot` | Price at signal generation (unadjusted) for outcome measurement |
| `weight_version` | Stamp on Discovery signals for IC interpretability after weight changes |

---

## Open Questions

- Exact minimum token threshold for Anthropic prompt caching (verify against current docs at implementation)
- Which Finnhub free-tier endpoints are available for the 35/30/25/10 Discovery scoring weights — validate empirically before writing scoring code
- Whether `messages.parse()` supports `temperature=0.0` alongside `output_format` (likely yes — verify at implementation; classification calls must be deterministic)
- Does `/stock/candle` support `adjusted=False` on Finnhub free tier? (needed for outcome backfill)
