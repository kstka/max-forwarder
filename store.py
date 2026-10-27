"""JSON-backed persistent store of forwarded message ids per MAX channel.

The store answers two questions:
- Have we already recorded this channel?
- Have we already forwarded this specific message id?

Writes are atomic (write to a temp file in the same directory then ``os.replace``).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from loguru import logger

import config


class ForwardedStore:
    """Per-channel set of forwarded message ids, persisted to a JSON file."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path or config.FORWARDED_STORE_PATH)
        self._lock = RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            if not isinstance(raw, dict):
                raise ValueError("root must be an object")
            self._data = raw
            channels = len(self._data)
            total = sum(len(v.get("ids", [])) for v in self._data.values())
            logger.info(
                "Forwarded store loaded: {} channel(s), {} ids total ({})",
                channels, total, self._path,
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "Corrupt forwarded store at {} ({}); starting empty",
                self._path, e,
            )
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".forwarded.", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(self._data, tmp, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self._path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def has_channel(self, channel: str) -> bool:
        with self._lock:
            return channel in self._data

    def get_known_ids(self, channel: str) -> set[str]:
        with self._lock:
            return set(self._data.get(channel, {}).get("ids", []))

    def is_forwarded(self, channel: str, message_id: str) -> bool:
        with self._lock:
            return message_id in self.get_known_ids(channel)

    def mark_forwarded(
        self,
        channel: str,
        message_id: str,
        last_seen_at: str | None = None,
    ) -> None:
        with self._lock:
            entry = self._data.setdefault(channel, {"ids": [], "last_seen_at": None})
            if message_id not in entry["ids"]:
                entry["ids"].append(message_id)
            entry["last_seen_at"] = (
                last_seen_at
                or datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            self._save()

    def mark_many(self, channel: str, message_ids: list[str]) -> None:
        """Bulk-register ids without touching ``last_seen_at`` more than once."""
        if not message_ids:
            return
        with self._lock:
            entry = self._data.setdefault(channel, {"ids": [], "last_seen_at": None})
            existing = set(entry["ids"])
            for mid in message_ids:
                if mid not in existing:
                    entry["ids"].append(mid)
                    existing.add(mid)
            entry["last_seen_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            self._save()

    def get_anchor(self, channel: str) -> str | None:
        """Id of the newest message the monitor has seen for this
        channel (the tail anchor used to tell live appends apart from
        already-seen history). None when never established."""
        with self._lock:
            return self._data.get(channel, {}).get("anchor_id")

    def set_anchor(self, channel: str, message_id: str | None) -> None:
        with self._lock:
            entry = self._data.setdefault(
                channel, {"ids": [], "last_seen_at": None}
            )
            if entry.get("anchor_id") != message_id:
                entry["anchor_id"] = message_id
                self._save()
