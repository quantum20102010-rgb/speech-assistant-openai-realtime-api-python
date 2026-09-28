"""Deterministic mission progress assembled from persisted call artifacts."""

from datetime import datetime, timezone
from copy import deepcopy
import re

from call_results import redact_configured_secrets
from mission_decision import create_pending_decision, normalize_decision_records


MISSION_STATE_VERSION = 1
MISSION_STATUSES = {
    "pending", "in_progress", "waiting_for_user", "completed", "blocked",
    "needs_follow_up",
}
MAX_ITEMS = 40
MAX_TEXT_LENGTH = 500

_PRIVATE_VALUE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|AC[0-9a-fA-F]{32}|"
    r"[A-Fa-f0-9]{32}|[A-Za-z0-9_-]{40,}|"
    r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
_CANNOT_PROVIDE = (
    "no puedo proporcionar", "no podemos proporcionar", "no dispongo",
    "no disponemos", "no cuento con", "no contamos con", "no lo se",
    "no lo sé", "no tengo esa informacion", "no tengo esa información",
    "cannot provide", "can't provide", "do not have", "don't have",
    "not available", "i don't know", "we don't know",
)


def _safe_text(value):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip()[:MAX_TEXT_LENGTH]
    if not value:
        return None
    value = redact_configured_secrets(value)
    value = _PRIVATE_VALUE.sub("[REDACTED]", value)
    value = _EMAIL.sub("[correo omitido]", value)
    value = _PHONE.sub("[teléfono omitido]", value)
    return value[:MAX_TEXT_LENGTH]


def _safe_items(values, limit=MAX_ITEMS):
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for item in values:
        text = _safe_text(item)
        key = text.casefold() if text else ""
        if text and key not in seen:
            seen.add(key)
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _merge_items(previous, current):
    return _safe_items((previous if isinstance(previous, list) else []) + current)


def _mission_fields(mission):
    fields = getattr(mission, "required_information", ()) or ()
    return [field for field in fields if isinstance(field, dict) and isinstance(field.get("key"), str)]


def _decision_facts(result, mission, decision, notification):
    facts = []
    findings = result.get("mission_findings")
    findings = findings if isinstance(findings, dict) else {}
    progress = decision.get("mission_progress")
    progress = progress if isinstance(progress, dict) else {}
    supported_fields = set(progress.get("supported_fields", []))
    field_labels = {field["key"]: field for field in _mission_fields(mission)}
    for key in supported_fields:
        field = field_labels.get(key)
        value = _safe_text(findings.get(key))
        if field and value:
            facts.append({"field": key, "label": _safe_text(field.get("description")) or key, "value": value})

    notified = notification.get("facts_to_notify")
    if isinstance(notified, list):
        for item in notified:
            if isinstance(item, dict) and item.get("source") == "transcript":
                value = _safe_text(item.get("text"))
                if value:
                    facts.append({"field": "conversation", "label": "Información expresada", "value": value})

    unique = []
    seen = set()
    for fact in facts:
        identity = (fact["field"], fact["value"].casefold())
        if identity not in seen:
            seen.add(identity)
            unique.append(fact)
    return unique[:MAX_ITEMS]


def _merge_facts(previous, current):
    values = previous if isinstance(previous, list) else []
    result = []
    seen = set()
    for fact in values + current:
        if not isinstance(fact, dict):
            continue
        field = _safe_text(fact.get("field"))
        label = _safe_text(fact.get("label"))
        value = _safe_text(fact.get("value"))
        if not field or not value:
            continue
        identity = (field.casefold(), value.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        result.append({"field": field, "label": label or field, "value": value})
        if len(result) >= MAX_ITEMS:
            break
    return result


def _missing_information(mission, obtained_facts):
    fields = _mission_fields(mission)
    obtained = {
        item.get("field") for item in obtained_facts
        if isinstance(item, dict) and isinstance(item.get("field"), str)
    }
    return [
        {"key": field["key"], "description": _safe_text(field.get("description")) or field["key"]}
        for field in fields if field["key"] not in obtained
    ]


def _last_call(result, decision, notification, message):
    message = message if isinstance(message, dict) else {}
    date_time = result.get("date_time")
    try:
        date_time = datetime.fromisoformat(date_time).isoformat() if isinstance(date_time, str) else None
    except ValueError:
        date_time = None
    return {
        "call_status": _safe_text(result.get("call_status")) or "unknown",
        "date_time": date_time,
        "duration_seconds": result.get("duration_seconds") if isinstance(result.get("duration_seconds"), (int, float)) else None,
        "decision_status": _safe_text(decision.get("status")) or "unknown",
        "notification_preview_ready": message.get("should_send") is True,
        "notification_priority": _safe_text(notification.get("priority")) or "none",
    }


def update_mission_state(previous_state, call_result, mission, decision,
                         notification_decision, notification_message,
                         whatsapp_approval_needed=False):
    """Return a cumulative state without reading transcript or calling a model."""
    result = call_result if isinstance(call_result, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    notification = notification_decision if isinstance(notification_decision, dict) else {}
    previous = previous_state if isinstance(previous_state, dict) else {}
    if previous.get("mission_id") != getattr(mission, "id", None):
        previous = {}

    mission_id = getattr(mission, "id", None)
    objective = _safe_text(getattr(mission, "objective", "")) or "Objetivo no disponible"
    facts = _merge_facts(
        previous.get("facts_obtained"),
        _decision_facts(result, mission, decision, notification),
    )
    required_keys = {field["key"] for field in _mission_fields(mission)}
    supported_keys = {
        item.get("field") for item in facts
        if isinstance(item, dict) and item.get("field") in required_keys
    }
    complete = bool(required_keys) and required_keys.issubset(supported_keys)
    supported_count = len(supported_keys)
    requests = _merge_items(
        previous.get("requests_detected"), _safe_items(notification.get("requests_from_contact"))
    )
    missing = _missing_information(mission, facts)

    legacy_pending = (
        previous.get("decisions_pending")
        if not isinstance(previous.get("decisions"), list) else []
    )
    decisions = normalize_decision_records(
        mission_id, previous.get("decisions"), legacy_pending
    )
    by_decision_id = {item["decision_id"]: item for item in decisions}
    for source, descriptions in (
        ("decision_engine", decision.get("approval_required")),
        ("notification_decision", notification.get("decisions_needed_from_user")),
        ("notification_decision", ["Enviar notificación por WhatsApp a Fabian"]
         if notification.get("should_notify") is True and whatsapp_approval_needed is True else []),
    ):
        for description in _safe_items(descriptions):
            try:
                item = create_pending_decision(
                    mission_id, description, source, result.get("date_time")
                )
            except ValueError:
                continue
            by_decision_id.setdefault(item["decision_id"], item)
    decisions = list(by_decision_id.values())
    decisions_pending = [
        item["decision_id"] for item in decisions if item["status"] == "pending"
    ]
    next_steps_now = _safe_items(notification.get("next_steps"))
    if not next_steps_now:
        follow_up = decision.get("follow_up")
        if isinstance(follow_up, dict):
            next_steps_now = _safe_items([follow_up.get("next_step")])
    next_steps = _merge_items(previous.get("next_steps"), next_steps_now)

    call_status = result.get("call_status")
    unable_to_provide = any(
        cue in item.casefold()
        for fact in notification.get("facts_to_notify", []) if isinstance(fact, dict)
        for item in [fact.get("text", "")]
        if isinstance(item, str)
        for cue in _CANNOT_PROVIDE
    )
    blocked = call_status in {
        "error", "rejected", "blocked", "connection_error", "openai_dependency_error"
    }
    follow_up_required = (
        bool(next_steps_now)
        or unable_to_provide
        or call_status in {"no-answer", "busy", "failed", "disconnected", "max_duration", "hold_detected"}
    )

    if blocked:
        status = "blocked"
        follow_up_reason = "call_blocked_or_failed"
    elif decisions_pending:
        status = "waiting_for_user"
        follow_up_reason = "decision_pending"
    elif complete:
        status = "completed"
        follow_up_reason = None
    elif follow_up_required:
        status = "needs_follow_up"
        follow_up_reason = (
            "contact_could_not_provide_information" if unable_to_provide
            else "call_ended_before_mission_completion" if call_status in {"max_duration", "hold_detected"}
            else "next_step_or_contact_follow_up_detected"
        )
    elif decision.get("relevance") == "not_relevant":
        status = previous.get("status") if previous.get("status") in MISSION_STATUSES else "pending"
        follow_up_reason = previous.get("follow_up_reason")
    elif supported_count > 0 or facts or requests:
        status = "in_progress"
        follow_up_reason = None
    else:
        status = previous.get("status") if previous.get("status") in MISSION_STATUSES else "pending"
        follow_up_reason = previous.get("follow_up_reason")

    # Completion is durable unless it has an explicit, unresolved user decision.
    if previous.get("status") == "completed" and not decisions_pending:
        status = "completed"

    prior_call_count = previous.get("call_count", 0)
    if not isinstance(prior_call_count, int) or prior_call_count < 0:
        prior_call_count = 0
    state = {
        "schema_version": MISSION_STATE_VERSION,
        "mission_id": mission_id,
        "objective": objective,
        "status": status,
        "facts_obtained": facts,
        "requests_detected": requests,
        "information_missing": missing,
        "evidence_complete": complete,
        "decisions": decisions,
        "decisions_pending": decisions_pending,
        "actions": deepcopy(previous.get("actions"))
        if isinstance(previous.get("actions"), list) else [],
        "next_steps": next_steps,
        "last_call_result": _last_call(result, decision, notification, notification_message),
        "needs_follow_up": status == "needs_follow_up",
        "follow_up_reason": follow_up_reason,
        "call_count": prior_call_count + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return state
