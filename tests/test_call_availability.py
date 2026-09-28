import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from call_availability import (
    AvailabilityConfigurationError,
    CallAvailabilityConfig,
    evaluate_call_availability,
)


class CallAvailabilityPolicyTests(unittest.TestCase):
    def config(self, **values):
        settings = {
            "CALL_OPERATING_TIMEZONE": "America/Mexico_City",
            "CALL_ALLOWED_WEEKDAYS": "monday,tuesday,wednesday,thursday,friday",
            "CALL_OPERATING_START": "09:00",
            "CALL_OPERATING_END": "17:00",
        }
        settings.update(values)
        with patch.dict(os.environ, settings, clear=False):
            return CallAvailabilityConfig.from_env()

    def test_defaults_are_weekdays_and_commercial_hours_in_mexico_city(self):
        relevant = (
            "CALL_OPERATING_TIMEZONE", "CALL_ALLOWED_WEEKDAYS",
            "CALL_OPERATING_START", "CALL_OPERATING_END",
        )
        with patch.dict(os.environ, {}, clear=False):
            original = {name: os.environ.pop(name, None) for name in relevant}
            try:
                config = CallAvailabilityConfig.from_env()
            finally:
                for name, value in original.items():
                    if value is not None:
                        os.environ[name] = value
        self.assertEqual(config.timezone_name, "America/Mexico_City")
        self.assertEqual(config.allowed_weekdays, (0, 1, 2, 3, 4))
        self.assertEqual(config.start_time.isoformat(timespec="minutes"), "09:00")
        self.assertEqual(config.end_time.isoformat(timespec="minutes"), "17:00")

    def test_allowed_window_has_inclusive_open_and_exclusive_close(self):
        config = self.config()
        # 2026-09-28 is a Monday in Mexico City (UTC-06).
        before_open = datetime(2026, 9, 28, 14, 59, tzinfo=timezone.utc)
        at_open = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        before_close = datetime(2026, 9, 28, 22, 59, tzinfo=timezone.utc)
        at_close = datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(evaluate_call_availability(config, before_open).reason, "outside_operating_hours")
        self.assertTrue(evaluate_call_availability(config, at_open).allowed)
        self.assertTrue(evaluate_call_availability(config, before_close).allowed)
        self.assertEqual(evaluate_call_availability(config, at_close).reason, "outside_operating_hours")

    def test_weekends_are_blocked_even_during_window(self):
        config = self.config()
        saturday = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)
        decision = evaluate_call_availability(config, saturday)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "outside_allowed_day")
        self.assertEqual(decision.weekday, "saturday")

    def test_allowed_weekdays_are_configurable(self):
        config = self.config(CALL_ALLOWED_WEEKDAYS="saturday,sunday")
        saturday = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)
        self.assertTrue(evaluate_call_availability(config, saturday).allowed)

    def test_timezone_conversion_uses_configured_zone(self):
        config = self.config(CALL_OPERATING_TIMEZONE="UTC")
        instant = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
        decision = evaluate_call_availability(config, instant)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.timezone_name, "UTC")
        self.assertIn("09:00:00+00:00", decision.local_time)

    def test_zoneinfo_handles_daylight_saving_fall_back_fold(self):
        config = self.config(
            CALL_OPERATING_TIMEZONE="America/New_York",
            CALL_ALLOWED_WEEKDAYS="sunday",
            CALL_OPERATING_START="01:00",
            CALL_OPERATING_END="02:00",
        )
        first_0130 = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        second_0130 = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
        self.assertTrue(evaluate_call_availability(config, first_0130).allowed)
        self.assertTrue(evaluate_call_availability(config, second_0130).allowed)

    def test_configuration_fails_closed_for_invalid_zone_days_and_times(self):
        invalid_cases = (
            {"CALL_OPERATING_TIMEZONE": "Invalid/Zone"},
            {"CALL_ALLOWED_WEEKDAYS": "monday,funday"},
            {"CALL_ALLOWED_WEEKDAYS": "monday,monday"},
            {"CALL_OPERATING_START": "9am"},
            {"CALL_OPERATING_END": "25:00"},
            {"CALL_OPERATING_START": "17:00", "CALL_OPERATING_END": "09:00"},
        )
        for override in invalid_cases:
            with self.subTest(override=tuple(override)):
                with patch.dict(os.environ, {
                    "CALL_OPERATING_TIMEZONE": "America/Mexico_City",
                    "CALL_ALLOWED_WEEKDAYS": "monday,tuesday,wednesday,thursday,friday",
                    "CALL_OPERATING_START": "09:00",
                    "CALL_OPERATING_END": "17:00",
                    **override,
                }, clear=False):
                    with self.assertRaises(AvailabilityConfigurationError):
                        CallAvailabilityConfig.from_env()

    def test_naive_evaluation_time_is_rejected(self):
        config = self.config()
        decision = evaluate_call_availability(config, datetime(2026, 9, 28, 10, 0))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "invalid_evaluation_time")


if __name__ == "__main__":
    unittest.main()
