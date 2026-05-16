import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from signal_system.models import Signal, compute_alert_id
from signal_system.monitoring import heartbeat
from signal_system.data import finnhub_client
from signal_system.state import repository
from signal_system.delivery import email_sender

logger = logging.getLogger(__name__)

def run() -> None:
    run_id = repository.insert_run("daily-close")
    try:
        with heartbeat.heartbeat():
            spy_close = finnhub_client.fetch_spy_close()

            now_et = datetime.now(ZoneInfo("America/New_York"))
            alert_id = compute_alert_id("SPY", now_et.date().isoformat(), "daily_close", "DAILY_CLOSE")
            signal = Signal(
                ticker="SPY",
                score=spy_close,
                severity="INFORMATIONAL",
                agent="DAILY_CLOSE",
                timestamp=now_et,
                alert_id=alert_id,
                title=f"SPY closed at {spy_close:.2f} (S&P 500 proxy)",
                body="Daily close captured at market close.",
            )
            repository.insert_signal(signal)

            email_sender.send_email(
                subject=f"Daily Close — SPY {spy_close:.2f}",
                body=f"SPY closed at {spy_close:.2f}\nAlert ID: {alert_id}",
            )
            repository.update_run(run_id, "success")  # inside heartbeat: DB failure trips /fail ping
    except Exception:
        repository.update_run(run_id, "failed")
        raise
