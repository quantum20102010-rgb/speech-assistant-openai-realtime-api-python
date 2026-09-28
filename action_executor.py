"""Central local policy gate and registry for future actions.

This module records and authorizes action metadata only. It has no provider,
network, credential, or execution capability; approved actions remain unexecuted.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re

from call_results import redact_configured_secrets


ACTION_TYPES = {
    "store_information",
    "analyze_information",
    "create_draft",
    "record_task",
    "update_mission_state",
    "prepare_action",
    "send_email",
    "send_whatsapp",
    "initiate_call",
    "external_follow_up",
    "review_catalog",
    "schedule_human_contact",
    "accept_price_or_condition",
    "accept_contract",
    "make_payment",
    "activate_service",
}
AUTOMATIC_TYPES = {
    "store_information", "analyze_information", "create_draft",
    "record_task", "update_mission_state", "prepare_action",
}
APPROVAL_TYPES = {
    "send_email", "send_whatsapp", "initiate_call", "external_follow_up",
    "review_catalog", "schedule_human_contact",
}
FORBIDDEN_TYPES = {
    "accept_price_or_condition", "accept_contract", "make_payment",
    "activate_service",
}
ACTION_STATUSES = {
    "prepared", "awaiting_approval", "approved", "rejected", "deferred",
    "blocked_by_policy", "executed", "failed",
}
_MISSION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EMAIL = re.compile(r"\b[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
_PRIVATE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|AC[0-9a-fA-F]{32}|"
    r"[A-Fa-f0-9]{32}|[A-Za-z0-9_-]{40,}|"
    r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)


class ActionPolicyError(ValueError):
    """A requested action transition violates mission or policy constraints."""


def _safe_text(value):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip()
    if not value or len(value) > 500:
        return None
    value = redact_configured_secrets(value)
    value = _PRIVATE.sub("[dato omitido]", value)
    value = _EMAIL.sub("[correo omitido]", value)
    value = _PHONE.sub("[teléfono omitido]", value)
    return value[:500]


def _timestamp(now=None):
    instant = now or datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ActionPolicyError("timestamp_invalid")
    return instant.astimezone(timezone.utc).isoformat()


def _formal_resolution_status(decision):
    if not isinstance(decision, dict) or decision.get("resolved_by") != "Fabian":
        return None
    status = decision.get("status")
    if not isinstance(status, str) or status not in {"approved", "rejected", "deferred"}:
        return None
    resolved_at = decision.get("resolved_at")
    try:
        instant = datetime.fromisoformat(resolved_at) if isinstance(resolved_at, str) else None
    except ValueError:
        return None
    return status if instant is not None and instant.tzinfo is not None else None


def classify_action_type(description):
    """Conservatively classify action language into the central policy types."""
    text = description.casefold() if isinstance(description, str) else ""
    if any(word in text for word in ("contrato", "contract", "terms acceptance")) and any(
        word in text for word in ("acept", "accept", "aprobar", "approve", "firm", "sign")
    ):
        return "accept_contract"
    if any(word in text for word in ("pago", "pagar", "payment", "pay", "comprar", "purchase")):
        return "make_payment"
    if any(word in text for word in ("activar servicio", "activate service", "subscription activation")):
        return "activate_service"
    if any(word in text for word in (
        "precio", "price", "condiciones", "conditions", "cotizacion", "cotización", "quote"
    )) and any(word in text for word in (
        "acept", "accept", "aprobar", "approve", "agree", "comprometer", "commit"
    )):
        return "accept_price_or_condition"
    if any(word in text for word in ("whatsapp", "wa.me")):
        return "send_whatsapp"
    if any(word in text for word in ("correo", "email", "e-mail", "send mail")):
        return "send_email"
    if any(word in text for word in ("llamada", "llamar", "call the", "initiate call")):
        return "initiate_call"
    if any(word in text for word in ("catálogo", "catalog", "catalogue")):
        return "review_catalog"
    if any(word in text for word in ("reunión", "reunion", "meeting", "programar contacto", "schedule contact")):
        return "schedule_human_contact"
    if any(word in text for word in ("seguimiento", "follow-up", "follow up", "contactar", "contact the")):
        return "external_follow_up"
    return "external_follow_up"


def create_action(mission_id, action_type, description, now=None,
                  approval_decision_id=None):
    """Create a local action record; no transition to executed is possible."""
    if not isinstance(mission_id, str) or not _MISSION_ID.fullmatch(mission_id):
        raise ActionPolicyError("mission_not_found")
    clean_description = _safe_text(description)
    if not clean_description:
        raise ActionPolicyError("description_invalid")
    if not isinstance(action_type, str) or action_type not in ACTION_TYPES:
        action_type = "unknown"

    created_at = _timestamp(now)
    digest = hashlib.sha256(
        f"{mission_id}\0{action_type}\0{clean_description.casefold()}".encode("utf-8")
    ).hexdigest()[:24]
    action_id = f"act_{digest}"
    if action_type in FORBIDDEN_TYPES or action_type == "unknown":
        status = "blocked_by_policy"
        approval_status = "blocked"
        reason = "policy_forbids_automatic_commitment"
        requires_approval = True
        approval_decision_id = None
    elif action_type in AUTOMATIC_TYPES:
        status = "prepared"
        approval_status = "not_required"
        reason = "local_action_permitted"
        requires_approval = False
        approval_decision_id = None
    elif action_type in APPROVAL_TYPES:
        status = "awaiting_approval"
        requires_approval = True
        approval_status = "pending" if isinstance(approval_decision_id, str) else "unlinked"
        reason = "explicit_mission_decision_required"
    else:
        status = "blocked_by_policy"
        approval_status = "blocked"
        reason = "unknown_action_type"
        requires_approval = True
        approval_decision_id = None

    return {
        "action_id": action_id,
        "mission_id": mission_id,
        "action_type": action_type,
        "description": clean_description,
        "status": status,
        "requires_approval": requires_approval,
        "approval_status": approval_status,
        "approval_decision_id": approval_decision_id,
        "created_at": created_at,
        "approved_at": None,
        "executed_at": None,
        "reason_code": reason,
    }


def prepare_action(mission_state, action_type, description, now=None,
                   approval_decision_id=None):
    """Idempotently register one policy-classified action on Mission State."""
    state = deepcopy(mission_state) if isinstance(mission_state, dict) else None
    if not isinstance(state, dict) or not isinstance(state.get("mission_id"), str):
        raise ActionPolicyError("mission_not_found")
    action = create_action(
        state["mission_id"], action_type, description, now,
        approval_decision_id,
    )
    existing = state.get("actions")
    valid_records = [
        item for item in existing
        if isinstance(item, dict) and item.get("mission_id") == state["mission_id"]
        and isinstance(item.get("action_id"), str)
    ] if isinstance(existing, list) else []
    by_id = {}
    for item in valid_records:
        by_id.setdefault(item["action_id"], item)
    records = list(by_id.values())
    current = next((item for item in records if item["action_id"] == action["action_id"]), None)
    if current is None:
        records.append(action)
    else:
        # Preserve the first record and its timestamps, maintaining idempotence.
        action = current
    state["actions"] = records
    return state, deepcopy(action)


def sync_action_approvals(mission_state, now=None):
    """Mirror formal Mission Decision outcomes onto linked action metadata."""
    state = deepcopy(mission_state) if isinstance(mission_state, dict) else None
    if not isinstance(state, dict):
        return state
    mission_id = state.get("mission_id")
    decisions = state.get("decisions", [])
    decision_by_id = {
        item.get("decision_id"): item for item in decisions
        if isinstance(item, dict) and item.get("mission_id") == mission_id
        and isinstance(item.get("decision_id"), str)
    } if isinstance(decisions, list) else {}
    instant = _timestamp(now)
    actions = state.get("actions")
    if not isinstance(actions, list):
        return state
    for action in actions:
        if not isinstance(action, dict) or action.get("mission_id") != mission_id:
            continue
        action_type = action.get("action_type")
        description = action.get("description")
        expected_id = None
        if isinstance(action_type, str) and action_type in ACTION_TYPES and isinstance(description, str):
            try:
                expected_id = create_action(
                    mission_id, action_type, description, now,
                    action.get("approval_decision_id"),
                )["action_id"]
            except ActionPolicyError:
                expected_id = None
        if action.get("executed_at") is not None or action.get("status") == "executed":
            action.update({
                "status": "blocked_by_policy",
                "approval_status": "blocked",
                "reason_code": "execution_not_available",
                "executed_at": None,
            })
            continue
        if expected_id is None or action.get("action_id") != expected_id:
            action.update({
                "status": "blocked_by_policy", "approval_status": "blocked",
                "reason_code": "invalid_action_record", "approved_at": None,
                "executed_at": None,
            })
            continue
        if action_type in FORBIDDEN_TYPES or action_type == "unknown":
            action.update({
                "status": "blocked_by_policy", "approval_status": "blocked",
                "reason_code": "policy_forbids_automatic_commitment",
                "approval_decision_id": None, "approved_at": None,
                "executed_at": None,
            })
            continue
        if action_type in AUTOMATIC_TYPES:
            action.update({
                "status": "prepared", "requires_approval": False,
                "approval_status": "not_required", "approval_decision_id": None,
                "approved_at": None, "executed_at": None,
            })
            continue
        if action_type not in APPROVAL_TYPES:
            action.update({
                "status": "blocked_by_policy", "approval_status": "blocked",
                "reason_code": "unknown_action_type", "approved_at": None,
                "executed_at": None,
            })
            continue
        action["requires_approval"] = True
        decision_id = action.get("approval_decision_id")
        decision = decision_by_id.get(decision_id) if isinstance(decision_id, str) else None
        if (
            decision is not None
            and (
                not isinstance(decision.get("description"), str)
                or classify_action_type(decision["description"]) != action_type
            )
        ):
            decision = None
        status = _formal_resolution_status(decision)
        if status is None:
            action.update({
                "status": "awaiting_approval",
                "approval_status": "pending" if decision else "unlinked",
                "approved_at": None,
                "executed_at": None,
            })
        elif status == "approved":
            action.update({
                "status": "approved",
                "approval_status": "approved",
                "approved_at": decision.get("resolved_at") or instant,
                "executed_at": None,
            })
        else:
            action.update({
                "status": status,
                "approval_status": status,
                "reason_code": f"mission_decision_{status}",
                "approved_at": None,
                "executed_at": None,
            })
    state["actions"] = actions
    return state


def resolve_action_from_mission_decision(actions, mission_id, action_id,
                                         decision_id, mission_decisions,
                                         now=None):
    """Validate a formal decision and return the mirrored action state.

    This only records authorization metadata. It never executes an action.
    """
    if not isinstance(mission_id, str) or not _MISSION_ID.fullmatch(mission_id):
        raise ActionPolicyError("mission_not_found")
    state = {"mission_id": mission_id, "actions": deepcopy(actions or []),
             "decisions": deepcopy(mission_decisions or [])}
    action = next((item for item in state["actions"] if isinstance(item, dict)
                   and item.get("action_id") == action_id), None)
    if action is None:
        raise ActionPolicyError("action_not_found")
    if action.get("mission_id") != mission_id:
        raise ActionPolicyError("mission_mismatch")
    if action.get("status") == "blocked_by_policy":
        raise ActionPolicyError("blocked_by_policy")
    if action.get("action_type") not in APPROVAL_TYPES or not action.get("requires_approval"):
        raise ActionPolicyError("action_not_approvable")
    if action.get("status") != "awaiting_approval":
        raise ActionPolicyError("action_already_resolved")
    if action.get("approval_decision_id") != decision_id:
        raise ActionPolicyError("decision_mismatch")
    decision = next((item for item in state["decisions"] if isinstance(item, dict)
                     and item.get("decision_id") == decision_id), None)
    if decision is None:
        raise ActionPolicyError("decision_not_found")
    if decision.get("mission_id") != mission_id:
        raise ActionPolicyError("mission_mismatch")
    if (
        not isinstance(decision.get("description"), str)
        or classify_action_type(decision["description"]) != action.get("action_type")
    ):
        raise ActionPolicyError("decision_mismatch")
    formal_status = _formal_resolution_status(decision)
    if formal_status is None:
        raise ActionPolicyError("approval_not_resolved")
    state = sync_action_approvals(state, now)
    return next(item for item in state["actions"] if item.get("action_id") == action_id)


def prepare_call_actions(mission_state, decision, email_draft=None,
                         whatsapp_preview=None, now=None, notification_message=None):
    """Register permitted local work and future external intents for one result."""
    if not isinstance(mission_state, dict):
        return mission_state
    mission_id = mission_state.get("mission_id")
    engine = decision if isinstance(decision, dict) else {}

    def linked_decision(action_type):
        decisions = mission_state.get("decisions", [])
        if not isinstance(decisions, list):
            return None
        return next((
            item.get("decision_id") for item in decisions
            if isinstance(item, dict)
            and item.get("mission_id") == mission_id
            and item.get("status") == "pending"
            and isinstance(item.get("decision_id"), str)
            and isinstance(item.get("description"), str)
            and classify_action_type(item["description"]) == action_type
        ), None)

    candidates = [
        ("store_information", "Registrar la información respaldada de la llamada", None),
        ("analyze_information", "Analizar la información comercial respaldada", None),
        ("update_mission_state", "Actualizar Mission State", None),
    ]
    if isinstance(email_draft, dict) and email_draft.get("should_create") is True:
        linked = email_draft.get("approval_decision_ids", [])
        decision_id = linked[0] if isinstance(linked, list) and linked else linked_decision("send_email")
        candidates.extend([
            ("create_draft", "Preparar borrador de email para revisión", None),
            ("send_email", "Enviar email comercial aprobado", decision_id),
        ])
    message_requests_whatsapp = (
        isinstance(notification_message, dict)
        and notification_message.get("should_send") is True
        and notification_message.get("channel") == "whatsapp"
    )
    adapter_ready = (
        isinstance(whatsapp_preview, dict)
        and whatsapp_preview.get("status") == "ready_for_send"
    )
    if adapter_ready and (message_requests_whatsapp or notification_message is None):
        candidates.append((
            "send_whatsapp", "Enviar mensaje WhatsApp con aprobación",
            linked_decision("send_whatsapp"),
        ))

    for description in engine.get("approval_required", []) if isinstance(engine.get("approval_required"), list) else []:
        if not isinstance(description, str):
            continue
        action_type = classify_action_type(description)
        linked_decision = next((
            item.get("decision_id") for item in mission_state.get("decisions", [])
            if isinstance(item, dict)
            and item.get("mission_id") == mission_id
            and isinstance(item.get("decision_id"), str)
            and isinstance(item.get("description"), str)
            and item["description"].casefold() == (_safe_text(description) or "").casefold()
        ), None) if isinstance(mission_state.get("decisions"), list) else None
        candidates.append((action_type, description, linked_decision))

    state = deepcopy(mission_state)
    state.setdefault("actions", [])
    unique_candidates = []
    seen_candidates = set()
    for action_type, description, decision_id in candidates:
        identity = (action_type, decision_id)
        if identity in seen_candidates:
            continue
        seen_candidates.add(identity)
        unique_candidates.append((action_type, description, decision_id))
    for action_type, description, decision_id in unique_candidates:
        state, _ = prepare_action(
            state, action_type, description, now, decision_id
        )
    return sync_action_approvals(state, now)
