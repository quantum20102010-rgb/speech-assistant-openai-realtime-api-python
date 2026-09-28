"""Build a preview message from a Smart Notification decision.

This module is deterministic and local. It has no message provider, network,
credential, storage, or action-execution capabilities.
"""

import re


VALID_PRIORITIES = {"high", "normal", "low", "none"}
PRIVATE_TOKEN_PATTERN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|AC[0-9a-fA-F]{32}|[A-Fa-f0-9]{32}|"
    r"[A-Za-z0-9_-]{40,}|eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
PHONE_PATTERN = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
EMAIL_PATTERN = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
MAX_FACTS = 4
MAX_NEXT_STEPS = 3
MAX_RECOMMENDATIONS = 2


def _clean_item(value, max_length=280):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip(" •-\t")
    if not value or PRIVATE_TOKEN_PATTERN.search(value) or PHONE_PATTERN.search(value):
        return None
    value = EMAIL_PATTERN.sub("[correo omitido]", value)
    if max_length is not None and len(value) > max_length:
        value = value[: max_length - 1].rstrip() + "…"
    return value or None


def _unique_clean(items, max_items=None, max_length=280):
    if not isinstance(items, list):
        return []
    result = []
    seen = set()
    for item in items:
        clean = _clean_item(item, max_length)
        key = clean.casefold() if clean else ""
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
        if max_items is not None and len(result) >= max_items:
            break
    return result


def _facts(decision):
    values = decision.get("facts_to_notify", [])
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for item in values:
        if not isinstance(item, dict) or item.get("source") != "transcript":
            continue
        # Contact details and identifiers do not belong in the notification.
        if item.get("field") in {"phone", "email", "call_sid", "id"}:
            continue
        clean = _clean_item(item.get("text"))
        key = clean.casefold() if clean else ""
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
        if len(result) >= MAX_FACTS:
            break
    return result


def _message_lines(notification_decision, call_result, max_facts=MAX_FACTS):
    facts = _facts(notification_decision)[:max_facts]
    requests = _unique_clean(notification_decision.get("requests_from_contact"))
    decisions = _unique_clean(
        notification_decision.get("decisions_needed_from_user"), max_length=None
    )
    next_steps = _unique_clean(
        notification_decision.get("next_steps"), MAX_NEXT_STEPS
    )
    recommendations = _unique_clean(
        notification_decision.get("recommendations"), MAX_RECOMMENDATIONS
    )

    lines = []
    company = _clean_item(call_result.get("company"), max_length=90)
    lines.append(f"📞 *{company or 'Actualización comercial'}*")

    if facts:
        lines.extend(["", "*Información obtenida*"])
        lines.extend(f"• {fact}" for fact in facts)
    if requests:
        lines.extend(["", "*El contacto solicita*"])
        lines.extend(f"• {request}" for request in requests)
    if decisions:
        lines.extend(["", "*Pendiente de ti*"])
        lines.extend(f"• {decision}" for decision in decisions)
    if next_steps:
        lines.extend(["", "*Siguiente paso acordado*"])
        lines.extend(f"• {step}" for step in next_steps)
    elif recommendations:
        # Recommendations are clearly labeled as suggestions and never as facts.
        suggestion = recommendations[0]
        if suggestion.casefold() not in {item.casefold() for item in facts + requests + decisions}:
            lines.extend(["", "*Siguiente paso sugerido (no ejecutado)*", f"• {suggestion}"])

    return lines


def build_notification_message(notification_decision, call_result=None):
    """Build an unsent WhatsApp-style message for Fabian.

    The `call_result` input is used only for the company label. The transcript,
    phone, provider identifiers, and other call metadata are never included.
    """
    decision = notification_decision if isinstance(notification_decision, dict) else {}
    result = call_result if isinstance(call_result, dict) else {}
    raw_priority = decision.get("priority")
    priority = raw_priority if isinstance(raw_priority, str) and raw_priority in VALID_PRIORITIES else "none"
    should_send = decision.get("should_notify") is True and priority != "none"
    if not should_send:
        return {
            "schema_version": 1,
            "should_send": False,
            "channel": "whatsapp",
            "priority": priority,
            "message": "",
            "message_type": "none",
        }

    lines = _message_lines(decision, result)
    message = "\n".join(lines).strip()
    for fact_count in range(MAX_FACTS - 1, -1, -1):
        if len(message) <= 1200:
            break
        message = "\n".join(_message_lines(decision, result, fact_count)).strip()
    if message == "📞 *Actualización comercial*" and isinstance(decision.get("notification_summary"), str):
        # The summary was created from already-grounded evidence; keep it short
        # and use it only when structured lists are unexpectedly empty.
        summary = _clean_item(decision["notification_summary"], max_length=500)
        if summary:
            message += f"\n\n{summary}"

    message_type = (
        "action_required"
        if _unique_clean(decision.get("decisions_needed_from_user"))
        or _unique_clean(decision.get("requests_from_contact"))
        else "business_update"
    )
    return {
        "schema_version": 1,
        "should_send": True,
        "channel": "whatsapp",
        "priority": priority,
        "message": message,
        "message_type": message_type,
    }
