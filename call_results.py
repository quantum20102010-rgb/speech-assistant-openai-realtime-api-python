"""Structured call reports and replaceable local result storage."""

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPORT_FIELDS = (
    "company",
    "contact_name",
    "contact_role",
    "email",
    "phone",
    "distribution_interest",
    "b2b_terms",
    "minimum_order_quantity",
    "pricing_information",
    "discount_information",
    "territories",
    "exclusivity",
    "relevant_information",
    "summary",
    "next_action",
    "follow_up_reason",
    "follow_up_date",
)

FIELD_LABELS = {
    "company": "Empresa",
    "contact_name": "Contacto",
    "contact_role": "Cargo",
    "email": "Correo",
    "phone": "Teléfono",
    "distribution_interest": "Interés en distribución",
    "b2b_terms": "Condiciones B2B",
    "minimum_order_quantity": "MOQ",
    "pricing_information": "Precios",
    "discount_information": "Descuentos",
    "territories": "Territorios",
    "exclusivity": "Exclusividad",
    "relevant_information": "Información expresada",
    "summary": "Resumen",
    "next_action": "Acción",
    "follow_up_reason": "Motivo",
    "follow_up_date": "Fecha sugerida",
}
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_ACCOUNT_SID",
    "CALL_SECRET",
)


def redact_configured_secrets(value):
    for name in SECRET_ENV_NAMES:
        secret = os.getenv(name, "")
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|AC[0-9a-fA-F]{32})\b",
        "[REDACTED]",
        value,
    )


class CallTranscript:
    """Collect final transcript events without logging conversation content."""

    def __init__(self):
        self._segments = []
        self._seen = set()
        self._pending_input = {}

    def add_realtime_event(self, event):
        event_type = event.get("type")
        if event_type == "input_audio_buffer.committed":
            item_id = event.get("item_id")
            if item_id and item_id not in self._pending_input:
                self._pending_input[item_id] = len(self._segments)
                self._segments.append(("Contacto", ""))
            return bool(item_id)
        if event_type == "conversation.item.input_audio_transcription.completed":
            speaker, text = "Contacto", event.get("transcript")
        elif event_type == "response.output_audio_transcript.done":
            speaker, text = "Asistente", event.get("transcript")
        else:
            return False
        item_id = event.get("item_id")
        identity = (speaker, item_id) if item_id else None
        if not isinstance(text, str) or not text.strip():
            return False
        if identity and identity in self._seen:
            return False
        if identity:
            self._seen.add(identity)
        safe_text = redact_configured_secrets(text.strip())
        if speaker == "Contacto" and item_id in self._pending_input:
            self._segments[self._pending_input.pop(item_id)] = (speaker, safe_text)
        else:
            self._segments.append((speaker, safe_text))
        return True

    def render(self):
        return "\n".join(
            f"{speaker}: {text}" for speaker, text in self._segments if text
        )


def empty_call_result(status, language="spanish", started_at=None, duration_seconds=0,
                      mission=None):
    result = {
        "call_status": status,
        "date_time": (started_at or datetime.now(timezone.utc)).isoformat(),
        "language": language,
        "duration_seconds": max(0, round(float(duration_seconds), 2)),
        "transcript": "",
        "summary_generated_by_model": False,
        "mission_id": mission.id if mission else None,
        "mission_name": mission.name if mission else None,
        "mission_findings": {
            field["key"]: None for field in mission.required_information
        } if mission else {},
    }
    result.update({field: None for field in REPORT_FIELDS})
    return result


def normalize_model_result(value, status, language, started_at, duration_seconds,
                           mission=None):
    result = empty_call_result(
        status, language, started_at, duration_seconds, mission
    )
    for field in REPORT_FIELDS:
        item = value.get(field) if isinstance(value, dict) else None
        if isinstance(item, str):
            item = redact_configured_secrets(item.strip()[:2000]) or None
        else:
            item = None
        result[field] = item
    result["summary_generated_by_model"] = bool(result["summary"])
    supplied_findings = value.get("mission_findings") if isinstance(value, dict) else None
    if mission and isinstance(supplied_findings, dict):
        for field in mission.required_information:
            item = supplied_findings.get(field["key"])
            if isinstance(item, str):
                result["mission_findings"][field["key"]] = (
                    redact_configured_secrets(item.strip()[:2000]) or None
                )
    return result


def render_call_report(result):
    def value(field):
        return result.get(field) if result.get(field) is not None else "No proporcionado"

    lines = [
        "GUZI STUFF — REPORTE DE LLAMADA",
        "================================",
        "",
        f"Fecha/hora: {result.get('date_time') or 'No proporcionado'}",
        f"Idioma: {result.get('language') or 'No proporcionado'}",
        f"Misión: {result.get('mission_name') or 'No especificada'}",
        f"Duración: {result.get('duration_seconds', 0)} segundos",
        f"Estado: {result.get('call_status') or 'error'}",
        "",
        "EMPRESA",
        "-------",
    ]
    for field in ("company", "contact_name", "contact_role", "email", "phone"):
        lines.append(f"{FIELD_LABELS[field]}: {value(field)}")
    lines.extend(["", "OPORTUNIDAD DE DISTRIBUCIÓN", "---------------------------"])
    for field in (
        "distribution_interest", "b2b_terms", "minimum_order_quantity",
        "pricing_information", "discount_information", "territories", "exclusivity",
    ):
        lines.append(f"{FIELD_LABELS[field]}: {value(field)}")
    mission_findings = result.get("mission_findings") or {}
    if mission_findings:
        lines.extend(["", "HALLAZGOS DE LA MISIÓN", "----------------------"])
        for key, finding in mission_findings.items():
            label = key.replace("_", " ").capitalize()
            lines.append(
                f"{label}: {finding if finding is not None else 'No proporcionado'}"
            )
    mission_state = result.get("mission_state")
    if isinstance(mission_state, dict):
        lines.extend(["", "MISSION STATE", "-------------"])
        lines.append(f"Objetivo: {mission_state.get('objective') or 'No disponible'}")
        lines.append(f"Estado: {mission_state.get('status') or 'pending'}")
        lines.append("Información obtenida:")
        facts = mission_state.get("facts_obtained")
        if isinstance(facts, list) and facts:
            lines.extend(
                f"- {item.get('label') or item.get('field')}: {item.get('value')}"
                for item in facts if isinstance(item, dict) and isinstance(item.get("value"), str)
            )
        else:
            lines.append("- Ninguna registrada")
        lines.append("Información faltante:")
        missing = mission_state.get("information_missing")
        if isinstance(missing, list) and missing:
            lines.extend(
                f"- {item.get('description') or item.get('key')}"
                for item in missing if isinstance(item, dict)
            )
        else:
            lines.append("- Ninguna registrada")
        lines.append("Decisiones pendientes:")
        decisions = mission_state.get("decisions_pending")
        if isinstance(decisions, list) and decisions:
            decision_records = mission_state.get("decisions")
            by_id = {
                item.get("decision_id"): item
                for item in decision_records if isinstance(item, dict)
            } if isinstance(decision_records, list) else {}
            for item in decisions:
                record = by_id.get(item) if isinstance(item, str) else None
                if record:
                    lines.append(f"- {record.get('description') or item}")
                elif isinstance(item, str):
                    # Legacy Mission State stored pending decisions as strings.
                    lines.append(f"- {item}")
        else:
            lines.append("- Ninguna")
        lines.append("Siguiente paso:")
        steps = mission_state.get("next_steps")
        if isinstance(steps, list) and steps:
            lines.extend(f"- {item}" for item in steps if isinstance(item, str))
        else:
            lines.append("- No definido")
        lines.append(
            f"Seguimiento: {'Sí' if mission_state.get('needs_follow_up') else 'No'}"
            + (f" ({mission_state.get('follow_up_reason')})" if mission_state.get("follow_up_reason") else "")
        )
        lines.append("")
        decision_records = mission_state.get("decisions")
        if isinstance(decision_records, list):
            lines.extend(["MISSION DECISIONS", "-----------------"])
            if decision_records:
                for item in decision_records:
                    if not isinstance(item, dict):
                        continue
                    lines.extend([
                        f"Decisión: {item.get('description') or 'No disponible'}",
                        f"Estado: {item.get('status') or 'pending'}",
                        f"Resolución: {item.get('resolution') or 'Sin resolver'}",
                        f"Resuelta por: {item.get('resolved_by') or 'Pendiente'}",
                        f"Fecha/hora: {item.get('resolved_at') or 'Pendiente'}",
                        "",
                    ])
            else:
                lines.extend(["Sin decisiones registradas.", ""])
        actions = mission_state.get("actions")
        if isinstance(actions, list):
            lines.extend(["MISSION ACTIONS", "---------------"])
            if actions:
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    lines.extend([
                        f"Acción: {action.get('action_type') or 'unknown'}",
                        f"Descripción: {action.get('description') or 'No disponible'}",
                        f"Estado: {action.get('status') or 'blocked_by_policy'}",
                        f"Requiere aprobación: {'Sí' if action.get('requires_approval') is True else 'No'}",
                        f"Aprobación: {action.get('approval_status') or 'unknown'}",
                        f"Ejecutada: {'Sí' if action.get('executed_at') else 'No'}",
                        "",
                    ])
            else:
                lines.extend(["Sin acciones registradas.", ""])
    lines.extend([
        "", "RESUMEN (GENERADO POR EL MODELO)", "-------------------------------",
        value("summary") if result.get("summary_generated_by_model") else "Resumen del modelo no disponible.",
        "", "INFORMACIÓN RELEVANTE", "---------------------", value("relevant_information"),
        "", "ACCIONES", "--------", value("next_action"),
        "", "PRÓXIMO PASO", "------------",
        f"Acción: {value('next_action')}",
        f"Motivo: {value('follow_up_reason')}",
        f"Fecha sugerida: {value('follow_up_date')}",
        "",
        "TRANSCRIPCIÓN DE LA CONVERSACIÓN",
        "---------------------------------",
        result.get("transcript") or "Transcripción no disponible.",
        "",
    ])
    decision = result.get("decision")
    if isinstance(decision, dict):
        lines.extend([
            "DECISIÓN COMERCIAL",
            "-------------------",
            f"Estado: {decision.get('status') or 'failed'}",
            f"Relevancia: {decision.get('relevance') or 'unknown'}",
        ])
        requests = decision.get("requests_from_contact") or []
        lines.append("Solicitudes del contacto:")
        lines.extend(f"- {item}" for item in requests if isinstance(item, str))
        lines.append("Información por proporcionar:")
        lines.extend(
            f"- {item}" for item in (decision.get("information_to_provide") or [])
            if isinstance(item, str)
        )
        approvals = decision.get("approval_required") or []
        lines.append("Acciones que requieren aprobación:")
        lines.extend(f"- {item}" for item in approvals if isinstance(item, str))
        summary = decision.get("summary")
        if isinstance(summary, dict):
            lines.append(summary.get("label") or "Resumen generado por el modelo no disponible.")
            if isinstance(summary.get("text"), str):
                lines.append(summary["text"])
        lines.append("")
    notification = result.get("notification_decision")
    if isinstance(notification, dict):
        lines.extend([
            "SMART NOTIFICATION DECISION",
            "----------------------------",
            f"Notificar a Fabian: {'Sí' if notification.get('should_notify') else 'No'}",
            f"Prioridad: {notification.get('priority') or 'none'}",
            f"Motivo: {notification.get('reason_code') or 'no_relevant_information'}",
            f"Resumen: {notification.get('notification_summary') or 'Sin información suficiente para notificar.'}",
        ])
        facts = notification.get("facts_to_notify") or []
        lines.append("Hechos respaldados por la conversación:")
        lines.extend(
            f"- {item.get('text')}" for item in facts
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
        requests = notification.get("requests_from_contact") or []
        lines.append("Solicitudes explícitas del contacto:")
        lines.extend(f"- {item}" for item in requests if isinstance(item, str))
        decisions = notification.get("decisions_needed_from_user") or []
        lines.append("Decisiones pendientes del usuario:")
        lines.extend(f"- {item}" for item in decisions if isinstance(item, str))
        recommendations = notification.get("recommendations") or []
        lines.append("Recomendaciones (no ejecutadas):")
        lines.extend(
            f"- {item}" for item in recommendations if isinstance(item, str)
        )
        lines.append("")
    message_preview = result.get("notification_message")
    if isinstance(message_preview, dict):
        lines.extend([
            "NOTIFICATION MESSAGE PREVIEW (NOT SENT)",
            "---------------------------------------",
            f"Should send: {'Yes' if message_preview.get('should_send') else 'No'}",
            f"Channel: {message_preview.get('channel') or 'No disponible'}",
            f"Priority: {message_preview.get('priority') or 'none'}",
            f"Type: {message_preview.get('message_type') or 'none'}",
            message_preview.get("message") or "No se generó mensaje.",
            "",
        ])
    whatsapp_preview = result.get("whatsapp_preview")
    if isinstance(whatsapp_preview, dict):
        lines.extend([
            "WHATSAPP PREVIEW",
            "----------------",
            "NOT SENT",
            f"Status: {whatsapp_preview.get('status') or 'blocked'}",
            f"dry_run={str(whatsapp_preview.get('dry_run') is True).lower()}",
            f"sent={str(whatsapp_preview.get('sent') is True).lower()}",
            f"Motivo: {whatsapp_preview.get('reason_code') or 'not_available'}",
            "",
        ])
    email_draft = result.get("email_draft")
    if isinstance(email_draft, dict):
        lines.extend([
            "EMAIL DRAFT (NOT SENT)",
            "-----------------------",
            "NOT SENT",
            f"Estado: {email_draft.get('approval_status') or 'not_created'}",
            f"Para: {', '.join(email_draft.get('to', [])) if isinstance(email_draft.get('to'), list) else ''}",
            f"Asunto: {email_draft.get('subject') or 'No generado'}",
            "Cuerpo:",
            email_draft.get("body") or "No se generó borrador.",
            f"Requiere aprobación: {'Sí' if email_draft.get('requires_approval') is True else 'No'}",
            "sent=false",
            "",
        ])
    executions = result.get("email_execution_results")
    if isinstance(executions, dict) and executions:
        all_dry_run = all(
            isinstance(item, dict) and item.get("dry_run") is True
            for item in executions.values()
        )
        heading = "EMAIL EXECUTOR (DRY RUN)" if all_dry_run else "EMAIL EXECUTOR (CONTROLLED)"
        lines.extend([heading, "------------------------"])
        for execution in executions.values():
            if not isinstance(execution, dict):
                continue
            lines.extend([
                "SENT" if execution.get("sent") is True else "NOT SENT",
                f"Estado: {execution.get('status') or 'blocked'}",
                f"Para: {', '.join(execution.get('to', [])) if isinstance(execution.get('to'), list) else ''}",
                f"Asunto: {execution.get('subject') or 'No disponible'}",
                f"Cuerpo: {execution.get('body') or 'No disponible'}",
                f"dry_run={str(execution.get('dry_run') is True).lower()}",
                f"sent={str(execution.get('sent') is True).lower()}",
                "",
            ])
    whatsapp_executions = result.get("whatsapp_execution_results")
    if isinstance(whatsapp_executions, dict) and whatsapp_executions:
        from whatsapp_executor import mask_whatsapp_recipient
        lines.extend(["WHATSAPP EXECUTOR (DRY RUN)", "---------------------------"])
        for execution in whatsapp_executions.values():
            if not isinstance(execution, dict):
                continue
            lines.extend([
                "NOT SENT",
                f"Estado: {execution.get('status') or 'blocked'}",
                f"Destinatario: {mask_whatsapp_recipient(execution.get('recipient'))}",
                "Mensaje:",
                execution.get("message") or "No disponible",
                "dry_run=true",
                "sent=false",
                "",
            ])
    availability = result.get("availability")
    if isinstance(availability, dict):
        lines.extend([
            "POLÍTICA DE DISPONIBILIDAD",
            "--------------------------",
            f"Llamada permitida: {'Sí' if availability.get('allowed') else 'No'}",
            f"Motivo: {availability.get('reason') or 'No disponible'}",
            f"Zona horaria: {availability.get('timezone') or 'No disponible'}",
            f"Hora local: {availability.get('local_time') or 'No disponible'}",
        ])
        window = availability.get("window")
        if isinstance(window, dict):
            lines.append(
                f"Ventana: {window.get('start') or '?'}–{window.get('end') or '?'}"
            )
        lines.append("")
    return "\n".join(lines)


class CallResultStorage:
    """Storage interface; implement this contract for S3-compatible or DB storage."""

    def write(self, result):
        raise NotImplementedError

    def latest_mission_state(self, mission_id):
        return None

    def latest_mission_result(self, mission_id):
        return None

    def update_latest_email_draft(self, mission_id, email_draft):
        return False

    def record_email_execution_result(self, mission_id, action_id, execution_result):
        return None

    def record_whatsapp_execution_result(self, mission_id, action_id, execution_result):
        return None


class LocalFileCallResultStorage(CallResultStorage):
    def __init__(self, directory=None):
        self.directory = Path(directory or os.getenv("CALL_RESULTS_DIR", "call_results"))
        self._lock = threading.RLock()

    def _atomic_private_write(self, destination, content):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
        descriptor, temp_path = tempfile.mkstemp(prefix=".report-", dir=self.directory)
        try:
            if os.name != "nt":
                os.chmod(temp_path, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, destination)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def latest_mission_state(self, mission_id):
        """Read the latest persisted state for this mission, if one exists."""
        with self._lock:
            latest_path = self._latest_mission_result_path(mission_id)
            if latest_path is None:
                return None
            try:
                result = json.loads(latest_path.read_text(encoding="utf-8"))
                state = result.get("mission_state")
                return state if isinstance(state, dict) else None
            except (OSError, json.JSONDecodeError, TypeError):
                return None

    def latest_mission_result(self, mission_id):
        """Read a copy of the latest persisted result for a mission ID."""
        with self._lock:
            path = self._latest_mission_result_path(mission_id)
            if path is None:
                return None
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
                return result if isinstance(result, dict) and result.get("mission_id") == mission_id else None
            except (OSError, json.JSONDecodeError, TypeError):
                return None

    def update_latest_email_draft(self, mission_id, email_draft):
        """Update only draft metadata in the latest report for this mission."""
        with self._lock:
            path = self._latest_mission_result_path(mission_id)
            if path is None or not isinstance(email_draft, dict):
                return False
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
                state = result.get("mission_state") if isinstance(result, dict) else None
                if (
                    not isinstance(state, dict)
                    or state.get("mission_id") != mission_id
                    or result.get("mission_id") != mission_id
                ):
                    return False
                current = result.get("email_draft")
                if not isinstance(current, dict):
                    return False
                linked_ids = current.get("approval_decision_ids", [])
                if not isinstance(linked_ids, list):
                    return False
                current["approval_status"] = email_draft.get("approval_status", "pending")
                if current["approval_status"] == "approved_for_future_send":
                    from email_draft import email_draft_content_digest
                    current["approved_content_sha256"] = email_draft_content_digest(current)
                else:
                    current.pop("approved_content_sha256", None)
                current["sent"] = False
                current["requires_approval"] = True
                json_data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
                text_data = render_call_report(result)
                self._atomic_private_write(path, json_data)
                self._atomic_private_write(path.with_suffix(".txt"), text_data)
                return True
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                return False

    def record_email_execution_result(self, mission_id, action_id, execution_result):
        """Persist one email outcome by action ID, without overwriting it."""
        with self._lock:
            path = self._latest_mission_result_path(mission_id)
            if path is None or not isinstance(action_id, str) or not isinstance(execution_result, dict):
                return None
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    return None
                state = result.get("mission_state")
                if result.get("mission_id") != mission_id or not isinstance(state, dict) or state.get("mission_id") != mission_id:
                    return None
                executions = result.setdefault("email_execution_results", {})
                if not isinstance(executions, dict):
                    return None
                if action_id in executions:
                    return executions[action_id]
                status = execution_result.get("status")
                valid_dry_run = status == "dry_run" and execution_result.get("sent") is False and execution_result.get("dry_run") is True
                valid_sent = status == "sent" and execution_result.get("sent") is True and execution_result.get("dry_run") is False
                valid_provider_failure = (
                    status == "blocked"
                    and execution_result.get("sent") is False
                    and execution_result.get("dry_run") is False
                    and execution_result.get("reason_code") == "email_provider_failed"
                )
                if (execution_result.get("mission_id") != mission_id
                        or execution_result.get("action_id") != action_id
                        or not (valid_dry_run or valid_sent or valid_provider_failure)):
                    return None
                executions[action_id] = execution_result
                json_data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
                text_data = render_call_report(result)
                self._atomic_private_write(path, json_data)
                self._atomic_private_write(path.with_suffix(".txt"), text_data)
                return execution_result
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                return None

    def record_whatsapp_execution_result(self, mission_id, action_id, execution_result):
        """Persist one WhatsApp dry-run result without overwriting prior output."""
        with self._lock:
            path = self._latest_mission_result_path(mission_id)
            if path is None or not isinstance(action_id, str) or not isinstance(execution_result, dict):
                return None
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    return None
                state = result.get("mission_state")
                if (result.get("mission_id") != mission_id or not isinstance(state, dict)
                        or state.get("mission_id") != mission_id):
                    return None
                executions = result.setdefault("whatsapp_execution_results", {})
                if not isinstance(executions, dict):
                    return None
                if action_id in executions:
                    return executions[action_id]
                if (execution_result.get("mission_id") != mission_id
                        or execution_result.get("action_id") != action_id
                        or execution_result.get("sent") is not False
                        or execution_result.get("dry_run") is not True):
                    return None
                executions[action_id] = execution_result
                json_data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
                text_data = render_call_report(result)
                self._atomic_private_write(path, json_data)
                self._atomic_private_write(path.with_suffix(".txt"), text_data)
                return execution_result
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                return None

    def _latest_mission_result_path(self, mission_id):
        if not isinstance(mission_id, str) or not self.directory.is_dir():
            return None
        latest_path = None
        latest_key = None
        for path in self.directory.glob("call-*.json"):
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
                state = result.get("mission_state") if isinstance(result, dict) else None
                if not isinstance(state, dict) or state.get("mission_id") != mission_id:
                    continue
                updated_at = state.get("updated_at")
                sort_key = (updated_at if isinstance(updated_at, str) else "", path.stat().st_mtime_ns)
                if latest_key is None or sort_key > latest_key:
                    latest_path, latest_key = path, sort_key
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return latest_path

    def update_latest_mission_state(self, mission_id, mission_state):
        """Persist a resolved state in the latest report for the same mission."""
        with self._lock:
            path = self._latest_mission_result_path(mission_id)
            if path is None or not isinstance(mission_state, dict):
                return False
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    return False
                current = result.get("mission_state")
                if (
                    not isinstance(current, dict)
                    or current.get("mission_id") != mission_id
                    or mission_state.get("mission_id") != mission_id
                ):
                    return False
                result["mission_state"] = mission_state
                json_data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
                text_data = render_call_report(result)
                self._atomic_private_write(path, json_data)
                self._atomic_private_write(path.with_suffix(".txt"), text_data)
                return True
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                return False

    def mutate_latest_mission_state(self, mission_id, mutator):
        """Apply a state transition under the storage lock and persist it."""
        with self._lock:
            path = self._latest_mission_result_path(mission_id)
            if path is None or not callable(mutator):
                return None
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                return None
            if not isinstance(result, dict):
                return None
            current = result.get("mission_state")
            if not isinstance(current, dict) or current.get("mission_id") != mission_id:
                return None
            updated = mutator(current)
            if not isinstance(updated, dict) or updated.get("mission_id") != mission_id:
                return None
            result["mission_state"] = updated
            try:
                json_data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
                text_data = render_call_report(result)
                self._atomic_private_write(path, json_data)
                self._atomic_private_write(path.with_suffix(".txt"), text_data)
                return updated
            except (OSError, TypeError, ValueError):
                return None

    def write(self, result):
        with self._lock:
            return self._write_unlocked(result)

    def _write_unlocked(self, result):
        identifier = uuid.uuid4().hex
        json_path = self.directory / f"call-{identifier}.json"
        txt_path = self.directory / f"call-{identifier}.txt"
        json_data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        text_data = render_call_report(result)
        self._atomic_private_write(json_path, json_data)
        try:
            self._atomic_private_write(txt_path, text_data)
        except Exception:
            try:
                json_path.unlink()
            except OSError:
                pass
            raise
        return json_path, txt_path
