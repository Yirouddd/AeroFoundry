import json
import tempfile
import unittest
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

from task_generation import EmbodimentRefs, GenerationRequest, TaskGenerationAgent
from task_generation.agent import OpenAIProposer, replay

ROOT = Path(__file__).resolve().parent / "fixtures" / "embodiment"
REFS = EmbodimentRefs(str(ROOT / "EmbodimentResult.json"), str(ROOT / "RobotProfile.json"), str(ROOT / "ModelValidationReport.json"))


def request(**kwargs):
    return GenerationRequest("phase1.1", "test-request", "existing-robot-bundle", REFS, **kwargs)


class Proposer:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def __call__(self, messages, parameters):
        self.calls += 1
        return json.dumps(self.response, ensure_ascii=False) if isinstance(self.response, dict) else self.response, {"response_id": "mock"}


class AgentTests(unittest.TestCase):
    def test_robot_only_and_upstream_needs_information(self):
        p = Proposer({"template_id": "pick_place", "language_task": "Pick and place a small object", "target_object": "small graspable rigid object"})
        result = TaskGenerationAgent("mock", p).run(request())
        self.assertEqual(result.status, "PRODUCED")
        self.assertEqual(result.task_generation_report["upstream_status"], "NEEDS_INFORMATION")
        self.assertIn("quadrotor.max_thrust", result.task_generation_report["upstream_missing"])
        self.assertEqual(result.success_spec["criteria"][0]["threshold_source"]["source"], "DEFAULT_CONFIG")
        self.assertEqual(result.task_spec["roles"], TaskGenerationAgent("mock", p).templates["pick_place"]["roles"])

    def test_partial_conditions_locked(self):
        p = Proposer({"template_id": "push", "language_task": "Push the red cube", "target_object": "red cube"})
        req = request(language_task="Push the red cube", target_object="red cube", task_parameters={"distance_m": 0.12})
        result = TaskGenerationAgent("mock", p).run(req)
        self.assertEqual(result.status, "PRODUCED")
        self.assertEqual(result.task_spec["original_language"], req.language_task)
        self.assertEqual(result.target_object_spec["asset_class"], req.target_object)
        self.assertEqual(result.success_spec["criteria"][0]["threshold"], 0.12)
        self.assertEqual(result.success_spec["criteria"][0]["threshold_source"]["source"], "USER_PROVIDED")

    def test_all_conditions_locked(self):
        p = Proposer({"template_id": "press", "language_task": "Press the blue button", "target_object": "blue button"})
        req = request(language_task="Press the blue button", target_object="blue button", template_id="press", scene_constraints={"target_side":"left_of_robot", "asset_ids":["button-1"]}, task_parameters={"travel_m":0.008})
        result = TaskGenerationAgent("mock", p).run(req)
        self.assertEqual(result.status, "PRODUCED")
        self.assertEqual(result.scene_constraint_spec["target_side"], "left_of_robot")
        self.assertIn("/scene_constraints/asset_ids", result.completed_generation_intent["locked_fields"])

    def test_conflicting_locked_conditions(self):
        p = Proposer({"template_id":"push", "language_task":"Push the cube", "target_object":"cube"})
        result = TaskGenerationAgent("mock", p).run(request(language_task="Push the cube", template_id="pull"))
        self.assertEqual(result.status, "LOCKED_CONFLICT")
        self.assertEqual(p.calls, 0)

    def test_model_changes_lock_and_bounded_repair(self):
        p = Proposer({"template_id":"push", "language_task":"Push something else", "target_object":"cube"})
        result = TaskGenerationAgent("mock", p, max_revisions=2).run(request(language_task="Push the cube", target_object="cube"))
        self.assertEqual(result.status, "MODEL_INVALID")
        self.assertEqual(p.calls, 3)
        self.assertIsNone(result.task_spec)

    def test_unsupported_task(self):
        p = Proposer({"template_id":"pick_place", "language_task":"Pick cube", "target_object":"cube"})
        result = TaskGenerationAgent("mock", p).run(request(template_id="weld"))
        self.assertEqual(result.status, "UNSUPPORTED_TASK")
        self.assertEqual(p.calls, 0)

    def test_raw_scene_constraint_requires_structure(self):
        p = Proposer({"template_id":"push", "language_task":"Push cube", "target_object":"cube"})
        result = TaskGenerationAgent("mock", p).run(request(scene_constraints={"raw_text":"Keep clear of people"}))
        self.assertEqual(result.status, "NEEDS_INFORMATION")

    def test_supported_raw_scene_constraint_is_compiled(self):
        p = Proposer({"template_id":"push", "language_task":"Push cube", "target_object":"cube"})
        result = TaskGenerationAgent("mock", p).run(request(scene_constraints={"raw_text":"机器人左侧"}))
        self.assertEqual(result.status, "PRODUCED")
        self.assertEqual(result.scene_constraint_spec["target_side"], "left_of_robot")
        self.assertEqual(result.scene_constraint_spec["field_sources"]["/target_side"]["source_ref"], "/scene_constraints/raw_text")

    def test_frozen_response_replay(self):
        p = Proposer({"template_id":"rotate", "language_task":"Rotate the dial", "target_object":"dial"})
        req = request()
        with tempfile.TemporaryDirectory() as directory:
            audit = str(Path(directory) / "audit.json")
            result = TaskGenerationAgent("mock", p).run(req, audit)
            self.assertEqual(replay(audit, req).to_dict(), result.to_dict())
            self.assertEqual(p.calls, 1)

    def test_openai_sdk_call_path_without_network(self):
        calls = []
        class Client:
            def __init__(self):
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
            def create(self, **kwargs):
                calls.append(kwargs)
                return types.SimpleNamespace(id="sdk-mock-id", model="configured-model", usage=None,
                    choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps({"template_id":"press", "language_task":"Press button", "target_object":"button"})))])
        with patch.dict(os.environ, {"OPENAI_API_KEY":"test-only-not-real"}), patch.dict(sys.modules, {"openai":types.SimpleNamespace(OpenAI=Client)}):
            result = TaskGenerationAgent("configured-model", OpenAIProposer("configured-model")).run(request())
        self.assertEqual(result.status, "PRODUCED")
        self.assertEqual(calls[0]["model"], "configured-model")
        self.assertEqual(calls[0]["response_format"], {"type":"json_object"})
        self.assertNotIn("temperature", calls[0])

    def test_all_templates_compile(self):
        for tid in ("pick_place", "press", "push", "pull", "rotate", "insert", "wipe"):
            with self.subTest(tid=tid):
                p = Proposer({"template_id":tid, "language_task": {"pick_place":"Pick and place", "press":"Press", "push":"Push", "pull":"Pull", "rotate":"Rotate", "insert":"Insert", "wipe":"Wipe"}[tid] + " object", "target_object":"object"})
                self.assertEqual(TaskGenerationAgent("mock", p).run(request()).status, "PRODUCED")


if __name__ == "__main__":
    unittest.main()
