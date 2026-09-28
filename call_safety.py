"""Fail-closed call admission and in-process abuse limits.

The manager is intentionally isolated so a shared Redis/DB implementation can
replace it without changing the HTTP or media-stream handlers.
"""

import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


DEFAULT_MAX_CALL_DURATION_SECONDS = 240
DEFAULT_ALLOWED_DESTINATION_COUNTRIES = ("+52", "+1")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_FORMATTED_E164 = re.compile(r"^\+[1-9][0-9 ()-]*$")


class SafetyConfigurationError(ValueError):
    pass


class CallAdmissionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CallSafetyConfig:
    enabled: bool
    max_concurrent: int
    max_per_hour: int
    max_per_day: int
    max_duration_seconds: int
    allowed_prefixes: tuple

    @classmethod
    def from_env(cls):
        raw_enabled = os.getenv("CALLS_ENABLED", "false")
        if raw_enabled not in {"true", "false"}:
            raise SafetyConfigurationError("CALLS_ENABLED")
        enabled = raw_enabled == "true"
        required = (
            "CALL_SECRET", "OPENAI_API_KEY", "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
        )
        if enabled and any(not os.getenv(name, "").strip() for name in required):
            enabled = False
        if enabled:
            account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
            caller_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
            if not re.fullmatch(r"AC[0-9a-fA-F]{32}", account_sid):
                raise SafetyConfigurationError("TWILIO_ACCOUNT_SID")
            if not _E164.fullmatch(caller_number):
                raise SafetyConfigurationError("TWILIO_PHONE_NUMBER")

        def positive_int(name, default, maximum=None):
            value = os.getenv(name)
            if value is None:
                parsed = default
            elif not re.fullmatch(r"[1-9]\d*", value):
                raise SafetyConfigurationError(name)
            else:
                parsed = int(value)
            if parsed <= 0 or (maximum is not None and parsed > maximum):
                raise SafetyConfigurationError(name)
            return parsed

        max_duration = positive_int(
            "MAX_CALL_DURATION_SECONDS", DEFAULT_MAX_CALL_DURATION_SECONDS, 240
        )
        raw_prefixes = os.getenv(
            "ALLOWED_DESTINATION_COUNTRIES", "+52,+1"
        )
        prefixes = tuple(
            prefix.strip() for prefix in raw_prefixes.split(",") if prefix.strip()
        )
        if (
            not prefixes
            or any(
                not re.fullmatch(r"\+[1-9]\d{0,2}", prefix)
                for prefix in prefixes
            )
            or len(set(prefixes)) != len(prefixes)
        ):
            raise SafetyConfigurationError("ALLOWED_DESTINATION_COUNTRIES")

        return cls(
            enabled=enabled,
            max_concurrent=positive_int("MAX_CONCURRENT_CALLS", 1),
            max_per_hour=positive_int("MAX_CALLS_PER_HOUR", 5),
            max_per_day=positive_int("MAX_CALLS_PER_DAY", 20),
            max_duration_seconds=max_duration,
            allowed_prefixes=prefixes,
        )


def normalize_destination(value, allowed_prefixes):
    """Normalize a clearly international number and enforce configured prefixes."""
    if not isinstance(value, str) or not _FORMATTED_E164.fullmatch(value.strip()):
        raise CallAdmissionError("invalid_destination")
    normalized = re.sub(r"[ ()-]", "", value.strip())
    if not _E164.fullmatch(normalized):
        raise CallAdmissionError("invalid_destination")

    matched_prefix = next(
        (prefix for prefix in sorted(allowed_prefixes, key=len, reverse=True)
         if normalized.startswith(prefix)),
        None,
    )
    if matched_prefix is None:
        raise CallAdmissionError("destination_not_allowed")

    digits = normalized[1:]
    national_number = digits[len(matched_prefix) - 1:]
    if matched_prefix == "+1":
        if (
            len(digits) != 11
            or national_number[0] not in "23456789"
            or national_number[3] not in "23456789"
        ):
            raise CallAdmissionError("invalid_destination")
    if matched_prefix == "+52":
        if len(digits) != 12 or national_number[0] not in "23456789":
            raise CallAdmissionError("invalid_destination")
    return normalized


@dataclass
class CallRecord:
    reservation_id: str
    language: str
    mission_id: str = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    call_sid: str = None
    stream_started: bool = False
    report_written: bool = False
    terminal_status: str = None


class InMemoryCallManager:
    """Single-process call limits; replace with shared storage for >1 replica."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._active = {}
        self._records = {}
        self._by_sid = {}
        self._attempts = deque()
        self._terminal_unmatched = {}

    @property
    def active_count(self):
        with self._lock:
            return len(self._active)

    def reserve(self, config, language, mission=None):
        now = self._clock()
        with self._lock:
            while self._attempts and now - self._attempts[0] >= 86400:
                self._attempts.popleft()
            stale_ids = [
                reservation_id
                for reservation_id, record in self._active.items()
                if now - record.created_at > config.max_duration_seconds + 30
            ]
            for reservation_id in stale_ids:
                stale = self._active.pop(reservation_id)
                if stale.call_sid:
                    self._by_sid.pop(stale.call_sid, None)
            old_ids = [
                reservation_id
                for reservation_id, record in self._records.items()
                if now - record.created_at >= 86400
            ]
            for reservation_id in old_ids:
                old = self._records.pop(reservation_id)
                if old.call_sid:
                    self._by_sid.pop(old.call_sid, None)
            if len(self._active) >= config.max_concurrent:
                raise CallAdmissionError("concurrent_limit")
            hourly_count = sum(
                now - started < 3600 for started in self._attempts
            )
            if hourly_count >= config.max_per_hour:
                raise CallAdmissionError("hourly_limit")
            if len(self._attempts) >= config.max_per_day:
                raise CallAdmissionError("daily_limit")

            record = CallRecord(
                uuid.uuid4().hex, language,
                mission_id=getattr(mission, "id", None), created_at=now,
            )
            self._active[record.reservation_id] = record
            self._records[record.reservation_id] = record
            self._attempts.append(now)
            return record

    def bind_call_sid(self, reservation_id, call_sid):
        with self._lock:
            record = self._active.get(reservation_id)
            if record is None:
                return None
            record.call_sid = call_sid
            if call_sid in self._terminal_unmatched:
                record.terminal_status = self._terminal_unmatched.pop(call_sid)
                self._active.pop(reservation_id, None)
            else:
                self._by_sid[call_sid] = reservation_id
            return record

    def admit_media_stream(self, call_sid, config, language, mission=None):
        with self._lock:
            reservation_id = self._by_sid.get(call_sid)
            if reservation_id:
                record = self._active.get(reservation_id)
                if record:
                    if record.terminal_status:
                        raise CallAdmissionError("call_already_ended")
                    record.stream_started = True
                    return record
        record = self.reserve(config, language, mission)
        self.bind_call_sid(record.reservation_id, call_sid)
        with self._lock:
            if record.terminal_status:
                raise CallAdmissionError("call_already_ended")
            record.stream_started = True
        return record

    def release(self, reservation_id=None, call_sid=None, terminal_status=None):
        with self._lock:
            if reservation_id is None and call_sid is not None:
                reservation_id = self._by_sid.pop(call_sid, None)
                if reservation_id is None:
                    if len(self._terminal_unmatched) >= 1000:
                        oldest_sid = next(iter(self._terminal_unmatched))
                        self._terminal_unmatched.pop(oldest_sid)
                    self._terminal_unmatched[call_sid] = terminal_status or "unknown"
                    return None
            record = self._records.get(reservation_id)
            if record and terminal_status:
                record.terminal_status = terminal_status
            self._active.pop(reservation_id, None)
            if record and record.call_sid:
                self._by_sid.pop(record.call_sid, None)
            return record

    def mark_terminal(self, call_sid, terminal_status):
        with self._lock:
            reservation_id = self._by_sid.get(call_sid)
            record = self._records.get(reservation_id) if reservation_id else None
            if record:
                record.terminal_status = terminal_status
            return record

    def claim_report(self, reservation_id=None, call_sid=None):
        with self._lock:
            reservation = reservation_id or self._by_sid.get(call_sid)
            record = self._records.get(reservation) if reservation else None
            if record is None:
                return False
            if record.report_written:
                return False
            record.report_written = True
            self._records.pop(reservation, None)
            self._active.pop(reservation, None)
            if record.call_sid:
                self._by_sid.pop(record.call_sid, None)
            return True

    def has_stream(self, call_sid):
        with self._lock:
            reservation = self._by_sid.get(call_sid)
            record = self._records.get(reservation) if reservation else None
            return bool(record and record.stream_started)

    def get_record(self, call_sid):
        with self._lock:
            reservation = self._by_sid.get(call_sid)
            return self._records.get(reservation) if reservation else None
