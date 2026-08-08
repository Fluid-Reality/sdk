"""Structured, out-of-band simulator event logging."""

from __future__ import annotations

import json
import hashlib
import dataclasses
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SimulatorConfig, resolved_path


class EventLogger:
    """Write one JSON object per event without touching protocol output."""

    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.settings = config.logging
        self.session_id = str(uuid.uuid4())
        hash_data = dataclasses.asdict(config)
        hash_data.pop("source_path", None)
        encoded = json.dumps(hash_data, sort_keys=True, default=str).encode("utf-8")
        self.config_hash = hashlib.sha256(encoded).hexdigest()
        self.path = resolved_path(config, self.settings.file)
        if self.settings.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, *, monotonic_ms: int, **fields: Any) -> None:
        if not self.settings.enabled and not self.settings.console:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "monotonic_ms": monotonic_ms,
            "session_id": self.session_id,
            "event": event,
            **fields,
        }
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        if self.settings.enabled:
            self._rotate_if_needed(len(line) + 1)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        if self.settings.console:
            print(line, file=sys.stderr, flush=True)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if self.settings.max_bytes <= 0 or not self.path.exists():
            return
        if self.path.stat().st_size + incoming_bytes <= self.settings.max_bytes:
            return
        for index in range(self.settings.backup_count, 0, -1):
            source = self.path.with_suffix(self.path.suffix + f".{index}")
            target = self.path.with_suffix(self.path.suffix + f".{index + 1}")
            if index == self.settings.backup_count and source.exists():
                source.unlink()
            elif source.exists():
                source.replace(target)
        if self.settings.backup_count > 0:
            self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        else:
            self.path.unlink()


class NullEventLogger:
    session_id = "disabled"
    config_hash = "disabled"

    def emit(self, event: str, *, monotonic_ms: int, **fields: Any) -> None:
        del event, monotonic_ms, fields
