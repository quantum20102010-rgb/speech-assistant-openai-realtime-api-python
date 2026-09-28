"""Credential-free commercial decision analysis for persisted call results.

This module is intentionally pure: it has no provider, network, filesystem,
secret, or action-execution capabilities. It returns data for downstream
systems; it never sends messages or initiates follow-up.
"""

import re


AUTOMATIC = "AUTOMATIC"
REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
FORBIDDEN_AUTOMATIC = "FORBIDDEN_AUTOMATIC"
AUTONOMY_LEVELS = {AUTOMATIC, REQUIRES_APPROVAL, FORBIDDEN_AUTOMATIC}

STATUSES = {
    "no_action", "informational", "actionable", "awaiting_user",
    "awaiting_third_party", "follow_up_required", "completed", "failed",
}

REQUEST_CUES = (
    "necesitamos", "necesito", "solicitamos", "solicito", "solicita", "solicitan",
    "pide", "piden", "envien", "envíen",
    "mande", "manden", "compartan", "comparta", "puede enviar", "pueden enviar",
    "podria enviar", "podría enviar", "por favor envia", "por favor envía",
    "please send", "please provide", "we need", "send us", "provide us", "could you send",
    "would you send", "share with us", "para preparar la cotizacion",
    "puede compartir", "podria compartir", "podría compartir",
    "para preparar la cotización", "para cotizar", "to prepare a quote",
)
RELEVANCE_CUES = (
    "interes", "interés", "interesa", "interesados", "distribu", "mayorista",
    "wholesale", "quote", "cotiz", "precio", "price", "costo", "cost",
    "producto", "product", "catalogo", "catálogo", "catalog", "proveedor",
    "supplier", "capacidad", "capacity", "disponible", "available", "pedido",
    "order", "volumen", "condiciones", "terms", "requisito", "requirement",
    "seguimiento", "follow up", "contacto", "contact", "envien", "envíen",
)
NO_INTEREST_CUES = (
    "no nos interesa", "no estoy interesado", "no estamos interesados",
    "no me interesa", "not interested", "no interest", "no buscamos proveedores",
    "ya tenemos proveedor", "we already have a supplier",
)
BUSINESS_FACT_CUES = RELEVANCE_CUES + (
    "no nos interesa", "no estoy interesado", "not interested", "correo",
    "email", "e-mail", "@", "aparte", "incluye", "incluido", "separado",
    "origen", "volumen", "almacenaje", "transporte", "despacho", "customs",
)
FORBIDDEN_CUES = (
    "aceptar precio", "aceptar el precio", "aceptar condiciones",
    "aceptar términos", "aceptar terminos", "aceptar contrato", "firmar contrato",
    "comprometer a guzi", "comprometer guzi", "comprometer la empresa",
    "autorizar pago", "realizar pago", "pagar", "activar llamadas",
    "activar calls_enabled", "cambiar límite", "cambiar limite", "activar servicio de pago",
    "aceptar la cotización", "aceptar la cotizacion", "aceptar oferta",
    "accept the quote", "accept the price", "accept the terms", "sign the contract",
    "make a payment", "approve payment", "purchase on behalf",
)
FORBIDDEN_PATTERN = re.compile(
    r"\b(?:aceptar|aceptamos|accept|approve|aprobar|firmar|sign)\b.{0,80}"
    r"\b(?:precio|precios|cotizaci[oó]n|quote|quotation|condiciones|terms|"
    r"contrato|contract|oferta|offer)\b",
    re.IGNORECASE,
)
APPROVAL_CUES = (
    "enviar email", "enviar correo", "mandar correo", "mandar email",
    "hacer una llamada", "realizar llamada", "llamar de nuevo", "enviar información",
    "enviar informacion", "enviar catálogo", "enviar catalogo", "seguimiento externo",
    "contactar al proveedor", "programar contacto", "contactar", "send an email", "send a message", "send information",
    "make another call", "initiate a call", "follow up with the contact", "contact the supplier",
    "send the catalog", "share information with", "provide information to the contact",
    "solicitar o revisar el catálogo", "solicitar o revisar el catalogo",
    "solicitar catálogo al proveedor", "solicitar catalogo al proveedor",
    "request the catalog from the supplier",
)

FIELD_LABELS = {
    "company": "empresa",
    "contact_name": "nombre del contacto",
    "contact_role": "cargo del contacto",
    "email": "correo proporcionado",
    "distribution_interest": "interés de distribución",
    "b2b_terms": "condiciones B2B",
    "minimum_order_quantity": "pedido mínimo",
    "pricing_information": "información de precios",
    "discount_information": "descuentos",
    "territories": "territorios",
    "exclusivity": "exclusividad",
    "relevant_information": "información comercial",
    "next_action": "siguiente paso",
    "follow_up_reason": "motivo de seguimiento",
    "follow_up_date": "fecha sugerida de seguimiento",
}
MISSION_STOP_WORDS = {
    "para", "como", "sobre", "esta", "este", "cuando", "desde", "entre",
    "ante", "donde", "puede", "pueden", "persona", "información", "informacion",
    "hechos", "hecho", "datos", "expresados", "expresadas", "explícitamente",
    "explicitamente", "relevante", "relevantes", "siguiente", "acuerdo", "acordado",
    "otra", "otro", "parte", "empresa", "dicho", "dicha", "with", "from", "that",
    "this", "their", "they", "have", "when", "what", "which", "about", "into",
}


def _fold(value):
    return value.casefold() if isinstance(value, str) else ""


def _contact_segments(transcript):
    if not isinstance(transcript, str):
        return []
    segments = []
    for line in transcript.splitlines():
        if line.startswith("Contacto:"):
            text = line[len("Contacto:"):].strip()
            if text:
                segments.extend(
                    part.strip() for part in re.split(r"(?<=[.!?])\s+", text)
                    if part.strip()
                )
    return segments


def _contains_any(text, cues):
    folded = _fold(text)
    return any(cue in folded for cue in cues)


def _mission_relevance_signals(mission, text):
    if mission is None:
        return []
    criteria = getattr(mission, "relevance_criteria", ()) or ()
    terms = set()
    for criterion in criteria:
        if isinstance(criterion, str):
            terms.update(re.findall(r"[a-záéíóúüñ]{4,}", _fold(criterion)))
    terms.difference_update(MISSION_STOP_WORDS)
    folded = _fold(text)
    return sorted(term for term in terms if term in folded)


def _is_supported(value, contact_text):
    if not isinstance(value, str) or not value.strip():
        return False
    return _fold(value.strip()) in _fold(contact_text)


def classify_action(action):
    """Assign autonomy to an action description without executing it."""
    text = _fold(action)
    if _contains_any(text, FORBIDDEN_CUES) or FORBIDDEN_PATTERN.search(text):
        level = FORBIDDEN_AUTOMATIC
    elif _contains_any(text, APPROVAL_CUES):
        level = REQUIRES_APPROVAL
    else:
        level = AUTOMATIC
    return {
        "action": str(action),
        "autonomy": level,
        "approval_required": level == REQUIRES_APPROVAL,
        "executable": False,
    }


def _extract_requested_information(requests):
    result = []
    seen = set()
    request_pattern = re.compile(
        r"(?:necesitamos|necesito|solicitamos|solicito|solicita|solicitan|env[ií]en|mande[n]?|"
        r"compartan|comparta|puede enviar|pueden enviar|podr[ií]a enviar|"
        r"puede compartir|podr[ií]a compartir|pide|piden|"
        r"please send|we need|send us|provide us with|could you send|share with us)"
        r"\s+([^.!?;]+)", re.IGNORECASE,
    )
    strip_prefix = re.compile(
        r"^(?:para\s+(?:preparar|hacer|emitir)\s+(?:la\s+)?(?:cotizaci[oó]n|quote)\s*)?"
        r"(?:un|una|unos|unas|el|la|los|las|a|an|the)\s+",
        re.IGNORECASE,
    )
    for sentence in requests:
        for match in request_pattern.finditer(sentence):
            tail = match.group(1).strip()
            tail = re.sub(r"\s+por correo(?:\s+electr[oó]nico)?$", "", tail, flags=re.IGNORECASE)
            parts = re.split(r"\s*(?:,|;|\by\b|\band\b)\s*", tail, flags=re.IGNORECASE)
            for part in parts:
                item = strip_prefix.sub("", part.strip()).strip(" .,:-")
                if _fold(item) in {"lo", "la", "los", "las", "it", "them", "this", "that"}:
                    continue
                if item and len(item) <= 180 and _fold(item) not in seen:
                    result.append(item)
                    seen.add(_fold(item))
    return result


def _mission_coverage(result, transcript, mission):
    if mission is None:
        return {"required": 0, "supported": 0, "missing": [], "complete": False}
    findings = result.get("mission_findings") if isinstance(result, dict) else None
    findings = findings if isinstance(findings, dict) else {}
    required = tuple(getattr(mission, "required_information", ()) or ())
    supported = []
    missing = []
    for field in required:
        key = field.get("key") if isinstance(field, dict) else None
        value = findings.get(key) if key else None
        if _is_supported(value, transcript):
            supported.append(key)
        elif key:
            missing.append(key)
    return {
        "required": len(required),
        "supported": len(supported),
        "supported_fields": supported,
        "missing": missing,
        "complete": bool(required) and len(supported) == len(required),
    }


def failed_decision(reason="analysis_failed"):
    return {
        "schema_version": 1,
        "status": "failed",
        "relevance": "unknown",
        "relevance_basis": {
            "matched_call_cues": [],
            "matched_mission_criteria_terms": [],
            "contact_requested_action": False,
            "transcript_verified_fields": [],
        },
        "facts": [],
        "requests_from_contact": [],
        "information_to_provide": [],
        "actions_required": [],
        "recommended_actions": [],
        "approval_required": [],
        "follow_up": {"required": False, "status": "unknown", "reason": reason},
        "summary": {"text": None, "generated_by": None, "label": "Resumen generado por el modelo no disponible."},
        "mission_progress": {"required": 0, "supported": 0, "missing": [], "complete": False},
        "analysis_error": reason,
    }


class DecisionEngine:
    """Build a conservative, persistable commercial decision from call artifacts."""

    def analyze(self, call_result, transcript, mission=None):
        if not isinstance(call_result, dict):
            raise TypeError("call_result must be a mapping")
        if not isinstance(transcript, str) or not transcript.strip():
            decision = failed_decision("transcript_unavailable")
            if call_result.get("call_status") not in {"error", "rejected"}:
                decision["status"] = "no_action"
                decision["follow_up"]["status"] = "not_required"
                decision.pop("analysis_error", None)
            decision["summary"] = {
                "text": call_result.get("summary") if call_result.get("summary_generated_by_model") else None,
                "generated_by": "model" if call_result.get("summary_generated_by_model") else None,
                "label": "Resumen generado por el modelo" if call_result.get("summary_generated_by_model") else "Resumen generado por el modelo no disponible.",
            }
            return decision

        contact_segments = _contact_segments(transcript)
        contact_text = "\n".join(contact_segments)
        mission_signals = _mission_relevance_signals(mission, contact_text)
        requests_from_contact = [
            sentence for sentence in contact_segments
            if _contains_any(sentence, REQUEST_CUES)
        ]
        information_to_provide = _extract_requested_information(requests_from_contact)
        facts = [
            {"type": "conversation_statement", "text": sentence, "source": "transcript"}
            for sentence in contact_segments
            if _contains_any(sentence, BUSINESS_FACT_CUES)
            or any(term in _fold(sentence) for term in mission_signals)
        ]
        verified_fields = []
        for field, label in FIELD_LABELS.items():
            value = call_result.get(field)
            if _is_supported(value, contact_text):
                verified_fields.append(field)
                facts.append({
                    "type": "structured_result", "field": field,
                    "label": label, "value": value,
                    "source": "transcript_verified_result",
                })

        lowered = _fold(contact_text)
        relevance_text = lowered
        for phrase in NO_INTEREST_CUES:
            relevance_text = relevance_text.replace(phrase, " ")
        matched_cues = [cue for cue in RELEVANCE_CUES if cue in relevance_text]
        commercial_signal = bool(matched_cues) or bool(mission_signals)
        relevant = commercial_signal or bool(verified_fields) or bool(requests_from_contact) or _contains_any(
            contact_text, ("correo", "email", "e-mail", "@")
        )
        if _contains_any(lowered, NO_INTEREST_CUES) and not requests_from_contact and not commercial_signal:
            relevant = False

        progress = _mission_coverage(call_result, contact_text, mission)
        explicit_follow_up = any(
            isinstance(call_result.get(field), str)
            and _is_supported(call_result.get(field), contact_text)
            for field in ("next_action", "follow_up_reason", "follow_up_date")
        ) or _contains_any(contact_text, ("nos hablamos", "dar seguimiento", "follow up", "call me", "llame", "llamar"))

        actions = [classify_action("Guardar información y análisis de la llamada")]
        recommended = []
        approval_required = []
        if relevant:
            recommended.append(classify_action("Registrar una tarea pendiente para revisar los hallazgos"))
        if information_to_provide or requests_from_contact:
            actions.append(classify_action("Preparar un borrador con la información solicitada"))
            external = classify_action("Enviar información comercial por correo al contacto")
            actions.append(external)
            approval_required.append(external["action"])
            recommended.append(classify_action("Preparar correo para revisión y autorización"))
        if explicit_follow_up:
            followup_action = classify_action("Iniciar seguimiento externo con el contacto")
            actions.append(followup_action)
            approval_required.append(followup_action["action"])
            recommended.append(classify_action("Registrar el seguimiento para autorización humana"))
        if relevant and mission is not None:
            for suggestion in getattr(mission, "follow_up_actions", ()) or ():
                suggestion_action = classify_action(suggestion)
                recommended.append(suggestion_action)
                if suggestion_action["autonomy"] == REQUIRES_APPROVAL:
                    approval_required.append(suggestion_action["action"])

        if not relevant:
            status = "no_action"
            relevance = "not_relevant"
        elif progress["complete"] and not information_to_provide:
            status = "completed"
            relevance = "actionable"
        elif information_to_provide or requests_from_contact:
            status = "awaiting_user"
            relevance = "actionable"
        elif explicit_follow_up:
            status = "follow_up_required"
            relevance = "actionable"
        elif progress["supported"]:
            status = "actionable"
            relevance = "actionable"
        else:
            status = "informational"
            relevance = "actionable"

        third_party_promises = (
            "le enviaremos la cotizacion", "le enviaremos la cotización",
            "te enviaremos la cotizacion", "te enviaremos la cotización",
            "we will send you a quote", "we'll send you a quote",
            "we will send the quote", "we'll send the quote",
        )
        if _contains_any(contact_text, third_party_promises):
            status = "awaiting_third_party"

        summary_text = call_result.get("summary") if call_result.get("summary_generated_by_model") else None
        decision = {
            "schema_version": 1,
            "status": status if status in STATUSES else "failed",
            "relevance": relevance,
            "relevance_basis": {
                "matched_call_cues": matched_cues,
                "matched_mission_criteria_terms": mission_signals,
                "contact_requested_action": bool(requests_from_contact),
                "transcript_verified_fields": verified_fields,
            },
            "facts": facts,
            "requests_from_contact": requests_from_contact,
            "information_to_provide": information_to_provide,
            "actions_required": actions,
            "recommended_actions": recommended,
            "approval_required": list(dict.fromkeys(approval_required)),
            "follow_up": {
                "required": bool(explicit_follow_up or approval_required),
                "status": "pending" if explicit_follow_up or approval_required else "not_required",
                "reason": "Se acordó o solicitó un siguiente paso." if explicit_follow_up else (
                    "Hay una acción externa que requiere autorización." if approval_required else None
                ),
                "next_step": call_result.get("next_action") if _is_supported(call_result.get("next_action"), contact_text) else None,
            },
            "summary": {
                "text": summary_text,
                "generated_by": "model" if summary_text else None,
                "label": "Resumen generado por el modelo" if summary_text else "Resumen generado por el modelo no disponible.",
            },
            "mission_progress": progress,
        }
        return decision
