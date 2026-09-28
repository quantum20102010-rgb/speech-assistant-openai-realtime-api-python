import ast
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import main
from call_results import LocalFileCallResultStorage, empty_call_result
from notification_decision import decide_notification
from notification_message import build_notification_message


def make_notification(**overrides):
    decision = {
        "schema_version": 1,
        "should_notify": True,
        "priority": "normal",
        "reason_code": "actionable_business_information",
        "reasons": ["transcript_supported_business_information"],
        "facts_to_notify": [],
        "requests_from_contact": [],
        "decisions_needed_from_user": [],
        "next_steps": [],
        "recommendations": [],
        "notification_summary": "",
    }
    decision.update(overrides)
    return decision


def fact(text, field=None, source="transcript"):
    item = {"text": text, "source": source}
    if field:
        item["field"] = field
    return item


class NotificationMessageBuilderTests(unittest.TestCase):
    def build(self, decision, result=None):
        return build_notification_message(decision, result)

    def test_availability_and_price_are_included_as_facts(self):
        decision = make_notification(facts_to_notify=[
            fact("Disponibilidad: puede surtir este mes.", "distribution_interest"),
            fact("Precio: 25 dólares por unidad.", "pricing_information"),
        ])
        built = self.build(decision, {"company": "Proveedor ABC"})
        self.assertTrue(built["should_send"])
        self.assertIn("Proveedor ABC", built["message"])
        self.assertIn("25 dólares por unidad", built["message"])
        self.assertIn("puede surtir este mes", built["message"])

    def test_availability_and_minimum_order_are_included(self):
        decision = make_notification(facts_to_notify=[
            fact("Disponibilidad: inmediata."),
            fact("Pedido mínimo: 500 unidades.", "minimum_order_quantity"),
        ])
        built = self.build(decision)
        self.assertIn("500 unidades", built["message"])

    def test_contact_catalog_request_is_labeled_separately(self):
        built = self.build(make_notification(
            requests_from_contact=["¿Puede enviar el catálogo?"],
        ))
        self.assertIn("*El contacto solicita*", built["message"])
        self.assertIn("¿Puede enviar el catálogo?", built["message"])

    def test_user_decision_is_clearly_pending(self):
        built = self.build(make_notification(
            decisions_needed_from_user=["Autorizar el envío del catálogo."],
        ))
        self.assertIn("*Pendiente de ti*", built["message"])
        self.assertIn("Autorizar el envío del catálogo.", built["message"])
        self.assertEqual(built["message_type"], "action_required")

    def test_concrete_next_step_is_distinguished_from_suggestion(self):
        built = self.build(make_notification(
            next_steps=["El proveedor enviará la cotización el martes."],
            recommendations=["Revisar la cotización cuando llegue."],
        ))
        self.assertIn("*Siguiente paso acordado*", built["message"])
        self.assertNotIn("Siguiente paso sugerido", built["message"])

    def test_recommendation_is_labeled_and_never_shown_as_a_fact(self):
        built = self.build(make_notification(
            recommendations=["Revisar si el MOQ es compatible con la operación."],
        ))
        self.assertIn("*Siguiente paso sugerido (no ejecutado)*", built["message"])
        self.assertNotIn("Información obtenida", built["message"])

    def test_partial_mission_progress_message_uses_only_supported_facts(self):
        decision = make_notification(
            reasons=["relevant_mission_progress"],
            facts_to_notify=[fact("El catálogo tiene 20 productos.", "products_and_catalog")],
        )
        self.assertIn("El catálogo tiene 20 productos", self.build(decision)["message"])

    def test_should_notify_false_produces_no_message(self):
        built = self.build(make_notification(should_notify=False, priority="high"))
        self.assertFalse(built["should_send"])
        self.assertEqual(built["message"], "")

    def test_none_priority_never_produces_message(self):
        built = self.build(make_notification(priority="none"))
        self.assertFalse(built["should_send"])
        self.assertEqual(built["message"], "")

    def test_missing_notification_or_empty_transcript_is_safe(self):
        built = self.build({}, {"transcript": "Contacto: El proveedor tiene disponibilidad."})
        self.assertFalse(built["should_send"])
        self.assertEqual(built["message"], "")

    def test_voicemail_or_irrelevant_decisions_do_not_send(self):
        for reason in ("no_contact_transcript", "no_relevant_information"):
            with self.subTest(reason=reason):
                built = self.build(make_notification(
                    should_notify=False, priority="none", reason_code=reason,
                ))
                self.assertFalse(built["should_send"])

    def test_unverified_facts_are_not_rendered(self):
        decision = make_notification(facts_to_notify=[
            fact("El agente cree que el precio bajará.", source="recommendation"),
            {"text": "El precio es 10.", "source": "model"},
        ])
        built = self.build(decision)
        self.assertNotIn("precio bajará", built["message"])
        self.assertNotIn("El precio es 10", built["message"])

    def test_absent_requests_are_not_invented(self):
        built = self.build(make_notification(
            facts_to_notify=[fact("El proveedor tiene disponibilidad.")],
            recommendations=["Solicitar catálogo al proveedor."],
        ))
        self.assertNotIn("*El contacto solicita*", built["message"])
        self.assertNotIn("solicita catálogo", built["message"].casefold())

    def test_recommendations_are_never_labeled_as_facts(self):
        built = self.build(make_notification(
            recommendations=["Aprobar la cotización."],
        ))
        self.assertNotIn("Aprobar la cotización", built["message"].split("*Siguiente paso sugerido", 1)[0])
        self.assertIn("Aprobar la cotización", built["message"])

    def test_required_decisions_are_never_dropped_for_length(self):
        decisions = [f"Decisión requerida {number}: autorizar revisión." for number in range(8)]
        built = self.build(make_notification(
            priority="high", decisions_needed_from_user=decisions,
            facts_to_notify=[fact("Dato comercial relevante " + ("detalle " * 200)) for _ in range(4)],
        ))
        for decision in decisions:
            self.assertIn(decision, built["message"])
        self.assertLessEqual(len(built["message"]), 1200)

    def test_all_contact_requests_remain_visible(self):
        requests = [f"Solicita documento comercial número {number}." for number in range(8)]
        built = self.build(make_notification(requests_from_contact=requests))
        for request in requests:
            self.assertIn(request, built["message"])

    def test_message_contains_no_secrets_or_phone_numbers(self):
        built = self.build(make_notification(
            facts_to_notify=[
                fact("Precio indicado: sk-proj-12345678901234567890", "pricing_information"),
                fact("Teléfono de contacto: +12125550100", "phone"),
                fact("TWILIO_ACCOUNT_ID_FIXTURE", "id"),
            ],
        ), {"company": "Proveedor"})
        self.assertNotIn("sk-proj-", built["message"])
        self.assertNotIn("TWILIO_ACCOUNT_ID_FIXTURE", built["message"])
        self.assertNotIn("+12125550100", built["message"])

    def test_message_does_not_include_phone_number_or_transcript(self):
        transcript = (
            "Contacto: Buenos días, el precio es 25 por unidad y tenemos 500 unidades.\n"
            "Asistente: Gracias por la información."
        )
        decision = make_notification(
            facts_to_notify=[fact("Precio: 25 por unidad.", "pricing_information")],
        )
        built = self.build(decision, {
            "company": "Proveedor ABC",
            "phone": "+12125550100",
            "transcript": transcript,
        })
        self.assertNotIn(transcript, built["message"])
        self.assertNotIn("Buenos días", built["message"])
        self.assertNotIn("+12125550100", built["message"])

    def test_company_label_is_sanitized_and_channel_is_preview_only(self):
        built = self.build(make_notification(
            facts_to_notify=[fact("El catálogo tiene 20 productos.")],
        ), {"company": "Proveedor ABC +12125550100"})
        self.assertNotIn("+12125550100", built["message"])
        self.assertEqual(built["channel"], "whatsapp")
        self.assertTrue(built["should_send"])

    def test_module_imports_no_external_capabilities(self):
        source = Path(__file__).parents[1].joinpath("notification_message.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertLessEqual(imported, {"re"})

    def test_saved_result_contains_both_notification_layers_and_legacy_is_compatible(self):
        transcript = "Contacto: El pedido mínimo es de 500 unidades."
        result = empty_call_result("completed", "spanish")
        result["transcript"] = transcript
        result["minimum_order_quantity"] = "El pedido mínimo es de 500 unidades."
        with tempfile.TemporaryDirectory() as directory:
            previous_storage = main.call_result_storage
            main.call_result_storage = LocalFileCallResultStorage(directory)
            try:
                self.assertTrue(asyncio.run(main.write_call_result(result)))
            finally:
                main.call_result_storage = previous_storage
            json_path = next(Path(directory).glob("call-*.json"))
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            report = json_path.with_suffix(".txt").read_text(encoding="utf-8")

        self.assertEqual(saved["transcript"], transcript)
        self.assertIn("decision", saved)
        self.assertIn("notification_decision", saved)
        self.assertIn("notification_message", saved)
        self.assertTrue(saved["notification_message"]["should_send"])
        self.assertIn("NOTIFICATION MESSAGE PREVIEW (NOT SENT)", report)
        self.assertIn("DECISIÓN COMERCIAL", report)
        legacy = empty_call_result("completed", "spanish")
        self.assertEqual(build_notification_message(legacy.get("notification_decision"))["message"], "")

    def test_old_report_without_either_notification_layer_renders(self):
        result = empty_call_result("completed", "spanish")
        from call_results import render_call_report
        self.assertIn("REPORTE DE LLAMADA", render_call_report(result))
        self.assertNotIn("NOTIFICATION MESSAGE PREVIEW", render_call_report(result))


if __name__ == "__main__":
    unittest.main()
