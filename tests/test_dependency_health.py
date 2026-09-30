import os
import unittest
from unittest.mock import Mock, patch
from requests.exceptions import Timeout as RequestTimeout

from dependency_health import (
    DependencyCheck,
    DependencyHealthRegistry,
    DependencyStatus,
    openai_error,
    openai_preflight,
    twilio_preflight,
    twilio_rest_error,
)
import main


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload


class DependencyHealthTests(unittest.TestCase):
    def test_twilio_preflight_checks_account_and_positive_balance(self):
        getter = Mock(side_effect=[
            Response(payload={"status": "active"}),
            Response(payload={"balance": "12.34", "currency": "USD"}),
        ])
        check = twilio_preflight("local-account-sid", "test-only-token", http_get=getter)
        self.assertEqual(check.status, DependencyStatus.AVAILABLE)
        self.assertEqual(getter.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] > 0 for call in getter.call_args_list))

    def test_twilio_zero_balance_blocks_and_does_not_retain_payload(self):
        getter = Mock(side_effect=[
            Response(payload={"status": "active"}),
            Response(payload={"balance": "0.00", "currency": "USD"}),
        ])
        check = twilio_preflight("local-account-sid", "local-token", http_get=getter)
        self.assertEqual(check.status, DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED)
        self.assertEqual(check.reason, "twilio_balance_below_minimum")

    def test_twilio_minimum_balance_blocks_below_threshold_and_allows_equal(self):
        below_getter = Mock(side_effect=[
            Response(payload={"status": "active"}),
            Response(payload={"balance": "4.99", "currency": "USD"}),
        ])
        below = twilio_preflight(
            "test-sid", "local-token", http_get=below_getter,
            minimum_balance="5.00",
        )
        self.assertEqual(
            below.reason, "twilio_balance_below_minimum"
        )
        self.assertEqual(below.status, DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED)

        equal_getter = Mock(side_effect=[
            Response(payload={"status": "active"}),
            Response(payload={"balance": "5.00", "currency": "USD"}),
        ])
        equal = twilio_preflight(
            "test-sid", "local-token", http_get=equal_getter,
            minimum_balance="5.00",
        )
        self.assertEqual(equal.status, DependencyStatus.AVAILABLE)

    def test_twilio_unknown_balance_and_invalid_minimum_fail_closed(self):
        for balance_data in (
            {"balance": "not-a-number", "currency": "USD"},
            {"balance": "12.00", "currency": "EUR"},
            {"balance": "12.00"},
        ):
            getter = Mock(side_effect=[
                Response(payload={"status": "active"}),
                Response(payload=balance_data),
            ])
            check = twilio_preflight(
                "test-sid", "local-token", http_get=getter,
                minimum_balance="5.00",
            )
            self.assertEqual(check.status, DependencyStatus.UNKNOWN)
            self.assertEqual(check.reason, "twilio_balance_unknown")

        getter = Mock()
        for invalid_minimum in ("not-a-number", "-0.01", "NaN", "Infinity"):
            check = twilio_preflight(
                "test-sid", "local-token", http_get=getter,
                minimum_balance=invalid_minimum,
            )
            self.assertEqual(check.status, DependencyStatus.UNKNOWN)
            self.assertEqual(check.reason, "twilio_minimum_balance_invalid")
        getter.assert_not_called()

    def test_invalid_configured_minimum_blocks_readiness_before_provider_checks(self):
        environment = {
            "CALLS_ENABLED": "true",
            "CALL_SECRET": "local-call-secret",
            "OPENAI_API_KEY": "local-openai-key",
            "TWILIO_ACCOUNT_SID": "AC" + "a" * 32,
            "TWILIO_AUTH_TOKEN": "local-twilio-token",
            "TWILIO_PHONE_NUMBER": "+12125550199",
            "TWILIO_MIN_BALANCE_USD": "-1.00",
        }
        with patch.dict(os.environ, environment):
            with patch("main.twilio_preflight") as twilio_check, \
                    patch("main.openai_preflight") as openai_check:
                check = main.asyncio.run(main.check_provider_readiness())

        self.assertEqual(check.reason, "twilio_minimum_balance_invalid")
        self.assertEqual(check.status, DependencyStatus.UNKNOWN)
        twilio_check.assert_not_called()
        openai_check.assert_not_called()

    def test_configured_minimum_is_passed_to_existing_twilio_preflight(self):
        environment = {
            "CALLS_ENABLED": "true",
            "CALL_SECRET": "local-call-secret",
            "OPENAI_API_KEY": "local-openai-key",
            "TWILIO_ACCOUNT_SID": "AC" + "a" * 32,
            "TWILIO_AUTH_TOKEN": "local-twilio-token",
            "TWILIO_PHONE_NUMBER": "+12125550199",
            "TWILIO_MIN_BALANCE_USD": "7.25",
        }
        with patch.dict(os.environ, environment):
            with patch(
                "main.twilio_preflight",
                return_value=DependencyCheck(
                    "twilio", DependencyStatus.AVAILABLE,
                    "account_active_balance_positive", "Ready.",
                ),
            ) as twilio_check, patch(
                "main.openai_preflight",
                return_value=DependencyCheck(
                    "openai", DependencyStatus.AVAILABLE, "model_available", "Ready."
                ),
            ):
                check = main.asyncio.run(main.check_provider_readiness())

        self.assertIsNone(check)
        self.assertEqual(
            str(twilio_check.call_args.kwargs["minimum_balance"]), "7.25"
        )

    def test_twilio_inactive_account_and_http_auth_are_classified(self):
        inactive = twilio_preflight(
            "test-sid", "local-token", http_get=Mock(return_value=Response(payload={"status": "suspended"}))
        )
        self.assertEqual(inactive.status, DependencyStatus.DISABLED)
        unauthorized = twilio_preflight(
            "test-sid", "local-token", http_get=Mock(return_value=Response(401))
        )
        self.assertEqual(unauthorized.status, DependencyStatus.AUTHENTICATION_ERROR)
        self.assertEqual(twilio_rest_error(10001).status, DependencyStatus.UNKNOWN)

    def test_openai_preflight_is_read_only_and_classifies_status(self):
        getter = Mock(return_value=Response(payload={"id": "gpt-realtime"}))
        check = openai_preflight("local-openai-key", http_get=getter)
        self.assertEqual(check.status, DependencyStatus.AVAILABLE)
        self.assertEqual(getter.call_args.args[0], "https://api.openai.com/v1/models/gpt-realtime")
        self.assertEqual(getter.call_args.kwargs["headers"], {"Authorization": "Bearer local-openai-key"})

        denied = openai_preflight(
            "local-openai-key",
            http_get=Mock(return_value=Response(429, {"error": {"code": "project_spend_limit_exceeded"}})),
        )
        self.assertEqual(denied.status, DependencyStatus.SPEND_LIMIT)
        self.assertEqual(
            openai_error("credit_balance_exhausted").status,
            DependencyStatus.QUOTA_OR_BALANCE_EXHAUSTED,
        )

    def test_missing_credentials_and_transport_fail_closed(self):
        getter = Mock()
        self.assertEqual(
            openai_preflight("", http_get=getter).status,
            DependencyStatus.NOT_CONFIGURED,
        )
        getter.assert_not_called()
        with patch("dependency_health.requests.get", side_effect=RequestTimeout):
            check = openai_preflight("local-key")
        self.assertEqual(check.status, DependencyStatus.SERVICE_ERROR)

    def test_circuit_latches_financial_and_auth_errors_until_available(self):
        registry = DependencyHealthRegistry()
        blocked = openai_error("organization_spend_limit_exceeded")
        registry.record(blocked)
        self.assertTrue(registry.is_latched("openai"))
        snapshot = registry.snapshot()
        self.assertEqual(snapshot["dependencies"]["openai"]["status"], "spend_limit")
        self.assertIn("recommended_action", snapshot["alerts"][0])
        registry.record(type(blocked)(
            "openai", DependencyStatus.AVAILABLE, "healthy", "No action needed."
        ))
        self.assertFalse(registry.is_latched("openai"))

    def test_calls_fail_closed_without_all_required_environment(self):
        with patch.dict(os.environ, {"CALLS_ENABLED": "true"}, clear=True):
            config = main.CallSafetyConfig.from_env()
        self.assertFalse(config.enabled)
        with patch.dict(os.environ, {"CALLS_ENABLED": "yes"}, clear=True):
            with self.assertRaises(main.SafetyConfigurationError):
                main.CallSafetyConfig.from_env()


if __name__ == "__main__":
    unittest.main()
