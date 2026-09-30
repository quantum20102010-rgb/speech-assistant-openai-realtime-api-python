import asyncio
import base64
import json
import math
import os
import threading
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from requests.exceptions import Timeout
from call_availability import CallAvailabilityDecision
from starlette.requests import Request
from twilio.base.exceptions import TwilioRestException
from call_results import LocalFileCallResultStorage
from call_safety import CallSafetyConfig, InMemoryCallManager

_previous_openai_key = os.environ.get("OPENAI_API_KEY")
os.environ["OPENAI_API_KEY"] = "local-test-placeholder"
import main
if _previous_openai_key is None:
    os.environ.pop("OPENAI_API_KEY", None)
else:
    os.environ["OPENAI_API_KEY"] = _previous_openai_key


TEST_SECRET = "local-test-placeholder"
TEST_NUMBER = "+12125550100"


class HealthEndpointTests(unittest.TestCase):
    def test_health_returns_ok(self):
        response = asyncio.run(main.health())
        self.assertEqual(response, {"status": "ok"})


def make_request(payload=None, headers=()):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/make-call",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json"), *headers],
            "server": ("test-domain.up.railway.app", 443),
            "scheme": "https",
        },
        receive=receive,
    )


def encode_ulaw_sample(sample):
    if sample < 0:
        sample = -sample
        mask = 0x7F
    else:
        mask = 0xFF

    sample = min(sample, 32635) + 0x84
    segment = 0
    segment_limits = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)
    while segment < len(segment_limits) - 1 and sample > segment_limits[segment]:
        segment += 1
    encoded = (segment << 4) | ((sample >> (segment + 3)) & 0x0F)
    return encoded ^ mask


def make_tone_payload():
    pcmu = bytes(
        encode_ulaw_sample(
            int(12000 * math.sin(2 * math.pi * 440 * index / 8000))
        )
        for index in range(160)
    )
    return base64.b64encode(pcmu).decode("ascii")


class MakeCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.result_directory = tempfile.TemporaryDirectory()
        main.call_result_storage = LocalFileCallResultStorage(self.result_directory.name)
        main.call_manager = InMemoryCallManager()
        main.make_call_idempotency.clear()
        self.saved_environment = {
            key: os.environ.get(key)
            for key in (
                "CALL_SECRET", "CALLS_ENABLED", "MAX_CONCURRENT_CALLS",
                "MAX_CALLS_PER_HOUR", "MAX_CALLS_PER_DAY",
                "MAX_CALL_DURATION_SECONDS", "ALLOWED_DESTINATION_COUNTRIES",
                "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
                "TWILIO_MIN_BALANCE_USD", "RAILWAY_PUBLIC_DOMAIN", "OPENAI_API_KEY",
            )
        }
        os.environ["CALL_SECRET"] = TEST_SECRET
        os.environ["CALLS_ENABLED"] = "true"
        os.environ["MAX_CONCURRENT_CALLS"] = "1"
        os.environ["MAX_CALLS_PER_HOUR"] = "5"
        os.environ["MAX_CALLS_PER_DAY"] = "20"
        os.environ["MAX_CALL_DURATION_SECONDS"] = "240"
        os.environ["ALLOWED_DESTINATION_COUNTRIES"] = "+52,+1"
        os.environ["TWILIO_ACCOUNT_SID"] = "AC" + ("a" * 32)
        os.environ["TWILIO_AUTH_TOKEN"] = "a" * 32
        os.environ["TWILIO_PHONE_NUMBER"] = "+12125550199"
        os.environ["TWILIO_MIN_BALANCE_USD"] = "5.00"
        os.environ["OPENAI_API_KEY"] = "local-test-openai-key"
        os.environ["RAILWAY_PUBLIC_DOMAIN"] = "test-domain.up.railway.app"
        self.readiness_mock = AsyncMock(return_value=None)
        self.readiness_patcher = patch(
            "main.check_provider_readiness", new=self.readiness_mock
        )
        self.readiness_patcher.start()
        self.availability_patcher = patch(
            "main.evaluate_call_availability",
            return_value=CallAvailabilityDecision(
                True, "within_operating_hours", "America/Mexico_City",
                "2026-09-28T10:00:00-06:00", "monday", "09:00", "17:00",
            ),
        )
        self.availability_patcher.start()

    def tearDown(self):
        self.readiness_patcher.stop()
        self.availability_patcher.stop()
        for key, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.result_directory.cleanup()

    async def test_authorization_is_required_before_twilio(self):
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"idempotency-key", b"unauthorized-request")],
        )
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(TEST_NUMBER.encode(), response.body)
        self.assertNotIn(TEST_SECRET.encode(), response.body)
        twilio_client.assert_not_called()

    async def test_idempotency_key_replays_success_without_second_reservation_or_call(self):
        headers = [
            (b"x-call-secret", TEST_SECRET.encode()),
            (b"idempotency-key", b"retry-call-001"),
        ]
        first_request = make_request({"to": TEST_NUMBER, "language": "spanish"}, headers)
        retry_request = make_request({"to": TEST_NUMBER, "language": "spanish"}, headers)
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.return_value.sid = "CA_TEST"
            first_response = await main.make_call(first_request)
            reservation_count = main.call_manager.active_count
            retry_response = await main.make_call(retry_request)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(retry_response.status_code, 200)
        self.assertEqual(first_response.body, retry_response.body)
        self.assertEqual(reservation_count, 1)
        self.assertEqual(main.call_manager.active_count, 1)
        twilio_client.return_value.calls.create.assert_called_once()

    async def test_reusing_idempotency_key_with_changed_parameters_is_rejected(self):
        headers = [
            (b"x-call-secret", TEST_SECRET.encode()),
            (b"idempotency-key", b"retry-call-002"),
        ]
        first_request = make_request({"to": TEST_NUMBER}, headers)
        changed_request = make_request({"to": "+12125550101"}, headers)
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.return_value.sid = "CA_TEST"
            first_response = await main.make_call(first_request)
            changed_response = await main.make_call(changed_request)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(changed_response.status_code, 409)
        self.assertEqual(main.call_manager.active_count, 1)
        twilio_client.return_value.calls.create.assert_called_once()

    async def test_different_idempotency_key_still_obeys_call_limits(self):
        auth = (b"x-call-secret", TEST_SECRET.encode())
        first_request = make_request(
            {"to": TEST_NUMBER}, [auth, (b"idempotency-key", b"distinct-call-001")]
        )
        second_request = make_request(
            {"to": TEST_NUMBER}, [auth, (b"idempotency-key", b"distinct-call-002")]
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.return_value.sid = "CA_TEST"
            first_response = await main.make_call(first_request)
            second_response = await main.make_call(second_request)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 429)
        self.assertEqual(main.call_manager.active_count, 1)
        twilio_client.return_value.calls.create.assert_called_once()

    async def test_missing_idempotency_key_preserves_legacy_behavior(self):
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.return_value.sid = "CA_TEST"
            response = await main.make_call(request)
        self.assertEqual(response.status_code, 200)
        twilio_client.return_value.calls.create.assert_called_once()

    async def test_security_rejection_does_not_reserve_idempotency_key(self):
        headers = [
            (b"x-call-secret", TEST_SECRET.encode()),
            (b"idempotency-key", b"retry-after-policy-rejection"),
        ]
        request = make_request({"to": TEST_NUMBER}, headers)
        main.evaluate_call_availability.return_value = CallAvailabilityDecision(
            False, "outside_operating_hours", "America/Mexico_City",
            "2026-09-28T20:00:00-06:00", "monday", "09:00", "17:00",
        )
        with patch("twilio.rest.Client") as twilio_client:
            rejected = await main.make_call(request)
            main.evaluate_call_availability.return_value = CallAvailabilityDecision(
                True, "within_operating_hours", "America/Mexico_City",
                "2026-09-28T10:00:00-06:00", "monday", "09:00", "17:00",
            )
            twilio_client.return_value.calls.create.return_value.sid = "CA_TEST"
            accepted = await main.make_call(
                make_request({"to": TEST_NUMBER}, headers)
            )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 200)
        twilio_client.return_value.calls.create.assert_called_once()
        self.assertEqual(main.call_manager.active_count, 1)

    async def test_incorrect_authorization_is_rejected_before_twilio(self):
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", b"incorrect-local-placeholder")],
        )
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(TEST_NUMBER.encode(), response.body)
        twilio_client.assert_not_called()

    async def test_missing_destination_returns_400_after_authorization(self):
        request = make_request(
            {},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 400)
        twilio_client.assert_not_called()

    async def test_success_uses_mocked_twilio_and_does_not_expose_number(self):
        request = make_request(
            {"to": TEST_NUMBER, "language": "spanish"},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        loop_thread = threading.get_ident()
        call_thread = []

        def create_call(**kwargs):
            call_thread.append(threading.get_ident())
            return SimpleNamespace(sid="CA_TEST")

        output = StringIO()
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.side_effect = create_call
            with redirect_stdout(output):
                response = await main.make_call(request)

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "call_created")
        self.assertNotIn("to", payload)
        self.assertNotIn(TEST_NUMBER, response.body.decode())
        self.assertNotIn(TEST_SECRET, response.body.decode())
        self.assertNotIn(TEST_NUMBER, output.getvalue())
        self.assertNotIn(TEST_SECRET, output.getvalue())
        self.assertNotEqual(call_thread[0], loop_thread)

        client_kwargs = twilio_client.call_args.kwargs
        self.assertEqual(client_kwargs["http_client"].timeout, main.TWILIO_HTTP_TIMEOUT)
        twilio_client.return_value.calls.create.assert_called_once_with(
            to=TEST_NUMBER,
            from_="+12125550199",
            url=("https://test-domain.up.railway.app/outbound-call?"
                 "language=spanish&mission=supplier_outreach"),
            status_callback="https://test-domain.up.railway.app/call-status",
            status_callback_method="POST",
            status_callback_event=["completed"],
        )

    async def test_selected_mission_is_validated_before_twilio_and_forwarded(self):
        request = make_request(
            {"to": TEST_NUMBER, "language": "english", "mission": "market_research"},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.return_value.sid = "CA_TEST"
            response = await main.make_call(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["mission"], "market_research")
        self.assertEqual(
            twilio_client.return_value.calls.create.call_args.kwargs["url"],
            "https://test-domain.up.railway.app/outbound-call?"
            "language=english&mission=market_research",
        )

        invalid = make_request(
            {"to": TEST_NUMBER, "mission": "not-configured"},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            rejected = await main.make_call(invalid)
        self.assertEqual(rejected.status_code, 400)
        twilio_client.assert_not_called()

    async def test_calls_disabled_fails_closed_before_twilio(self):
        os.environ["CALLS_ENABLED"] = "false"
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body), {"error": "Outbound calls are disabled."})
        twilio_client.assert_not_called()

    async def test_provider_readiness_failure_blocks_call_before_twilio(self):
        from dependency_health import DependencyCheck, DependencyStatus
        self.readiness_patcher.stop()
        self.readiness_patcher = patch(
            "main.check_provider_readiness",
            new=AsyncMock(return_value=DependencyCheck(
                "twilio", DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED,
                "non_positive_balance", "Review billing.", latch=True,
            )),
        )
        self.readiness_patcher.start()
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.body)
        self.assertEqual(body["dependency_status"], "quota_or_balance_exhausted")
        self.assertNotIn(TEST_NUMBER, response.body.decode())
        twilio_client.assert_not_called()

    async def test_twilio_balance_failure_returns_safe_reason_code_and_blocks_call(self):
        from dependency_health import DependencyCheck, DependencyStatus

        for check, reason_code, expected_error in (
            (
                DependencyCheck(
                    "twilio", DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED,
                    "twilio_balance_below_minimum", "Review balance.", latch=True,
                ),
                "twilio_balance_below_minimum",
                "Twilio balance is below the configured minimum; the call was blocked.",
            ),
            (
                DependencyCheck(
                    "twilio", DependencyStatus.UNKNOWN,
                    "twilio_balance_unknown", "Review balance.", latch=True,
                ),
                "twilio_balance_unknown",
                "Twilio balance could not be verified safely; the call was blocked.",
            ),
            (
                DependencyCheck(
                    "twilio", DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED,
                    "non_positive_balance", "Review balance.", latch=True,
                ),
                "twilio_balance_non_positive",
                "Twilio balance is not positive; the call was blocked.",
            ),
        ):
            self.readiness_mock.return_value = check
            request = make_request(
                {"to": TEST_NUMBER},
                headers=[(b"x-call-secret", TEST_SECRET.encode())],
            )
            with patch("twilio.rest.Client") as twilio_client:
                response = await main.make_call(request)

            body = json.loads(response.body)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(body["reason_code"], reason_code)
            self.assertEqual(body["error"], expected_error)
            self.assertNotIn(os.environ["TWILIO_AUTH_TOKEN"], response.body.decode())
            self.assertNotIn(os.environ["OPENAI_API_KEY"], response.body.decode())
            self.assertEqual(main.call_manager.active_count, 0)
            twilio_client.assert_not_called()

    async def test_outside_operating_window_is_recorded_before_provider_checks(self):
        self.availability_patcher.stop()
        self.availability_patcher = patch(
            "main.evaluate_call_availability",
            return_value=CallAvailabilityDecision(
                False, "outside_allowed_day", "America/Mexico_City",
                "2026-09-26T10:00:00-06:00", "saturday", "09:00", "17:00",
            ),
        )
        self.availability_patcher.start()
        request = make_request(
            {"to": TEST_NUMBER, "language": "spanish"},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(payload["availability"]["reason"], "outside_allowed_day")
        self.assertFalse(payload["availability"]["allowed"])
        self.assertNotIn(TEST_NUMBER, response.body.decode())
        self.readiness_mock.assert_not_awaited()
        self.assertEqual(main.call_manager.active_count, 0)
        twilio_client.assert_not_called()
        saved = next(Path(self.result_directory.name).glob("call-*.json"))
        result = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(result["availability"]["reason"], "outside_allowed_day")
        self.assertNotIn(TEST_NUMBER, saved.read_text(encoding="utf-8"))
        report_text = saved.with_suffix(".txt").read_text(encoding="utf-8")
        self.assertIn("outside_allowed_day", report_text)

    async def test_dependency_health_requires_auth_and_does_not_probe(self):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request({
            "type": "http", "method": "GET", "path": "/health/dependencies",
            "query_string": b"", "headers": [(b"x-call-secret", TEST_SECRET.encode())],
            "server": ("test-domain.up.railway.app", 443), "scheme": "https",
        }, receive=receive)
        with patch("main.check_provider_readiness", new=AsyncMock()) as probe:
            response = await main.dependency_status(request)
        self.assertEqual(response.status_code, 200)
        health = json.loads(response.body)
        self.assertEqual(set(health["dependencies"]), {"twilio", "openai"})
        self.assertTrue(all("status" in state for state in health["dependencies"].values()))
        probe.assert_not_awaited()

        unauthorized = Request({
            "type": "http", "method": "GET", "path": "/health/dependencies",
            "query_string": b"", "headers": [],
            "server": ("test-domain.up.railway.app", 443), "scheme": "https",
        }, receive=receive)
        with patch("main.check_provider_readiness", new=AsyncMock()) as probe:
            rejected = await main.dependency_status(unauthorized)
        self.assertEqual(rejected.status_code, 401)
        self.assertNotIn(TEST_SECRET, rejected.body.decode())
        self.assertNotIn(TEST_NUMBER, rejected.body.decode())
        probe.assert_not_awaited()

    async def test_limits_are_checked_before_schedule_and_provider_health(self):
        config = CallSafetyConfig.from_env()
        reservation = main.call_manager.reserve(config, "spanish")
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("main.evaluate_call_availability") as schedule_check, \
                patch("main.check_provider_readiness", new=AsyncMock()) as health_check:
            response = await main.make_call(request)
        self.assertEqual(response.status_code, 429)
        schedule_check.assert_not_called()
        health_check.assert_not_awaited()
        main.call_manager.release(reservation_id=reservation.reservation_id)

    async def test_missing_public_domain_fails_before_provider_health(self):
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        for configured_domain in (
            None, "invalid public host", "https://example.railway.app/path"
        ):
            with self.subTest(domain_status="missing" if configured_domain is None else "invalid"):
                if configured_domain is None:
                    os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)
                else:
                    os.environ["RAILWAY_PUBLIC_DOMAIN"] = configured_domain
                with patch("main.check_provider_readiness", new=AsyncMock()) as health_check:
                    response = await main.make_call(request)
                self.assertEqual(response.status_code, 503)
                health_check.assert_not_awaited()
                self.assertEqual(main.call_manager.active_count, 0)

    async def test_invalid_safety_configuration_fails_closed(self):
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        for name, value in (
            ("MAX_CALL_DURATION_SECONDS", "900"),
            ("TWILIO_ACCOUNT_SID", "malformed"),
            ("TWILIO_PHONE_NUMBER", "not-a-number"),
        ):
            with self.subTest(variable=name):
                os.environ[name] = value
                with patch("main.check_provider_readiness", new=AsyncMock()) as health_check, \
                        patch("twilio.rest.Client") as twilio_client:
                    response = await main.make_call(request)
                self.assertEqual(response.status_code, 503)
                health_check.assert_not_awaited()
                twilio_client.assert_not_called()
                if name == "TWILIO_ACCOUNT_SID":
                    os.environ[name] = "AC" + ("a" * 32)
                elif name == "TWILIO_PHONE_NUMBER":
                    os.environ[name] = "+12125550199"

    async def test_concurrency_hourly_and_daily_limits_precede_twilio(self):
        cases = (
            ({}, "concurrent_limit"),
            ({"MAX_CALLS_PER_HOUR": "1"}, "hourly_limit"),
            ({"MAX_CALLS_PER_DAY": "1"}, "daily_limit"),
        )
        for overrides, expected_code in cases:
            with self.subTest(limit=expected_code):
                main.call_manager = InMemoryCallManager()
                with patch.dict(os.environ, overrides):
                    config = CallSafetyConfig.from_env()
                    reservation = main.call_manager.reserve(config, "spanish")
                    if expected_code != "concurrent_limit":
                        main.call_manager.release(reservation_id=reservation.reservation_id)
                    request = make_request(
                        {"to": TEST_NUMBER},
                        headers=[(b"x-call-secret", TEST_SECRET.encode())],
                    )
                    with patch("twilio.rest.Client") as twilio_client:
                        response = await main.make_call(request)
                self.assertEqual(response.status_code, 429)
                twilio_client.assert_not_called()

    async def test_destination_is_normalized_and_must_be_allowed(self):
        request = make_request(
            {"to": "+1 (212) 555-0100"},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.return_value.sid = "CA_TEST"
            response = await main.make_call(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            twilio_client.return_value.calls.create.call_args.kwargs["to"], TEST_NUMBER
        )

        request = make_request(
            {"to": "+442079460000"},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            response = await main.make_call(request)
        self.assertEqual(response.status_code, 403)
        twilio_client.assert_not_called()

    async def test_http_timeout_returns_sanitized_504(self):
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.side_effect = Timeout()
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 504)
        self.assertEqual(json.loads(response.body), {"error": "The request to Twilio timed out."})
        self.assertNotIn(TEST_NUMBER.encode(), response.body)
        self.assertNotIn(TEST_SECRET.encode(), response.body)

    async def test_twilio_exception_returns_sanitized_error(self):
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        exception = TwilioRestException(
            400,
            "/2010-04-01/Calls.json",
            "invalid destination",
            code=21211,
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.side_effect = exception
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            json.loads(response.body),
            {"error": "Twilio rejected the call request."},
        )
        self.assertNotIn(TEST_NUMBER.encode(), response.body)
        self.assertNotIn(TEST_SECRET.encode(), response.body)
        report_files = list(Path(self.result_directory.name).glob("call-*.json"))
        self.assertEqual(len(report_files), 1)
        self.assertEqual(json.loads(report_files[0].read_text())["call_status"], "rejected")

    async def test_unexpected_twilio_error_returns_sanitized_error(self):
        request = make_request(
            {"to": TEST_NUMBER},
            headers=[(b"x-call-secret", TEST_SECRET.encode())],
        )
        with patch("twilio.rest.Client") as twilio_client:
            twilio_client.return_value.calls.create.side_effect = RuntimeError(
                f"unexpected failure for {TEST_NUMBER}"
            )
            response = await main.make_call(request)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            json.loads(response.body),
            {"error": "Could not create the call."},
        )
        self.assertNotIn(TEST_NUMBER.encode(), response.body)
        self.assertNotIn(TEST_SECRET.encode(), response.body)

    async def test_make_call_route_only_accepts_post(self):
        route = next(route for route in main.app.routes if route.path == "/make-call")
        self.assertEqual(route.methods, {"POST"})

    async def test_sustained_repeating_hold_pattern_is_detected_and_ended(self):
        detector = main.HoldDetector()
        tone_payload = make_tone_payload()
        silence_payload = base64.b64encode(bytes([0xFF] * 160)).decode("ascii")
        detected_at = None

        for frame_index in range(40 * 50):
            position_in_cycle = frame_index % (5 * 50)
            payload = tone_payload if position_in_cycle < 2 * 50 else silence_payload
            timestamp_ms = frame_index * 20
            if detector.observe(payload, timestamp_ms):
                detected_at = timestamp_ms
                break

        self.assertIsNotNone(detected_at)
        self.assertGreaterEqual(detected_at, main.HoldDetector.MIN_HOLD_SECONDS * 1000)
        await self.assert_finish_closes_call("hold_detected")

    async def test_silence_and_brief_tone_do_not_trigger_hold_detection(self):
        detector = main.HoldDetector()
        tone_payload = make_tone_payload()
        silence_payload = base64.b64encode(bytes([0xFF] * 160)).decode("ascii")

        for frame_index in range(45 * 50):
            payload = tone_payload if frame_index < 2 * 50 else silence_payload
            self.assertFalse(detector.observe(payload, frame_index * 20))

    async def test_max_call_duration_ends_twilio_and_closes_realtime(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main.configured_call_duration_limit(), 240)

        with patch.dict(os.environ, {"MAX_CALL_DURATION_SECONDS": "0.01"}):
            self.assertEqual(main.configured_call_duration_limit(), 0.01)

        receive_task = asyncio.create_task(asyncio.Event().wait())
        send_task = asyncio.create_task(asyncio.Event().wait())
        end_reason = await main.wait_for_call_end(
            receive_task,
            send_task,
            duration_seconds=0.01,
        )
        self.assertEqual(end_reason, "max_duration")
        await self.assert_finish_closes_call(end_reason)

    async def assert_finish_closes_call(self, end_reason):
        openai_ws = AsyncMock()
        twilio_ws = AsyncMock()
        with patch("twilio.rest.Client") as twilio_client:
            await main.finish_call_for_reason(
                end_reason,
                "CA_TEST",
                openai_ws,
                twilio_ws,
            )

        twilio_client.return_value.calls.assert_called_once_with("CA_TEST")
        twilio_client.return_value.calls.return_value.update.assert_called_once_with(
            status="completed"
        )
        openai_ws.close.assert_awaited_once_with()
        twilio_ws.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
