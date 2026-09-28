import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import decision_engine
import main
from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from decision_engine import (
    AUTOMATIC,
    FORBIDDEN_AUTOMATIC,
    REQUIRES_APPROVAL,
    DecisionEngine,
    classify_action,
)


def mission(fields=("products", "terms"), suggestions=()):
    return SimpleNamespace(
        id="test_mission",
        name="Prueba comercial",
        required_information=tuple(
            {"key": key, "description": key.replace("_", " ")}
            for key in fields
        ),
        relevance_criteria=("Interés o información comercial explícita.",),
        follow_up_actions=tuple(suggestions),
    )


def result(**updates):
    value = {
        "call_status": "completed",
        "summary": "El modelo resumió la conversación.",
        "summary_generated_by_model": True,
        "mission_id": "test_mission",
        "mission_findings": {},
        "email": None,
        "pricing_information": None,
        "b2b_terms": None,
    }
    value.update(updates)
    return value


class DecisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine()
        self.mission = mission()

    def analyze(self, text, call_result=None, selected_mission=None):
        return self.engine.analyze(
            call_result or result(), text,
            selected_mission if selected_mission is not None else self.mission,
        )

    def test_call_without_relevant_information_is_no_action(self):
        decision = self.analyze("Contacto: Hola, gracias por contestar.\nAsistente: Mucho gusto.")
        self.assertEqual(decision["status"], "no_action")
        self.assertEqual(decision["relevance"], "not_relevant")
        self.assertEqual(decision["facts"], [])

    def test_clear_rejection_is_not_actionable(self):
        decision = self.analyze("Contacto: Gracias, pero no nos interesa por el momento.")
        self.assertEqual(decision["status"], "no_action")
        self.assertEqual(decision["relevance"], "not_relevant")

    def test_interested_provider_is_actionable(self):
        decision = self.analyze("Contacto: Sí, nos interesa explorar la distribución de nuestros productos.")
        self.assertEqual(decision["relevance"], "actionable")
        self.assertIn(decision["status"], {"informational", "actionable"})
        self.assertTrue(decision["facts"])

    def test_provider_request_for_catalog_is_captured(self):
        decision = self.analyze("Contacto: ¿Puede enviar el catálogo para revisar sus productos?")
        self.assertEqual(decision["status"], "awaiting_user")
        self.assertTrue(decision["requests_from_contact"])
        self.assertIn("catálogo", " ".join(decision["information_to_provide"]).casefold())

    def test_email_provided_is_a_transcript_verified_fact(self):
        utterance = "Contacto: Mi correo es ventas@ejemplo.test para recibir la información."
        decision = self.analyze(
            utterance,
            result(email="ventas@ejemplo.test"),
        )
        self.assertEqual(decision["relevance"], "actionable")
        self.assertTrue(any(f.get("field") == "email" for f in decision["facts"]))

    def test_provider_request_for_additional_information_is_actionable(self):
        decision = self.analyze(
            "Contacto: Para preparar la cotización necesitamos catálogo, país de origen, valor estimado y volumen."
        )
        self.assertEqual(decision["status"], "awaiting_user")
        self.assertEqual(
            decision["information_to_provide"],
            ["catálogo", "país de origen", "valor estimado", "volumen"],
        )

    def test_provider_price_is_recorded_only_when_transcribed(self):
        phrase = "Contacto: El precio mayorista es 25 dólares por unidad."
        decision = self.analyze(phrase, result(pricing_information="25 dólares por unidad"))
        self.assertTrue(any(f.get("field") == "pricing_information" for f in decision["facts"]))

    def test_provider_terms_are_recorded_as_literal_statements(self):
        phrase = "Contacto: Transporte y almacenaje se cotizan por separado."
        decision = self.analyze(phrase, result(b2b_terms="Transporte y almacenaje se cotizan por separado"))
        self.assertTrue(any("Transporte y almacenaje" in f.get("text", "") for f in decision["facts"]))

    def test_mission_can_be_partially_complete(self):
        selected = mission(("products", "terms", "pricing"))
        phrase = "Contacto: Manejamos la marca Sol y el catálogo Sol 2026."
        decision = self.analyze(
            phrase,
            result(mission_findings={"products": "Manejamos la marca Sol y el catálogo Sol 2026"}),
            selected,
        )
        self.assertEqual(decision["mission_progress"]["supported"], 1)
        self.assertEqual(decision["mission_progress"]["missing"], ["terms", "pricing"])
        self.assertNotEqual(decision["status"], "completed")

    def test_mission_completes_only_when_all_findings_are_transcript_supported(self):
        selected = mission(("products", "terms"))
        phrase = "Contacto: Vendemos producto Sol. Las condiciones son pago a 30 días."
        decision = self.analyze(
            phrase,
            result(mission_findings={
                "products": "Vendemos producto Sol",
                "terms": "Las condiciones son pago a 30 días",
            }),
            selected,
        )
        self.assertTrue(decision["mission_progress"]["complete"])
        self.assertEqual(decision["status"], "completed")

    def test_explicit_next_step_requires_follow_up(self):
        decision = self.analyze("Contacto: Llámame el martes para dar seguimiento a la cotización.")
        self.assertEqual(decision["status"], "follow_up_required")
        self.assertTrue(decision["follow_up"]["required"])

    def test_waiting_for_provider_commitment_has_distinct_status(self):
        decision = self.analyze("Contacto: Le enviaremos la cotización la próxima semana.")
        self.assertEqual(decision["status"], "awaiting_third_party")

    def test_mission_suggestion_cannot_automatically_accept_commercial_terms(self):
        selected = mission(
            suggestions=(
                "Aceptar la cotización y firmar contrato",
                "Programar contacto humano solo si la otra parte acepta un siguiente paso.",
            )
        )
        decision = self.analyze(
            "Contacto: Nos interesa la distribución.", selected_mission=selected
        )
        self.assertTrue(any(
            item["autonomy"] == FORBIDDEN_AUTOMATIC
            for item in decision["recommended_actions"]
        ))
        self.assertIn(
            "Programar contacto humano solo si la otra parte acepta un siguiente paso.",
            decision["approval_required"],
        )

    def test_structured_inference_not_present_in_transcript_is_excluded(self):
        decision = self.analyze(
            "Contacto: Nos interesa conocer sus productos.",
            result(email="persona@inventada.test", pricing_information="999 USD"),
        )
        supported_values = [fact.get("value") for fact in decision["facts"]]
        self.assertNotIn("persona@inventada.test", supported_values)
        self.assertNotIn("999 USD", supported_values)

    def test_external_actions_require_approval(self):
        action = classify_action("Enviar información comercial por correo al contacto")
        self.assertEqual(action["autonomy"], REQUIRES_APPROVAL)
        self.assertTrue(action["approval_required"])
        self.assertFalse(action["executable"])

    def test_forbidden_commercial_commitment_is_never_automatic(self):
        action = classify_action("Aceptar el precio y comprometer a Guzi Stuff")
        self.assertEqual(action["autonomy"], FORBIDDEN_AUTOMATIC)
        self.assertFalse(action["executable"])
        automatic = classify_action("Guardar información y análisis de la llamada")
        self.assertEqual(automatic["autonomy"], AUTOMATIC)
        self.assertFalse(automatic["executable"])

    def test_absent_transcript_does_not_invent_facts(self):
        decision = self.analyze("", result())
        self.assertEqual(decision["facts"], [])
        self.assertEqual(decision["relevance"], "unknown")
        self.assertEqual(decision["status"], "no_action")

    def test_decision_summary_identifies_model_generated_summary(self):
        decision = self.analyze("Contacto: Sí, nos interesa la distribución.")
        self.assertEqual(decision["summary"]["generated_by"], "model")
        self.assertEqual(decision["summary"]["label"], "Resumen generado por el modelo")

    def test_engine_has_no_provider_or_secret_capabilities(self):
        for capability in ("os", "requests", "websockets", "twilio", "subprocess"):
            self.assertFalse(hasattr(decision_engine, capability))


class DecisionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_error_is_persisted_as_failed_decision_without_error_text(self):
        with tempfile.TemporaryDirectory() as directory:
            old_storage = main.call_result_storage
            main.call_result_storage = LocalFileCallResultStorage(directory)
            item = empty_call_result("completed", "spanish")
            item["transcript"] = "Contacto: Nos interesa el producto."
            try:
                with patch.object(main.DecisionEngine, "analyze", side_effect=RuntimeError("private failure")):
                    self.assertTrue(await main.write_call_result(item))
            finally:
                main.call_result_storage = old_storage
            saved = json.loads(next(Path(directory).glob("call-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(saved["decision"]["status"], "failed")
            self.assertNotIn("private failure", json.dumps(saved))

    async def test_existing_results_without_decision_still_render_and_store(self):
        old_result = empty_call_result("completed", "spanish")
        self.assertNotIn("decision", old_result)
        self.assertIn("REPORTE DE LLAMADA", render_call_report(old_result))
        with tempfile.TemporaryDirectory() as directory:
            json_path, text_path = LocalFileCallResultStorage(directory).write(old_result)
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertNotIn("decision", loaded)
            self.assertTrue(text_path.exists())


if __name__ == "__main__":
    unittest.main()
