import logging
import sys

from signal_system.state import repository
from signal_system.jobs.daily_close import run as run_daily_close
from signal_system.jobs.discovery import run as run_discovery
from signal_system.jobs.news_morning import run as run_news_morning
from signal_system.jobs.advisor import run as run_advisor, advise_ticker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s -- %(message)s",
)

JOBS = {
    "daily-close": run_daily_close,
    "discovery": run_discovery,
    "news-morning": run_news_morning,
    "advisor": run_advisor,
}

if __name__ == "__main__":
    repository.init_db()
    if len(sys.argv) < 2:
        print(f"Usage: python -m signal_system <job>  or  python -m signal_system advise <TICKER>")
        print(f"Available jobs: {list(JOBS)}")
        sys.exit(2)

    cmd = sys.argv[1]

    if cmd == "advise":
        if len(sys.argv) < 3:
            print("Usage: python -m signal_system advise <TICKER>")
            sys.exit(2)
        advise_ticker(sys.argv[2])
    elif cmd in JOBS:
        JOBS[cmd]()
    else:
        print(f"Unknown command: {cmd!r}")
        print(f"Available jobs: {list(JOBS)}  or  advise <TICKER>")
        sys.exit(2)
