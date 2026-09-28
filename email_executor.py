"""Approval-gated email executor with dry-run and simulation-only provider use."""

import os
import re
from datetime import datetime, timezone

from action_executor import classify_action_type, create_action
from call_results import redact_configured_secrets
from email_draft import EMAIL_PATTERN, _verified_recipient, email_draft_content_digest


_PRIVATE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|AC[0-9a-fA-F]{32}|"
    r"[A-Fa-f0-9]{32}|[A-Za-z0-9_-]{40,}|"
    r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_SECRET_NAME = re.compile(
    r"\b(?:CALL_SECRET|OPENAI_API_KEY|TWILIO_[A-Z_]+|EMAIL_[A-Z_]+)\b",
    re.IGNORECASE,
)
_EMAIL_IN_TEXT = re.compile(r"\b[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+\b")
_PHONE_IN_TEXT = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")


def _safe_text(value, limit):
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split()).strip()[:limit]
    value = redact_configured_secrets(value)
    value = _PRIVATE.sub("[dato omitido]", value)
    value = _EMAIL_IN_TEXT.sub("[dato omitido]", value)
    value = _PHONE_IN_TEXT.sub("[dato omitido]", value)
    return _SECRET_NAME.sub("[dato omitido]", value)


def _result(mission_id, action_id, status, reason, *, draft=None, processed_at=None,
            provider="none", provider_message_id=None, sent=False, dry_run=None,
            executed_at=None):
    draft = draft if isinstance(draft, dict) else {}
    recipients = draft.get("to") if isinstance(draft.get("to"), list) else []
    cc = draft.get("cc") if isinstance(draft.get("cc"), list) else []
    value = {
        "mission_id": mission_id,
        "action_id": action_id,
        "status": status,
        "reason_code": reason,
        "provider": provider,
        "to": recipients,
        "cc": cc,
        "subject": _safe_text(draft.get("subject"), 200),
        "body": _safe_text(draft.get("body"), 3000),
        "requires_approval": True,
        "sent": sent,
        "dry_run": status == "dry_run" if dry_run is None else dry_run,
        "executed_at": executed_at,
        "processed_at": processed_at,
    }
    if provider_message_id is not None:
        value["provider_message_id"] = provider_message_id
    return value


def execute_email_action(mission_id, action_id, mission_state, email_draft,
                         call_result, *, existing_results=None, dry_run_setting=None,
                         now=None, provider=None):
    """Validate persisted approval and process one controlled email action.

    `existing_results` is the persisted action-id keyed result map used for
    idempotency. A boolean approval input is intentionally not accepted.
    """
    if not isinstance(mission_id, str) or not isinstance(action_id, str):
        return _result(mission_id, action_id, "blocked", "invalid_request")
    if isinstance(existing_results, dict) and action_id in existing_results:
        return _result(mission_id, action_id, "already_processed", "already_processed")
    state = mission_state if isinstance(mission_state, dict) else {}
    result = call_result if isinstance(call_result, dict) else {}
    if state.get("mission_id") != mission_id or result.get("mission_id") != mission_id:
        return _result(mission_id, action_id, "blocked", "mission_mismatch")
    actions = state.get("actions")
    action = next((item for item in actions if isinstance(item, dict)
                   and item.get("action_id") == action_id), None) if isinstance(actions, list) else None
    if action is None:
        return _result(mission_id, action_id, "blocked", "action_not_found")
    if action.get("mission_id") != mission_id:
        return _result(mission_id, action_id, "blocked", "mission_mismatch")
    if action.get("action_type") != "send_email":
        return _result(mission_id, action_id, "blocked", "wrong_action_type")
    try:
        registered_id = create_action(
            mission_id, "send_email", action.get("description"),
            approval_decision_id=action.get("approval_decision_id"),
        )["action_id"]
    except (TypeError, ValueError):
        registered_id = None
    if registered_id != action_id:
        return _result(mission_id, action_id, "blocked", "invalid_action_record")
    if action.get("status") != "approved" or action.get("requires_approval") is not True:
        return _result(mission_id, action_id, "blocked", "formal_approval_required")
    if action.get("approval_status") != "approved":
        return _result(mission_id, action_id, "blocked", "formal_approval_required")
    decision_id = action.get("approval_decision_id")
    decisions = state.get("decisions")
    decision = next((item for item in decisions if isinstance(item, dict)
                     and item.get("decision_id") == decision_id), None) if isinstance(decisions, list) else None
    if (not isinstance(decision, dict) or decision.get("mission_id") != mission_id
            or decision.get("status") != "approved"
            or decision.get("resolved_by") != "Fabian"
            or classify_action_type(decision.get("description")) != "send_email"):
        return _result(mission_id, action_id, "blocked", "formal_approval_required")
    try:
        resolved_at = datetime.fromisoformat(decision.get("resolved_at"))
    except (TypeError, ValueError):
        resolved_at = None
    if resolved_at is None or resolved_at.tzinfo is None:
        return _result(mission_id, action_id, "blocked", "formal_approval_required")
    if email_draft is None:
        return _result(mission_id, action_id, "blocked", "draft_unavailable")
    if (result.get("mission_id") != mission_id
            or not isinstance(email_draft, dict)
            or email_draft.get("mission_id", mission_id) != mission_id
            or result.get("email_draft") != email_draft):
        return _result(mission_id, action_id, "blocked", "draft_mismatch")
    linked_decisions = email_draft.get("approval_decision_ids")
    if not isinstance(linked_decisions, list) or decision_id not in linked_decisions:
        return _result(mission_id, action_id, "blocked", "draft_approval_mismatch")
    if email_draft.get("should_create") is not True:
        return _result(mission_id, action_id, "blocked", "draft_unavailable")
    if email_draft.get("requires_approval") is not True or email_draft.get("sent") is not False:
        return _result(mission_id, action_id, "blocked", "draft_policy_invalid")
    if email_draft.get("approval_status") != "approved_for_future_send":
        return _result(mission_id, action_id, "blocked", "draft_not_approved")
    if action_id in (result.get("email_execution_results") or {}):
        return _result(mission_id, action_id, "already_processed", "already_processed")
    if dry_run_setting is None:
        dry_run_setting = os.getenv("EMAIL_DRY_RUN")
    if not isinstance(dry_run_setting, str) or dry_run_setting.strip().casefold() not in {"true", "false"}:
        return _result(mission_id, action_id, "blocked", "email_configuration_invalid")
    is_dry_run = dry_run_setting.strip().casefold() == "true"

    email_enabled = os.getenv("EMAIL_ENABLED", "false").strip().casefold()
    if email_enabled not in {"true", "false"}:
        return _result(mission_id, action_id, "blocked", "email_configuration_invalid")
    if not is_dry_run and email_enabled != "true":
        return _result(mission_id, action_id, "blocked", "email_disabled")
    if not is_dry_run and email_draft.get("approved_content_sha256") != email_draft_content_digest(email_draft):
        return _result(mission_id, action_id, "blocked", "email_draft_mismatch")

    to = email_draft.get("to")
    cc = email_draft.get("cc", [])
    recipient = to[0] if isinstance(to, list) and len(to) == 1 else None
    transcript = result.get("transcript")
    contact_text = "\n".join(
        line[len("Contacto:"):].strip()
        for line in transcript.splitlines() if line.startswith("Contacto:")
    ) if isinstance(transcript, str) else ""
    verified = _verified_recipient(result, result.get("decision"), contact_text)
    if (not isinstance(recipient, str) or not EMAIL_PATTERN.fullmatch(recipient)
            or recipient.casefold() != (verified or "").casefold()):
        return _result(mission_id, action_id, "blocked", "recipient_not_verified")
    if (not isinstance(cc, list)
            or any(not isinstance(address, str) or not EMAIL_PATTERN.fullmatch(address) for address in cc)
            or not isinstance(email_draft.get("subject"), str)
            or not email_draft["subject"].strip()
            or not isinstance(email_draft.get("body"), str)
            or not email_draft["body"].strip()):
        return _result(mission_id, action_id, "blocked", "draft_content_invalid")
    instant = now or datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        return _result(mission_id, action_id, "blocked", "timestamp_invalid")
    preview = dict(email_draft)
    preview["to"] = [recipient]
    preview["cc"] = cc
    processed_at = instant.astimezone(timezone.utc).isoformat()
    if is_dry_run and email_enabled != "true":
        return _result(mission_id, action_id, "dry_run", "dry_run_only",
                       draft=preview, processed_at=processed_at)

    configured_provider = os.getenv("EMAIL_PROVIDER", "").strip().casefold()
    if is_dry_run and configured_provider and configured_provider not in {"fake", "resend"}:
        return _result(mission_id, action_id, "blocked", "email_provider_unavailable",
                       draft=preview)
    if not is_dry_run and configured_provider != "resend":
        return _result(mission_id, action_id, "blocked", "email_provider_unavailable", draft=preview)
    configured_key = os.getenv("RESEND_API_KEY", "").strip()
    configured_from = os.getenv("RESEND_FROM", "").strip()
    if not is_dry_run and configured_provider == "resend":
        if not configured_key:
            return _result(mission_id, action_id, "blocked", "provider_not_configured", draft=preview)
        if not configured_from or len(configured_from) > 254 or not EMAIL_PATTERN.fullmatch(configured_from):
            return _result(mission_id, action_id, "blocked", "from_address_invalid", draft=preview)
    if provider is None:
        reason = (
            "email_provider_unavailable"
            if configured_provider
            else "email_provider_not_configured"
        )
        return _result(mission_id, action_id, "blocked", reason, draft=preview)
    provider_name = getattr(provider, "provider_name", None)
    provider_is_valid = (
        callable(getattr(provider, "send", None))
        and (provider_name == configured_provider or (
            not is_dry_run and configured_provider == "resend"
            and provider_name == "fake" and getattr(provider, "simulation_only", False) is True
        ))
        and (
            (provider_name == "fake" and getattr(provider, "simulation_only", False) is True)
            or (provider_name == "resend" and getattr(provider, "simulation_only", None) is False)
        )
    )
    if not provider_is_valid:
        return _result(mission_id, action_id, "blocked", "email_provider_invalid",
                       draft=preview)

    if not is_dry_run and provider_name == "resend":
        if (getattr(provider, "_api_key", None) != configured_key
                or getattr(provider, "_from_address", None) != configured_from):
            return _result(mission_id, action_id, "blocked", "email_provider_invalid", draft=preview)

    try:
        provider_result = provider.send(
            to=[recipient],
            cc=cc,
            subject=email_draft["subject"],
            body=email_draft["body"],
        )
    except Exception:
        return _result(mission_id, action_id, "blocked", "email_provider_failed",
                       draft=preview, provider=provider_name, processed_at=processed_at,
                       dry_run=is_dry_run)
    if not isinstance(provider_result, dict) or provider_result.get("provider") != provider_name:
        return _result(mission_id, action_id, "blocked", "email_provider_failed",
                       draft=preview, provider=provider_name, processed_at=processed_at,
                       dry_run=is_dry_run)
    if provider_name == "resend" and provider_result.get("reason_code") == "dry_run_only":
        return _result(mission_id, action_id, "dry_run", "dry_run_only",
                       draft=preview, processed_at=processed_at, provider="resend")
    if (
        provider_result.get("success") is not True
        or not isinstance(provider_result.get("provider_message_id"), str)
        or not provider_result["provider_message_id"]
        or len(provider_result["provider_message_id"]) > 200
    ):
        return _result(mission_id, action_id, "blocked", "email_provider_failed",
                       draft=preview, provider=provider_name, processed_at=processed_at,
                       dry_run=is_dry_run)
    if not is_dry_run:
        return _result(
            mission_id, action_id, "sent", "email_sent", draft=preview,
            processed_at=processed_at, provider=provider_name,
            provider_message_id=provider_result["provider_message_id"],
            sent=True, dry_run=False, executed_at=processed_at,
        )
    if provider_name == "resend":
        return _result(
            mission_id, action_id, "dry_run", "resend_provider_processed",
            draft=preview, processed_at=processed_at, provider="resend",
            provider_message_id=provider_result["provider_message_id"],
        )
    return _result(
        mission_id, action_id, "dry_run", "fake_provider_simulated",
        draft=preview, processed_at=processed_at, provider="fake",
        provider_message_id=provider_result["provider_message_id"],
    )
