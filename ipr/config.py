"""Settings and clocks.

The demo uses a fixed, advanceable clock so the fixture's publication timeline
(section 12 example: cutoff 03:59:59Z, knowledge-complete 04:00Z, publication
04:05Z, run start 04:10Z) is reproducible. Durations are always measured with
a monotonic timer, independently of the business clock.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .canonical import parse_ts

DEMO_NOW = "2026-09-16T04:10:00Z"


class Clock:
    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError

    def now_z(self) -> str:
        return self.now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """Business clock that advances with real elapsed time from a fixed origin."""

    def __init__(self, origin: str | datetime = DEMO_NOW, frozen: bool = False):
        self._origin = parse_ts(origin) if isinstance(origin, str) else origin
        self._offset = timedelta(0)
        self._frozen = frozen
        self._started = datetime.now(timezone.utc)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            elapsed = timedelta(0) if self._frozen else datetime.now(timezone.utc) - self._started
            return self._origin + self._offset + elapsed

    def advance(self, delta: timedelta) -> None:
        with self._lock:
            self._offset += delta

    def set(self, when: str | datetime) -> None:
        when_dt = parse_ts(when) if isinstance(when, str) else when
        with self._lock:
            self._origin = when_dt
            self._offset = timedelta(0)
            self._started = datetime.now(timezone.utc)


@dataclass
class Settings:
    home: Path = field(default_factory=lambda: Path(os.environ.get("IPR_HOME", ".ipr_data")).resolve())
    workers: int = int(os.environ.get("IPR_WORKERS", "4"))
    lease_seconds: float = 30.0
    heartbeat_seconds: float = 0.5
    poll_seconds: float = 0.02
    fixture_seed: int = 20260916
    fixture_submissions: int = 2400
    engine: str = os.environ.get("IPR_ENGINE", "sqlite")
    max_plan_revisions: int = 3
    stage_ttl_hours: int = 24
    cache_ttl_days: int = 7
    clock: Clock = field(default_factory=FixedClock)

    @property
    def control_db(self) -> Path:
        return self.home / "control.db"

    @property
    def data_db(self) -> Path:
        return self.home / "data.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @property
    def feedback_dir(self) -> Path:
        return self.home / "feedback"

    @property
    def recipes_dir(self) -> Path:
        return self.home / "recipes"

    def ensure_dirs(self) -> None:
        for p in (self.home, self.artifacts_dir, self.feedback_dir, self.recipes_dir / "drafts"):
            p.mkdir(parents=True, exist_ok=True)
