import logging
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
            alert_id = repository.insert_signal(
                agent="DAILY_CLOSE",
                ticker="SPY",
                title=f"SPY closed at {spy_close:.2f} (S&P 500 proxy)",
                body="Daily close captured at market close.",
                score=spy_close,
            )
            email_sender.send_email(
                subject=f"Daily Close — SPY {spy_close:.2f}",
                body=f"SPY closed at {spy_close:.2f}\nAlert ID: {alert_id}",
            )
        repository.update_run(run_id, "success")
    except Exception:
        repository.update_run(run_id, "failed")
        raise
