import ast
import json
import tempfile
import unittest
from pathlib import Path

import main
from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from decision_engine import DecisionEngine
from missions import load_mission_catalog
from notification_decision import decide_notification


class SmartNotificationDecisionTests(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine()

    def evaluate(self, transcript, fields=None, mission=None):
        result = empty_call_result("completed", "spanish", mission=mission)
        result.update(fields or {})
        decision = self.engine.analyze(result, transcript, mission)
        return decide_notification(result, decision, transcript)

    def test_notifies_on_provider_availability(self):
        result = self.evaluate("Contacto: Tenemos producto disponible para surtir este mes.")
        self.assertTrue(result["should_notify"])
        self.assertEqual(result["priority"], "high")

    def test_notifies_on_concrete_distribution_opportunity(self):
        result = self.evaluate(
            "Contacto: Nos interesa explorar la distribución de nuestros productos."
        )
        self.assertTrue(result["should_notify"])
        self.assertEqual(result["priority"], "high")

    def test_secondary_relevant_contact_details_receive_low_priority(self):
        statement = "Soy María, directora de compras de ABC."
        result = self.evaluate(f"Contacto: {statement}", {
            "company": "ABC",
            "contact_name": "María",
            "contact_role": "directora de compras",
        })
        self.assertTrue(result["should_notify"])
        self.assertEqual(result["priority"], "low")

    def test_notifies_on_transcript_verified_price(self):
        statement = "El precio es 25 dólares por unidad."
        result = self.evaluate(
            f"Contacto: {statement}", {"pricing_information": statement}
        )
        self.assertTrue(result["should_notify"])
        self.assertTrue(any("25 dólares" in fact["text"] for fact in result["facts_to_notify"]))

    def test_notifies_on_transcript_verified_minimum_order(self):
        statement = "El pedido mínimo es de 500 unidades."
        result = self.evaluate(
            f"Contacto: {statement}", {"minimum_order_quantity": statement}
        )
        self.assertTrue(result["should_notify"])
        self.assertEqual(result["priority"], "high")

    def test_notifies_when_contact_requests_catalog(self):
        result = self.evaluate("Contacto: ¿Puede enviar el catálogo para revisar sus productos?")
        self.assertTrue(result["should_notify"])
        self.assertTrue(result["requests_from_contact"])

    def test_notifies_when_contact_requests_information(self):
        result = self.evaluate("Contacto: Necesito que nos proporcione el país de origen.")
        self.assertTrue(result["should_notify"])
        self.assertTrue(result["requests_from_contact"])

    def test_user_decision_is_separate_from_external_action(self):
        result = self.evaluate("Contacto: ¿Puede enviar el catálogo para revisar sus productos?")
        self.assertTrue(result["decisions_needed_from_user"])
        self.assertNotIn("actions", result)
        self.assertTrue(result["recommendations"])
        self.assertFalse(any("Preparar correo" in fact["text"] for fact in result["facts_to_notify"]))

    def test_notifies_on_concrete_next_step(self):
        step = "Llámame el martes para dar seguimiento a la cotización."
        result = self.evaluate(f"Contacto: {step}", {"next_action": step})
        self.assertTrue(result["should_notify"])
        self.assertTrue(result["next_steps"])

    def test_notifies_on_relevant_partial_mission_progress(self):
        mission = load_mission_catalog().get("supplier_outreach")
        statement = "Nuestros productos están disponibles para distribución."
        result = self.evaluate(
            f"Contacto: {statement}",
            {"mission_findings": {"products_and_catalog": statement}},
            mission,
        )
        self.assertTrue(result["should_notify"])
        self.assertIn("relevant_mission_progress", result["reasons"])

    def test_no_notification_without_contact_transcript(self):
        self.assertFalse(self.evaluate("")["should_notify"])

    def test_voicemail_without_information_does_not_notify(self):
        result = self.evaluate("Contacto: Deje su mensaje después del tono.")
        self.assertFalse(result["should_notify"])

    def test_wrong_number_does_not_notify(self):
        result = self.evaluate("Contacto: Este no es el proveedor, es número equivocado.")
        self.assertFalse(result["should_notify"])

    def test_irrelevant_conversation_does_not_notify(self):
        result = self.evaluate("Contacto: Hoy el clima está soleado.")
        self.assertFalse(result["should_notify"])

    def test_duplicate_information_without_update_does_not_notify(self):
        result = self.evaluate(
            "Contacto: Los precios son los mismos que la vez pasada, sin cambios."
        )
        self.assertFalse(result["should_notify"])
        self.assertEqual(result["reason_code"], "no_new_information")

    def test_greeting_only_does_not_notify(self):
        result = self.evaluate("Contacto: Hola, buenos días. Asistente: Mucho gusto.")
        self.assertFalse(result["should_notify"])

    def test_facts_are_transcript_evidence_not_recommendations(self):
        transcript = "Contacto: El catálogo incluye 20 productos."
        decision = {
            "facts": [
                {"type": "conversation_statement", "text": "El catálogo incluye 20 productos.", "source": "transcript"},
                {"type": "conversation_statement", "text": "El agente recomienda negociar.", "source": "recommendation"},
            ],
            "recommended_actions": [{"action": "Negociar mejores condiciones."}],
        }
        result = decide_notification({}, decision, transcript)
        self.assertEqual(
            result["facts_to_notify"],
            [{"text": "El catálogo incluye 20 productos.", "source": "transcript"}],
        )
        self.assertNotIn("Negociar mejores condiciones.", result["notification_summary"])
        self.assertIn("Negociar mejores condiciones.", result["recommendations"])

    def test_requests_must_appear_in_contact_transcript(self):
        result = decide_notification(
            {},
            {"requests_from_contact": ["Enviar el catálogo."]},
            "Contacto: Gracias por la llamada.",
        )
        self.assertEqual(result["requests_from_contact"], [])
        self.assertFalse(result["should_notify"])

    def test_decisions_are_not_executable_actions(self):
        result = decide_notification(
            {},
            {"approval_required": ["Aprobar el envío del catálogo."], "facts": []},
            "Contacto: ¿Puede enviar el catálogo?",
        )
        self.assertEqual(result["decisions_needed_from_user"], ["Aprobar el envío del catálogo."])
        self.assertNotIn("actions_required", result)
        self.assertNotIn("executable", result)

    def test_module_imports_no_external_clients_or_action_execution(self):
        source = Path(__file__).parents[1].joinpath("notification_decision.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertLessEqual(imported, {"re"})

    def test_write_result_persists_transcript_analysis_engine_and_notification(self):
        transcript = "Contacto: El precio es 25 dólares por unidad."
        result = empty_call_result("completed", "spanish")
        result["transcript"] = transcript
        result["pricing_information"] = "El precio es 25 dólares por unidad."
        with tempfile.TemporaryDirectory() as directory:
            previous_storage = main.call_result_storage
            main.call_result_storage = LocalFileCallResultStorage(directory)
            try:
                self.assertTrue(__import__("asyncio").run(main.write_call_result(result)))
            finally:
                main.call_result_storage = previous_storage
            json_path = next(Path(directory).glob("call-*.json"))
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            report = json_path.with_suffix(".txt").read_text(encoding="utf-8")

        self.assertEqual(saved["transcript"], transcript)
        self.assertIn("decision", saved)
        self.assertIn("notification_decision", saved)
        self.assertTrue(saved["notification_decision"]["should_notify"])
        self.assertIn("DECISIÓN COMERCIAL", report)
        self.assertIn("SMART NOTIFICATION DECISION", report)
        self.assertIn(transcript, report)

    def test_legacy_result_without_notification_still_renders(self):
        result = empty_call_result("completed", "spanish")
        result["decision"] = {"status": "no_action", "relevance": "not_relevant"}
        self.assertIn("REPORTE DE LLAMADA", render_call_report(result))
        self.assertNotIn("SMART NOTIFICATION DECISION", render_call_report(result))


if __name__ == "__main__":
    unittest.main()
