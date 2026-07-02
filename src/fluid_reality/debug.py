"""Debug output helpers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, TextIO

DebugDestination = None | Path | TextIO | Callable[[str], None]


class DebugOut:
    """Configurable debug text sink.

    The default destination is ``None``, which drops all debug messages.
    Supported destinations are:

    - ``None``: disable debug output
    - ``Path``: append to that file
    - file-like object with ``write``
    - callback accepting one formatted debug line, for example ``print``
    """

    def __init__(self, destination: DebugDestination = None) -> None:
        self._destination: DebugDestination = None
        self._owned_file: TextIO | None = None
        self.set_destination(destination)

    def set_destination(self, destination: DebugDestination) -> None:
        if isinstance(destination, Path):
            destination.parent.mkdir(parents=True, exist_ok=True)
            new_owned_file: TextIO | None = destination.open("a", encoding="utf-8")
        elif destination is not None and not callable(destination):
            write = getattr(destination, "write", None)
            if write is None:
                raise TypeError(
                    "debug destination must be None, a Path, file, or callback"
                )
            new_owned_file = None
        else:
            new_owned_file = None

        self.close()
        self._destination = destination
        self._owned_file = new_owned_file

    def close(self) -> None:
        if self._owned_file is not None:
            self._owned_file.close()
            self._owned_file = None

    def emit(self, component: str, event: str, **fields: object) -> None:
        line = self.format(component, event, **fields)
        self.write(line)

    def write(self, line: str) -> None:
        destination = self._destination
        if destination is None:
            return
        if callable(destination):
            destination(line)
            return
        handle = self._owned_file if self._owned_file is not None else destination
        write = getattr(handle, "write", None)
        if write is None:
            raise TypeError("debug destination must be None, a Path, file, or callback")
        write(line + "\n")
        flush = getattr(handle, "flush", None)
        if flush is not None:
            flush()

    @staticmethod
    def format(component: str, event: str, **fields: object) -> str:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = [timestamp, component, event]
        for key, value in fields.items():
            parts.append(f"{key}={value!r}")
        return " | ".join(parts)
