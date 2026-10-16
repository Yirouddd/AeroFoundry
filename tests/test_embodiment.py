import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from embodiment_agent import EmbodimentRequest, run


class AgentTests(unittest.TestCase):
    def _call(self, root, out):
        return run(EmbodimentRequest("1.0", "job", "request", str(root), str(out)))

    def test_normal_run_keeps_unknown_numeric_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "model", Path(tmp) / "out"
            root.mkdir()
            (root / "robot.usda").write_text('''#usda 1.0
(defaultPrim = "Robot"
 metersPerUnit = 1
 kilogramsPerUnit = 1)
def Xform "Robot" {
 def Xform "body" (
  prepend apiSchemas = ["PhysicsMassAPI"]
 ) {
  float physics:mass = 2
 }
}
''')
            result = self._call(root, out)
            self.assertEqual(result.status, "NEEDS_INFORMATION", result.errors)
            profile = json.loads((out / "RobotProfile.json").read_text())
            self.assertIsNone(profile["quadrotor"]["max_thrust"])
            self.assertIsNone(profile["end_effector"]["rated_payload"])
            self.assertIsNone(profile["end_effector"]["tcp"])
            self.assertNotIn("end_effector.tcp", result.missing)
            self.assertEqual(profile["links"][0]["mass"]["value"], 2)
            self.assertIsNone(profile["links"][0]["diagonal_inertia"])

    def test_broken_reference_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "model", Path(tmp) / "out"
            root.mkdir()
            (root / "robot.usda").write_text('''#usda 1.0
(defaultPrim = "Robot")
def Xform "Robot" (prepend references = @missing.usda@) {}
''')
            result = self._call(root, out)
            self.assertEqual(result.status, "ERROR")
            report = json.loads((out / "ModelValidationReport.json").read_text())
            self.assertTrue(report["errors"])

    def test_missing_fields_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "model", Path(tmp) / "out"
            root.mkdir()
            (root / "robot.usda").write_text('#usda 1.0\n(defaultPrim = "Robot")\ndef Xform "Robot" {}\n')
            result = self._call(root, out)
            self.assertEqual(result.status, "NEEDS_INFORMATION")
            self.assertNotIn("end_effector.tcp", result.missing)
            report = json.loads((out / "ModelValidationReport.json").read_text())
            self.assertIn("end_effector.tcp", [x["field"] for x in report["optional_unset"]])
            profile = json.loads((out / "RobotProfile.json").read_text())
            self.assertEqual(profile["links"], [])

    def test_tool_failure_reports_error_and_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "model", Path(tmp) / "out"
            root.mkdir()
            (root / "robot.usda").write_text('#usda 1.0\n(defaultPrim = "Robot")\ndef Xform "Robot" {}\n')
            from embodiment_agent import agent
            actual = agent._sha256
            calls = 0
            def fail_once(path):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("hash tool failed")
                return actual(path)
            with patch("embodiment_agent.agent._sha256", side_effect=fail_once):
                result = self._call(root, out)
            self.assertEqual(result.status, "ERROR")
            self.assertEqual(len(result.artifact_refs), 2)
            self.assertIn("hash tool failed", result.errors[0])


if __name__ == "__main__":
    unittest.main()
