"""Local mission decision records and safe, non-executing resolution."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re

from call_results import redact_configured_secrets
from missions import MissionConfigurationError, MissionNotFoundError, load_mission_catalog


DECISION_STATUSES = {"pending", "approved", "rejected", "deferred"}
RESOLUTION_STATUSES = {"approved", "rejected", "deferred"}
_MISSION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PRIVATE_VALUE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|AC[0-9a-fA-F]{32}|"
    r"[A-Fa-f0-9]{32}|[A-Za-z0-9_-]{40,}|"
    r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")


class MissionDecisionError(ValueError):
    """A mission decision cannot be created or resolved safely."""


def _safe_text(value, field, required=True):
    if not isinstance(value, str):
        if required:
            raise MissionDecisionError(f"{field}_invalid")
        return None
    value = " ".join(value.split()).strip()
    if not value or len(value) > 500:
        if required:
            raise MissionDecisionError(f"{field}_invalid")
        return None
    value = redact_configured_secrets(value)
    value = _PRIVATE_VALUE.sub("[REDACTED]", value)
    value = _EMAIL.sub("[correo omitido]", value)
    value = _PHONE.sub("[teléfono omitido]", value)
    return value[:500]


def _canonical_occurrence(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def create_pending_decision(mission_id, description, source, occurrence_id=None):
    """Create a stable pending record; repeated calls do not duplicate it."""
    if not isinstance(mission_id, str) or not _MISSION_ID.fullmatch(mission_id):
        raise MissionDecisionError("mission_not_found")
    clean_description = _safe_text(description, "description")
    clean_source = _safe_text(source, "source")
    created_at = _canonical_occurrence(occurrence_id)
    digest = hashlib.sha256(
        f"{mission_id}\0{clean_source}\0{clean_description}\0{created_at or ''}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "decision_id": f"md_{digest}",
        "mission_id": mission_id,
        "description": clean_description,
        "source": clean_source,
        "status": "pending",
        "resolved_by": None,
        "resolution": None,
        "resolved_at": None,
        "created_at": created_at,
    }


def normalize_decision_records(mission_id, decisions, legacy_pending=None):
    """Validate stored records and migrate the previous string-only shape."""
    records = []
    by_id = {}
    for item in decisions if isinstance(decisions, list) else []:
        if not isinstance(item, dict) or item.get("mission_id") != mission_id:
            continue
        if not isinstance(item.get("status"), str) or item.get("status") not in DECISION_STATUSES:
            continue
        try:
            record = create_pending_decision(
                mission_id, item.get("description"), item.get("source"),
                item.get("created_at"),
            )
        except MissionDecisionError:
            continue
        supplied_id = item.get("decision_id")
        if not isinstance(supplied_id, str) or supplied_id != record["decision_id"]:
            continue
        record["status"] = item["status"]
        if item["status"] != "pending":
            try:
                record["resolved_by"] = _safe_text(item.get("resolved_by"), "resolved_by")
                record["resolution"] = _safe_text(item.get("resolution"), "resolution")
                resolved_at = item.get("resolved_at")
                parsed_time = datetime.fromisoformat(resolved_at) if isinstance(resolved_at, str) else None
                if parsed_time is None or parsed_time.tzinfo is None:
                    continue
                record["resolved_at"] = parsed_time.astimezone(timezone.utc).isoformat()
            except MissionDecisionError:
                continue
            except ValueError:
                continue
        by_id[record["decision_id"]] = record
    for description in legacy_pending if isinstance(legacy_pending, list) else []:
        if not isinstance(description, str):
            continue
        try:
            record = create_pending_decision(mission_id, description, "legacy_mission_state")
        except MissionDecisionError:
            continue
        by_id.setdefault(record["decision_id"], record)
    records.extend(by_id.values())
    return records


def resolve_mission_decision(state, mission, mission_id, decision_id, status,
                             resolution, now=None):
    """Resolve one pending decision and return a copied, updated Mission State.

    This function only records Fabian's choice. It has no provider clients and
    intentionally never performs the action described by the decision.
    """
    if mission is None or not isinstance(getattr(mission, "id", None), str):
        raise MissionDecisionError("mission_not_found")
    if not isinstance(mission_id, str) or mission.id != mission_id:
        raise MissionDecisionError("mission_mismatch")
    if not isinstance(state, dict) or state.get("mission_id") != mission_id:
        raise MissionDecisionError("mission_mismatch")
    if not isinstance(status, str) or status not in RESOLUTION_STATUSES:
        raise MissionDecisionError("resolution_status_invalid")
    if not isinstance(decision_id, str) or not decision_id:
        raise MissionDecisionError("decision_not_found")

    updated = deepcopy(state)
    legacy_pending = (
        updated.get("decisions_pending")
        if not isinstance(updated.get("decisions"), list) else []
    )
    records = normalize_decision_records(
        mission_id, updated.get("decisions"), legacy_pending
    )
    record = next((item for item in records if item["decision_id"] == decision_id), None)
    if record is None:
        raise MissionDecisionError("decision_not_found")
    if record["mission_id"] != mission_id:
        raise MissionDecisionError("mission_mismatch")
    if record["status"] != "pending":
        raise MissionDecisionError("decision_already_resolved")

    clean_resolution = _safe_text(resolution, "resolution")
    resolved_at = now or datetime.now(timezone.utc)
    if not isinstance(resolved_at, datetime) or resolved_at.tzinfo is None:
        raise MissionDecisionError("resolution_time_invalid")
    record.update({
        "status": status,
        "resolved_by": "Fabian",
        "resolution": clean_resolution,
        "resolved_at": resolved_at.astimezone(timezone.utc).isoformat(),
    })
    updated["decisions"] = records
    pending_ids = [item["decision_id"] for item in records if item["status"] == "pending"]
    updated["decisions_pending"] = pending_ids
    updated["updated_at"] = resolved_at.astimezone(timezone.utc).isoformat()

    if pending_ids:
        updated["status"] = "waiting_for_user"
        updated["needs_follow_up"] = False
        updated["follow_up_reason"] = "decision_pending"
    elif status == "deferred":
        updated["status"] = "needs_follow_up"
        updated["needs_follow_up"] = True
        updated["follow_up_reason"] = "decision_deferred"
    elif status == "approved" and updated.get("next_steps"):
        updated["status"] = "needs_follow_up"
        updated["needs_follow_up"] = True
        updated["follow_up_reason"] = "approved_action_not_executed"
    elif updated.get("evidence_complete") is True:
        updated["status"] = "completed"
        updated["needs_follow_up"] = False
        updated["follow_up_reason"] = None
    elif updated.get("needs_follow_up") is True or updated.get("next_steps"):
        updated["status"] = "needs_follow_up"
        updated["needs_follow_up"] = True
        updated["follow_up_reason"] = updated.get("follow_up_reason") or "approved_action_not_executed"
    elif updated.get("facts_obtained") or updated.get("requests_detected"):
        updated["status"] = "in_progress"
        updated["needs_follow_up"] = False
        updated["follow_up_reason"] = None
    else:
        updated["status"] = "pending"
        updated["needs_follow_up"] = False
        updated["follow_up_reason"] = None

    # Mission Decision is the sole source of action approvals. This mirrors
    # its outcome onto local action metadata without executing anything.
    from action_executor import sync_action_approvals
    updated = sync_action_approvals(updated, resolved_at)

    return updated


def resolve_persisted_mission_decision(storage, mission_id, decision_id,
                                       status, resolution, mission_loader=None,
                                       now=None):
    """Resolve a decision from local storage and persist the updated state."""
    loader = mission_loader or load_mission_catalog
    try:
        mission = loader().get(mission_id)
    except (MissionConfigurationError, MissionNotFoundError):
        raise MissionDecisionError("mission_not_found") from None
    if mission is None:
        raise MissionDecisionError("mission_not_found")
    if mission.id != mission_id:
        raise MissionDecisionError("mission_mismatch")
    mutate = getattr(storage, "mutate_latest_mission_state", None)
    latest = getattr(storage, "latest_mission_state", None)
    if not callable(mutate) or not callable(latest):
        raise MissionDecisionError("mission_state_storage_unavailable")
    if latest(mission_id) is None:
        raise MissionDecisionError("decision_not_found")
    updated = mutate(
        mission_id,
        lambda state: resolve_mission_decision(
            state, mission, mission_id, decision_id, status, resolution, now
        ),
    )
    if updated is None:
        raise MissionDecisionError("mission_state_persistence_failed")
    return updated
