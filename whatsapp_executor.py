"""Local, approval-gated WhatsApp dry-run executor.

There is intentionally no messaging provider, HTTP client, socket, Twilio, Meta,
or credential integration in this module. A successful result is never sent.
"""

import re
from datetime import datetime, timezone

from action_executor import sync_action_approvals
from call_results import redact_configured_secrets
from notification_message import build_notification_message
from whatsapp_adapter import recipient_for_ready_message


_PRIVATE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|AC[0-9a-fA-F]{32}|"
    r"[A-Fa-f0-9]{32}|[A-Za-z0-9_-]{40,}|"
    r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_SECRET_NAME = re.compile(
    r"\b(?:CALL_SECRET|OPENAI_API_KEY|TWILIO_[A-Z_]+|WHATSAPP_[A-Z_]+|"
    r"RAILWAY_[A-Z_]+|EMAIL_[A-Z_]+)\b", re.IGNORECASE,
)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")


def mask_whatsapp_recipient(value):
    """Mask a phone number for reports and API responses."""
    if not isinstance(value, str) or len(value) < 5:
        return "[oculto]" if value else ""
    return value[:2] + "•" * max(0, len(value) - 6) + value[-4:]


def _safe_message(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    value = redact_configured_secrets(value)
    value = _PRIVATE.sub("[dato omitido]", value)
    value = _SECRET_NAME.sub("[dato omitido]", value)
    value = _PHONE.sub("[dato omitido]", value)
    return value[:1200].strip()


def _blocked(mission_id, action_id, reason):
    return {
        "mission_id": mission_id,
        "action_id": action_id,
        "status": "blocked",
        "provider": "none",
        "recipient": "",
        "message": "",
        "sent": False,
        "dry_run": False,
        "executed_at": None,
        "processed_at": None,
        "reason_code": reason,
        "notification_reason_code": None,
        "message_type": None,
    }


def execute_whatsapp_action(mission_id, action_id, mission_state, call_result,
                            *, existing_results=None, now=None):
    """Validate persisted action approval and notification artifacts; never send.

    There is no `approved` argument. Formal approval must already be present
    in the persisted Mission Decision and mirrored Action record.
    """
    if not isinstance(mission_id, str) or not isinstance(action_id, str):
        return _blocked(mission_id, action_id, "invalid_request")
    if isinstance(existing_results, dict) and action_id in existing_results:
        result = _blocked(mission_id, action_id, "already_processed")
        result["status"] = "already_processed"
        return result
    state = mission_state if isinstance(mission_state, dict) else {}
    persisted = call_result if isinstance(call_result, dict) else {}
    if state.get("mission_id") != mission_id or persisted.get("mission_id") != mission_id:
        return _blocked(mission_id, action_id, "mission_mismatch")
    if not isinstance(persisted.get("whatsapp_preview"), dict):
        return _blocked(mission_id, action_id, "adapter_preview_missing")

    actions = state.get("actions")
    action = next((item for item in actions if isinstance(item, dict)
                   and item.get("action_id") == action_id), None) if isinstance(actions, list) else None
    if action is None:
        return _blocked(mission_id, action_id, "action_not_found")
    if action.get("mission_id") != mission_id:
        return _blocked(mission_id, action_id, "mission_mismatch")
    if action.get("action_type") != "send_whatsapp":
        return _blocked(mission_id, action_id, "wrong_action_type")
    if action.get("status") == "blocked_by_policy":
        return _blocked(mission_id, action_id, "blocked_by_policy")
    if action.get("status") != "approved" or action.get("approval_status") != "approved":
        return _blocked(mission_id, action_id, "formal_approval_required")

    try:
        synchronized = sync_action_approvals(state, now)
    except (TypeError, ValueError):
        return _blocked(mission_id, action_id, "invalid_action_record")
    approved_action = next((item for item in synchronized.get("actions", [])
                            if isinstance(item, dict) and item.get("action_id") == action_id), None)
    if approved_action is None:
        return _blocked(mission_id, action_id, "action_not_found")
    if approved_action.get("status") != "approved" or approved_action.get("approval_status") != "approved":
        return _blocked(mission_id, action_id, "formal_approval_required")
    if approved_action.get("requires_approval") is not True:
        return _blocked(mission_id, action_id, "formal_approval_required")

    notification = persisted.get("notification_decision")
    message_artifact = persisted.get("notification_message")
    if not isinstance(notification, dict):
        return _blocked(mission_id, action_id, "notification_decision_missing")
    if notification.get("should_notify") is not True:
        return _blocked(mission_id, action_id, "notification_not_approved_for_send")
    if not isinstance(message_artifact, dict):
        return _blocked(mission_id, action_id, "notification_message_missing")
    if (message_artifact.get("should_send") is not True
            or message_artifact.get("channel") != "whatsapp"):
        return _blocked(mission_id, action_id, "message_not_sendable")
    if message_artifact.get("priority") not in {"high", "normal", "low"}:
        return _blocked(mission_id, action_id, "message_priority_invalid")
    if not isinstance(message_artifact.get("message"), str) or not message_artifact["message"].strip():
        return _blocked(mission_id, action_id, "message_empty")
    expected_message = build_notification_message(notification, persisted)
    if expected_message != message_artifact:
        return _blocked(mission_id, action_id, "notification_message_mismatch")
    message = _safe_message(message_artifact.get("message"))
    if not message:
        return _blocked(mission_id, action_id, "message_empty")

    recipient = recipient_for_ready_message(message_artifact)
    if recipient is None:
        return _blocked(mission_id, action_id, "recipient_or_adapter_not_ready")
    instant = now or datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        return _blocked(mission_id, action_id, "timestamp_invalid")
    reason_code = notification.get("reason_code")
    if reason_code not in {
        "actionable_business_information", "no_relevant_information",
        "no_contact_transcript", "wrong_number_or_contact", "no_new_information",
    }:
        reason_code = None
    message_type = message_artifact.get("message_type")
    if message_type not in {"business_update", "action_required"}:
        message_type = None
    return {
        "mission_id": mission_id,
        "action_id": action_id,
        "status": "dry_run",
        "provider": "none",
        "recipient": recipient,
        "message": message,
        "sent": False,
        "dry_run": True,
        "executed_at": None,
        "processed_at": instant.astimezone(timezone.utc).isoformat(),
        "reason_code": "dry_run_only",
        "notification_reason_code": reason_code,
        "message_type": message_type,
    }
