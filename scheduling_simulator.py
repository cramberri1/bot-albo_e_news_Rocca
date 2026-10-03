"""Deterministic, offline model of workflow handovers and application policy.

This predicts structural availability, not network success or handler latency.
The single pending slot models the repository's concurrency configuration.
Arrivals precede releases at identical instants; GitHub does not promise that
tie order. No result is an uptime guarantee for the real provider.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import heapq
from itertools import count
from zoneinfo import ZoneInfo

from runtime_policy import (
    JOB_TIMEOUT_SECONDS, MAX_RUNTIME_SECONDS, SHUTDOWN_GRACE_SECONDS, SETUP_BUDGET_SECONDS,
    RuntimePolicy, as_utc,
)


LEGACY_CRONS = ((6, 57), (12, 37), (18, 17), (0, 7), (3, 37))
NIGHT_CRONS = {"7 0 * * *", "37 3 * * *"}


def _seconds(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} deve essere un intero non negativo.")


@dataclass(frozen=True)
class Trigger:
    run_id: str
    nominal_at: datetime
    delay_seconds: int = 0
    setup_seconds: int = 0
    runtime_seconds: int = MAX_RUNTIME_SECONDS
    cleanup_seconds: int = 0
    shutdown_seconds: int = 0
    runner_wait_seconds: int = 0
    crash_after_seconds: int | None = None
    event_name: str = "schedule"
    nominal_cron: str = "17 * * * *"

    def __post_init__(self) -> None:
        as_utc(self.nominal_at)
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id deve essere non vuoto.")
        for name in ("delay_seconds", "setup_seconds", "runtime_seconds", "cleanup_seconds",
                     "shutdown_seconds", "runner_wait_seconds"):
            _seconds(getattr(self, name), name)
        if self.crash_after_seconds is not None:
            _seconds(self.crash_after_seconds, "crash_after_seconds")
        if self.event_name not in {"schedule", "workflow_dispatch"}:
            raise ValueError("Tipo di trigger non supportato.")

    @property
    def arrived_at(self) -> datetime:
        return as_utc(self.nominal_at) + timedelta(seconds=self.delay_seconds)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class ProfileSpan(Interval):
    run_id: str
    automatic_enabled: bool
    interval_minutes: int | None


@dataclass(frozen=True)
class Run:
    trigger: Trigger
    admitted_at: datetime
    job_started_at: datetime
    bot_started_at: datetime | None
    bot_stopped_at: datetime | None
    released_at: datetime
    outcome: str

    @property
    def queue_seconds(self) -> float:
        return (self.admitted_at - self.trigger.arrived_at).total_seconds()


@dataclass(frozen=True)
class QueueEvent:
    at: datetime
    run_id: str
    replaced_run_id: str | None


@dataclass(frozen=True)
class Simulation:
    runs: tuple[Run, ...]
    telegram: tuple[Interval, ...]
    profiles: tuple[ProfileSpan, ...]
    automatic: tuple[Interval, ...]
    gaps: tuple[Interval, ...]
    queued: tuple[str, ...]
    replaced: tuple[str, ...]
    pending: str | None
    queue_events: tuple[QueueEvent, ...]


def _legacy_settings(trigger: Trigger) -> tuple[int, int]:
    if trigger.event_name == "workflow_dispatch":
        return 600, 15
    if trigger.nominal_cron in NIGHT_CRONS:
        return 3300, 30
    return 20580, 15


def _merge(intervals: list[Interval]) -> tuple[Interval, ...]:
    merged: list[Interval] = []
    for current in sorted(intervals, key=lambda span: span.start):
        if merged and current.start <= merged[-1].end:
            merged[-1] = Interval(merged[-1].start, max(current.end, merged[-1].end))
        else:
            merged.append(current)
    return tuple(merged)


def simulate(
    triggers: list[Trigger], horizon_start: datetime, horizon_end: datetime,
    policy: RuntimePolicy | None = None, *, strategy: str = "actual_time",
    job_timeout_seconds: int = JOB_TIMEOUT_SECONDS,
    shutdown_grace_seconds: int = SHUTDOWN_GRACE_SECONDS,
    setup_cap_seconds: int | None = None,
    concurrency: str = "single_pending", cancel_in_progress: bool = False,
) -> Simulation:
    """Simulate the configured single-writer, latest-pending-wins policy.

    Supply earlier triggers as warm-up when modelling an already-running bot.
    A finite horizon is not assumed to start with an active predecessor.
    Actual-time runs have fixed lifetimes and a conservative aggregate setup
    cap (the deployed individual step caps can fail earlier). Impossible custom
    budgets are refused, not silently shortened. Legacy runs have no setup cap
    by default and can hit the hard job deadline, losing finalization time.
    """
    start, end = as_utc(horizon_start), as_utc(horizon_end)
    if end <= start:
        raise ValueError("L'orizzonte deve avere durata positiva.")
    if strategy not in {"actual_time", "legacy_by_event"}:
        raise ValueError("Strategia sconosciuta.")
    if concurrency != "single_pending" or cancel_in_progress:
        raise ValueError("Il modello supporta single pending senza cancellare l'attivo.")
    for value, name in ((job_timeout_seconds, "job_timeout_seconds"),
                        (shutdown_grace_seconds, "shutdown_grace_seconds")):
        _seconds(value, name)
    if setup_cap_seconds is not None:
        _seconds(setup_cap_seconds, "setup_cap_seconds")
    elif strategy == "actual_time":
        setup_cap_seconds = SETUP_BUDGET_SECONDS
    if len({trigger.run_id for trigger in triggers}) != len(triggers):
        raise ValueError("Gli identificativi delle run devono essere unici.")
    policy = policy or RuntimePolicy()
    serial = count()
    events = [(t.arrived_at, 0, next(serial), t) for t in triggers]
    heapq.heapify(events)
    runs: list[Run] = []
    queued: list[str] = []
    replaced: list[str] = []
    queue_events: list[QueueEvent] = []
    active: Run | None = None
    pending: Trigger | None = None

    def admit(trigger: Trigger, admitted_at: datetime) -> Run:
        job_start = admitted_at + timedelta(seconds=trigger.runner_wait_seconds)
        deadline = job_start + timedelta(seconds=job_timeout_seconds)
        setup = trigger.setup_seconds
        outcome = "completed"
        bot_start = bot_stop = None
        if setup_cap_seconds is not None and setup > setup_cap_seconds:
            release = min(deadline, job_start + timedelta(
                seconds=setup_cap_seconds + trigger.cleanup_seconds))
            outcome = "setup_timeout"
        else:
            requested = _legacy_settings(trigger)[0] if strategy == "legacy_by_event" else trigger.runtime_seconds
            required = setup + requested + shutdown_grace_seconds + trigger.cleanup_seconds
            if strategy == "actual_time" and required > job_timeout_seconds:
                release = min(deadline, job_start + timedelta(seconds=setup + trigger.cleanup_seconds))
                outcome = "budget_refused"
            elif setup >= job_timeout_seconds:
                release = deadline
                outcome = "job_timeout"
            else:
                duration = requested
                if trigger.crash_after_seconds is not None and trigger.crash_after_seconds < duration:
                    duration = trigger.crash_after_seconds
                    outcome = "crashed"
                bot_start = job_start + timedelta(seconds=setup)
                bot_stop = bot_start + timedelta(seconds=duration)
                # A too-slow shutdown is forcibly stopped at the reserved grace.
                shutdown = min(trigger.shutdown_seconds, shutdown_grace_seconds)
                if trigger.shutdown_seconds > shutdown_grace_seconds:
                    outcome = "shutdown_killed"
                release = bot_stop + timedelta(seconds=shutdown + trigger.cleanup_seconds)
                if release > deadline:
                    bot_stop = min(bot_stop, deadline)
                    release = deadline
                    outcome = "job_timeout"
        result = Run(trigger, admitted_at, job_start, bot_start, bot_stop, release, outcome)
        runs.append(result)
        heapq.heappush(events, (release, 1, next(serial), result))
        return result

    while events:
        instant, kind, _, event = heapq.heappop(events)
        if instant >= end:
            break
        if kind == 0:
            if active is None:
                active = admit(event, instant)
            else:
                queued.append(event.run_id)
                queue_events.append(QueueEvent(instant, event.run_id, pending.run_id if pending else None))
                if pending is not None:
                    replaced.append(pending.run_id)
                pending = event
        else:
            active = None
            if pending is not None:
                active = admit(pending, instant)
                pending = None

    telegram: list[Interval] = []
    profiles: list[ProfileSpan] = []
    for run in runs:
        if run.bot_started_at is None:
            continue
        cursor = max(start, run.bot_started_at)
        stop = min(end, run.bot_stopped_at)
        if cursor >= stop:
            continue
        telegram.append(Interval(cursor, stop))
        while cursor < stop:
            if strategy == "legacy_by_event":
                boundary, allowed, interval = stop, True, _legacy_settings(run.trigger)[1]
            else:
                boundary = min(stop, policy.next_transition(cursor))
                allowed = policy.allows(cursor)
                interval = policy.interval_minutes if allowed else None
            profiles.append(ProfileSpan(cursor, boundary, run.trigger.run_id, allowed, interval))
            cursor = boundary
    coverage = _merge(telegram)
    gaps: list[Interval] = []
    cursor = start
    for span in coverage:
        if cursor < span.start:
            gaps.append(Interval(cursor, span.start))
        cursor = span.end
    if cursor < end:
        gaps.append(Interval(cursor, end))
    automatic = _merge([Interval(p.start, p.end) for p in profiles if p.automatic_enabled])
    return Simulation(tuple(runs), coverage, tuple(profiles), automatic, tuple(gaps),
                      tuple(queued), tuple(replaced), pending.run_id if pending else None, tuple(queue_events))


def scheduled_triggers(
    start: datetime, end: datetime, *, schedule: str = "hourly",
    delay_seconds: int = 0, setup_seconds: int = 0,
    cleanup_seconds: int = 0, ambiguous: str = "both",
) -> list[Trigger]:
    """Generate local nominal times, not predictions of provider delivery.

    Spring-gap times advance to the first valid local minute, as documented by
    GitHub. The autumn repeated hour is explicit scenario input: 'both' stresses
    duplicate triggers, while 'first'/'second' model either single occurrence.
    GitHub's ordering/occurrence in that repeated hour is not assumed guaranteed.
    """
    begin, finish = as_utc(start), as_utc(end)
    if finish <= begin or schedule not in {"hourly", "legacy"}:
        raise ValueError("Orizzonte o calendario non valido.")
    if ambiguous not in {"both", "first", "second"}:
        raise ValueError("Politica per l'ora ripetuta non valida.")
    zone = ZoneInfo("Europe/Rome")
    times = [(hour, 17) for hour in range(24)] if schedule == "hourly" else LEGACY_CRONS
    day = begin.astimezone(zone).date()
    last_day = finish.astimezone(zone).date()
    result = []
    while day <= last_day:
        for hour, minute in times:
            naive = datetime(day.year, day.month, day.day, hour, minute)
            candidate = naive
            while True:
                valid = sorted({
                    candidate.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
                    for fold in (0, 1)
                    if candidate.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
                    .astimezone(zone).replace(tzinfo=None) == candidate
                })
                if valid:
                    break
                candidate += timedelta(minutes=1)
            if ambiguous == "first":
                valid = valid[:1]
            elif ambiguous == "second":
                valid = valid[-1:]
            for instant in valid:
                if begin <= instant < finish:
                    cron = "17 * * * *" if schedule == "hourly" else f"{minute} {hour} * * *"
                    result.append(Trigger(
                        run_id=f"{schedule}-{naive.isoformat()}-{instant.isoformat()}",
                        nominal_at=instant, delay_seconds=delay_seconds,
                        setup_seconds=setup_seconds, cleanup_seconds=cleanup_seconds,
                        nominal_cron=cron,
                    ))
        day += timedelta(days=1)
    return sorted(result, key=lambda trigger: trigger.nominal_at)
