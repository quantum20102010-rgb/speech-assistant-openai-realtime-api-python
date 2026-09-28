import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Request

import email_draft
import main
from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from mission_decision import create_pending_decision
from missions import MissionNotFoundError


MISSION_ID = "email_draft_mission"
OTHER_MISSION_ID = "other_email_mission"
CALL_SECRET_TEST_VALUE = "local-email-draft-test-secret"
CONTACT_EMAIL = "ventas@example.test"
TRANSCRIPT = (
    "Contacto: Nos interesa distribuir sus productos y conocer el catálogo. "
    "Nuestro correo es ventas@example.test.\n"
    "Asistente: Con gusto, revisaré la información disponible."
)


def mission_fixture(mission_id=MISSION_ID):
    return SimpleNamespace(
        id=mission_id,
        name="Commercial outreach",
        objective="Confirmar interés comercial.",
        required_information=({"key": "interest", "description": "Interés comercial"},),
        relevance_criteria=("distribución",),
        follow_up_actions=("Revisar solicitud del catálogo",),
    )


def artifacts(mission_id=MISSION_ID, email=CONTACT_EMAIL, transcript=TRANSCRIPT):
    state = {
        "mission_id": mission_id,
        "status": "in_progress",
        "facts_obtained": [{
            "field": "interest",
            "label": "Interés comercial",
            "value": "Distribución de productos",
        }],
        "decisions": [],
        "decisions_pending": [],
        "next_steps": [],
    }
    decision = {
        "status": "awaiting_user",
        "relevance": "actionable",
        "facts": [
            {"field": "email", "value": email, "source": "transcript_verified_result"},
            {"field": "interest", "label": "Interés comercial",
             "value": "Distribución de productos", "source": "transcript_verified_result"},
        ],
        "requests_from_contact": ["conocer el catálogo"],
        "follow_up": {"required": False, "next_step": None},
    }
    notification = {
        "should_notify": True,
        "facts_to_notify": [{
            "field": "interest", "text": "Interés comercial: Distribución de productos",
            "source": "transcript",
        }],
        "requests_from_contact": ["conocer el catálogo"],
        "next_steps": [],
    }
    result = {
        "mission_id": mission_id,
        "email": email,
        "transcript": transcript,
        "company": "Empresa de ejemplo",
    }
    return state, decision, notification, result


def make_request(method, path, secret=CALL_SECRET_TEST_VALUE):
    headers = [] if secret is None else [(b"x-call-secret", secret.encode())]
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": headers, "client": ("127.0.0.1", 1),
        "server": ("testserver", 443),
    }, receive)


class Catalog:
    def get(self, mission_id):
        if mission_id not in {MISSION_ID, OTHER_MISSION_ID}:
            raise MissionNotFoundError("not found")
        return mission_fixture(mission_id)


class EmailDraftTests(unittest.TestCase):
    def build(self, **overrides):
        state, decision, notification, result = artifacts()
        values = {
            "mission_id": MISSION_ID,
            "mission_state": state,
            "decision": decision,
            "notification_decision": notification,
            "call_result": result,
        }
        values.update(overrides)
        return email_draft.build_email_draft(**values)

    def test_no_draft_without_transcript_verified_email(self):
        draft = self.build(call_result={"mission_id": MISSION_ID, "email": None,
                                       "transcript": TRANSCRIPT})
        self.assertFalse(draft["should_create"])
        self.assertEqual(draft["reason_code"], "no_verified_email")

    def test_creates_draft_with_valid_email_and_supported_reason(self):
        draft = self.build()
        self.assertTrue(draft["should_create"])
        self.assertEqual(draft["to"], [CONTACT_EMAIL])

    def test_never_invents_email_from_unverified_structured_field(self):
        state, decision, notification, result = artifacts()
        result["email"] = "invented@example.test"
        draft = self.build(call_result=result)
        self.assertFalse(draft["should_create"])

    def test_phone_number_is_not_accepted_as_email(self):
        state, decision, notification, result = artifacts()
        result["email"] = "+525555010203"
        decision["facts"][0]["value"] = result["email"]
        draft = self.build(call_result=result, decision=decision)
        self.assertFalse(draft["should_create"])
        self.assertEqual(draft["to"], [])

    def test_subject_and_body_are_present_and_commercially_structured(self):
        draft = self.build()
        self.assertTrue(draft["subject"])
        self.assertIn("Hola,", draft["body"])
        self.assertIn("Información relevante:", draft["body"])
        self.assertIn("Quedó registrada tu solicitud:", draft["body"])
        self.assertIn("Saludos,", draft["body"])

    def test_body_uses_only_persisted_supported_facts(self):
        draft = self.build()
        self.assertIn("Distribución de productos", draft["body"])
        self.assertNotIn("precio", draft["body"].casefold())

    def test_body_does_not_copy_the_full_transcript(self):
        draft = self.build()
        self.assertNotIn(TRANSCRIPT, draft["body"])
        self.assertNotIn("Asistente:", draft["body"])

    def test_secret_and_internal_technical_data_are_removed(self):
        previous = os.environ.get("CALL_SECRET")
        os.environ["CALL_SECRET"] = CALL_SECRET_TEST_VALUE
        try:
            state, decision, notification, result = artifacts()
            state["facts_obtained"][0]["value"] += " " + CALL_SECRET_TEST_VALUE
            state["facts_obtained"].append({
                "field": "other", "label": "Dato", "value": "OPENAI_API_KEY internal"
            })
            draft = self.build(mission_state=state)
        finally:
            if previous is None:
                os.environ.pop("CALL_SECRET", None)
            else:
                os.environ["CALL_SECRET"] = previous
        self.assertNotIn(CALL_SECRET_TEST_VALUE, json.dumps(draft))
        self.assertNotIn("OPENAI_API_KEY", draft["body"])

    def test_every_result_requires_approval_and_is_unsent(self):
        for draft in (self.build(), self.build(call_result={"mission_id": MISSION_ID,
                                                            "transcript": TRANSCRIPT})):
            self.assertIs(draft["requires_approval"], True)
            self.assertIs(draft["sent"], False)

    def test_notification_without_reason_does_not_create_draft(self):
        _, _, notification, _ = artifacts()
        notification["should_notify"] = False
        draft = self.build(notification_decision=notification)
        self.assertFalse(draft["should_create"])

    def test_mission_id_mismatch_is_blocked(self):
        draft = self.build(mission_id=OTHER_MISSION_ID)
        self.assertFalse(draft["should_create"])
        self.assertEqual(draft["reason_code"], "mission_mismatch")

    def test_no_external_clients_or_send_operation_are_present(self):
        source = Path(email_draft.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {"smtplib", "requests", "httpx", "urllib", "twilio", "msgraph"}
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(imported & forbidden)
        self.assertFalse(any(
            isinstance(node, ast.Attribute) and node.attr.casefold() in {"send", "post", "request"}
            for node in ast.walk(tree)
        ))


class EmailDraftEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = LocalFileCallResultStorage(self.temp_dir.name)
        self.patches = [
            patch.object(main, "call_result_storage", self.storage),
            patch.object(main, "load_mission_catalog", Catalog),
            patch.dict(os.environ, {"CALL_SECRET": CALL_SECRET_TEST_VALUE}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    async def test_authenticated_endpoint_returns_only_persisted_draft(self):
        state, decision, notification, result = artifacts()
        call_result = empty_call_result("completed", "spanish", mission=mission_fixture())
        call_result.update(result)
        call_result["mission_state"] = state
        call_result["decision"] = decision
        call_result["notification_decision"] = notification
        call_result["email_draft"] = email_draft.build_email_draft(
            MISSION_ID, state, decision, notification, call_result
        )
        self.storage.write(call_result)
        response = await main.get_mission_email_draft(
            MISSION_ID, make_request("GET", f"/missions/{MISSION_ID}/email-draft")
        )
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["to"], [CONTACT_EMAIL])
        self.assertFalse(payload["sent"])
        self.assertNotIn("transcript", payload)
        self.assertNotIn("mission_id", payload)
        self.assertNotIn(CALL_SECRET_TEST_VALUE, response.body.decode("utf-8"))

    async def test_endpoint_requires_authentication(self):
        response = await main.get_mission_email_draft(
            MISSION_ID, make_request("GET", "/email-draft", secret=None)
        )
        self.assertEqual(response.status_code, 401)

    async def test_endpoint_rejects_unknown_mission(self):
        response = await main.get_mission_email_draft(
            "unknown_mission", make_request("GET", "/email-draft")
        )
        self.assertEqual(response.status_code, 404)

    async def test_endpoint_does_not_read_another_missions_result(self):
        state, decision, notification, result_values = artifacts()
        result = empty_call_result("completed", "spanish", mission=mission_fixture())
        result.update(result_values)
        result["mission_state"] = state
        result["email_draft"] = email_draft.build_email_draft(
            MISSION_ID, state, decision, notification, result
        )
        self.storage.write(result)
        response = await main.get_mission_email_draft(
            OTHER_MISSION_ID,
            make_request("GET", f"/missions/{OTHER_MISSION_ID}/email-draft"),
        )
        self.assertEqual(response.status_code, 404)

    async def test_historical_result_without_email_draft_remains_readable(self):
        old = empty_call_result("completed", "spanish", mission=mission_fixture())
        self.storage.write(old)
        report = render_call_report(old)
        self.assertIn("REPORTE DE LLAMADA", report)
        self.assertNotIn("EMAIL DRAFT", report)

    async def test_call_result_pipeline_persists_draft_after_mission_state(self):
        _, decision, notification, result_values = artifacts()
        mission = mission_fixture()
        result = empty_call_result("completed", "spanish", mission=mission)
        result.update(result_values)
        result["mission_findings"] = {"interest": "Distribución de productos"}
        decision["approval_required"] = [
            "Enviar información comercial por correo al contacto"
        ]
        with patch.object(main, "DecisionEngine") as engine_type, \
             patch.object(main, "decide_notification", return_value=notification), \
             patch.object(main, "build_notification_message", return_value={"should_send": False}), \
             patch.object(main, "prepare_whatsapp", return_value={"sent": False, "dry_run": True}):
            engine_type.return_value.analyze.return_value = decision
            stored = await main.write_call_result(result, mission=mission)
        self.assertTrue(stored)
        saved = self.storage.latest_mission_result(MISSION_ID)
        self.assertTrue(saved["email_draft"]["should_create"])
        self.assertIs(saved["email_draft"]["sent"], False)
        email_actions = [
            action for action in saved["mission_state"]["actions"]
            if action["action_type"] == "send_email"
        ]
        self.assertEqual(len(email_actions), 1)
        self.assertEqual(email_actions[0]["status"], "awaiting_approval")
        self.assertEqual(email_actions[0]["approval_decision_id"],
                         saved["email_draft"]["approval_decision_ids"][0])
        report_path = next(Path(self.temp_dir.name).glob("call-*.txt"))
        self.assertIn("EMAIL DRAFT (NOT SENT)", report_path.read_text(encoding="utf-8"))

    async def test_approved_rejected_and_deferred_never_send_or_change_sent_flag(self):
        for status in ("approved", "rejected", "deferred"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as directory:
                    storage = LocalFileCallResultStorage(directory)
                    decision = create_pending_decision(
                        MISSION_ID, "Enviar información por email al contacto", "decision_engine"
                    )
                    result = empty_call_result("completed", "spanish", mission=mission_fixture())
                    result["mission_state"] = {
                        "mission_id": MISSION_ID, "status": "waiting_for_user",
                        "decisions": [decision], "decisions_pending": [decision["decision_id"]],
                        "facts_obtained": [], "requests_detected": [], "next_steps": [],
                        "needs_follow_up": False, "evidence_complete": False,
                    }
                    result["email_draft"] = {
                        "should_create": True, "to": [CONTACT_EMAIL], "cc": [],
                        "subject": "Seguimiento comercial", "body": "NOT SENT",
                        "requires_approval": True, "sent": False,
                        "approval_status": "pending",
                        "approval_decision_ids": [decision["decision_id"]],
                    }
                    storage.write(result)
                    with patch.object(main, "call_result_storage", storage), \
                         patch.object(main, "requests") as http, \
                         patch.object(main, "prepare_whatsapp") as whatsapp:
                        body = json.dumps({"status": status}).encode()
                        async def receive():
                            return {"type": "http.request", "body": body, "more_body": False}
                        request = Request({
                            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                            "http_version": "1.1", "method": "POST", "scheme": "https",
                            "path": "/resolve", "raw_path": b"/resolve", "query_string": b"",
                            "root_path": "", "headers": [(b"x-call-secret", CALL_SECRET_TEST_VALUE.encode()),
                                                               (b"content-type", b"application/json")],
                            "client": ("127.0.0.1", 1), "server": ("testserver", 443),
                        }, receive)
                        response = await main.resolve_mission_decision_endpoint(
                            MISSION_ID, decision["decision_id"], request
                        )
                        http.assert_not_called()
                        whatsapp.assert_not_called()
                    saved = storage.latest_mission_result(MISSION_ID)
                    self.assertEqual(response.status_code, 200)
                    self.assertIs(saved["email_draft"]["sent"], False)
                    self.assertIs(saved["email_draft"]["requires_approval"], True)
                    expected = "approved_for_future_send" if status == "approved" else status
                    self.assertEqual(saved["email_draft"]["approval_status"], expected)


if __name__ == "__main__":
    unittest.main()
