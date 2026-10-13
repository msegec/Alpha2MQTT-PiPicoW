"""One JSON line per controller step, for tuning. A background thread does the file writes, and rotation caps
disk use at (backups + 1) x max_bytes. No Home Assistant imports, so it can be tested alone."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import re


def file_name(base_topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", base_topic.lower()).strip("_") + ".jsonl"


class StepLog:
    def __init__(self, path: str, max_bytes: int = 10_000_000, backups: int = 2) -> None:
        # Opens the file: call it off the event loop.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._handler = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups)
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._listener = logging.handlers.QueueListener(self._queue, self._handler)
        self._listener.start()

    def write(self, record: dict) -> None:
        self._queue.put(logging.makeLogRecord({"msg": json.dumps(record, separators=(",", ":"))}))

    def close(self) -> None:
        # Drains the queue and closes the file: call it off the event loop.
        self._listener.stop()
        self._handler.close()
