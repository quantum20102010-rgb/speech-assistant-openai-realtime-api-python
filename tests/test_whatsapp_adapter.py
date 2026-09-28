import ast
import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from whatsapp_adapter import prepare_whatsapp


FAKE_RECIPIENT = "+19999999999"  # Syntactic E.164 test fixture; never transmitted.
VALID_MESSAGE = {
    "should_send": True,
    "channel": "whatsapp",
    "priority": "high",
    "message": "Vista previa local de prueba.",
    "message_type": "business_update",
}


class WhatsAppAdapterTests(unittest.TestCase):
    def prepare(self, message=VALID_MESSAGE, **environment):
        with patch.dict(os.environ, environment, clear=True):
            return prepare_whatsapp(message)

    def test_absent_and_false_switch_are_disabled(self):
        absent = self.prepare()
        disabled = self.prepare(WHATSAPP_ENABLED="false", WHATSAPP_RECIPIENT=FAKE_RECIPIENT)
        self.assertEqual(absent["status"], "disabled")
        self.assertEqual(disabled["status"], "disabled")

    def test_enabled_without_recipient_is_not_configured(self):
        result = self.prepare(WHATSAPP_ENABLED="true")
        self.assertEqual(result["status"], "not_configured")

    def test_invalid_enable_flag_fails_closed(self):
        result = self.prepare(WHATSAPP_ENABLED="yes", WHATSAPP_RECIPIENT=FAKE_RECIPIENT)
        self.assertEqual(result["status"], "invalid_configuration")
        self.assertEqual(result["reason_code"], "invalid_whatsapp_enabled")

    def test_invalid_recipient_is_rejected_without_echoing_it(self):
        private_value = "recipient-with-spaces-5551234567"
        result = self.prepare(WHATSAPP_ENABLED="true", WHATSAPP_RECIPIENT=private_value)
        self.assertEqual(result["status"], "invalid_configuration")
        self.assertNotIn(private_value, json.dumps(result))

    def test_valid_recipient_returns_only_dry_run_ready_status(self):
        result = self.prepare(WHATSAPP_ENABLED="true", WHATSAPP_RECIPIENT=FAKE_RECIPIENT)
        self.assertEqual(result["status"], "ready_for_send")
        self.assertTrue(result["recipient_configured"])
        self.assertNotIn("recipient", result)
        self.assertFalse(result["sent"])
        self.assertTrue(result["dry_run"])

    def test_notification_should_send_false_is_blocked(self):
        message = {**VALID_MESSAGE, "should_send": False}
        result = self.prepare(message, WHATSAPP_ENABLED="true", WHATSAPP_RECIPIENT=FAKE_RECIPIENT)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "notification_not_sendable")

    def test_empty_message_is_blocked(self):
        message = {**VALID_MESSAGE, "message": "  "}
        result = self.prepare(message, WHATSAPP_ENABLED="true", WHATSAPP_RECIPIENT=FAKE_RECIPIENT)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "message_empty")

    def test_none_priority_is_blocked(self):
        message = {**VALID_MESSAGE, "priority": "none"}
        result = self.prepare(message, WHATSAPP_ENABLED="true", WHATSAPP_RECIPIENT=FAKE_RECIPIENT)
        self.assertEqual(result["status"], "blocked")

    def test_every_adapter_result_is_unsent_dry_run(self):
        cases = (
            self.prepare(),
            self.prepare(WHATSAPP_ENABLED="true"),
            self.prepare(WHATSAPP_ENABLED="invalid"),
            self.prepare(WHATSAPP_ENABLED="true", WHATSAPP_RECIPIENT=FAKE_RECIPIENT),
        )
        for result in cases:
            with self.subTest(status=result["status"]):
                self.assertFalse(result["sent"])
                self.assertTrue(result["dry_run"])

    def test_no_network_or_external_client_imports(self):
        source = Path(__file__).parents[1].joinpath("whatsapp_adapter.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertLessEqual(imported, {"os", "re"})

    def test_no_full_recipient_or_secret_in_application_logs(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        secret_fixture = "synthetic-test-secret-value"
        with patch.dict(os.environ, {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_RECIPIENT": FAKE_RECIPIENT,
            "WHATSAPP_TEST_SECRET": secret_fixture,
        }, clear=True), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = prepare_whatsapp(VALID_MESSAGE)
        emitted = stdout.getvalue() + stderr.getvalue() + json.dumps(result)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(FAKE_RECIPIENT, emitted)
        self.assertNotIn(secret_fixture, emitted)

    def test_old_result_without_notification_fields_remains_renderable(self):
        old_result = empty_call_result("completed", "spanish")
        old_result.pop("notification_decision", None)
        old_result.pop("notification_message", None)
        old_result.pop("whatsapp_preview", None)
        self.assertIn("REPORTE DE LLAMADA", render_call_report(old_result))
        self.assertNotIn("WHATSAPP PREVIEW", render_call_report(old_result))

    def test_integration_persists_preview_as_explicitly_not_sent(self):
        result = empty_call_result("completed", "spanish")
        result["transcript"] = "Contacto: El pedido mínimo es de 500 unidades."
        result["minimum_order_quantity"] = "El pedido mínimo es de 500 unidades."
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_RECIPIENT": FAKE_RECIPIENT,
        }, clear=False):
            previous_storage = main.call_result_storage
            main.call_result_storage = LocalFileCallResultStorage(directory)
            try:
                self.assertTrue(asyncio.run(main.write_call_result(result)))
            finally:
                main.call_result_storage = previous_storage
            json_path = next(Path(directory).glob("call-*.json"))
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            report = json_path.with_suffix(".txt").read_text(encoding="utf-8")

        self.assertIn("notification_decision", saved)
        self.assertIn("notification_message", saved)
        self.assertEqual(saved["whatsapp_preview"]["status"], "ready_for_send")
        self.assertFalse(saved["whatsapp_preview"]["sent"])
        self.assertTrue(saved["whatsapp_preview"]["dry_run"])
        self.assertNotIn(FAKE_RECIPIENT, json.dumps(saved))
        self.assertIn("WHATSAPP PREVIEW", report)
        self.assertIn("NOT SENT", report)
        self.assertIn("dry_run=true", report)
        self.assertIn("sent=false", report)


if __name__ == "__main__":
    unittest.main()
