import ast
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from call_results import LocalFileCallResultStorage, empty_call_result, render_call_report
from mission_decision import (
    MissionDecisionError,
    create_pending_decision,
    normalize_decision_records,
    resolve_mission_decision,
    resolve_persisted_mission_decision,
)
from missions import MissionNotFoundError


def mission_fixture(mission_id="test_mission"):
    return SimpleNamespace(
        id=mission_id,
        name="Test mission",
        objective="Obtener datos comerciales confirmados.",
        required_information=({"key": "product", "description": "Producto"},),
        relevance_criteria=("producto",),
        follow_up_actions=(),
    )


def state_fixture(mission_id="test_mission", evidence_complete=False):
    decision = create_pending_decision(
        mission_id,
        "Aprobar el envío del catálogo",
        "notification_decision",
    )
    state = {
        "schema_version": 1,
        "mission_id": mission_id,
        "objective": "Obtener datos comerciales confirmados.",
        "status": "waiting_for_user",
        "facts_obtained": [{"field": "context", "label": "Contexto", "value": "Información verificada"}],
        "requests_detected": [],
        "information_missing": [] if evidence_complete else [
            {"key": "product", "description": "Producto"}
        ],
        "evidence_complete": evidence_complete,
        "decisions": [decision],
        "decisions_pending": [decision["decision_id"]],
        "next_steps": [],
        "last_call_result": {"call_status": "completed"},
        "needs_follow_up": False,
        "call_count": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return state, decision


class MissionDecisionTests(unittest.TestCase):
    def test_create_pending_decision_is_stable_and_has_required_fields(self):
        first = create_pending_decision("test_mission", "Aprobar catálogo", "decision_engine")
        second = create_pending_decision("test_mission", "Aprobar catálogo", "decision_engine")
        self.assertEqual(first, second)
        self.assertEqual(set(first), {
            "decision_id", "mission_id", "description", "source", "status",
            "resolved_by", "resolution", "resolved_at", "created_at",
        })
        self.assertEqual(first["status"], "pending")

    def test_same_decision_text_on_separate_calls_has_distinct_identity(self):
        first = create_pending_decision(
            "test_mission", "Aprobar catálogo", "decision_engine",
            "2026-09-27T12:00:00+00:00",
        )
        second = create_pending_decision(
            "test_mission", "Aprobar catálogo", "decision_engine",
            "2026-09-28T12:00:00+00:00",
        )
        self.assertNotEqual(first["decision_id"], second["decision_id"])

    def test_legacy_pending_strings_are_migrated_to_pending_records(self):
        migrated = normalize_decision_records(
            "test_mission", [], ["Aprobar una revisión manual"]
        )
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0]["status"], "pending")
        self.assertEqual(migrated[0]["source"], "legacy_mission_state")

    def test_approve_reject_and_defer_are_recorded_without_execution(self):
        mission = mission_fixture()
        for outcome in ("approved", "rejected", "deferred"):
            with self.subTest(outcome=outcome):
                state, decision = state_fixture()
                resolved = resolve_mission_decision(
                    state, mission, mission.id, decision["decision_id"],
                    outcome, "Fabian registró la decisión.",
                    now=datetime(2026, 9, 27, 12, tzinfo=timezone.utc),
                )
                record = resolved["decisions"][0]
                self.assertEqual(record["status"], outcome)
                self.assertEqual(record["resolved_by"], "Fabian")
                self.assertEqual(record["resolved_at"], "2026-09-27T12:00:00+00:00")
                self.assertEqual(state["decisions"][0]["status"], "pending")

    def test_nonexistent_mission_and_decision_are_rejected(self):
        state, decision = state_fixture()
        with self.assertRaises(MissionDecisionError) as missing_mission:
            resolve_mission_decision(
                state, None, "test_mission", decision["decision_id"],
                "approved", "Registrado.",
            )
        self.assertEqual(str(missing_mission.exception), "mission_not_found")
        with self.assertRaises(MissionDecisionError) as missing_decision:
            resolve_mission_decision(
                state, mission_fixture(), "test_mission", "unknown",
                "approved", "Registrado.",
            )
        self.assertEqual(str(missing_decision.exception), "decision_not_found")

    def test_decision_must_belong_to_the_supplied_mission(self):
        state, decision = state_fixture()
        with self.assertRaises(MissionDecisionError) as mismatch:
            resolve_mission_decision(
                state, mission_fixture("other_mission"), "other_mission",
                decision["decision_id"], "approved", "Registrado.",
            )
        self.assertEqual(str(mismatch.exception), "mission_mismatch")
        with self.assertRaises(MissionDecisionError):
            resolve_mission_decision(
                state, mission_fixture(), "other_mission",
                decision["decision_id"], "approved", "Registrado.",
            )

    def test_resolved_decisions_are_immutable(self):
        state, decision = state_fixture()
        resolved = resolve_mission_decision(
            state, mission_fixture(), "test_mission", decision["decision_id"],
            "approved", "Aprobado.",
        )
        with self.assertRaises(MissionDecisionError) as error:
            resolve_mission_decision(
                resolved, mission_fixture(), "test_mission", decision["decision_id"],
                "rejected", "Intento de cambio.",
            )
        self.assertEqual(str(error.exception), "decision_already_resolved")
        self.assertEqual(resolved["decisions"][0]["status"], "approved")

    def test_mission_state_resumes_only_when_all_decisions_are_resolved(self):
        state, first = state_fixture(evidence_complete=False)
        second = create_pending_decision("test_mission", "Confirmar condiciones", "decision_engine")
        state["decisions"].append(second)
        state["decisions_pending"].append(second["decision_id"])
        state = resolve_mission_decision(
            state, mission_fixture(), "test_mission", first["decision_id"],
            "approved", "Autorizado para registro, sin ejecutar envío.",
        )
        self.assertEqual(state["status"], "waiting_for_user")
        self.assertEqual(state["decisions_pending"], [second["decision_id"]])
        state = resolve_mission_decision(
            state, mission_fixture(), "test_mission", second["decision_id"],
            "rejected", "No aprobar por ahora.",
        )
        self.assertEqual(state["status"], "in_progress")
        self.assertEqual(state["decisions_pending"], [])
        self.assertFalse(state["evidence_complete"])
        self.assertNotEqual(state["status"], "completed")

    def test_no_pending_decisions_can_complete_only_with_stored_evidence_flag(self):
        mission = mission_fixture()
        state, decision = state_fixture(evidence_complete=True)
        updated = resolve_mission_decision(
            state, mission, mission.id, decision["decision_id"],
            "approved", "Decisión registrada.",
        )
        self.assertEqual(updated["status"], "completed")
        state, decision = state_fixture(evidence_complete=False)
        updated = resolve_mission_decision(
            state, mission, mission.id, decision["decision_id"],
            "approved", "Decisión registrada.",
        )
        self.assertNotEqual(updated["status"], "completed")

    def test_approved_action_is_only_recorded_and_remains_follow_up(self):
        state, decision = state_fixture(evidence_complete=True)
        state["next_steps"] = ["Enviar catálogo al contacto"]
        updated = resolve_mission_decision(
            state, mission_fixture(), "test_mission", decision["decision_id"],
            "approved", "Aprobado para ejecución posterior.",
        )
        self.assertEqual(updated["status"], "needs_follow_up")
        self.assertTrue(updated["needs_follow_up"])
        self.assertEqual(updated["decisions"][0]["status"], "approved")
        self.assertIn("Enviar catálogo al contacto", updated["next_steps"])

    def test_deferred_decision_marks_follow_up_without_execution(self):
        state, decision = state_fixture()
        updated = resolve_mission_decision(
            state, mission_fixture(), "test_mission", decision["decision_id"],
            "deferred", "Revisar más adelante.",
        )
        self.assertEqual(updated["status"], "needs_follow_up")
        self.assertTrue(updated["needs_follow_up"])
        self.assertEqual(updated["follow_up_reason"], "decision_deferred")

    def test_persisted_resolution_updates_json_and_txt(self):
        state, decision = state_fixture()
        mission = mission_fixture()
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFileCallResultStorage(directory)
            result = empty_call_result("completed", "spanish", mission=mission)
            result["mission_state"] = state
            storage.write(result)
            catalog = SimpleNamespace(get=lambda mission_id: mission if mission_id == mission.id else None)
            persisted = resolve_persisted_mission_decision(
                storage, mission.id, decision["decision_id"], "approved",
                "Aprobado para revisión manual.",
                mission_loader=lambda: catalog,
            )
            saved_path = next(Path(directory).glob("call-*.json"))
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            report = saved_path.with_suffix(".txt").read_text(encoding="utf-8")
            with self.assertRaises(MissionDecisionError) as error:
                resolve_persisted_mission_decision(
                    storage, mission.id, decision["decision_id"], "rejected",
                    "No cambiar una decisión ya resuelta.",
                    mission_loader=lambda: catalog,
                )
            self.assertEqual(str(error.exception), "decision_already_resolved")
        record = saved["mission_state"]["decisions"][0]
        self.assertEqual(record["status"], "approved")
        self.assertEqual(persisted["decisions_pending"], [])
        self.assertIn("MISSION DECISIONS", report)
        self.assertIn("Estado: approved", report)
        self.assertIn("Resuelta por: Fabian", report)
        self.assertIn("Aprobado para revisión manual.", report)

    def test_persisted_resolver_rejects_missing_mission(self):
        class MissingCatalog:
            def get(self, mission_id):
                raise MissionNotFoundError("missing")

        storage = SimpleNamespace()
        with self.assertRaises(MissionDecisionError) as error:
            resolve_persisted_mission_decision(
                storage, "unknown", "unknown", "approved", "No existe.",
                mission_loader=lambda: MissingCatalog(),
            )
        self.assertEqual(str(error.exception), "mission_not_found")

    def test_historical_reports_without_decisions_remain_readable(self):
        legacy = empty_call_result("completed", "spanish")
        report = render_call_report(legacy)
        self.assertIn("REPORTE DE LLAMADA", report)
        self.assertNotIn("MISSION DECISIONS", report)

    def test_resolution_redacts_secrets_and_never_imports_action_clients(self):
        secret = "local-mission-decision-secret"
        previous = os.environ.get("CALL_SECRET")
        os.environ["CALL_SECRET"] = secret
        try:
            state, decision = state_fixture()
            resolved = resolve_mission_decision(
                state, mission_fixture(), "test_mission", decision["decision_id"],
                "approved", f"{secret} was recorded.",
            )
        finally:
            if previous is None:
                os.environ.pop("CALL_SECRET", None)
            else:
                os.environ["CALL_SECRET"] = previous
        self.assertNotIn(secret, json.dumps(resolved))
        source = Path(__file__).parents[1].joinpath("mission_decision.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("requests", imports)
        self.assertNotIn("twilio", imports)
        self.assertNotIn("websockets", imports)


if __name__ == "__main__":
    unittest.main()
