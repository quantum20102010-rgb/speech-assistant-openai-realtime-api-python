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

import action_executor
import main
from action_executor import (
    ActionPolicyError,
    create_action,
    prepare_action,
    prepare_call_actions,
    resolve_action_from_mission_decision,
    sync_action_approvals,
)
from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from mission_decision import create_pending_decision, resolve_mission_decision
from mission_state import update_mission_state
from missions import MissionNotFoundError


MISSION_ID = "action_mission"
OTHER_MISSION_ID = "other_action_mission"
TEST_SECRET = "local-action-executor-secret"


def mission_fixture(mission_id=MISSION_ID):
    return SimpleNamespace(
        id=mission_id, name="Mission", objective="Commercial objective",
        required_information=({"key": "interest", "description": "Interest"},),
        relevance_criteria=("interest",), follow_up_actions=("Review manually",),
    )


class Catalog:
    def get(self, mission_id):
        if mission_id not in {MISSION_ID, OTHER_MISSION_ID}:
            raise MissionNotFoundError("not found")
        return mission_fixture(mission_id)


def request(method, path, secret=TEST_SECRET):
    headers = [] if secret is None else [(b"x-call-secret", secret.encode())]
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "https", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": headers, "client": ("127.0.0.1", 1), "server": ("testserver", 443),
    }, receive)


class ActionExecutorCoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)

    def pending_decision(self, mission_id=MISSION_ID, description="Enviar email comercial"):
        return create_pending_decision(mission_id, description, "decision_engine")

    def test_automatic_local_action_is_prepared_without_approval(self):
        state, action = prepare_action(
            {"mission_id": MISSION_ID}, "store_information", "Guardar hechos verificados", self.now
        )
        self.assertEqual(action["status"], "prepared")
        self.assertFalse(action["requires_approval"])
        self.assertEqual(state["actions"], [action])

    def test_external_action_waits_for_formal_approval(self):
        decision = self.pending_decision()
        action = create_action(
            MISSION_ID, "send_email", "Enviar email revisado", self.now,
            decision["decision_id"],
        )
        self.assertEqual(action["status"], "awaiting_approval")
        self.assertTrue(action["requires_approval"])
        self.assertEqual(action["approval_status"], "pending")

    def test_forbidden_action_stays_blocked_even_with_approved_decision(self):
        decision = self.pending_decision(description="Aceptar contrato")
        action = create_action(
            MISSION_ID, "accept_contract", "Aceptar contrato", self.now,
            decision["decision_id"],
        )
        decision.update({"status": "approved", "resolved_by": "Fabian",
                         "resolved_at": self.now.isoformat()})
        state = sync_action_approvals({
            "mission_id": MISSION_ID, "actions": [action], "decisions": [decision]
        }, self.now)
        self.assertEqual(action["status"], "blocked_by_policy")
        self.assertEqual(state["actions"][0]["status"], "blocked_by_policy")

    def test_commercial_commitment_phrases_classify_as_forbidden(self):
        cases = {
            "Aprobar precio": "accept_price_or_condition",
            "Aceptar condiciones": "accept_price_or_condition",
            "Firmar contrato": "accept_contract",
            "Realizar pago": "make_payment",
            "Activar servicio": "activate_service",
        }
        for description, expected_type in cases.items():
            with self.subTest(description=description):
                action_type = action_executor.classify_action_type(description)
                action = create_action(MISSION_ID, action_type, description, self.now)
                self.assertEqual(action_type, expected_type)
                self.assertEqual(action["status"], "blocked_by_policy")

    def test_approval_requires_matching_resolved_mission_decision(self):
        decision = self.pending_decision()
        action = create_action(
            MISSION_ID, "send_email", "Enviar email revisado", self.now,
            decision["decision_id"],
        )
        approved = dict(decision, status="approved", resolved_by="Fabian",
                        resolved_at=self.now.isoformat())
        resolved = resolve_action_from_mission_decision(
            [action], MISSION_ID, action["action_id"], decision["decision_id"],
            [approved], self.now,
        )
        self.assertEqual(resolved["status"], "approved")
        self.assertEqual(resolved["approval_status"], "approved")
        self.assertEqual(resolved["approved_at"], self.now.isoformat())

    def test_pending_decision_is_not_an_approval(self):
        decision = self.pending_decision()
        action = create_action(
            MISSION_ID, "send_email", "Enviar email revisado", self.now,
            decision["decision_id"],
        )
        with self.assertRaises(ActionPolicyError) as error:
            resolve_action_from_mission_decision(
                [action], MISSION_ID, action["action_id"], decision["decision_id"],
                [decision], self.now,
            )
        self.assertEqual(str(error.exception), "approval_not_resolved")

    def test_approval_for_a_different_action_type_is_not_accepted(self):
        decision = self.pending_decision(description="Firmar contrato")
        action = create_action(
            MISSION_ID, "send_email", "Enviar email revisado", self.now,
            decision["decision_id"],
        )
        approved = dict(
            decision, status="approved", resolved_by="Fabian",
            resolved_at=self.now.isoformat(),
        )
        state = sync_action_approvals({
            "mission_id": MISSION_ID, "actions": [action], "decisions": [approved],
        }, self.now)
        self.assertEqual(state["actions"][0]["status"], "awaiting_approval")
        self.assertEqual(state["actions"][0]["approval_status"], "unlinked")

    def test_decision_from_another_mission_cannot_approve_action(self):
        decision = self.pending_decision(OTHER_MISSION_ID)
        action = create_action(
            MISSION_ID, "send_email", "Enviar email revisado", self.now,
            decision["decision_id"],
        )
        with self.assertRaises(ActionPolicyError) as error:
            resolve_action_from_mission_decision(
                [action], MISSION_ID, action["action_id"], decision["decision_id"],
                [decision], self.now,
            )
        self.assertEqual(str(error.exception), "mission_mismatch")

    def test_action_from_another_mission_is_rejected(self):
        foreign = create_action(OTHER_MISSION_ID, "send_email", "Enviar email", self.now)
        with self.assertRaises(ActionPolicyError) as error:
            resolve_action_from_mission_decision(
                [foreign], MISSION_ID, foreign["action_id"], "decision",
                [], self.now,
            )
        self.assertEqual(str(error.exception), "mission_mismatch")

    def test_nonexistent_action_is_rejected(self):
        with self.assertRaises(ActionPolicyError) as error:
            resolve_action_from_mission_decision([], MISSION_ID, "missing", "decision", [])
        self.assertEqual(str(error.exception), "action_not_found")

    def test_rejection_and_deferral_are_mirrored_without_execution(self):
        for outcome in ("rejected", "deferred"):
            with self.subTest(outcome=outcome):
                decision = self.pending_decision()
                action = create_action(
                    MISSION_ID, "send_email", "Enviar email revisado", self.now,
                    decision["decision_id"],
                )
                resolved_decision = dict(decision, status=outcome, resolved_by="Fabian",
                                         resolved_at=self.now.isoformat())
                resolved = resolve_action_from_mission_decision(
                    [action], MISSION_ID, action["action_id"], decision["decision_id"],
                    [resolved_decision], self.now,
                )
                self.assertEqual(resolved["status"], outcome)
                self.assertIsNone(resolved["executed_at"])

    def test_double_approval_is_rejected(self):
        decision = self.pending_decision()
        action = create_action(MISSION_ID, "send_email", "Enviar email", self.now,
                               decision["decision_id"])
        approved = dict(decision, status="approved", resolved_by="Fabian",
                        resolved_at=self.now.isoformat())
        first = resolve_action_from_mission_decision(
            [action], MISSION_ID, action["action_id"], decision["decision_id"], [approved], self.now
        )
        with self.assertRaises(ActionPolicyError) as error:
            resolve_action_from_mission_decision(
                [first], MISSION_ID, first["action_id"], decision["decision_id"], [approved], self.now
            )
        self.assertEqual(str(error.exception), "action_already_resolved")

    def test_rejected_or_deferred_action_cannot_be_approved_later(self):
        for previous in ("rejected", "deferred"):
            with self.subTest(previous=previous):
                decision = self.pending_decision()
                action = create_action(
                    MISSION_ID, "send_email", "Enviar email", self.now,
                    decision["decision_id"],
                )
                rejected = dict(decision, status=previous, resolved_by="Fabian",
                                resolved_at=self.now.isoformat())
                first = resolve_action_from_mission_decision(
                    [action], MISSION_ID, action["action_id"], decision["decision_id"],
                    [rejected], self.now,
                )
                approved = dict(decision, status="approved", resolved_by="Fabian",
                                resolved_at=self.now.isoformat())
                with self.assertRaises(ActionPolicyError) as error:
                    resolve_action_from_mission_decision(
                        [first], MISSION_ID, first["action_id"], decision["decision_id"],
                        [approved], self.now,
                    )
                self.assertEqual(str(error.exception), "action_already_resolved")

    def test_prepare_is_idempotent_and_preserves_first_timestamp(self):
        initial, first = prepare_action(
            {"mission_id": MISSION_ID}, "send_email", "Enviar email", self.now,
        )
        later, second = prepare_action(
            initial, "send_email", "Enviar email", datetime(2026, 9, 28, tzinfo=timezone.utc),
        )
        self.assertEqual(first["action_id"], second["action_id"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(len(later["actions"]), 1)

    def test_action_ids_are_unique_across_missions(self):
        first = create_action(MISSION_ID, "send_email", "Enviar email", self.now)
        second = create_action(OTHER_MISSION_ID, "send_email", "Enviar email", self.now)
        self.assertNotEqual(first["action_id"], second["action_id"])

    def test_approved_action_has_no_executed_transition(self):
        action = create_action(MISSION_ID, "send_email", "Enviar email", self.now)
        self.assertIn("executed", action_executor.ACTION_STATUSES)
        self.assertIsNone(action["executed_at"])
        self.assertFalse(hasattr(action_executor, "execute_action"))

    def test_prepare_call_actions_links_email_to_formal_mission_decision(self):
        decision_record = self.pending_decision()
        state = {"mission_id": MISSION_ID, "decisions": [decision_record], "actions": []}
        prepared = prepare_call_actions(
            state,
            {"approval_required": [decision_record["description"]]},
            {"should_create": True, "approval_decision_ids": [decision_record["decision_id"]]},
            {"should_send": False}, self.now,
        )
        email_actions = [item for item in prepared["actions"] if item["action_type"] == "send_email"]
        self.assertEqual(len(email_actions), 1)
        self.assertEqual(email_actions[0]["approval_decision_id"], decision_record["decision_id"])
        self.assertEqual(email_actions[0]["status"], "awaiting_approval")

    def test_action_records_survive_subsequent_mission_state_updates(self):
        action = create_action(MISSION_ID, "store_information", "Registrar hechos")
        previous = {"mission_id": MISSION_ID, "actions": [action]}
        updated = update_mission_state(
            previous,
            {"mission_id": MISSION_ID, "call_status": "completed"},
            mission_fixture(),
            {}, {}, {},
        )
        self.assertEqual(updated["actions"], [action])

    def test_formal_mission_decision_updates_linked_action_metadata_only(self):
        for outcome in ("approved", "rejected", "deferred"):
            with self.subTest(outcome=outcome):
                decision = self.pending_decision()
                action = create_action(
                    MISSION_ID, "send_email", "Enviar email comercial",
                    self.now, decision["decision_id"],
                )
                state = {
                    "mission_id": MISSION_ID,
                    "status": "waiting_for_user",
                    "decisions": [decision],
                    "decisions_pending": [decision["decision_id"]],
                    "actions": [action],
                    "facts_obtained": [], "requests_detected": [],
                    "information_missing": [], "next_steps": [],
                    "needs_follow_up": False, "evidence_complete": False,
                }
                updated = resolve_mission_decision(
                    state, mission_fixture(), MISSION_ID, decision["decision_id"],
                    outcome, "Decisión registrada por Fabian.", now=self.now,
                )
                saved_action = updated["actions"][0]
                self.assertEqual(saved_action["status"], outcome)
                self.assertIsNone(saved_action["executed_at"])
                self.assertEqual(state["actions"][0]["status"], "awaiting_approval")

    def test_action_descriptions_redact_secrets_addresses_and_phone_numbers(self):
        previous = os.environ.get("CALL_SECRET")
        os.environ["CALL_SECRET"] = TEST_SECRET
        try:
            action = create_action(
                MISSION_ID, "external_follow_up",
                f"Contactar ventas@example.test al +52 555 010 2030 usando {TEST_SECRET}",
                self.now,
            )
        finally:
            if previous is None:
                os.environ.pop("CALL_SECRET", None)
            else:
                os.environ["CALL_SECRET"] = previous
        serialized = json.dumps(action)
        self.assertNotIn(TEST_SECRET, serialized)
        self.assertNotIn("ventas@example.test", serialized)
        self.assertNotIn("555 010 2030", serialized)

    def test_no_external_clients_or_execution_calls_exist_in_module(self):
        tree = ast.parse(Path(action_executor.__file__).read_text(encoding="utf-8"))
        forbidden = {"twilio", "smtplib", "requests", "httpx", "urllib", "email", "msgraph"}
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(imported & forbidden)
        self.assertFalse(any(
            isinstance(node, ast.Attribute) and node.attr.casefold() in {"send", "post", "request", "connect"}
            for node in ast.walk(tree)
        ))


class ActionEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage = LocalFileCallResultStorage(self.directory.name)
        self.patches = [
            patch.object(main, "call_result_storage", self.storage),
            patch.object(main, "load_mission_catalog", Catalog),
            patch.dict(os.environ, {"CALL_SECRET": TEST_SECRET}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def test_authenticated_action_list_returns_mission_actions_only(self):
        action = create_action(MISSION_ID, "store_information", "Guardar datos")
        result = empty_call_result("completed", "spanish", mission=mission_fixture())
        result["mission_state"] = {"mission_id": MISSION_ID, "actions": [action]}
        self.storage.write(result)
        response = await main.list_mission_actions(
            MISSION_ID, request("GET", f"/missions/{MISSION_ID}/actions")
        )
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(payload["actions"]), 1)
        self.assertNotIn("approval_decision_id", payload["actions"][0])

    async def test_action_list_requires_authentication(self):
        response = await main.list_mission_actions(
            MISSION_ID, request("GET", "/actions", secret=None)
        )
        self.assertEqual(response.status_code, 401)

    async def test_action_list_rejects_unknown_mission(self):
        response = await main.list_mission_actions(
            "unknown_mission", request("GET", "/unknown/actions")
        )
        self.assertEqual(response.status_code, 404)

    async def test_historical_result_without_actions_remains_readable(self):
        old = empty_call_result("completed", "spanish", mission=mission_fixture())
        old["mission_state"] = {"mission_id": MISSION_ID, "status": "pending"}
        self.storage.write(old)
        self.assertIn("REPORTE DE LLAMADA", render_call_report(old))
        response = await main.list_mission_actions(
            MISSION_ID, request("GET", "/actions")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["actions"], [])

    async def test_formal_resolution_persists_action_approval_without_execution(self):
        decision = create_pending_decision(
            MISSION_ID, "Enviar email comercial", "decision_engine"
        )
        action = create_action(
            MISSION_ID, "send_email", "Enviar email comercial",
            approval_decision_id=decision["decision_id"],
        )
        result = empty_call_result("completed", "spanish", mission=mission_fixture())
        result["mission_state"] = {
            "mission_id": MISSION_ID,
            "status": "waiting_for_user",
            "decisions": [decision],
            "decisions_pending": [decision["decision_id"]],
            "actions": [action],
            "facts_obtained": [], "requests_detected": [], "next_steps": [],
            "needs_follow_up": False, "evidence_complete": False,
        }
        self.storage.write(result)
        body = json.dumps({"status": "approved", "resolution": "Decisión explícita."}).encode()
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        req = Request({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "https",
            "path": "/resolve", "raw_path": b"/resolve", "query_string": b"",
            "root_path": "", "headers": [
                (b"x-call-secret", TEST_SECRET.encode()),
                (b"content-type", b"application/json"),
            ], "client": ("127.0.0.1", 1), "server": ("testserver", 443),
        }, receive)
        with patch.object(main, "requests") as http, patch.object(main, "prepare_whatsapp") as whatsapp:
            response = await main.resolve_mission_decision_endpoint(
                MISSION_ID, decision["decision_id"], req
            )
        saved = self.storage.latest_mission_state(MISSION_ID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved["actions"][0]["status"], "approved")
        self.assertIsNone(saved["actions"][0]["executed_at"])
        http.assert_not_called()
        whatsapp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
