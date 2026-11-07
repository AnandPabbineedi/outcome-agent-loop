"""
Event Ingestor — idempotent, restartable, backlog-safe.

Reads event chunks from the simulator, deduplicates by event_id,
and feeds each event to the appropriate handler.
Tracks last processed chunk in the AuditStore for crash recovery.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from .audit import AuditStore
from .client import SimClient

log = logging.getLogger(__name__)

# Type alias for event handler
EventHandler = Callable[[dict, float], None]


class EventIngestor:
    """
    Fetches and processes event chunks in order.
    Idempotent: safe to call multiple times with the same chunks.
    """

    def __init__(self, client: SimClient, audit: AuditStore):
        self._client = client
        self._audit = audit
        self._last_event_id: int = audit.last_event_id()
        self._last_chunk: int = audit.last_chunk()
        self._handlers: list[EventHandler] = []

    def add_handler(self, fn: EventHandler) -> None:
        self._handlers.append(fn)

    def ingest_history(self) -> int:
        """Ingest pre-D0 history chunks. Returns total events processed."""
        chunks = self._client.list_history_chunks()
        total = 0
        for chunk_meta in sorted(chunks, key=lambda c: c["chunk"]):
            total += self._process_chunk(chunk_meta["chunk"])
        log.info("History ingest: %d events across %d chunks", total, len(chunks))
        return total

    def ingest_new_chunks(self, new_chunks: list[dict]) -> int:
        """Ingest chunks returned by /sim/advance. Returns events processed."""
        total = 0
        for chunk_meta in sorted(new_chunks, key=lambda c: c["chunk"]):
            total += self._process_chunk(chunk_meta["chunk"])
        return total

    def replay_missed_chunks(self) -> int:
        """
        On restart: find any chunks after last_chunk and process them.
        Called once at startup before the main loop.
        """
        all_chunks = self._client.list_chunks(kind="events", after=self._last_chunk)
        total = 0
        for chunk_meta in sorted(all_chunks, key=lambda c: c["chunk"]):
            total += self._process_chunk(chunk_meta["chunk"])
        if total:
            log.info("Replay: caught up %d missed events", total)
        return total

    def _process_chunk(self, chunk_n: int) -> int:
        """Process one chunk. Skips events already seen (by event_id)."""
        count = 0
        try:
            for event in self._client.get_chunk_lines(chunk_n):
                eid = event.get("event_id", -1)
                # Idempotency: skip events we've already processed
                if eid <= self._last_event_id:
                    continue
                ts_epoch = _parse_ts(event.get("ts", ""))
                for handler in self._handlers:
                    try:
                        handler(event, ts_epoch)
                    except Exception as e:
                        log.exception("Handler error on event %d: %s", eid, e)
                self._last_event_id = max(self._last_event_id, eid)
                count += 1
        except Exception as e:
            log.exception("Error processing chunk %d: %s", chunk_n, e)
            return count

        self._last_chunk = max(self._last_chunk, chunk_n)
        # Persist progress for crash recovery
        self._audit.save_last_event_id(self._last_event_id)
        self._audit.save_last_chunk(self._last_chunk)
        return count


def _parse_ts(ts_str: str) -> float:
    """Parse ISO 8601 timestamp to epoch seconds. Returns 0 on failure."""
    if not ts_str:
        return 0.0
    try:
        # Handle +05:30 offset
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except Exception:
        return 0.0
