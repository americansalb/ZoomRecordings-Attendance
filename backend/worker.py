"""
RQ worker entry point.

Run as a separate process when REDIS_URL is set. Picks up upload jobs
from the queue and runs them via services.upload_worker.run_upload_job.

Usage:
    REDIS_URL=redis://... python worker.py
    # or via Render: startCommand: cd backend && python worker.py
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("upload-worker")


def main() -> int:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        logger.error("REDIS_URL is not set; the RQ worker requires Redis.")
        return 1

    try:
        from redis import Redis
        from rq import Connection, Queue, Worker
    except ImportError as e:
        logger.error(f"rq/redis not installed: {e}")
        return 1

    # Confirm Google + Zoom envs are visible to the worker too. These are
    # read lazily by services.drive_service / services.zoom_service when a
    # job runs; if they're missing the job will fail loudly with a clear
    # error in the job store.
    for name in ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET",
                 "GOOGLE_CLIENT_EMAIL", "GOOGLE_PRIVATE_KEY"):
        logger.info(f"  {name}: {'SET' if os.getenv(name) else 'MISSING'}")

    queue_name = os.getenv("RQ_QUEUE", "uploads")
    redis_conn = Redis.from_url(redis_url)
    logger.info(f"Connecting to Redis at {redis_url.split('@')[-1]}, queue={queue_name}")

    # Touch the job store on startup so the SQLite path is created if the
    # worker is being run in single-container mode by mistake.
    from services.job_store import get_job_store, using_redis  # noqa
    get_job_store()
    if not using_redis():
        logger.warning(
            "Job store fell back to SQLite; the worker won't share state "
            "with the web service. Check REDIS_URL connectivity."
        )

    with Connection(redis_conn):
        Worker([Queue(queue_name)]).work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
