import logging
import sys

from signal_system.state import repository
from signal_system.jobs.daily_close import run as run_daily_close
from signal_system.jobs.news_morning import run as run_news_morning

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

JOBS = {
    "daily-close": run_daily_close,
    "news-morning": run_news_morning,
}

if __name__ == "__main__":
    repository.init_db()
    if len(sys.argv) < 2 or sys.argv[1] not in JOBS:
        print(f"Usage: python -m signal_system <job>. Available: {list(JOBS)}")
        sys.exit(2)
    JOBS[sys.argv[1]]()
