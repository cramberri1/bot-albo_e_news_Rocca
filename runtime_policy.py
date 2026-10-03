"""Pure operational policy; trigger names never determine current behaviour."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
import re
from typing import Mapping
from zoneinfo import ZoneInfo


MAX_RUNTIME_SECONDS = 18_000
JOB_TIMEOUT_SECONDS = 355 * 60
SHUTDOWN_GRACE_SECONDS = 120
SETUP_BUDGET_SECONDS = 18 * 60
FINALIZATION_BUDGET_SECONDS = 9 * 60


def as_utc(instant: datetime) -> datetime:
    """Reject ambiguous naive inputs instead of assuming the host's timezone."""
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError("Serve un istante datetime con fuso orario.")
    return instant.astimezone(timezone.utc)


def _minute(label: str) -> int:
    if not isinstance(label, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", label):
        raise ValueError("Gli orari automatici devono avere formato HH:MM (00:00–23:59).")
    hour, minute = map(int, label.split(":"))
    return hour * 60 + minute


@dataclass(frozen=True)
class RuntimePolicy:
    """Daily half-open window [start, end), evaluated in Europe/Rome.

    Windows crossing midnight are supported. Equal endpoints are rejected:
    they would otherwise ambiguously mean either no polling or all-day polling.
    A missing timezone database is a configuration error, never a UTC fallback.
    """

    start_label: str = "07:00"
    end_label: str = "23:00"
    interval_minutes: int = 15
    timezone_name: str = "Europe/Rome"
    _zone: ZoneInfo = field(init=False, repr=False, compare=False)
    _start: int = field(init=False, repr=False)
    _end: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        start, end = _minute(self.start_label), _minute(self.end_label)
        if start == end:
            raise ValueError("Inizio e fine della fascia automatica devono essere diversi.")
        if type(self.interval_minutes) is not int or not 1 <= self.interval_minutes <= 1440:
            raise ValueError("AUTO_POLL_INTERVAL_MINUTES deve essere un intero tra 1 e 1440.")
        object.__setattr__(self, "_start", start)
        object.__setattr__(self, "_end", end)
        object.__setattr__(self, "_zone", ZoneInfo(self.timezone_name))

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "RuntimePolicy":
        values = os.environ if environ is None else environ
        raw_interval = values.get("AUTO_POLL_INTERVAL_MINUTES", "15")
        if not isinstance(raw_interval, str) or not re.fullmatch(r"[0-9]+", raw_interval):
            raise ValueError("AUTO_POLL_INTERVAL_MINUTES deve essere un intero positivo.")
        return cls(
            start_label=values.get("AUTO_POLL_START", "07:00"),
            end_label=values.get("AUTO_POLL_END", "23:00"),
            interval_minutes=int(raw_interval),
        )

    def allows(self, instant: datetime) -> bool:
        local = as_utc(instant).astimezone(self._zone)
        minute = local.hour * 60 + local.minute
        if self._start < self._end:
            return self._start <= minute < self._end
        return minute >= self._start or minute < self._end

    def next_transition(self, instant: datetime) -> datetime:
        """Return the next actual UTC instant at which permission changes.

        Minute scanning is bounded and intentionally uses UTC: constructing a
        local boundary directly invents nonexistent spring times and can miss
        transitions inside the repeated autumn hour. Forty-nine hours also
        covers a short daily window skipped entirely by the spring transition.
        """
        current = as_utc(instant)
        allowed = self.allows(current)
        candidate = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(49 * 60):
            if self.allows(candidate) != allowed:
                return candidate
            candidate += timedelta(minutes=1)
        raise RuntimeError("Nessuna transizione della policy trovata entro 49 ore.")
