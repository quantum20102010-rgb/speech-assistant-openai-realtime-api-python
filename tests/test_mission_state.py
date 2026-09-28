import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from decision_engine import DecisionEngine
from mission_state import update_mission_state
from notification_decision import decide_notification
from notification_message import build_notification_message


def make_mission():
    return SimpleNamespace(
        id="test_mission",
        name="Prueba",
        objective="Entender productos y condiciones comerciales.",
        required_information=(
            {"key": "products", "description": "Productos ofrecidos"},
            {"key": "terms", "description": "Condiciones comerciales"},
        ),
        relevance_criteria=("productos comerciales",),
        follow_up_actions=("Revisar los hallazgos internamente.",),
    )


def artifacts(findings=None, transcript="", call_status="completed"):
    mission = make_mission()
    result = empty_call_result(call_status, "spanish", mission=mission)
    result["mission_findings"].update(findings or {})
    result["transcript"] = transcript
    decision = DecisionEngine().analyze(result, transcript, mission)
    notification = decide_notification(result, decision, transcript)
    message = build_notification_message(notification, result)
    return mission, result, decision, notification, message


class MissionStateTests(unittest.TestCase):
    def update(self, findings=None, transcript="", status="completed", previous=None,
               decision_changes=None, notification_changes=None):
        mission, result, decision, notification, message = artifacts(
            findings, transcript, status
        )
        decision.update(decision_changes or {})
        notification.update(notification_changes or {})
        state = update_mission_state(
            previous, result, mission, decision, notification, message
        )
        return state, mission, result, decision, notification, message

    def test_new_mission_starts_pending_with_all_information_missing(self):
        state, *_ = self.update()
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["mission_id"], "test_mission")
        self.assertEqual(len(state["information_missing"]), 2)
        self.assertEqual(state["call_count"], 1)

    def test_relevant_partial_call_is_in_progress_and_records_facts(self):
        state, *_ = self.update(
            {"products": "Lámparas Sol"},
            "Contacto: Productos: Lámparas Sol.",
        )
        self.assertEqual(state["status"], "in_progress")
        self.assertEqual(state["facts_obtained"][0]["field"], "products")
        self.assertEqual([item["key"] for item in state["information_missing"]], ["terms"])

    def test_information_missing_is_explicit(self):
        state, *_ = self.update(
            {"products": "Lámparas Sol"}, "Contacto: Lámparas Sol."
        )
        self.assertEqual(state["information_missing"][0]["key"], "terms")

    def test_pending_decision_waits_for_user(self):
        state, *_ = self.update(
            {"products": "Lámparas Sol"},
            "Contacto: Lámparas Sol.",
            decision_changes={"approval_required": ["Revisar condiciones antes de aceptar"]},
            notification_changes={"decisions_needed_from_user": ["Aprobar revisión interna"]},
        )
        self.assertEqual(state["status"], "waiting_for_user")
        self.assertTrue(state["decisions_pending"])
        self.assertEqual(len(state["decisions"]), len(state["decisions_pending"]))
        self.assertTrue(all(item["status"] == "pending" for item in state["decisions"]))

    def test_mission_completes_only_with_all_transcript_supported_fields(self):
        state, *_ = self.update(
            {"products": "Lámparas Sol", "terms": "Pago a 30 días"},
            "Contacto: Lámparas Sol. Condiciones: Pago a 30 días.",
        )
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["information_missing"], [])

    def test_call_failure_blocks_mission(self):
        state, *_ = self.update(status="error")
        self.assertEqual(state["status"], "blocked")

    def test_contact_inability_creates_follow_up(self):
        state, *_ = self.update(
            {"products": "Lámparas Sol"},
            "Contacto: Lámparas Sol. No disponemos de las condiciones comerciales.",
        )
        self.assertEqual(state["status"], "needs_follow_up")
        self.assertTrue(state["needs_follow_up"])
        self.assertEqual(state["follow_up_reason"], "contact_could_not_provide_information")

    def test_several_calls_accumulate_evidence_and_can_complete(self):
        first, mission, *_ = self.update(
            {"products": "Lámparas Sol"}, "Contacto: Lámparas Sol."
        )
        second_result = empty_call_result("completed", "spanish", mission=mission)
        second_result["mission_findings"]["terms"] = "Pago a 30 días"
        second_transcript = "Contacto: Las condiciones son Pago a 30 días."
        decision = DecisionEngine().analyze(second_result, second_transcript, mission)
        notification = decide_notification(second_result, decision, second_transcript)
        message = build_notification_message(notification, second_result)
        second = update_mission_state(
            first, second_result, mission, decision, notification, message
        )
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["call_count"], 2)
        mission_facts = {
            fact["field"] for fact in second["facts_obtained"]
            if fact["field"] != "conversation"
        }
        self.assertEqual(mission_facts, {"products", "terms"})

    def test_irrelevant_conversation_does_not_complete_mission(self):
        state, *_ = self.update(transcript="Contacto: El clima está soleado.")
        self.assertEqual(state["status"], "pending")
        self.assertFalse(state["needs_follow_up"])

    def test_unsupported_model_findings_are_not_recorded(self):
        state, *_ = self.update(
            {"products": "Dato inventado por el modelo"},
            "Contacto: Gracias por llamar.",
        )
        self.assertEqual(state["facts_obtained"], [])
        self.assertNotEqual(state["status"], "completed")

    def test_state_does_not_copy_transcript_or_keep_secrets(self):
        secret = "local-mission-secret-value"
        old = os.environ.get("CALL_SECRET")
        os.environ["CALL_SECRET"] = secret
        try:
            state, *_ = self.update(
                {"products": "Producto conocido"},
                f"Contacto: Producto conocido. {secret} correo persona@example.test +1 212 555 0100",
            )
        finally:
            if old is None:
                os.environ.pop("CALL_SECRET", None)
            else:
                os.environ["CALL_SECRET"] = old
        serialized = json.dumps(state)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("Contacto:", serialized)
        self.assertNotIn("persona@example.test", serialized)
        self.assertNotIn("212 555 0100", serialized)

    def test_legacy_report_without_mission_state_remains_readable(self):
        legacy = empty_call_result("completed", "spanish")
        report = render_call_report(legacy)
        self.assertIn("REPORTE DE LLAMADA", report)
        self.assertNotIn("MISSION STATE", report)

    def test_local_storage_loads_latest_state_by_mission_id(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFileCallResultStorage(directory)
            state, mission, result, decision, notification, message = self.update()
            result["mission_state"] = state
            storage.write(result)
            self.assertEqual(storage.latest_mission_state(mission.id), state)
            self.assertIsNone(storage.latest_mission_state("other_mission"))


class MissionStatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_call_result_continues_same_mission_across_reports(self):
        import main

        mission = make_mission()
        with tempfile.TemporaryDirectory() as directory:
            previous_storage = main.call_result_storage
            main.call_result_storage = LocalFileCallResultStorage(directory)
            try:
                first = empty_call_result("completed", "spanish", mission=mission)
                first["mission_findings"]["products"] = "Lámparas Sol"
                first["transcript"] = "Contacto: Lámparas Sol."
                self.assertTrue(await main.write_call_result(first, mission=mission))

                second = empty_call_result("completed", "spanish", mission=mission)
                second["mission_findings"]["terms"] = "Pago a 30 días"
                second["transcript"] = "Contacto: Las condiciones son Pago a 30 días."
                self.assertTrue(await main.write_call_result(second, mission=mission))

                saved_files = list(Path(directory).glob("call-*.json"))
                reports = [json.loads(path.read_text(encoding="utf-8")) for path in saved_files]
                latest = max(reports, key=lambda item: item["mission_state"]["updated_at"])
                self.assertEqual(latest["mission_state"]["mission_id"], mission.id)
                self.assertEqual(latest["mission_state"]["call_count"], 2)
                self.assertEqual(latest["mission_state"]["status"], "completed")
                latest_json = max(
                    saved_files,
                    key=lambda path: json.loads(path.read_text(encoding="utf-8"))["mission_state"]["updated_at"],
                )
                txt = latest_json.with_suffix(".txt").read_text(encoding="utf-8")
                self.assertIn("MISSION STATE", txt)
                self.assertIn("Objetivo:", txt)
                self.assertIn("Información obtenida:", txt)
                self.assertIn("Información faltante:", txt)
                self.assertIn("Seguimiento:", txt)
            finally:
                main.call_result_storage = previous_storage


if __name__ == "__main__":
    unittest.main()
