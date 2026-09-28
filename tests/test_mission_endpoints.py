import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Request

import main
from call_results import LocalFileCallResultStorage, empty_call_result
from mission_decision import create_pending_decision
from missions import MissionNotFoundError


MISSION_ID = "endpoint_mission"
OTHER_MISSION_ID = "other_endpoint_mission"
TEST_SECRET = "endpoint-test-secret-value"


def mission_fixture(mission_id=MISSION_ID):
    return SimpleNamespace(
        id=mission_id,
        name="Test mission",
        objective="Obtener información comercial.",
        required_information=({"key": "contact", "description": "Contacto"},),
        relevance_criteria=("información comercial",),
        follow_up_actions=("Revisión manual",),
    )


class Catalog:
    def get(self, mission_id):
        if mission_id not in {MISSION_ID, OTHER_MISSION_ID}:
            raise MissionNotFoundError("not found")
        return mission_fixture(mission_id)


def make_request(method, path, body=None, secret=TEST_SECRET):
    encoded = b"" if body is None else json.dumps(body).encode("utf-8")
    headers = []
    if secret is not None:
        headers.append((b"x-call-secret", secret.encode("utf-8")))
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }
    return Request(scope, receive)


class MissionEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = LocalFileCallResultStorage(self.temp_dir.name)
        self.storage_patch = patch.object(main, "call_result_storage", self.storage)
        self.catalog_patch = patch.object(main, "load_mission_catalog", Catalog)
        self.env_patch = patch.dict(os.environ, {"CALL_SECRET": TEST_SECRET})
        self.storage_patch.start()
        self.catalog_patch.start()
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.catalog_patch.stop()
        self.storage_patch.stop()
        self.temp_dir.cleanup()

    def seed(self, mission_id=MISSION_ID, extra_decisions=()):
        decision = create_pending_decision(
            mission_id, "Revisar el catálogo", "decision_engine"
        )
        state = {
            "schema_version": 1,
            "mission_id": mission_id,
            "objective": "Obtener información comercial.",
            "status": "waiting_for_user",
            "facts_obtained": [],
            "requests_detected": [],
            "information_missing": [],
            "evidence_complete": False,
            "decisions": [decision, *extra_decisions],
            "decisions_pending": [decision["decision_id"]],
            "next_steps": [],
            "last_call_result": {"call_status": "completed"},
            "needs_follow_up": False,
            "call_count": 1,
        }
        result = empty_call_result("completed", "spanish", mission=mission_fixture(mission_id))
        result["mission_state"] = state
        self.storage.write(result)
        return decision

    async def resolve(self, decision_id, status="approved", resolution=None,
                      mission_id=MISSION_ID, secret=TEST_SECRET):
        body = {"status": status}
        if resolution is not None:
            body["resolution"] = resolution
        request = make_request(
            "POST", f"/missions/{mission_id}/decisions/{decision_id}/resolve",
            body, secret,
        )
        return await main.resolve_mission_decision_endpoint(
            mission_id, decision_id, request
        )

    async def test_authenticated_query_returns_only_pending_minimal_decisions(self):
        decision = self.seed()
        response = await main.list_mission_decisions(
            MISSION_ID, make_request("GET", f"/missions/{MISSION_ID}/decisions")
        )
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload, {
            "mission_id": MISSION_ID,
            "decisions": [{
                "decision_id": decision["decision_id"],
                "description": "Revisar el catálogo",
                "status": "pending",
            }],
        })
        self.assertNotIn("source", payload["decisions"][0])

    async def test_query_without_authentication_is_rejected(self):
        self.seed()
        response = await main.list_mission_decisions(
            MISSION_ID, make_request("GET", f"/missions/{MISSION_ID}/decisions", secret=None)
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.body), {"error": "Unauthorized."})

    async def test_missing_mission_is_not_found(self):
        response = await main.list_mission_decisions(
            "unknown_mission", make_request("GET", "/missions/unknown_mission/decisions")
        )
        self.assertEqual(response.status_code, 404)

    async def test_mission_without_decisions_returns_empty_list(self):
        result = empty_call_result("completed", "spanish", mission=mission_fixture())
        result["mission_state"] = {"mission_id": MISSION_ID, "decisions": []}
        self.storage.write(result)
        response = await main.list_mission_decisions(
            MISSION_ID, make_request("GET", f"/missions/{MISSION_ID}/decisions")
        )
        self.assertEqual(json.loads(response.body)["decisions"], [])

    async def test_legacy_pending_state_is_readable(self):
        result = empty_call_result("completed", "spanish", mission=mission_fixture())
        result["mission_state"] = {
            "mission_id": MISSION_ID,
            "decisions_pending": ["Revisar legacy de forma manual"],
        }
        self.storage.write(result)
        response = await main.list_mission_decisions(
            MISSION_ID, make_request("GET", f"/missions/{MISSION_ID}/decisions")
        )
        decisions = json.loads(response.body)["decisions"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["status"], "pending")
        self.assertEqual(decisions[0]["description"], "Revisar legacy de forma manual")

    async def test_approved_rejected_and_deferred_are_recorded_via_endpoint(self):
        for status in ("approved", "rejected", "deferred"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as directory:
                    storage = LocalFileCallResultStorage(directory)
                    with patch.object(main, "call_result_storage", storage):
                        decision = create_pending_decision(
                            MISSION_ID, "Revisar el catálogo", "decision_engine"
                        )
                        result = empty_call_result("completed", "spanish", mission=mission_fixture())
                        result["mission_state"] = {
                            "mission_id": MISSION_ID, "status": "waiting_for_user",
                            "decisions": [decision], "decisions_pending": [decision["decision_id"]],
                            "facts_obtained": [], "requests_detected": [], "next_steps": [],
                            "needs_follow_up": False, "evidence_complete": False,
                        }
                        storage.write(result)
                        response = await self.resolve(decision["decision_id"], status=status)
                        self.assertEqual(response.status_code, 200)
                        payload = json.loads(response.body)
                        self.assertEqual(payload["status"], status)
                        self.assertEqual(payload["resolved_by"], "Fabian")
                        self.assertIn("resolved_at", payload)
                        self.assertNotEqual(payload["mission_state"]["status"], "completed")
                        self.assertEqual(
                            storage.latest_mission_state(MISSION_ID)["decisions"][0]["status"],
                            status,
                        )

    async def test_nonexistent_decision_is_rejected(self):
        self.seed()
        response = await self.resolve("md_not_a_real_id")
        self.assertEqual(response.status_code, 404)

    async def test_decision_from_another_mission_is_rejected(self):
        foreign = create_pending_decision(
            OTHER_MISSION_ID, "Revisar otro expediente", "decision_engine"
        )
        self.seed(extra_decisions=(foreign,))
        response = await self.resolve(foreign["decision_id"], mission_id=MISSION_ID)
        self.assertEqual(response.status_code, 404)

    async def test_already_resolved_decision_is_rejected(self):
        decision = self.seed()
        first = await self.resolve(decision["decision_id"], status="approved")
        second = await self.resolve(decision["decision_id"], status="rejected")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

    async def test_resolution_updates_mission_state_and_does_not_execute_actions(self):
        decision = self.seed()
        with (
            patch.object(main, "prepare_whatsapp") as whatsapp,
            patch.object(main, "requests") as http,
            patch.object(main, "twilio_preflight") as twilio,
        ):
            response = await self.resolve(decision["decision_id"], status="approved")
        payload = json.loads(response.body)
        self.assertEqual(payload["mission_state"]["status"], "pending")
        self.assertEqual(payload["mission_state"]["decisions_pending"], [])
        whatsapp.assert_not_called()
        http.assert_not_called()
        twilio.assert_not_called()

    async def test_resolution_is_authenticated(self):
        decision = self.seed()
        response = await self.resolve(decision["decision_id"], secret=None)
        self.assertEqual(response.status_code, 401)

    async def test_invalid_body_status_and_extra_fields_are_rejected(self):
        self.seed()
        invalid = await main.resolve_mission_decision_endpoint(
            MISSION_ID, "some-id",
            make_request("POST", "/resolve", {"status": ["approved"]}),
        )
        extra = await main.resolve_mission_decision_endpoint(
            MISSION_ID, "some-id",
            make_request("POST", "/resolve", {"status": "approved", "execute": True}),
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(extra.status_code, 400)

    async def test_secret_is_redacted_from_response_and_not_logged(self):
        decision = self.seed()
        previous = os.environ.get("CALL_SECRET")
        os.environ["CALL_SECRET"] = TEST_SECRET
        class Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                self.messages.append(self.format(record))

        capture = Capture()
        logger = logging.getLogger()
        logger.addHandler(capture)
        try:
            response = await self.resolve(
                decision["decision_id"], resolution=f"Registro {TEST_SECRET}"
            )
        finally:
            logger.removeHandler(capture)
            if previous is None:
                os.environ.pop("CALL_SECRET", None)
            else:
                os.environ["CALL_SECRET"] = previous
        combined = response.body.decode("utf-8") + "\n".join(capture.messages)
        self.assertNotIn(TEST_SECRET, combined)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
