"""Fail-closed commercial calling window policy using IANA time zones."""

from dataclasses import dataclass
from datetime import datetime, time, timezone
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Defaults mirror .env.example; deployments can override every value by env.
DEFAULT_TIMEZONE = "America/Mexico_City"
DEFAULT_ALLOWED_WEEKDAYS = "monday,tuesday,wednesday,thursday,friday"
DEFAULT_START_TIME = "09:00"
DEFAULT_END_TIME = "17:00"

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
WEEKDAY_LABELS = {number: name for name, number in WEEKDAYS.items()}
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AvailabilityConfigurationError(ValueError):
    """Calling-window settings are invalid or unavailable."""


@dataclass(frozen=True)
class CallAvailabilityConfig:
    timezone_name: str
    allowed_weekdays: tuple
    start_time: time
    end_time: time

    @classmethod
    def from_env(cls):
        timezone_name = os.getenv("CALL_OPERATING_TIMEZONE", DEFAULT_TIMEZONE).strip()
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise AvailabilityConfigurationError("CALL_OPERATING_TIMEZONE") from None

        raw_days = os.getenv("CALL_ALLOWED_WEEKDAYS", DEFAULT_ALLOWED_WEEKDAYS)
        day_names = tuple(value.strip().lower() for value in raw_days.split(","))
        if (
            not day_names
            or any(day not in WEEKDAYS for day in day_names)
            or len(set(day_names)) != len(day_names)
        ):
            raise AvailabilityConfigurationError("CALL_ALLOWED_WEEKDAYS")

        start_time = _parse_clock(
            os.getenv("CALL_OPERATING_START", DEFAULT_START_TIME),
            "CALL_OPERATING_START",
        )
        end_time = _parse_clock(
            os.getenv("CALL_OPERATING_END", DEFAULT_END_TIME),
            "CALL_OPERATING_END",
        )
        if start_time >= end_time:
            raise AvailabilityConfigurationError("CALL_OPERATING_END")
        return cls(
            timezone_name=timezone_name,
            allowed_weekdays=tuple(WEEKDAYS[day] for day in day_names),
            start_time=start_time,
            end_time=end_time,
        )


def _parse_clock(value, variable):
    if not isinstance(value, str) or not _CLOCK.fullmatch(value):
        raise AvailabilityConfigurationError(variable)
    return time.fromisoformat(value)


@dataclass(frozen=True)
class CallAvailabilityDecision:
    allowed: bool
    reason: str
    timezone_name: str = None
    local_time: str = None
    weekday: str = None
    window_start: str = None
    window_end: str = None

    def to_dict(self):
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "timezone": self.timezone_name,
            "local_time": self.local_time,
            "weekday": self.weekday,
            "window": {
                "start": self.window_start,
                "end": self.window_end,
            } if self.window_start and self.window_end else None,
        }


def evaluate_call_availability(config=None, now=None):
    """Check the local weekday and half-open [start, end) window.

    ``now`` should be timezone-aware; UTC is used when omitted. Converting an
    instant through ``zoneinfo`` handles the selected zone's DST transitions.
    """
    try:
        config = config or CallAvailabilityConfig.from_env()
        if not isinstance(config, CallAvailabilityConfig):
            raise AvailabilityConfigurationError("configuration")
        zone = ZoneInfo(config.timezone_name)
    except (AvailabilityConfigurationError, ZoneInfoNotFoundError, ValueError):
        return CallAvailabilityDecision(
            False, "invalid_operating_hours_configuration"
        )

    instant = now or datetime.now(timezone.utc)
    if (
        not isinstance(instant, datetime)
        or instant.tzinfo is None
        or instant.utcoffset() is None
    ):
        return CallAvailabilityDecision(False, "invalid_evaluation_time")
    local = instant.astimezone(zone)
    common = {
        "timezone_name": config.timezone_name,
        "local_time": local.isoformat(),
        "weekday": WEEKDAY_LABELS[local.weekday()],
        "window_start": config.start_time.isoformat(timespec="minutes"),
        "window_end": config.end_time.isoformat(timespec="minutes"),
    }
    if local.weekday() not in config.allowed_weekdays:
        return CallAvailabilityDecision(False, "outside_allowed_day", **common)
    local_clock = local.timetz().replace(tzinfo=None)
    if not config.start_time <= local_clock < config.end_time:
        return CallAvailabilityDecision(False, "outside_operating_hours", **common)
    return CallAvailabilityDecision(True, "within_operating_hours", **common)
