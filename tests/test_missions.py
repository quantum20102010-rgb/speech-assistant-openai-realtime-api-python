import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from missions import (
    MissionConfigurationError,
    MissionNotFoundError,
    load_mission_catalog,
)


class MissionCatalogTests(unittest.TestCase):
    def test_repository_catalog_has_default_and_commercial_research_missions(self):
        with patch.dict(os.environ, {
            "MISSIONS_FILE": "missions.json",
            "DEFAULT_MISSION_ID": "supplier_outreach",
        }):
            catalog = load_mission_catalog()
        self.assertEqual(catalog.get().id, "supplier_outreach")
        self.assertEqual(catalog.get("market_research").id, "market_research")
        for mission in catalog.missions.values():
            self.assertTrue(mission.objective)
            self.assertTrue(mission.required_information)
            self.assertTrue(mission.relevance_criteria)
            self.assertTrue(mission.follow_up_actions)
            self.assertIn("not as a checklist or script", mission.instructions())
            self.assertIn("do not promise or execute", mission.instructions())

    def test_environment_can_select_catalog_and_default_mission(self):
        with patch.dict(os.environ, {"DEFAULT_MISSION_ID": "market_research"}):
            self.assertEqual(load_mission_catalog().get().id, "market_research")

        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "missions.json"
            catalog_path.write_text(json.dumps({
                "default_mission": "field_research",
                "missions": [{
                    "id": "field_research",
                    "name": "Field research",
                    "objective": "Learn what the contact has observed.",
                    "required_information": [{
                        "key": "observation",
                        "description": "A relevant observation.",
                    }],
                    "relevance_criteria": ["Relevant to the study."],
                    "follow_up_actions": ["Review the finding."],
                }],
            }), encoding="utf-8")
            with patch.dict(os.environ, {"MISSIONS_FILE": str(catalog_path)}):
                self.assertEqual(load_mission_catalog().get().id, "field_research")

    def test_unknown_mission_is_rejected(self):
        with self.assertRaises(MissionNotFoundError):
            load_mission_catalog().get("unknown")
        with self.assertRaises(MissionNotFoundError):
            load_mission_catalog().get({"unexpected": "object"})

    def test_malformed_catalog_and_duplicate_ids_fail_closed(self):
        invalid_catalogs = (
            {"default_mission": "x", "missions": []},
            {"default_mission": "x", "missions": [{
                "id": "x", "name": "X", "objective": "Objective",
                "required_information": [{"key": "a", "description": "A"}],
                "relevance_criteria": ["Relevant"],
                "follow_up_actions": ["Review"],
            }, {
                "id": "x", "name": "X2", "objective": "Objective",
                "required_information": [{"key": "a", "description": "A"}],
                "relevance_criteria": ["Relevant"],
                "follow_up_actions": ["Review"],
            }]},
        )
        for content in invalid_catalogs:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                catalog_path = Path(directory) / "missions.json"
                catalog_path.write_text(json.dumps(content), encoding="utf-8")
                with self.assertRaises(MissionConfigurationError):
                    load_mission_catalog(path=catalog_path)


if __name__ == "__main__":
    unittest.main()
