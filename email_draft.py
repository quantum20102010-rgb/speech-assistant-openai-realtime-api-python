"""Deterministic, local-only commercial email draft builder.

This module has no mail, HTTP, provider, credential, or action-execution client.
It produces an unsent draft only from persisted, transcript-supported artifacts.
"""

import re
import hashlib
import json
from copy import deepcopy

from call_results import redact_configured_secrets


SOURCE = "mission_state+decision_engine+notification_decision"
MAX_FACTS = 4
MAX_REQUESTS = 3
MAX_NEXT_STEPS = 2
MAX_ITEM_LENGTH = 220

EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
EMAIL_IN_TEXT = re.compile(r"\b[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+\b")
PHONE_IN_TEXT = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
PRIVATE_VALUE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|AC[0-9a-fA-F]{32}|"
    r"[A-Fa-f0-9]{32}|[A-Za-z0-9_-]{40,}|"
    r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
SECRET_NAME = re.compile(
    r"\b(?:CALL_SECRET|OPENAI_API_KEY|TWILIO_[A-Z_]+|"
    r"WHATSAPP_[A-Z_]+|RAILWAY_[A-Z_]+)\b",
    re.IGNORECASE,
)
TECHNICAL_CONTENT = re.compile(
    r"\b(?:openai|twilio|railway|realtime|websocket|api\s*key|"
    r"access\s*token|bearer\s+|smtp|microsoft\s+graph|gmail)\b",
    re.IGNORECASE,
)
PERSONAL_FIELDS = {
    "email", "phone", "contact_name", "contact_role", "conversation",
    "call_sid", "id",
}


def _contact_text(transcript):
    if not isinstance(transcript, str):
        return ""
    return "\n".join(
        line[len("Contacto:"):].strip()
        for line in transcript.splitlines()
        if line.startswith("Contacto:") and line[len("Contacto:"):].strip()
    )


def _valid_email(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 254 or not EMAIL_PATTERN.fullmatch(value):
        return None
    return value


def _verified_recipient(call_result, decision, contact_text):
    candidate = _valid_email(call_result.get("email"))
    if not candidate or not contact_text:
        return None
    evidence = decision.get("facts") if isinstance(decision, dict) else None
    if not isinstance(evidence, list):
        return None
    verified = any(
        isinstance(item, dict)
        and item.get("field") == "email"
        and item.get("source") == "transcript_verified_result"
        and isinstance(item.get("value"), str)
        and item["value"].strip().casefold() == candidate.casefold()
        for item in evidence
    )
    if not verified or candidate.casefold() not in contact_text.casefold():
        return None
    return candidate


def _safe_copy(value, limit=MAX_ITEM_LENGTH):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip(" \t\r\n•-–—")
    if not value or TECHNICAL_CONTENT.search(value):
        return None
    value = redact_configured_secrets(value)
    value = PRIVATE_VALUE.sub("[dato omitido]", value)
    value = SECRET_NAME.sub("[dato omitido]", value)
    value = EMAIL_IN_TEXT.sub("[dato omitido]", value)
    value = PHONE_IN_TEXT.sub("[dato omitido]", value)
    value = value[:limit].rstrip()
    return value or None


def _unique(values, limit):
    result = []
    seen = set()
    for value in values:
        clean = _safe_copy(value)
        key = clean.casefold() if clean else ""
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
        if len(result) >= limit:
            break
    return result


def _grounded_facts(mission_state, notification, decision, contact_text):
    facts = []
    state_facts = mission_state.get("facts_obtained", [])
    if isinstance(state_facts, list):
        for item in state_facts:
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            if not isinstance(field, str) or field.casefold() in PERSONAL_FIELDS:
                continue
            label = _safe_copy(item.get("label"))
            value = _safe_copy(item.get("value"))
            if label and value:
                facts.append(f"{label}: {value}")

    decision_facts = decision.get("facts", []) if isinstance(decision, dict) else []
    decision_evidence = set()
    verified_fields = {}
    if isinstance(decision_facts, list):
        for item in decision_facts:
            if not isinstance(item, dict) or item.get("source") not in {
                "transcript", "transcript_verified_result"
            }:
                continue
            for key in ("text", "value"):
                value = item.get(key)
                if isinstance(value, str):
                    decision_evidence.add(" ".join(value.split()).casefold())
            if all(isinstance(item.get(key), str) for key in ("label", "value")):
                decision_evidence.add(
                    f"{item['label']}: {item['value']}".casefold()
                )
            if (
                item.get("source") == "transcript_verified_result"
                and isinstance(item.get("field"), str)
                and isinstance(item.get("value"), str)
            ):
                verified_fields[item["field"]] = item["value"]

    notified_facts = notification.get("facts_to_notify", [])
    if isinstance(notified_facts, list):
        for item in notified_facts:
            if not isinstance(item, dict) or item.get("source") != "transcript":
                continue
            field = item.get("field")
            if isinstance(field, str) and field.casefold() in PERSONAL_FIELDS:
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            normalized = " ".join(text.split()).casefold()
            field = item.get("field")
            verified_value = verified_fields.get(field) if isinstance(field, str) else None
            transcript_supported = (
                verified_value.casefold() in contact_text.casefold()
                if isinstance(verified_value, str)
                else normalized in contact_text.casefold()
            )
            if normalized not in decision_evidence or not transcript_supported:
                continue
            clean = _safe_copy(text)
            if clean:
                facts.append(clean)
    return _unique(facts, MAX_FACTS)


def _grounded_requests(notification, decision, contact_text):
    requests = notification.get("requests_from_contact", [])
    explicit = decision.get("requests_from_contact", []) if isinstance(decision, dict) else []
    if not isinstance(requests, list) or not isinstance(explicit, list):
        return []
    supported = {item.casefold() for item in explicit if isinstance(item, str)}
    return _unique(
        [item for item in requests if isinstance(item, str)
         and item.casefold() in supported
         and item.casefold() in contact_text.casefold()],
        MAX_REQUESTS,
    )


def _grounded_next_steps(notification, decision, contact_text):
    steps = notification.get("next_steps", [])
    follow_up = decision.get("follow_up", {}) if isinstance(decision, dict) else {}
    step = follow_up.get("next_step") if isinstance(follow_up, dict) else None
    if not isinstance(steps, list) or not isinstance(step, str):
        return []
    supported = step.casefold()
    return _unique(
        [item for item in steps if isinstance(item, str)
         and item.casefold() == supported
         and item.casefold() in contact_text.casefold()],
        MAX_NEXT_STEPS,
    )


def _empty(reason):
    return {
        "should_create": False,
        "to": [],
        "cc": [],
        "subject": "",
        "body": "",
        "source": SOURCE,
        "requires_approval": True,
        "sent": False,
        "approval_status": "not_created",
        "reason_code": reason,
        "approval_decision_ids": [],
    }


def build_email_draft(mission_id, mission_state, decision,
                      notification_decision, call_result):
    """Build a safe draft from persisted evidence, never send or infer content."""
    state = mission_state if isinstance(mission_state, dict) else {}
    engine = decision if isinstance(decision, dict) else {}
    notification = notification_decision if isinstance(notification_decision, dict) else {}
    result = call_result if isinstance(call_result, dict) else {}
    if (
        not isinstance(mission_id, str)
        or state.get("mission_id") != mission_id
        or result.get("mission_id") != mission_id
    ):
        return _empty("mission_mismatch")
    if notification.get("should_notify") is not True:
        return _empty("no_communication_reason")
    if engine.get("relevance") != "actionable" or engine.get("status") in {"failed", "no_action"}:
        return _empty("decision_not_actionable")

    contact_text = _contact_text(result.get("transcript"))
    recipient = _verified_recipient(result, engine, contact_text)
    if recipient is None:
        return _empty("no_verified_email")

    facts = _grounded_facts(state, notification, engine, contact_text)
    requests = _grounded_requests(notification, engine, contact_text)
    next_steps = _grounded_next_steps(notification, engine, contact_text)
    if not facts and not requests and not next_steps:
        return _empty("no_supported_content")

    decisions = state.get("decisions", [])
    approval_ids = [
        item.get("decision_id") for item in decisions
        if isinstance(item, dict)
        and item.get("mission_id") == mission_id
        and item.get("status") in {"pending", "approved", "rejected", "deferred"}
        and isinstance(item.get("decision_id"), str)
        and isinstance(item.get("description"), str)
        and re.search(r"\b(?:email|e-mail|correo)\b", item["description"], re.IGNORECASE)
    ] if isinstance(decisions, list) else []

    sections = [
        "Hola,",
        "Gracias por conversar con nosotros.",
    ]
    if facts:
        sections.append("Información relevante:\n" + "\n".join(f"- {item}" for item in facts))
    if requests:
        sections.append("Quedó registrada tu solicitud:\n" + "\n".join(f"- {item}" for item in requests))
    if next_steps:
        sections.append("Próximo paso registrado:\n" + "\n".join(f"- {item}" for item in next_steps))
    sections.extend(["Quedo atento a tus comentarios.", "Saludos,\nGuzi Stuff"])
    body = "\n\n".join(sections)
    body = redact_configured_secrets(body)
    body = PRIVATE_VALUE.sub("[dato omitido]", body)
    body = SECRET_NAME.sub("[dato omitido]", body)
    return {
        "should_create": True,
        "to": [recipient],
        "cc": [],
        "subject": "Seguimiento comercial",
        "body": body,
        "source": SOURCE,
        "requires_approval": True,
        "sent": False,
        "approval_status": "pending",
        "approval_decision_ids": approval_ids,
        "reason_code": "supported_commercial_communication",
    }


def update_draft_approval_status(email_draft, mission_state):
    """Reflect a resolved email-related mission decision without executing it."""
    draft = deepcopy(email_draft) if isinstance(email_draft, dict) else _empty("not_available")
    state = mission_state if isinstance(mission_state, dict) else {}
    linked_ids = draft.get("approval_decision_ids", [])
    decisions = state.get("decisions", [])
    if not isinstance(linked_ids, list) or not isinstance(decisions, list):
        return draft
    linked = [
        item for item in decisions
        if isinstance(item, dict) and item.get("decision_id") in linked_ids
    ]
    if not linked:
        return draft
    statuses = {item.get("status") for item in linked}
    if "pending" in statuses:
        approval = "pending"
    elif "rejected" in statuses:
        approval = "rejected"
    elif "deferred" in statuses:
        approval = "deferred"
    elif statuses == {"approved"}:
        approval = "approved_for_future_send"
    else:
        approval = "pending"
    draft["approval_status"] = approval
    if approval == "approved_for_future_send":
        draft["approved_content_sha256"] = email_draft_content_digest(draft)
    # This value is invariant, including after approval.
    draft["sent"] = False
    draft["requires_approval"] = True
    return draft


def email_draft_content_digest(draft):
    """Return a stable digest for the approved delivery fields only."""
    if not isinstance(draft, dict):
        return None
    fields = {key: draft.get(key) for key in ("to", "cc", "subject", "body")}
    try:
        canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
