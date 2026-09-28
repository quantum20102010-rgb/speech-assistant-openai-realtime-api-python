"""NO SEND WhatsApp adapter preparation and recipient validation.

This module is intentionally offline: it has no provider client, networking,
message-delivery, logging, or action-execution code. A ready result is only a
dry-run preview for a future integration.
"""

import os
import re


_E164_RECIPIENT = re.compile(r"^\+[1-9][0-9]{7,14}$")
_VALID_STATUSES = {
    "disabled",
    "not_configured",
    "invalid_configuration",
    "ready_for_send",
    "blocked",
}


def _base_result(status, reason_code):
    return {
        "schema_version": 1,
        "status": status if status in _VALID_STATUSES else "blocked",
        "reason_code": reason_code,
        "sent": False,
        "dry_run": True,
        "recipient_configured": False,
    }


def _enabled_setting():
    raw = os.getenv("WHATSAPP_ENABLED")
    if raw is None or raw == "false":
        return False, None
    if raw == "true":
        return True, None
    return False, "invalid_whatsapp_enabled"


def _recipient_setting():
    raw = os.getenv("WHATSAPP_RECIPIENT")
    if raw is None or not raw.strip():
        return None, "not_configured"
    recipient = raw.strip()
    if not _E164_RECIPIENT.fullmatch(recipient):
        return None, "invalid_recipient_format"
    return recipient, None


def recipient_for_ready_message(notification_message):
    """Return the configured recipient only when the existing adapter accepts the message."""
    if prepare_whatsapp(notification_message).get("status") != "ready_for_send":
        return None
    recipient, error = _recipient_setting()
    return recipient if error is None else None


def prepare_whatsapp(notification_message):
    """Classify a future send safely; never send or log any recipient data."""
    enabled, configuration_error = _enabled_setting()
    if configuration_error:
        return _base_result("invalid_configuration", configuration_error)
    if not enabled:
        return _base_result("disabled", "whatsapp_disabled")

    recipient, recipient_error = _recipient_setting()
    if recipient_error == "not_configured":
        return _base_result("not_configured", "recipient_missing")
    if recipient_error:
        return _base_result("invalid_configuration", recipient_error)

    base = _base_result("blocked", "notification_not_sendable")
    base["recipient_configured"] = True
    if not isinstance(notification_message, dict):
        return base
    if notification_message.get("should_send") is not True:
        return base
    message = notification_message.get("message")
    if not isinstance(message, str) or not message.strip():
        base["reason_code"] = "message_empty"
        return base
    priority = notification_message.get("priority")
    if not isinstance(priority, str) or priority not in {"high", "normal", "low"}:
        base["reason_code"] = "priority_none_or_invalid"
        return base
    if notification_message.get("channel") != "whatsapp":
        base["reason_code"] = "channel_mismatch"
        return base

    base["status"] = "ready_for_send"
    base["reason_code"] = "dry_run_ready"
    message_type = notification_message.get("message_type")
    base["message_type"] = (
        message_type
        if message_type in {"business_update", "action_required"}
        else "business_update"
    )
    return base
