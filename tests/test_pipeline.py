import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aerial_vla_pipeline import PipelineRequest, run
from aerial_vla_pipeline.pipeline import _expanded_path
from embodiment_agent import EmbodimentResult


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "embodiment"


class PipelineTests(unittest.TestCase):
    def test_request_paths_expand_linux_environment_variables(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AEROFOUNDRY_TEST_ROOT": temp}):
            self.assertEqual(_expanded_path("$AEROFOUNDRY_TEST_ROOT/model").parent, Path(temp))

    def test_task_result_is_passed_to_asset_scene(self):
        saved = json.loads((FIXTURE / "EmbodimentResult.json").read_text(encoding="utf-8"))

        def fake_embodiment(request):
            out = Path(request.output_directory)
            out.mkdir(parents=True)
            for name in ("RobotProfile.json", "ModelValidationReport.json"):
                shutil.copy2(FIXTURE / name, out / name)
            return EmbodimentResult(**saved)

        proposal = json.dumps({"template_id": "press", "language_task": "Press a button.",
                               "target_object": "button"})
        with tempfile.TemporaryDirectory() as tmp, patch(
            "aerial_vla_pipeline.pipeline.run_embodiment", side_effect=fake_embodiment
        ), patch("aerial_vla_pipeline.pipeline.AssetSceneAgent") as scene_class:
            def fake_scene(task_result, robot_profile, **kwargs):
                task = json.loads(Path(task_result).read_text(encoding="utf-8"))
                task_id = task["task_spec"]["task_id"]
                self.assertEqual(kwargs["embodiment_result"].name, "EmbodimentResult.json")
                self.assertEqual(Path(robot_profile).name, "RobotProfile.json")
                out = Path(tmp) / "asset_scene" / "scenes" / task_id
                out.mkdir(parents=True)
                (out / "result.json").write_text(json.dumps({"task_id": task_id, "status": "PASSED"}))
                (out / "scene.usd").write_text("#usda 1.0\n")
                return {"task_id": task_id, "status": "PASSED"}

            scene_class.return_value.run.side_effect = fake_scene
            request = PipelineRequest(
                job_id="swan-gamma-v2", request_id="actual-model",
                model_directory="model-is-read-by-the-mocked-upstream",
                output_directory=tmp, isaac_root="fake-isaac",
            )
            result = run(request, proposer=lambda _m, _p: (proposal, {"source": "test"}))
            self.assertEqual(result.status, "SCENE_BUILT")
            self.assertEqual(result.scene_status, "PASSED")
            self.assertIn("SceneUSD", result.artifact_refs)

    def test_existing_artifact_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            for name in ("EmbodimentResult.json", "RobotProfile.json", "ModelValidationReport.json"):
                shutil.copy2(FIXTURE / name, source / name)
            with (source / "RobotProfile.json").open("a", encoding="utf-8") as stream:
                stream.write(" ")
            request = PipelineRequest(
                job_id="swan-gamma-v2", request_id="actual-model",
                model_directory="unused-in-reuse-mode",
                output_directory=str(Path(tmp) / "out"),
                embodiment_directory=str(source),
            )
            with self.assertRaisesRegex(ValueError, "hash check"):
                run(request, proposer=lambda _m, _p: ("{}", {}))

    def test_embodiment_artifacts_feed_task_agent(self):
        saved = json.loads((FIXTURE / "EmbodimentResult.json").read_text(encoding="utf-8"))

        def fake_embodiment(request):
            out = Path(request.output_directory)
            out.mkdir(parents=True)
            for name in ("RobotProfile.json", "ModelValidationReport.json"):
                shutil.copy2(FIXTURE / name, out / name)
            return EmbodimentResult(**saved)

        proposal = json.dumps({"template_id": "press", "language_task": "Press a button.",
                               "target_object": "button"})
        with tempfile.TemporaryDirectory() as tmp, patch(
            "aerial_vla_pipeline.pipeline.run_embodiment", side_effect=fake_embodiment
        ):
            request = PipelineRequest(
                job_id="swan-gamma-v2", request_id="actual-model",
                model_directory="model-is-read-by-the-mocked-upstream",
                output_directory=tmp,
            )
            result = run(request, proposer=lambda _m, _p: (proposal, {"source": "test"}))
            self.assertEqual(result.status, "TASK_INTENT_PRODUCED")
            self.assertEqual(result.embodiment_status, "NEEDS_INFORMATION")
            self.assertEqual(result.task_status, "PRODUCED")
            task = json.loads((Path(tmp) / "task_generation" / "GenerationResult.json").read_text())
            self.assertEqual(task["task_spec"]["template_id"], "press")
            self.assertEqual(task["task_generation_report"]["upstream_status"], "NEEDS_INFORMATION")
            self.assertIn("quadrotor.max_thrust", result.missing)
            self.assertNotIn("gate_a", result.artifact_refs)


if __name__ == "__main__":
    unittest.main()
