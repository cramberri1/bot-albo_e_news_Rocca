"""Read-only diagnostics for checks performed by the current bot process.

These observations deliberately live only in memory: an old persisted attempt
timestamp cannot establish that a new process has completed a successful check.
Callers retain their existing locks and all responsibility for check execution.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar


_Result = TypeVar("_Result")
_COUNTERS = ("new", "updated", "failed", "total")


def _counter(value: Any) -> int | None:
    """Accept only bounded, nonnegative integer counters, never bools or text."""
    if type(value) is int and 0 <= value <= 2**63 - 1:
        return value
    return None


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return "non disponibile"
    return value.strftime("%d/%m/%Y %H:%M:%S UTC")


def _number(value: int | None) -> str:
    return str(value) if value is not None else "non disponibile"


class CheckHealth:
    """Observe checks without persisting data or changing their outcome.

    One check per source may run at a time, as enforced by the bot's existing
    source locks. Counts describe only the most recent attempt. A failed or
    cancelled attempt retains the previous successful timestamp, but never its
    counters. Exception messages are neither stored nor displayed.
    """

    def __init__(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        self._started_monotonic = time.monotonic()
        self.checks: dict[str, dict[str, Any]] = {
            name: {
                "status": "non ancora verificato",
                "last_attempt": None,
                "last_success": None,
                "duration_seconds": None,
                "started_monotonic": None,
                **dict.fromkeys(_COUNTERS),
            }
            for name in ("Albo", "News")
        }

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started_monotonic)

    async def run(
        self, name: str, check: Callable[[Any], Awaitable[_Result]], bot: Any
    ) -> _Result:
        state = self.checks[name]
        started = time.monotonic()
        state.update(
            status="in corso",
            last_attempt=datetime.now(timezone.utc),
            duration_seconds=None,
            started_monotonic=started,
            **dict.fromkeys(_COUNTERS),
        )
        try:
            result = await check(bot)
        except asyncio.CancelledError:
            state["status"] = "interrotto"
            raise
        except Exception:
            state["status"] = "errore"
            raise
        else:
            # The application returns plain dictionaries. Other return types
            # remain untouched, but cannot establish a successful check.
            details = result if type(result) is dict else {}
            succeeded = details.get("ok") is True
            state["status"] = "completato" if succeeded else "incompleto o bloccato"
            if succeeded:
                state["last_success"] = datetime.now(timezone.utc)
            for key in _COUNTERS:
                state[key] = _counter(details.get(key))
            return result
        finally:
            state["duration_seconds"] = max(0.0, time.monotonic() - started)
            state["started_monotonic"] = None

    def lines(self) -> list[str]:
        """Return bounded, plain-text lines with no stored exception details."""
        lines: list[str] = []
        for name, state in self.checks.items():
            if lines:
                lines.append("")
            lines.extend((
                f"{name}: {state['status']}",
                f"Ultimo tentativo: {_timestamp(state['last_attempt'])}",
                f"Ultimo successo: {_timestamp(state['last_success'])}",
            ))
            if state["status"] == "in corso":
                elapsed = max(0.0, time.monotonic() - state["started_monotonic"])
                duration = f"in corso ({elapsed:.1f} s)"
            elif state["duration_seconds"] is None:
                duration = "non disponibile"
            else:
                duration = f"{state['duration_seconds']:.1f} s"
            lines.append(f"Durata ciclo: {duration}")
            lines.append(f"Nuovi nel ciclo: {_number(state['new'])}")
            if name == "Albo":
                lines.append(f"Revisioni nel ciclo: {_number(state['updated'])}")
            lines.append(f"Consegne fallite nel ciclo: {_number(state['failed'])}")
        return lines
