"""Load and validate configurable conversational mission definitions."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MISSIONS_FILE = "missions.json"
MISSION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_MISSION_TEXT_LENGTH = 2000
MAX_MISSION_ITEMS = 20
MAX_MISSION_TOTAL_TEXT_LENGTH = 10000


class MissionConfigurationError(ValueError):
    """Mission catalog configuration is missing or malformed."""


class MissionNotFoundError(ValueError):
    """The requested mission ID is not present in the catalog."""


def _required_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise MissionConfigurationError(f"Mission field '{field}' must be text.")
    value = value.strip()
    if len(value) > MAX_MISSION_TEXT_LENGTH:
        raise MissionConfigurationError(f"Mission field '{field}' is too long.")
    return value


def _text_list(value, field):
    if not isinstance(value, list) or len(value) > MAX_MISSION_ITEMS:
        raise MissionConfigurationError(f"Mission field '{field}' must be a short list.")
    values = tuple(_required_text(item, field) for item in value)
    if not values:
        raise MissionConfigurationError(f"Mission field '{field}' cannot be empty.")
    return values


@dataclass(frozen=True)
class Mission:
    id: str
    name: str
    objective: str
    required_information: tuple
    relevance_criteria: tuple
    follow_up_actions: tuple

    def instructions(self):
        required = "\n".join(f"- {item['key']}: {item['description']}" for item in self.required_information)
        relevance = "\n".join(f"- {item}" for item in self.relevance_criteria)
        actions = "\n".join(f"- {item}" for item in self.follow_up_actions)
        return (
            f"MISSION: {self.name} ({self.id})\n"
            f"OBJECTIVE:\n{self.objective}\n"
            f"INFORMATION TO CAPTURE WHEN RELEVANT:\n{required}\n"
            f"RELEVANCE CRITERIA:\n{relevance}\n"
            f"POSSIBLE POST-CALL FOLLOW-UP ACTIONS:\n{actions}\n"
            "Use this mission as context and priorities, not as a checklist or script. "
            "Let the conversation develop naturally, ask only relevant questions, "
            "and do not force a topic if the other person is not interested. "
            "Treat follow-up actions as recommendations for the human operator; "
            "do not promise or execute calls, emails, purchases, or other external actions."
        )


@dataclass(frozen=True)
class MissionCatalog:
    default_id: str
    missions: dict

    def get(self, mission_id=None):
        if mission_id is not None and (
            not isinstance(mission_id, str)
            or not MISSION_ID_PATTERN.fullmatch(mission_id)
        ):
            raise MissionNotFoundError("Requested mission was not found.")
        selected_id = mission_id or self.default_id
        mission = self.missions.get(selected_id)
        if mission is None:
            raise MissionNotFoundError("Requested mission was not found.")
        return mission


def load_mission_catalog(path=None, default_mission_id=None):
    catalog_path = Path(path or os.getenv("MISSIONS_FILE", DEFAULT_MISSIONS_FILE))
    try:
        content = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise MissionConfigurationError("Mission catalog could not be loaded.") from None

    if not isinstance(content, dict) or not isinstance(content.get("missions"), list):
        raise MissionConfigurationError("Mission catalog has an invalid structure.")

    missions = {}
    for entry in content["missions"]:
        if not isinstance(entry, dict):
            raise MissionConfigurationError("Mission catalog entry is invalid.")
        mission_id = entry.get("id")
        if not isinstance(mission_id, str) or not MISSION_ID_PATTERN.fullmatch(mission_id):
            raise MissionConfigurationError("Mission ID is invalid.")
        if mission_id in missions:
            raise MissionConfigurationError("Mission IDs must be unique.")

        required_information = entry.get("required_information")
        if not isinstance(required_information, list) or not required_information:
            raise MissionConfigurationError("Required information must be a non-empty list.")
        if len(required_information) > MAX_MISSION_ITEMS:
            raise MissionConfigurationError("Mission contains too many information fields.")
        normalized_fields = []
        used_keys = set()
        for field in required_information:
            if not isinstance(field, dict):
                raise MissionConfigurationError("Mission information field is invalid.")
            key = field.get("key")
            if not isinstance(key, str) or not MISSION_ID_PATTERN.fullmatch(key):
                raise MissionConfigurationError("Mission information key is invalid.")
            if key in used_keys:
                raise MissionConfigurationError("Mission information keys must be unique.")
            used_keys.add(key)
            normalized_fields.append({
                "key": key,
                "description": _required_text(field.get("description"), "required_information"),
            })

        name = _required_text(entry.get("name"), "name")
        objective = _required_text(entry.get("objective"), "objective")
        relevance_criteria = _text_list(
            entry.get("relevance_criteria"), "relevance_criteria"
        )
        follow_up_actions = _text_list(
            entry.get("follow_up_actions"), "follow_up_actions"
        )
        all_text = (
            [name, objective, *relevance_criteria, *follow_up_actions]
            + [field["description"] for field in normalized_fields]
        )
        if sum(len(item) for item in all_text) > MAX_MISSION_TOTAL_TEXT_LENGTH:
            raise MissionConfigurationError("Mission instructions are too long.")

        missions[mission_id] = Mission(
            id=mission_id,
            name=name,
            objective=objective,
            required_information=tuple(normalized_fields),
            relevance_criteria=relevance_criteria,
            follow_up_actions=follow_up_actions,
        )

    selected_default = (
        default_mission_id
        or os.getenv("DEFAULT_MISSION_ID")
        or content.get("default_mission")
    )
    if not isinstance(selected_default, str) or selected_default not in missions:
        raise MissionConfigurationError("Default mission is not defined in the catalog.")
    return MissionCatalog(selected_default, missions)
