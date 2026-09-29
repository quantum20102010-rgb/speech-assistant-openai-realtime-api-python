import ast
import asyncio
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
from action_executor import create_action, prepare_call_actions
from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from mission_decision import create_pending_decision
from mission_state import update_mission_state
from missions import MissionNotFoundError
from notification_message import build_notification_message
from whatsapp_adapter import prepare_whatsapp
from whatsapp_executor import execute_whatsapp_action, mask_whatsapp_recipient


MISSION = "wa_executor_mission"
OTHER_MISSION = "other_wa_executor_mission"
TEST_RECIPIENT = "+19999999999"
TEST_SECRET = "local-whatsapp-executor-test-secret"
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def mission_fixture(mission_id=MISSION):
    return SimpleNamespace(id=mission_id, name="Mission", objective="Objective",
                           required_information=(), relevance_criteria=(), follow_up_actions=())


class Catalog:
    def get(self, mission_id):
        if mission_id not in {MISSION, OTHER_MISSION}:
            raise MissionNotFoundError("not found")
        return mission_fixture(mission_id)


def make_request(method, path, body=None, secret=TEST_SECRET):
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


def artifacts(mission_id=MISSION):
    notification = {
        "schema_version": 1, "should_notify": True, "priority": "normal",
        "reason_code": "actionable_business_information", "reasons": ["transcript_supported_business_information"],
        "facts_to_notify": [], "requests_from_contact": [], "decisions_needed_from_user": [],
        "next_steps": [], "recommendations": [], "notification_summary": "",
    }
    message = build_notification_message(notification, {"company": "Proveedor"})
    decision = create_pending_decision(
        mission_id, "Enviar notificación por WhatsApp a Fabian", "notification_decision"
    )
    decision.update({"status": "approved", "resolved_by": "Fabian", "resolution": "Approved",
                     "resolved_at": NOW.isoformat()})
    action = create_action(mission_id, "send_whatsapp",
                           "Enviar mensaje WhatsApp con aprobación", NOW,
                           decision["decision_id"])
    action.update({"status": "approved", "approval_status": "approved",
                   "approved_at": NOW.isoformat()})
    state = {"mission_id": mission_id, "decisions": [decision], "actions": [action]}
    result = {
        "mission_id": mission_id,
        "company": "Proveedor",
        "notification_decision": notification,
        "notification_message": message,
        "whatsapp_preview": {"schema_version": 1, "status": "ready_for_send",
                             "sent": False, "dry_run": True, "recipient_configured": True},
    }
    return state, result, action, decision


class WhatsAppExecutorTests(unittest.TestCase):
    def execute(self, state=None, result=None, action=None, **overrides):
        default_state, default_result, default_action, _ = artifacts()
        state = default_state if state is None else state
        result = default_result if result is None else result
        action = default_action if action is None else action
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true",
                                     "WHATSAPP_RECIPIENT": TEST_RECIPIENT}, clear=False):
            return execute_whatsapp_action(MISSION, action["action_id"], state, result, **overrides)

    def test_approved_action_produces_dry_run_only(self):
        state, result, action, _ = artifacts()
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true", "WHATSAPP_RECIPIENT": TEST_RECIPIENT}):
            output = execute_whatsapp_action(MISSION, action["action_id"], state, result, now=NOW)
        self.assertEqual(output["status"], "dry_run")
        self.assertEqual(output["provider"], "none")
        self.assertEqual(output["recipient"], TEST_RECIPIENT)
        self.assertEqual(output["message"], result["notification_message"]["message"])
        self.assertIs(output["sent"], False)
        self.assertIs(output["dry_run"], True)
        self.assertIsNone(output["executed_at"])

    def test_nonapproved_action_states_do_not_run(self):
        for status in ("awaiting_approval", "rejected", "deferred", "prepared", "blocked_by_policy"):
            state, result, action, _ = artifacts()
            state["actions"][0]["status"] = status
            with self.subTest(status=status), patch.dict(os.environ, {
                "WHATSAPP_ENABLED": "true", "WHATSAPP_RECIPIENT": TEST_RECIPIENT
            }):
                output = execute_whatsapp_action(MISSION, action["action_id"], state, result)
            self.assertNotEqual(output["status"], "dry_run")
            self.assertIs(output["sent"], False)

    def test_action_type_mission_and_missing_action_checks(self):
        state, result, action, _ = artifacts()
        state["actions"][0]["action_type"] = "send_email"
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "wrong_action_type")
        state, result, action, _ = artifacts(OTHER_MISSION)
        self.assertEqual(execute_whatsapp_action(MISSION, action["action_id"], state, result)["reason_code"],
                         "mission_mismatch")
        state, result, action, _ = artifacts()
        self.assertEqual(execute_whatsapp_action(MISSION, "act_missing", state, result)["reason_code"],
                         "action_not_found")

    def test_missing_notification_or_incompatible_notification_is_blocked(self):
        state, result, action, _ = artifacts()
        del result["notification_decision"]
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "notification_decision_missing")
        state, result, action, _ = artifacts()
        result["notification_decision"]["should_notify"] = False
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "notification_not_approved_for_send")
        state, result, action, _ = artifacts()
        del result["notification_message"]
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "notification_message_missing")
        state, result, action, _ = artifacts()
        result["notification_message"]["should_send"] = False
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "message_not_sendable")

    def test_empty_or_tampered_message_is_blocked(self):
        state, result, action, _ = artifacts()
        result["notification_message"]["message"] = "Mensaje alterado intencionalmente"
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "notification_message_mismatch")
        state, result, action, _ = artifacts()
        result["notification_message"]["message"] = ""
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "message_empty")

    def test_recipient_missing_or_invalid_blocks_without_echoing_number(self):
        state, result, action, _ = artifacts()
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "false", "WHATSAPP_RECIPIENT": TEST_RECIPIENT}):
            disabled = execute_whatsapp_action(MISSION, action["action_id"], state, result)
        self.assertNotEqual(disabled["status"], "dry_run")
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true", "WHATSAPP_RECIPIENT": "invalid-private-recipient"}):
            invalid = execute_whatsapp_action(MISSION, action["action_id"], state, result)
        self.assertNotEqual(invalid["status"], "dry_run")
        self.assertNotIn("invalid-private-recipient", json.dumps(invalid))
        self.assertEqual(invalid["recipient"], "")

    def test_already_processed_and_idempotency(self):
        state, result, action, _ = artifacts()
        existing = {action["action_id"]: {"status": "dry_run"}}
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true", "WHATSAPP_RECIPIENT": TEST_RECIPIENT}):
            repeated = execute_whatsapp_action(MISSION, action["action_id"], state, result,
                                               existing_results=existing)
        self.assertEqual(repeated["status"], "already_processed")
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFileCallResultStorage(directory)
            result.update({"mission_state": state})
            storage.write(result)
            with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true", "WHATSAPP_RECIPIENT": TEST_RECIPIENT}):
                output = execute_whatsapp_action(MISSION, action["action_id"], state, result, now=NOW)
            first = storage.record_whatsapp_execution_result(MISSION, action["action_id"], output)
            second = storage.record_whatsapp_execution_result(
                MISSION, action["action_id"], {**output, "message": "other"}
            )
            self.assertEqual(first, second)
            self.assertEqual(storage.latest_mission_result(MISSION)["whatsapp_execution_results"][action["action_id"]], output)
            text = next(Path(directory).glob("call-*.txt")).read_text(encoding="utf-8")
            self.assertIn("WHATSAPP EXECUTOR (DRY RUN)", text)
            self.assertIn(mask_whatsapp_recipient(TEST_RECIPIENT), text)

    def test_formal_approval_cannot_be_forged_with_argument(self):
        state, result, action, _ = artifacts()
        with self.assertRaises(TypeError):
            execute_whatsapp_action(MISSION, action["action_id"], state, result, approved=True)
        state["decisions"] = []
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "formal_approval_required")

    def test_action_and_decision_are_linked_to_same_mission(self):
        state, result, action, decision = artifacts()
        state["decisions"][0]["mission_id"] = OTHER_MISSION
        self.assertNotEqual(self.execute(state=state, result=result, action=action)["status"], "dry_run")
        state, result, action, decision = artifacts()
        state["actions"][0]["approval_decision_id"] = "another_decision"
        self.assertNotEqual(self.execute(state=state, result=result, action=action)["status"], "dry_run")

    def test_notification_and_adapter_configuration_fail_closed(self):
        state, result, action, _ = artifacts()
        result.pop("whatsapp_preview")
        self.assertEqual(self.execute(state=state, result=result, action=action)["reason_code"],
                         "adapter_preview_missing")
        state, result, action, _ = artifacts()
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true"}, clear=True):
            missing = execute_whatsapp_action(MISSION, action["action_id"], state, result)
        self.assertEqual(missing["reason_code"], "recipient_or_adapter_not_ready")

    def test_approved_still_dry_run_when_whatsapp_enabled_true(self):
        state, result, action, _ = artifacts()
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true", "WHATSAPP_RECIPIENT": TEST_RECIPIENT}):
            output = execute_whatsapp_action(MISSION, action["action_id"], state, result)
        self.assertEqual(output["status"], "dry_run")
        self.assertFalse(output["sent"])

    def test_no_network_or_provider_clients(self):
        for filename in ("whatsapp_executor.py", "whatsapp_adapter.py"):
            source = Path(filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imported = {alias.name.split(".")[0].casefold()
                        for node in ast.walk(tree)
                        if isinstance(node, (ast.Import, ast.ImportFrom))
                        for alias in node.names}
            self.assertFalse(imported & {"requests", "httpx", "aiohttp", "urllib3", "socket",
                                         "websockets", "twilio", "smtplib", "meta"})
        source = Path("whatsapp_executor.py").read_text(encoding="utf-8").casefold()
        self.assertNotIn("graph.facebook", source)
        self.assertNotIn("graph.microsoft", source)


class WhatsAppExecutorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = LocalFileCallResultStorage(self.temp_dir.name)
        self.patches = [
            patch.object(main, "call_result_storage", self.storage),
            patch.object(main, "load_mission_catalog", Catalog),
            patch.dict(os.environ, {"CALL_SECRET": TEST_SECRET,
                                    "WHATSAPP_ENABLED": "true",
                                    "WHATSAPP_RECIPIENT": TEST_RECIPIENT}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    async def seed_pending_action(self):
        state, result, action, decision = artifacts()
        decision.update({"status": "pending", "resolved_by": None, "resolved_at": None,
                         "resolution": None})
        action.update({"status": "awaiting_approval", "approval_status": "pending",
                       "approved_at": None})
        state.update({"schema_version": 1, "status": "waiting_for_user",
                      "decisions_pending": [decision["decision_id"]], "facts_obtained": [],
                      "requests_detected": [], "next_steps": [], "needs_follow_up": False,
                      "evidence_complete": False})
        result["mission_state"] = state
        self.storage.write(result)
        request = make_request("POST", "/resolve", body={"status": "approved"})
        response = await main.resolve_mission_decision_endpoint(
            MISSION, decision["decision_id"], request
        )
        return response, action

    async def test_approval_persists_preview_without_provider_calls(self):
        with patch.object(main, "requests") as http:
            response, action = await self.seed_pending_action()
        http.assert_not_called()
        self.assertEqual(response.status_code, 200)
        saved = self.storage.latest_mission_result(MISSION)
        output = saved["whatsapp_execution_results"][action["action_id"]]
        self.assertEqual(output["status"], "dry_run")
        self.assertIs(output["sent"], False)
        self.assertIs(output["dry_run"], True)
        self.assertNotIn("transcript", output)
        saved_action = next(
            item for item in saved["mission_state"]["actions"]
            if item["action_id"] == action["action_id"]
        )
        self.assertEqual(saved_action["status"], "dry_run")
        self.assertIsNone(saved_action["executed_at"])

    async def test_result_endpoint_authentication_mission_and_masking(self):
        _, action = await self.seed_pending_action()
        unauthenticated = await main.get_whatsapp_action_result(
            MISSION, action["action_id"], make_request("GET", "/result", secret=None)
        )
        self.assertEqual(unauthenticated.status_code, 401)
        unknown = await main.get_whatsapp_action_result(
            "unknown_mission", action["action_id"], make_request("GET", "/result")
        )
        self.assertEqual(unknown.status_code, 404)
        response = await main.get_whatsapp_action_result(
            MISSION, action["action_id"], make_request("GET", "/result")
        )
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["recipient"], mask_whatsapp_recipient(TEST_RECIPIENT))
        self.assertNotIn(TEST_RECIPIENT, response.body.decode())
        self.assertFalse(payload["sent"])
        self.assertTrue(payload["dry_run"])
        self.assertNotIn("transcript", payload)

    async def test_disabled_adapter_does_not_create_approvable_whatsapp_action(self):
        decision = create_pending_decision(MISSION, "Enviar notificación por WhatsApp a Fabian",
                                           "notification_decision")
        state = {"mission_id": MISSION, "decisions": [decision], "actions": []}
        message = {"should_send": True, "channel": "whatsapp", "priority": "normal",
                   "message": "Vista previa", "message_type": "business_update"}
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "false", "WHATSAPP_RECIPIENT": TEST_RECIPIENT}):
            prepared = prepare_call_actions(state, {}, None, prepare_whatsapp(message),
                                            notification_message=message)
        self.assertFalse(any(item["action_type"] == "send_whatsapp" for item in prepared["actions"]))
        with patch.dict(os.environ, {"WHATSAPP_ENABLED": "true", "WHATSAPP_RECIPIENT": TEST_RECIPIENT}):
            ready = prepare_whatsapp(message)
            prepared = prepare_call_actions(state, {}, None, ready, notification_message=message)
        action = next(item for item in prepared["actions"] if item["action_type"] == "send_whatsapp")
        self.assertEqual(action["status"], "awaiting_approval")
        self.assertEqual(action["approval_decision_id"], decision["decision_id"])
        self.assertFalse(prepare_whatsapp(message)["sent"])

    async def test_mission_state_records_notification_approval_decision(self):
        state = update_mission_state(
            None, {"mission_id": MISSION, "call_status": "completed"}, mission_fixture(),
            {}, {"should_notify": True, "decisions_needed_from_user": []}, {},
            whatsapp_approval_needed=True,
        )
        self.assertTrue(any("WhatsApp" in item["description"] for item in state["decisions"]))


if __name__ == "__main__":
    unittest.main()
