import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from asset_scene.pipeline import (
    AssetSceneAgent,
    TEMPLATES,
    isaac_subprocess_environment,
    resolve_isaac_python,
    validate,
)


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "assets" / "cache" / "kenney_factory-kit_3.0.zip"


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.task = {
            "status": "PRODUCED",
            "completed_generation_intent": {"task_id": "case-a"},
            "task_spec": {"task_id": "case-a", "template_id": "press"},
            "success_spec": {"task_id": "case-a", "criteria": [{"threshold": 0.005}]},
            "target_object_spec": {"task_id": "case-a", "asset_class": "button", "semantic_attributes": {}, "hard_attributes": {}},
            "scene_constraint_spec": {"task_id": "case-a", "raw_text": None, "target_side": None,
                                      "workspace_box_m": None, "forbidden_regions": [], "support_relation": None, "asset_ids": []},
        }
        self.profile = {"model_entry": "robot.usda", "stage": {"meters_per_unit": {"value": 1.0}}}

    def test_all_template_ids_are_explicit(self):
        self.assertEqual(set(TEMPLATES), {"pick_place", "press", "push", "pull", "rotate", "insert", "wipe"})

    def test_task_id_conflict_is_rejected(self):
        self.task["success_spec"]["task_id"] = "other"
        with self.assertRaisesRegex(ValueError, "task_id mismatch"):
            validate(self.task, self.profile)

    def test_capability_model_not_required(self):
        self.assertEqual(validate(self.task, self.profile), ("case-a", "press"))

    def test_unimplemented_constraint_yields_failure_without_editing_input(self):
        self.task["scene_constraint_spec"]["target_side"] = "left"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_path, profile_path = root / "task.json", root / "profile.json"
            task_path.write_text(json.dumps(self.task), encoding="utf-8")
            profile_path.write_text(json.dumps(self.profile), encoding="utf-8")
            result = AssetSceneAgent(root / "absent_isaac", root / "out").run(task_path, profile_path)
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["failure"]["type"], "NotImplementedError")
            self.assertEqual(json.loads(task_path.read_text())["scene_constraint_spec"]["target_side"], "left")

    def test_explicit_isaac_python_is_accepted(self):
        self.assertEqual(resolve_isaac_python(isaac_python=sys.executable), Path(sys.executable).resolve())

    @unittest.skipIf(os.name == "nt", "Linux Isaac runtime path layout")
    def test_linux_isaac_root_resolves_conda_python(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "bin" / "python").symlink_to(sys.executable)
            self.assertEqual(resolve_isaac_python(root), Path(sys.executable).resolve())

    @unittest.skipIf(os.name == "nt", "Linux dynamic-library search path")
    def test_isaac_runtime_library_path_is_preferred(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix = Path(temp)
            (prefix / "bin").mkdir()
            (prefix / "lib").mkdir()
            env = isaac_subprocess_environment(prefix / "bin" / "python", {"LD_LIBRARY_PATH": "/usr/lib"})
            self.assertEqual(env["LD_LIBRARY_PATH"].split(os.pathsep), [str(prefix / "lib"), "/usr/lib"])


class FixedAssetTests(unittest.TestCase):
    @unittest.skipUnless(ARCHIVE.exists(), "real downloaded archive unavailable")
    def test_downloaded_source_has_mesh_and_license(self):
        with zipfile.ZipFile(ARCHIVE) as bundle:
            text = bundle.read("License.txt").decode("utf-8")
            obj = bundle.read("Models/OBJ format/button-floor-round.obj").decode("utf-8")
        self.assertIn("Creative Commons Zero", text)
        self.assertIn("commercial purposes", text)
        self.assertGreater(sum(line.startswith("v ") for line in obj.splitlines()), 20)
        self.assertGreater(sum(line.startswith("f ") for line in obj.splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
