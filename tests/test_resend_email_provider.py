import ast
import inspect
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from email_provider import EmailProvider
from resend_email_provider import ResendEmailProvider


class ResendEmailProviderTests(unittest.TestCase):
    def setUp(self):
        self.sdk = SimpleNamespace(api_key=None, Emails=SimpleNamespace(send=Mock(return_value={"id": "re_test_message"})))
        self.provider = ResendEmailProvider("re_test_key_not_real", "sender@example.com", sdk=self.sdk)

    def call_provider(self, **overrides):
        fields = {"to": ["recipient@example.com"], "cc": [],
                  "subject": "Follow up", "body": "Agreed follow up."}
        fields.update(overrides)
        return self.provider.send(**fields)

    def test_implements_provider_interface_and_accepts_only_message_fields(self):
        self.assertIsInstance(self.provider, EmailProvider)
        self.assertEqual(set(inspect.signature(ResendEmailProvider.send).parameters),
                         {"self", "to", "cc", "subject", "body"})

    def test_dry_run_never_calls_sdk(self):
        with patch.dict("os.environ", {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true"}):
            result = self.call_provider()
        self.assertEqual(result["reason_code"], "dry_run_only")
        self.assertFalse(result["success"])
        self.sdk.Emails.send.assert_not_called()

    def test_disabled_email_never_calls_sdk(self):
        with patch.dict("os.environ", {"EMAIL_ENABLED": "false", "EMAIL_DRY_RUN": "false"}):
            result = self.call_provider()
        self.assertEqual(result["reason_code"], "email_disabled")
        self.sdk.Emails.send.assert_not_called()

    def test_missing_key_and_from_fail_closed(self):
        missing_key = ResendEmailProvider("", "sender@example.com", sdk=self.sdk)
        missing_from = ResendEmailProvider("re_test_key_not_real", "", sdk=self.sdk)
        with patch.dict("os.environ", {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false"}):
            self.assertEqual(missing_key.send(to=["recipient@example.com"], cc=[], subject="s", body="b")["reason_code"], "provider_not_configured")
            self.assertEqual(missing_from.send(to=["recipient@example.com"], cc=[], subject="s", body="b")["reason_code"], "from_address_invalid")
        self.sdk.Emails.send.assert_not_called()

    def test_invalid_input_fails_before_sdk(self):
        with patch.dict("os.environ", {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false"}):
            reasons = [
                self.call_provider(to=[])["reason_code"],
                self.call_provider(to=["not-an-email"])["reason_code"],
                self.call_provider(cc=["bad"])["reason_code"],
                self.call_provider(subject=" ")["reason_code"],
                self.call_provider(body="")["reason_code"],
            ]
        self.assertEqual(reasons, ["recipient_invalid", "recipient_invalid", "recipient_invalid", "subject_empty", "body_empty"])
        self.sdk.Emails.send.assert_not_called()

    def test_validated_payload_is_minimal_and_only_sdk_is_mocked(self):
        with patch.dict("os.environ", {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false"}):
            result = self.call_provider(cc=["copy@example.com"])
        self.assertTrue(result["success"])
        self.assertEqual(result["provider_message_id"], "re_test_message")
        self.sdk.Emails.send.assert_called_once_with({
            "from": "sender@example.com", "to": ["recipient@example.com"],
            "cc": ["copy@example.com"], "subject": "Follow up", "text": "Agreed follow up.",
        })
        self.assertEqual(self.sdk.api_key, None)

    def test_provider_errors_are_sanitized(self):
        self.sdk.Emails.send.side_effect = RuntimeError("re_test_key_not_real Authorization secret transcript")
        with patch.dict("os.environ", {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false"}):
            result = self.call_provider()
        serialized = json.dumps(result)
        self.assertEqual(result["reason_code"], "provider_request_failed")
        self.assertNotIn("re_test_key_not_real", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("transcript", serialized)

    def test_api_key_is_restored_after_sdk_error(self):
        self.sdk.api_key = "previous_test_key"
        self.sdk.Emails.send.side_effect = RuntimeError("failed")
        with patch.dict("os.environ", {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false"}):
            self.call_provider()
        self.assertEqual(self.sdk.api_key, "previous_test_key")

    def test_bad_sdk_response_is_sanitized(self):
        self.sdk.Emails.send.return_value = {"id": ""}
        with patch.dict("os.environ", {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false"}):
            result = self.call_provider()
        self.assertEqual(result["reason_code"], "provider_response_invalid")
        self.assertIsNone(result["provider_message_id"])

    def test_provider_module_has_no_direct_network_or_other_vendor_imports(self):
        tree = ast.parse(open("resend_email_provider.py", encoding="utf-8").read())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
        self.assertIn("resend", imported)
        self.assertFalse(imported & {"requests", "httpx", "aiohttp", "urllib", "socket", "smtplib", "twilio"})


if __name__ == "__main__":
    unittest.main()
