"""Keep the last few hundred log lines in memory so the UI's Status page can show them."""
from __future__ import annotations

import logging
from collections import deque


class LogRing(logging.Handler):
    def __init__(self, capacity: int = 400):
        super().__init__(level=logging.INFO)
        self.lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass

    def tail(self, n: int = 200) -> list[str]:
        return list(self.lines)[-n:]


RING = LogRing()


def install() -> LogRing:
    root = logging.getLogger()
    if RING not in root.handlers:
        root.addHandler(RING)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
    return RING
