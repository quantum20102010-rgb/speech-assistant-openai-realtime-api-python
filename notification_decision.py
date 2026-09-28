"""Pure, deterministic notification recommendation from persisted call data.

This module deliberately has no provider, network, credential, filesystem, or
action-execution capabilities. It only returns a structured recommendation.
"""

import re


HIGH_VALUE_FIELDS = {
    "distribution_interest",
    "b2b_terms",
    "minimum_order_quantity",
    "pricing_information",
    "discount_information",
    "territories",
    "exclusivity",
}
HIGH_VALUE_CUES = (
    "disponible", "disponibilidad", "available", "availability", "capacidad",
    "capacity", "precio", "precios", "price", "pricing", "descuento",
    "discount", "pedido mínimo", "pedido minimo", "minimum order", "moq",
    "condiciones", "terms", "territorio", "territories", "exclusividad",
    "exclusivity", "entrega", "delivery", "lead time", "catálogo", "catalogo",
    "catalog", "cotización", "cotizacion", "quote", "oportunidad", "opportunity",
    "interés", "interes", "interested", "distribution", "distribución",
    "distribucion", "wholesale", "mayorista",
)
IMPORTANT_REQUEST_CUES = (
    "catálogo", "catalogo", "catalog", "cotización", "cotizacion", "quote",
    "documentación", "documentacion", "documents", "datos fiscales", "tax details",
    "cantidades", "quantities", "volumen", "volume", "reunión", "reunion", "meeting",
)
DUPLICATE_ONLY_CUES = (
    "sin cambios", "nada nuevo", "lo mismo de antes", "igual que la vez pasada",
    "same as before", "nothing new", "no changes", "same information as last time",
)
WRONG_NUMBER_CUES = (
    "número equivocado", "numero equivocado", "marcó mal", "marco mal",
    "wrong number", "you have the wrong number", "contacto equivocado",
    "wrong contact",
)


def _fold(value):
    return value.casefold() if isinstance(value, str) else ""


def _contact_turns(transcript):
    if not isinstance(transcript, str):
        return []
    turns = []
    for line in transcript.splitlines():
        if line.startswith("Contacto:"):
            text = line[len("Contacto:"):].strip()
            if text:
                turns.extend(
                    part.strip()
                    for part in re.split(r"(?<=[.!?])\s+", text)
                    if part.strip()
                )
    return turns


def _contains(text, cues):
    folded = _fold(text)
    return any(cue in folded for cue in cues)


def _grounded_facts(decision, contact_text):
    facts = []
    seen = set()
    source_facts = decision.get("facts", []) if isinstance(decision, dict) else []
    if not isinstance(source_facts, list):
        return facts

    folded_contact = _fold(contact_text)
    for item in source_facts:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        if source == "transcript" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if not text or _fold(text) not in folded_contact:
                continue
            fact = {"text": text, "source": "transcript"}
            identity = ("transcript", _fold(text))
        elif source == "transcript_verified_result":
            value = item.get("value")
            field = item.get("field")
            label = item.get("label")
            if (
                not isinstance(value, str)
                or not value.strip()
                or not isinstance(field, str)
                or not isinstance(label, str)
                or _fold(value.strip()) not in folded_contact
            ):
                continue
            fact = {
                "field": field,
                "text": f"{label}: {value.strip()}",
                "source": "transcript",
            }
            identity = (field, _fold(value.strip()))
        else:
            # Never turn model recommendations or unverified fields into facts.
            continue
        if identity not in seen:
            facts.append(fact)
            seen.add(identity)
    return facts


def _explicit_requests(decision, contact_text):
    source = decision.get("requests_from_contact", []) if isinstance(decision, dict) else []
    if not isinstance(source, list):
        return []
    folded_contact = _fold(contact_text)
    requests = []
    seen = set()
    for request in source:
        if not isinstance(request, str):
            continue
        request = request.strip()
        folded = _fold(request)
        if folded and folded in folded_contact and folded not in seen:
            requests.append(request)
            seen.add(folded)
    return requests


def _approval_decisions(decision):
    values = decision.get("approval_required", []) if isinstance(decision, dict) else []
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        value.strip() for value in values
        if isinstance(value, str) and value.strip()
    ))


def _verified_next_steps(decision, contact_text):
    follow_up = decision.get("follow_up", {}) if isinstance(decision, dict) else {}
    step = follow_up.get("next_step") if isinstance(follow_up, dict) else None
    if isinstance(step, str) and step.strip() and _fold(step.strip()) in _fold(contact_text):
        return [step.strip()]
    return []


def _mission_progress(decision):
    progress = decision.get("mission_progress", {}) if isinstance(decision, dict) else {}
    if not isinstance(progress, dict):
        return 0, False
    supported = progress.get("supported", 0)
    supported = supported if isinstance(supported, int) and not isinstance(supported, bool) else 0
    return max(0, supported), progress.get("complete") is True


def _empty(reason_code="no_relevant_information"):
    return {
        "schema_version": 1,
        "should_notify": False,
        "priority": "none",
        "reason_code": reason_code,
        "reasons": [],
        "facts_to_notify": [],
        "requests_from_contact": [],
        "decisions_needed_from_user": [],
        "next_steps": [],
        "recommendations": [],
        "notification_summary": "",
    }


def decide_notification(call_result, decision=None, transcript=None):
    """Return whether a completed analysis contains useful, evidenced updates.

    Facts and requests are accepted only when they can be matched against the
    contact's literal transcript turns. Recommendations remain a separate list
    and never become facts or executable actions.
    """
    call_result = call_result if isinstance(call_result, dict) else {}
    decision = decision if isinstance(decision, dict) else call_result.get("decision", {})
    decision = decision if isinstance(decision, dict) else {}
    transcript = transcript if isinstance(transcript, str) else call_result.get("transcript", "")
    turns = _contact_turns(transcript)
    contact_text = "\n".join(turns)
    if not turns:
        return _empty("no_contact_transcript")

    facts = _grounded_facts(decision, contact_text)
    requests = _explicit_requests(decision, contact_text)
    decisions_needed = _approval_decisions(decision)
    next_steps = _verified_next_steps(decision, contact_text)
    recommendations = []
    recommended = decision.get("recommended_actions", [])
    if isinstance(recommended, list):
        recommendations = list(dict.fromkeys(
            item.get("action", "").strip()
            for item in recommended
            if isinstance(item, dict) and isinstance(item.get("action"), str)
            and item.get("action", "").strip()
        ))

    # A wrong-number message or an explicit duplicate-only update is not useful
    # by itself, even if a broad keyword in the phrase resembles a business cue.
    if _contains(contact_text, WRONG_NUMBER_CUES):
        return _empty("wrong_number_or_contact")
    duplicate_turns = [turn for turn in turns if _contains(turn, DUPLICATE_ONLY_CUES)]
    other_turns = [turn for turn in turns if not _contains(turn, DUPLICATE_ONLY_CUES)]
    if (
        duplicate_turns and not requests and not decisions_needed and not next_steps
        and not _contains(" ".join(other_turns), HIGH_VALUE_CUES)
    ):
        return _empty("no_new_information")

    supported, mission_complete = _mission_progress(decision)
    field_names = {
        fact.get("field") for fact in facts if isinstance(fact.get("field"), str)
    }
    fact_text = " ".join(fact.get("text", "") for fact in facts)
    high_value = bool(field_names & HIGH_VALUE_FIELDS) or _contains(fact_text, HIGH_VALUE_CUES)
    mission_signal = supported > 0 and bool(facts)
    concrete_contact = bool(requests or next_steps)
    relevant_contact = bool(facts) and (
        high_value or mission_signal or _contains(contact_text, HIGH_VALUE_CUES)
        or bool(field_names & {"company", "contact_role"})
    )

    if not (relevant_contact or concrete_contact or decisions_needed):
        return _empty()

    reasons = []
    if facts:
        reasons.append("transcript_supported_business_information")
    if requests:
        reasons.append("explicit_contact_request")
    if decisions_needed:
        reasons.append("user_decision_required")
    if next_steps:
        reasons.append("concrete_next_step")
    if mission_signal:
        reasons.append("relevant_mission_progress")
    if mission_complete:
        reasons.append("mission_completed_with_evidence")

    contact_only_fields = {"company", "contact_name", "contact_role", "email"}
    secondary_contact_only = bool(facts) and field_names.issubset(contact_only_fields) and not (
        high_value or requests or next_steps or decisions_needed or mission_signal
    )
    if decisions_needed or high_value or (requests and _contains(" ".join(requests), IMPORTANT_REQUEST_CUES)):
        priority = "high"
    elif secondary_contact_only:
        priority = "low"
    elif relevant_contact or requests or next_steps or mission_signal:
        priority = "normal"
    else:
        priority = "low"

    summary_parts = []
    for fact in facts[:2]:
        summary_parts.append(fact["text"])
    if requests:
        summary_parts.append(f"Solicitud explícita: {requests[0]}")
    if decisions_needed:
        summary_parts.append(f"Requiere decisión: {decisions_needed[0]}")
    elif next_steps:
        summary_parts.append(f"Siguiente paso: {next_steps[0]}")
    summary = "; ".join(summary_parts)[:500]

    return {
        "schema_version": 1,
        "should_notify": True,
        "priority": priority,
        "reason_code": "actionable_business_information",
        "reasons": reasons,
        "facts_to_notify": facts,
        "requests_from_contact": requests,
        "decisions_needed_from_user": decisions_needed,
        "next_steps": next_steps,
        "recommendations": recommendations,
        "notification_summary": summary,
    }
