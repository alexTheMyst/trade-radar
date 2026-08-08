import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system.monitoring import heartbeat
from signal_system.data import finnhub_client
from signal_system.state import repository
from signal_system.delivery import telegram_sender

logger = logging.getLogger(__name__)

def run() -> None:
    run_id = repository.insert_run("daily-close")
    try:
        with heartbeat.heartbeat():
            spy_close = finnhub_client.fetch_spy_close()

            now_et = datetime.now(ZoneInfo("America/New_York"))
            repository.upsert_benchmark_price(
                ticker="SPY",
                date_iso=now_et.date().isoformat(),
                close=spy_close,
                source="finnhub_quote",
            )

            telegram_sender.send_message(
                f"Daily Close — SPY {spy_close:.2f}\n\nSPY closed at {spy_close:.2f}"
            )
            repository.update_run(run_id, "success")  # inside heartbeat: DB failure trips /fail ping
    except Exception:
        repository.update_run(run_id, "failed")
        raise
