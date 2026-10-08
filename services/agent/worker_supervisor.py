"""Supervise a fixed pool of single-Task Agent workers.

Each child owns its own AgentContainer and database pool. The Redis consumer
group distributes wake-ups; Task leases remain the final guard against two
children executing the same Task.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

SERVICE_ROOT = Path(__file__).resolve().parent
load_dotenv(SERVICE_ROOT / ".env", override=False)

from app.config import settings  # noqa: E402
from app.container import build_container  # noqa: E402

logger = logging.getLogger("agent.worker_supervisor")

_stop = False


def _handle_signal(signum, frame) -> None:  # noqa: ARG001 - signal handler signature
    global _stop
    _stop = True


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # One Task Worker may fan out several read-only steps. Six connections per
    # worker keeps four workers plus the API inside a predictable local budget.
    env.setdefault("AGENT_DATABASE_MAX_POOL_SIZE", "6")
    return env


def _spawn(index: int, env: dict[str, str]) -> subprocess.Popen:
    consumer_name = f"task-worker-{index}"
    logger.info("starting %s", consumer_name)
    return subprocess.Popen(
        [
            sys.executable,
            str(SERVICE_ROOT / "worker.py"),
            "--consumer-name",
            consumer_name,
            "--skip-recovery",
        ],
        cwd=str(SERVICE_ROOT),
        env=env,
    )


def main() -> None:
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    if not settings.redis_url:
        logger.error("AGENT_REDIS_URL is not configured; task workers cannot start")
        return

    count = settings.resolved_task_worker_processes
    env = _child_env()
    recovery_container = build_container(settings)
    try:
        try:
            recovered = recovery_container.execution_service.resume_unfinished_tasks()
        except Exception:
            # A shared database can briefly hit max_connections while the rest
            # of the stack is starting. That must not leave the supervisor
            # absent; the workers and Redis wake-ups can recover once a slot
            # becomes available.
            logger.exception(
                "initial unfinished-task recovery failed; starting workers anyway"
            )
        else:
            if recovered:
                logger.info(
                    "re-queued %s unfinished tasks once for all workers",
                    recovered,
                )
    finally:
        recovery_container.close()
    children = {index: _spawn(index, env) for index in range(1, count + 1)}

    while not _stop:
        time.sleep(1.0)
        for index, child in list(children.items()):
            code = child.poll()
            if code is None:
                continue
            if _stop:
                break
            logger.error("task-worker-%s exited with code %s; restarting", index, code)
            children[index] = _spawn(index, env)

    for child in children.values():
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 10.0
    for child in children.values():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            child.kill()


if __name__ == "__main__":
    main()
