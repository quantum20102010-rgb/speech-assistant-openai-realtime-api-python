import ast
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Request

import main
from action_executor import create_action
from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from email_executor import execute_email_action
from email_draft import email_draft_content_digest
from email_provider_fake import FakeEmailProvider
from resend_email_provider import ResendEmailProvider
from mission_decision import create_pending_decision
from missions import MissionNotFoundError


MISSION = "email_executor_mission"
OTHER = "other_email_executor_mission"
EMAIL = "contact@example.com"
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def mission_fixture(mission_id=MISSION):
    return SimpleNamespace(id=mission_id, name="Mission", objective="Objective",
                            required_information=(), relevance_criteria=(), follow_up_actions=())


class Catalog:
    def get(self, mission_id):
        if mission_id not in {MISSION, OTHER}:
            raise MissionNotFoundError("not found")
        return mission_fixture(mission_id)


def request(method, path, secret="email-executor-test-secret", body=None):
    headers = [] if secret is None else [(b"x-call-secret", secret.encode())]
    encoded = b"" if body is None else json.dumps(body).encode()
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": encoded, "more_body": False}
    return Request({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                    "http_version": "1.1", "method": method, "scheme": "https", "path": path,
                    "raw_path": path.encode(), "query_string": b"", "root_path": "",
                    "headers": headers, "client": ("127.0.0.1", 1),
                    "server": ("testserver", 443)}, receive)


def artifacts(status="approved", mission_id=MISSION, action_type="send_email"):
    decision = create_pending_decision(mission_id, "Enviar email comercial", "decision_engine")
    decision.update({"status": status, "resolved_by": "Fabian" if status == "approved" else None,
                     "resolved_at": NOW.isoformat() if status == "approved" else None})
    action = create_action(mission_id, action_type, "Enviar email comercial aprobado", NOW,
                           decision["decision_id"])
    action["status"] = status
    action["approval_status"] = "approved" if status == "approved" else status
    state = {"mission_id": mission_id, "decisions": [decision], "actions": [action]}
    result = {"mission_id": mission_id, "email": EMAIL,
              "transcript": f"Contacto: {EMAIL}",
              "decision": {"facts": [{"field": "email", "value": EMAIL,
                                       "source": "transcript_verified_result"}]}}
    draft = {"should_create": True, "to": [EMAIL], "cc": [],
             "subject": "Seguimiento comercial", "body": "Hola, seguimiento acordado.",
             "requires_approval": True, "sent": False,
             "approval_status": "approved_for_future_send" if status == "approved" else status,
             "approval_decision_ids": [decision["decision_id"]]}
    result["email_draft"] = draft
    draft["approved_content_sha256"] = email_draft_content_digest(draft)
    return state, draft, result, action, decision


class EmailExecutorTests(unittest.TestCase):
    def test_formally_approved_action_returns_only_dry_run_preview(self):
        state, draft, result, action, _ = artifacts()
        output = execute_email_action(MISSION, action["action_id"], state, draft,
                                      result, dry_run_setting="true", now=NOW)
        self.assertEqual(output["status"], "dry_run")
        self.assertEqual(output["provider"], "none")
        self.assertEqual(output["to"], [EMAIL])
        self.assertEqual(output["subject"], draft["subject"])
        self.assertEqual(output["body"], draft["body"])
        self.assertIs(output["sent"], False)
        self.assertIs(output["dry_run"], True)
        self.assertIsNone(output["executed_at"])

    def test_pending_rejected_and_deferred_actions_are_blocked(self):
        for status in ("awaiting_approval", "rejected", "deferred"):
            state, draft, result, action, _ = artifacts(status="approved")
            state["actions"][0]["status"] = status
            output = execute_email_action(MISSION, action["action_id"], state, draft,
                                          result, dry_run_setting="true")
            self.assertEqual(output["status"], "blocked")
            self.assertEqual(output["reason_code"], "formal_approval_required")

    def test_wrong_action_type_and_mission_are_blocked(self):
        state, draft, result, action, _ = artifacts()
        state["actions"][0]["action_type"] = "send_whatsapp"
        self.assertEqual(execute_email_action(MISSION, action["action_id"], state, draft,
                                              result, dry_run_setting="true")["reason_code"],
                         "wrong_action_type")
        state, draft, result, action, _ = artifacts(mission_id=OTHER)
        self.assertEqual(execute_email_action(MISSION, action["action_id"], state, draft,
                                              result, dry_run_setting="true")["reason_code"],
                         "mission_mismatch")

    def test_missing_or_invalid_draft_content_is_blocked(self):
        state, draft, result, action, _ = artifacts()
        provider = FakeEmailProvider()
        for invalid in ({**draft, "should_create": False}, {**draft, "to": ["+525555555555"]},
                        {**draft, "subject": ""}, {**draft, "body": ""},
                        {**draft, "sent": True}, {**draft, "requires_approval": False}):
            output = execute_email_action(MISSION, action["action_id"], state, invalid,
                                          result, dry_run_setting="true", provider=provider)
            self.assertEqual(output["status"], "blocked")
        self.assertEqual(provider.attempts, [])
        self.assertEqual(execute_email_action(MISSION, action["action_id"], state, None,
                                              result, dry_run_setting="true")["reason_code"],
                         "draft_unavailable")

    def test_requires_formal_mission_decision_not_boolean_approval(self):
        state, draft, result, action, decision = artifacts()
        state["decisions"] = []
        self.assertEqual(execute_email_action(MISSION, action["action_id"], state, draft,
                                              result, dry_run_setting="true")["reason_code"],
                         "formal_approval_required")
        state, draft, result, action, decision = artifacts()
        decision["resolved_by"] = "Someone else"
        self.assertEqual(execute_email_action(MISSION, action["action_id"], state, draft,
                                              result, dry_run_setting="true")["reason_code"],
                         "formal_approval_required")

    def test_dry_run_setting_must_be_explicit_and_email_enabled_never_sends(self):
        state, draft, result, action, _ = artifacts()
        self.assertEqual(execute_email_action(MISSION, action["action_id"], state, draft,
                                              result, dry_run_setting="false")["status"], "blocked")
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "",
        }):
            output = execute_email_action(MISSION, action["action_id"], state, draft, result)
        self.assertEqual(output["status"], "blocked")
        self.assertEqual(output["reason_code"], "email_provider_not_configured")
        self.assertIs(output["sent"], False)

    def test_fake_provider_receives_only_validated_message_fields(self):
        state, draft, result, action, _ = artifacts()
        provider = FakeEmailProvider()
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "fake",
        }, clear=False):
            output = execute_email_action(MISSION, action["action_id"], state, draft,
                                          result, now=NOW, provider=provider)
        self.assertEqual(provider.attempts, [{
            "to": [EMAIL], "cc": [], "subject": draft["subject"], "body": draft["body"],
        }])
        self.assertEqual(set(provider.attempts[0]), {"to", "cc", "subject", "body"})
        self.assertEqual(output["status"], "dry_run")
        self.assertEqual(output["reason_code"], "fake_provider_simulated")
        self.assertEqual(output["provider"], "fake")
        self.assertEqual(output["provider_message_id"], "fake-message-1")
        self.assertFalse(output["sent"])
        self.assertTrue(output["dry_run"])
        self.assertIsNone(output["executed_at"])

    def test_resend_provider_is_injected_but_dry_run_does_not_call_sdk(self):
        state, draft, result, action, _ = artifacts()
        sdk = SimpleNamespace(api_key=None, Emails=SimpleNamespace(send=lambda _payload: self.fail("SDK called")))
        provider = ResendEmailProvider("re_test_not_real", "sender@example.com", sdk=sdk)
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "resend",
        }, clear=False):
            output = execute_email_action(MISSION, action["action_id"], state, draft,
                                          result, now=NOW, provider=provider)
        self.assertEqual(output["status"], "dry_run")
        self.assertEqual(output["reason_code"], "dry_run_only")
        self.assertEqual(output["provider"], "resend")
        self.assertFalse(output["sent"])
        self.assertIsNone(output["executed_at"])

    def test_disabled_email_keeps_dry_run_and_does_not_call_injected_provider(self):
        state, draft, result, action, _ = artifacts()
        provider = FakeEmailProvider()
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "false", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "fake",
        }, clear=False):
            output = execute_email_action(MISSION, action["action_id"], state, draft,
                                          result, now=NOW, provider=provider)
        self.assertEqual(output["status"], "dry_run")
        self.assertEqual(output["reason_code"], "dry_run_only")
        self.assertEqual(output["provider"], "none")
        self.assertEqual(provider.attempts, [])

    def test_enabled_without_provider_blocks_and_provider_names_fail_closed(self):
        state, draft, result, action, _ = artifacts()
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "",
        }, clear=False):
            missing = execute_email_action(MISSION, action["action_id"], state, draft, result)
        self.assertEqual(missing["status"], "blocked")
        self.assertEqual(missing["reason_code"], "email_provider_not_configured")
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "not-installed",
        }, clear=False):
            invalid = execute_email_action(MISSION, action["action_id"], state, draft, result)
        self.assertEqual(invalid["status"], "blocked")
        self.assertEqual(invalid["reason_code"], "email_provider_unavailable")
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "fake",
        }, clear=False):
            missing_injected = execute_email_action(
                MISSION, action["action_id"], state, draft, result
            )
        self.assertEqual(missing_injected["status"], "blocked")
        self.assertEqual(missing_injected["reason_code"], "email_provider_unavailable")

    def test_invalid_email_configuration_and_injected_provider_are_blocked(self):
        state, draft, result, action, _ = artifacts()
        provider = FakeEmailProvider()
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "yes", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "",
        }, clear=False):
            invalid_config = execute_email_action(MISSION, action["action_id"], state,
                                                  draft, result, provider=provider)
        self.assertEqual(invalid_config["reason_code"], "email_configuration_invalid")
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "",
        }, clear=False):
            invalid_provider = execute_email_action(
                MISSION, action["action_id"], state, draft, result, provider=object()
            )
        self.assertEqual(invalid_provider["reason_code"], "email_provider_invalid")
        self.assertEqual(provider.attempts, [])

    def test_pending_rejected_deferred_and_blocked_actions_never_reach_provider(self):
        for status in ("awaiting_approval", "rejected", "deferred", "blocked_by_policy"):
            state, draft, result, action, _ = artifacts()
            state["actions"][0]["status"] = status
            state["actions"][0]["approval_status"] = status
            provider = FakeEmailProvider()
            with self.subTest(status=status), patch.dict(os.environ, {
                "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false", "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "re_test_key_not_real", "RESEND_FROM": "sender@example.com",
            }, clear=False):
                output = execute_email_action(MISSION, action["action_id"], state,
                                              draft, result, provider=provider)
            self.assertEqual(output["status"], "blocked")
            self.assertEqual(provider.attempts, [])

    def test_fake_provider_idempotency_prevents_a_second_attempt(self):
        state, draft, result, action, _ = artifacts()
        provider = FakeEmailProvider()
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "fake",
        }, clear=False):
            first = execute_email_action(MISSION, action["action_id"], state, draft,
                                         result, provider=provider)
            second = execute_email_action(
                MISSION, action["action_id"], state, draft, result,
                existing_results={action["action_id"]: first}, provider=provider,
            )
        self.assertEqual(first["status"], "dry_run")
        self.assertEqual(second["reason_code"], "already_processed")
        self.assertEqual(len(provider.attempts), 1)

    def test_production_configuration_with_fake_provider_simulates_sent_result(self):
        state, draft, result, action, _ = artifacts()
        provider = FakeEmailProvider()
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false", "EMAIL_PROVIDER": "resend",
            "RESEND_API_KEY": "re_test_key_not_real", "RESEND_FROM": "sender@example.com",
        }, clear=False):
            output = execute_email_action(MISSION, action["action_id"], state, draft,
                                          result, now=NOW, provider=provider)
        self.assertEqual(output["status"], "sent")
        self.assertTrue(output["sent"])
        self.assertFalse(output["dry_run"])
        self.assertEqual(output["provider"], "fake")
        self.assertEqual(output["provider_message_id"], "fake-message-1")
        self.assertEqual(output["executed_at"], NOW.isoformat())
        self.assertEqual(output["action_id"], action["action_id"])
        self.assertEqual(output["mission_id"], MISSION)
        self.assertEqual(set(provider.attempts[0]), {"to", "cc", "subject", "body"})
        duplicate = execute_email_action(
            MISSION, action["action_id"], state, draft, result,
            existing_results={action["action_id"]: output}, provider=provider,
        )
        self.assertEqual(duplicate["reason_code"], "already_processed")
        self.assertEqual(len(provider.attempts), 1)

    def test_production_configuration_fails_closed_for_missing_or_invalid_settings(self):
        cases = [
            ({"EMAIL_PROVIDER": "resend", "RESEND_API_KEY": "", "RESEND_FROM": "sender@example.com"}, "provider_not_configured"),
            ({"EMAIL_PROVIDER": "resend", "RESEND_API_KEY": "re_test_key_not_real", "RESEND_FROM": ""}, "from_address_invalid"),
            ({"EMAIL_PROVIDER": "resend", "RESEND_API_KEY": "re_test_key_not_real", "RESEND_FROM": "not an address"}, "from_address_invalid"),
            ({"EMAIL_PROVIDER": "unknown", "RESEND_API_KEY": "re_test_key_not_real", "RESEND_FROM": "sender@example.com"}, "email_provider_unavailable"),
        ]
        for config, expected in cases:
            state, draft, result, action, _ = artifacts()
            provider = FakeEmailProvider()
            values = {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false", **config}
            with self.subTest(config=config), patch.dict(os.environ, values, clear=False):
                output = execute_email_action(MISSION, action["action_id"], state,
                                              draft, result, provider=provider)
            self.assertEqual(output["status"], "blocked")
            self.assertEqual(output["reason_code"], expected)
            self.assertFalse(output["sent"])
            self.assertIsNone(output["executed_at"])
            self.assertEqual(provider.attempts, [])

    def test_production_rejects_changes_to_each_approved_draft_field(self):
        for field, replacement in (
            ("to", ["attacker@example.com"]),
            ("cc", ["copy@example.com"]),
            ("subject", "Changed after approval"),
            ("body", "Changed body after approval"),
        ):
            state, draft, result, action, _ = artifacts()
            draft[field] = replacement
            result["email_draft"] = draft
            provider = FakeEmailProvider()
            with self.subTest(field=field), patch.dict(os.environ, {
                "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false", "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "re_test_key_not_real", "RESEND_FROM": "sender@example.com",
            }, clear=False):
                output = execute_email_action(MISSION, action["action_id"], state,
                                              draft, result, provider=provider)
            self.assertEqual(output["reason_code"], "email_draft_mismatch")
            self.assertFalse(output["sent"])
            self.assertEqual(provider.attempts, [])

    def test_provider_failure_is_sanitized_persistable_and_never_retried(self):
        class FailingFake:
            provider_name = "fake"
            simulation_only = True
            attempts = 0

            def send(self, **_payload):
                self.attempts += 1
                return {"success": False, "provider": "fake",
                        "provider_message_id": None, "reason_code": "provider_request_failed"}

        state, draft, result, action, _ = artifacts()
        provider = FailingFake()
        env = {"EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "false", "EMAIL_PROVIDER": "resend",
               "RESEND_API_KEY": "re_test_key_not_real", "RESEND_FROM": "sender@example.com"}
        with patch.dict(os.environ, env, clear=False):
            failure = execute_email_action(MISSION, action["action_id"], state, draft,
                                           result, now=NOW, provider=provider)
            self.assertEqual(failure["status"], "blocked")
            self.assertEqual(failure["reason_code"], "email_provider_failed")
            self.assertFalse(failure["sent"])
            self.assertFalse(failure["dry_run"])
            self.assertIsNone(failure["executed_at"])
            self.assertNotIn("re_test_key_not_real", json.dumps(failure))
            repeated = execute_email_action(
                MISSION, action["action_id"], state, draft, result,
                existing_results={action["action_id"]: failure}, provider=provider,
            )
        self.assertEqual(repeated["reason_code"], "already_processed")
        self.assertEqual(provider.attempts, 1)

    def test_local_storage_persists_sent_and_provider_failure_once(self):
        state, draft, result, action, _ = artifacts()
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFileCallResultStorage(directory)
            result["mission_state"] = state
            storage.write(result)
            sent = {"mission_id": MISSION, "action_id": action["action_id"],
                    "status": "sent", "sent": True, "dry_run": False,
                    "executed_at": NOW.isoformat()}
            self.assertEqual(storage.record_email_execution_result(MISSION, action["action_id"], sent), sent)
            changed = {**sent, "provider_message_id": "different"}
            self.assertEqual(storage.record_email_execution_result(MISSION, action["action_id"], changed), sent)

    def test_fake_provider_failure_can_be_stored_without_success_fields(self):
        state, draft, result, action, _ = artifacts()
        failure = {"mission_id": MISSION, "action_id": action["action_id"],
                   "status": "blocked", "reason_code": "email_provider_failed",
                   "sent": False, "dry_run": False, "executed_at": None}
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFileCallResultStorage(directory)
            result["mission_state"] = state
            storage.write(result)
            saved = storage.record_email_execution_result(MISSION, action["action_id"], failure)
        self.assertEqual(saved, failure)
        self.assertFalse(saved["sent"])
        self.assertIsNone(saved["executed_at"])

    def test_provider_exception_is_sanitized_and_does_not_expose_secrets(self):
        class RaisingFake:
            provider_name = "fake"
            simulation_only = True

            def send(self, **_fields):
                raise RuntimeError("CALL_SECRET local-provider-secret-value")

        state, draft, result, action, _ = artifacts()
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "fake",
        }, clear=False):
            output = execute_email_action(MISSION, action["action_id"], state, draft,
                                          result, provider=RaisingFake())
        self.assertEqual(output["reason_code"], "email_provider_failed")
        self.assertNotIn("CALL_SECRET", json.dumps(output))
        self.assertNotIn("local-provider-secret-value", json.dumps(output))

    def test_idempotency_storage_and_text_report(self):
        state, draft, result, action, _ = artifacts()
        preview = execute_email_action(MISSION, action["action_id"], state, draft,
                                       result, dry_run_setting="true", now=NOW)
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFileCallResultStorage(directory)
            persisted = {**result, "mission_state": state, "email_draft": draft}
            storage.write(persisted)
            first = storage.record_email_execution_result(MISSION, action["action_id"], preview)
            second = storage.record_email_execution_result(MISSION, action["action_id"],
                                                           {**preview, "body": "different"})
            self.assertEqual(first, second)
            latest = storage.latest_mission_result(MISSION)
            self.assertEqual(latest["email_execution_results"][action["action_id"]], preview)
            text = next(Path(directory).glob("call-*.txt")).read_text(encoding="utf-8")
            self.assertIn("EMAIL EXECUTOR (DRY RUN)", text)
            self.assertIn("NOT SENT", text)
            duplicate = execute_email_action(MISSION, action["action_id"], state, draft,
                                             result, existing_results=latest["email_execution_results"],
                                             dry_run_setting="true")
            self.assertEqual(duplicate["reason_code"], "already_processed")

    def test_secret_redaction_and_no_external_clients(self):
        state, draft, result, action, _ = artifacts()
        draft["body"] = "CALL_SECRET is sensitive; harmless follow up."
        output = execute_email_action(MISSION, action["action_id"], state, draft,
                                      result, dry_run_setting="true")
        self.assertNotIn("CALL_SECRET", output["body"])
        source = Path("email_executor.py").read_text(encoding="utf-8").casefold()
        tree = ast.parse(source)
        imported = {alias.name.casefold() for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        self.assertFalse(imported & {"smtplib", "requests", "httpx", "aiohttp", "socket", "websockets"})
        self.assertNotIn("smtp", source)
        self.assertNotIn("graph.microsoft", source)
        for filename in ("email_provider.py", "email_provider_fake.py"):
            source = Path(filename).read_text(encoding="utf-8").casefold()
            tree = ast.parse(source)
            imported = {alias.name.split(".")[0]
                        for node in ast.walk(tree)
                        if isinstance(node, (ast.Import, ast.ImportFrom))
                        for alias in node.names}
            self.assertFalse(imported & {"requests", "httpx", "aiohttp", "urllib",
                                         "socket", "smtplib", "twilio"})
        fake_source = Path("email_provider_fake.py").read_text(encoding="utf-8").casefold()
        self.assertNotIn("credential", fake_source)
        self.assertNotIn("http", fake_source)


class EmailExecutorEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = LocalFileCallResultStorage(self.temp_dir.name)
        self.patches = [
            patch.object(main, "call_result_storage", self.storage),
            patch.object(main, "load_mission_catalog", Catalog),
            patch.dict(os.environ, {"CALL_SECRET": "email-executor-test-secret", "EMAIL_DRY_RUN": "true"}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    async def seed_and_approve(self):
        state, draft, result, action, decision = artifacts(status="awaiting_approval")
        decision["status"] = "pending"
        decision["resolved_by"] = None
        decision["resolved_at"] = None
        state["decisions"] = [decision]
        action["status"] = "awaiting_approval"
        state["actions"] = [action]
        state.update({"schema_version": 1, "status": "waiting_for_user", "decisions_pending": [decision["decision_id"]],
                      "facts_obtained": [], "requests_detected": [], "next_steps": [],
                      "needs_follow_up": False, "evidence_complete": False})
        draft["approval_status"] = "pending"
        result.update({"mission_state": state, "email_draft": draft})
        self.storage.write(result)
        response = await main.resolve_mission_decision_endpoint(
            MISSION, decision["decision_id"], request("POST", "/resolve", body={"status": "approved"})
        )
        return response, action

    async def test_authenticated_approval_records_dry_run_only(self):
        with patch.object(main, "requests") as http:
            response, action = await self.seed_and_approve()
        http.assert_not_called()
        self.assertEqual(response.status_code, 200)
        persisted = self.storage.latest_mission_result(MISSION)
        execution = persisted["email_execution_results"][action["action_id"]]
        self.assertEqual(execution["status"], "dry_run")
        self.assertIs(execution["sent"], False)
        self.assertIs(execution["dry_run"], True)

    async def test_approved_action_uses_only_the_injected_fake_provider(self):
        provider = FakeEmailProvider()
        with patch.dict(os.environ, {
            "EMAIL_ENABLED": "true", "EMAIL_DRY_RUN": "true", "EMAIL_PROVIDER": "fake",
        }, clear=False), patch.object(main, "email_action_provider", provider):
            response, action = await self.seed_and_approve()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(provider.attempts), 1)
        saved = self.storage.latest_mission_result(MISSION)
        execution = saved["email_execution_results"][action["action_id"]]
        self.assertEqual(execution["status"], "dry_run")
        self.assertEqual(execution["provider"], "fake")
        self.assertEqual(execution["provider_message_id"], "fake-message-1")
        self.assertFalse(execution["sent"])
        self.assertIsNone(execution["executed_at"])
        self.assertEqual(set(provider.attempts[0]), {"to", "cc", "subject", "body"})
        response = await main.get_email_action_result(
            MISSION, action["action_id"], request("GET", "/result")
        )
        self.assertEqual(len(provider.attempts), 1)
        payload = json.loads(response.body)
        self.assertEqual(payload["provider"], "fake")
        self.assertEqual(payload["provider_message_id"], "fake-message-1")

    async def test_result_endpoint_requires_auth_and_mission(self):
        response, action = await self.seed_and_approve()
        unauthorized = await main.get_email_action_result(
            MISSION, action["action_id"], request("GET", "/result", secret=None)
        )
        self.assertEqual(unauthorized.status_code, 401)
        missing_mission = await main.get_email_action_result(
            "unknown_mission", action["action_id"], request("GET", "/result")
        )
        self.assertEqual(missing_mission.status_code, 404)
        allowed = await main.get_email_action_result(
            MISSION, action["action_id"], request("GET", "/result")
        )
        payload = json.loads(allowed.body)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(payload["mission_id"], MISSION)
        self.assertIs(payload["sent"], False)
        self.assertIs(payload["dry_run"], True)
        self.assertEqual(payload["provider"], "none")
        self.assertNotIn("transcript", payload)

    async def test_no_public_email_send_route_exists(self):
        routes = [
            (route.path, set(getattr(route, "methods", set())))
            for route in main.app.routes
            if getattr(route, "methods", None) is not None
        ]
        self.assertFalse(any("/send" in path and "email" in path for path, _ in routes))
        self.assertFalse(any(path.endswith("/email-result") and "POST" in methods
                             for path, methods in routes))


if __name__ == "__main__":
    unittest.main()
