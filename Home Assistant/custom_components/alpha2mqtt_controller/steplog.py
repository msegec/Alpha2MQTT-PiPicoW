"""One JSON line per record, for tuning and replay. A background thread does the file writes. At local
midnight the day's file is gzipped to <name>.YYYY-MM-DD.gz and the oldest beyond `days` are deleted, so disk
use is bounded by age and count. No Home Assistant imports, so it can be tested alone."""

from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil


def file_name(base_topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", base_topic.lower()).strip("_") + ".jsonl"


def _gzip(source: str, dest: str) -> None:
    # Through a temporary name, so a gzip cut short by a restart is done again rather than taken as done.
    with open(source, "rb") as f, gzip.open(dest + ".tmp", "wb") as g:
        shutil.copyfileobj(f, g)
    os.replace(dest + ".tmp", dest)
    os.remove(source)


class StepLog:
    def __init__(self, path: str, days: int = 400) -> None:
        # Opens the file: call it off the event loop. A file left from an earlier day rolls over on the first write.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._handler = logging.handlers.TimedRotatingFileHandler(path, when="midnight", backupCount=days)
        self._handler.namer = lambda name: name + ".gz"
        self._handler.rotator = _gzip
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._listener = logging.handlers.QueueListener(self._queue, self._handler)
        self._listener.start()

    def write(self, record: dict) -> None:
        # Entity attributes may hold datetimes, which go in as text.
        self._queue.put(logging.makeLogRecord({"msg": json.dumps(record, separators=(",", ":"), default=str)}))

    def close(self) -> None:
        # Drains the queue and closes the file: call it off the event loop.
        self._listener.stop()
        self._handler.close()
