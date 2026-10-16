"""Phase-one task intent generator. GPT proposes semantics; deterministic code publishes artifacts."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

SCHEMA = "phase1.1"
PROMPT_VERSION = "task-semantics-1.0.0"
SOURCE = {"USER_PROVIDED", "AGENT_GENERATED", "TOOL_DERIVED", "DEFAULT_CONFIG"}
VERBS = {"pick_place": ["pick", "place", "grasp", "放", "抓", "搬"], "press": ["press", "button", "按", "压"], "push": ["push", "推"], "pull": ["pull", "拉"], "rotate": ["rotate", "turn", "旋", "转"], "insert": ["insert", "插"], "wipe": ["wipe", "擦"]}
SIMPLE_SCENE_TEXT = {"left of robot": "left_of_robot", "right of robot": "right_of_robot",
                     "front of robot": "front_of_robot", "behind robot": "behind_robot",
                     "机器人左侧": "left_of_robot", "机器人右侧": "right_of_robot",
                     "机器人前方": "front_of_robot", "机器人后方": "behind_robot"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class EmbodimentRefs:
    result: str
    profile: str
    validation_report: str


@dataclass(frozen=True)
class GenerationRequest:
    schema_version: str
    request_id: str
    robot_bundle_ref: str
    embodiment_refs: EmbodimentRefs
    language_task: str | None = None
    target_object: str | None = None
    scene_constraints: dict[str, Any] | None = None
    template_id: str | None = None
    task_parameters: dict[str, float] = field(default_factory=dict)
    task_count: int = 1


@dataclass
class GenerationResult:
    status: str
    completed_generation_intent: dict | None
    task_spec: dict | None
    success_spec: dict | None
    target_object_spec: dict | None
    scene_constraint_spec: dict | None
    task_generation_report: dict

    def to_dict(self) -> dict:
        return asdict(self)


class GenerationError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


class OpenAIProposer:
    def __init__(self, model: str):
        if not model:
            raise ValueError("model must be configured")
        self.model = model

    def __call__(self, messages: list[dict], parameters: dict) -> tuple[str, dict]:
        if not os.getenv("OPENAI_API_KEY"):
            raise GenerationError("CREDENTIAL_MISSING", "Set OPENAI_API_KEY in the process environment")
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(model=self.model, messages=messages, **parameters)
        raw = response.choices[0].message.content
        if not raw:
            raise GenerationError("MODEL_EMPTY", "The model returned no content")
        return raw, {"response_id": response.id, "usage": response.usage.model_dump() if response.usage else None,
                     "actual_model": response.model}


class TaskGenerationAgent:
    def __init__(self, model: str, proposer: Callable | None = None, max_revisions: int = 2):
        self.model = model
        self.proposer = proposer or OpenAIProposer(model)
        self.max_revisions = max_revisions
        catalog_path = Path(__file__).with_name("templates.json")
        self.catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.catalog_hash = digest(catalog_path)
        self.templates = {t["id"]: t for t in self.catalog["templates"]}

    def _load(self, refs: EmbodimentRefs) -> tuple[dict, dict, dict, dict]:
        paths = {"EmbodimentResult": Path(refs.result), "RobotProfile": Path(refs.profile),
                 "ModelValidationReport": Path(refs.validation_report)}
        artifacts = {k: json.loads(p.read_text(encoding="utf-8")) for k, p in paths.items()}
        result = artifacts["EmbodimentResult"]
        if result.get("status") not in {"PRODUCED", "NEEDS_INFORMATION"}:
            raise GenerationError("UPSTREAM_ERROR", "EmbodimentResult is not usable")
        if any(artifacts[k].get("schema_version") != "1.0" for k in ("RobotProfile", "ModelValidationReport")):
            raise GenerationError("UPSTREAM_SCHEMA", "Unsupported upstream artifact schema")
        if any(artifacts[k].get("job_id") != result.get("job_id") or artifacts[k].get("request_id") != result.get("request_id") for k in ("RobotProfile", "ModelValidationReport")):
            raise GenerationError("UPSTREAM_MISMATCH", "Upstream job/request IDs disagree")
        for ref in result.get("artifact_refs", []):
            kind = ref.get("artifact_type")
            if kind in paths and digest(paths[kind]) != ref.get("sha256"):
                raise GenerationError("HASH_MISMATCH", f"Upstream hash mismatch: {kind}")
        return result, artifacts["RobotProfile"], artifacts["ModelValidationReport"], {k: digest(p) for k, p in paths.items()}

    def _validate_request(self, req: GenerationRequest) -> None:
        if req.schema_version != SCHEMA or not req.request_id or not req.robot_bundle_ref:
            raise GenerationError("SCHEMA_INVALID", "Invalid request version or required identifiers")
        if type(req.task_count) is not int or not 1 <= req.task_count <= 20:
            raise GenerationError("SCHEMA_INVALID", "task_count must be 1..20")
        if req.template_id and req.template_id not in self.templates:
            raise GenerationError("UNSUPPORTED_TASK", req.template_id)
        allowed = {"raw_text", "target_side", "workspace_box_m", "forbidden_regions", "support_relation", "asset_ids"}
        if req.scene_constraints is not None and not isinstance(req.scene_constraints, dict):
            raise GenerationError("SCHEMA_INVALID", "scene_constraints must be an object")
        if req.scene_constraints and set(req.scene_constraints) - allowed:
            raise GenerationError("SCHEMA_INVALID", "Unknown scene constraint field")
        if req.scene_constraints and req.scene_constraints.get("raw_text"):
            raw = req.scene_constraints["raw_text"]
            side = SIMPLE_SCENE_TEXT.get(raw.strip().lower()) if isinstance(raw, str) else None
            if not side:
                raise GenerationError("NEEDS_INFORMATION", "raw_text has no supported deterministic scene interpretation")
            if req.scene_constraints.get("target_side") not in (None, side):
                raise GenerationError("LOCKED_CONFLICT", "raw_text conflicts with target_side")
        if req.scene_constraints and req.scene_constraints.get("target_side") not in (None, "left_of_robot", "right_of_robot", "front_of_robot", "behind_robot"):
            raise GenerationError("SCHEMA_INVALID", "Unsupported target_side")
        if req.language_task is not None and (not isinstance(req.language_task, str) or not req.language_task.strip()):
            raise GenerationError("SCHEMA_INVALID", "language_task must be nonempty text")
        if req.target_object is not None and (not isinstance(req.target_object, str) or not req.target_object.strip()):
            raise GenerationError("SCHEMA_INVALID", "target_object must be nonempty text")
        if not isinstance(req.task_parameters, dict):
            raise GenerationError("SCHEMA_INVALID", "task_parameters must be an object")
        for name, value in req.task_parameters.items():
            if name not in {"margin_m", "travel_m", "distance_m", "angle_rad", "depth_m", "coverage_ratio"} or type(value) not in (int, float) or not 0 < value <= (1 if name == "coverage_ratio" else 100):
                raise GenerationError("SCHEMA_INVALID", f"Invalid task parameter: {name}")

    def _language_template(self, language: str | None) -> str | None:
        if not language:
            return None
        lower = language.lower()
        matches = [k for k, words in VERBS.items() if any(w in lower for w in words)]
        if len(matches) > 1:
            raise GenerationError("LOCKED_CONFLICT", "Language appears to request multiple incompatible templates")
        if not matches:
            raise GenerationError("UNSUPPORTED_TASK", "Language task cannot be mapped deterministically to a supported template")
        return matches[0]

    def _validate_proposal(self, proposal: dict, req: GenerationRequest, language_template: str | None) -> None:
        if set(proposal) != {"template_id", "language_task", "target_object"}:
            raise GenerationError("MODEL_INVALID", "Proposal has missing or unknown fields")
        tid = proposal["template_id"]
        if tid not in self.templates:
            raise GenerationError("UNSUPPORTED_TASK", str(tid))
        if req.template_id and tid != req.template_id or language_template and tid != language_template:
            raise GenerationError("LOCKED_CONFLICT", "Model changed the user task")
        for key in ("language_task", "target_object"):
            if not isinstance(proposal[key], str) or not proposal[key].strip():
                raise GenerationError("MODEL_INVALID", f"Empty {key}")
            locked = getattr(req, key)
            if locked is not None and proposal[key] != locked:
                raise GenerationError("LOCKED_CONFLICT", f"Model changed locked {key}")
        generated_language = self._language_template(proposal["language_task"])
        if generated_language != tid:
            raise GenerationError("MODEL_INVALID", "Generated language does not match template")

    def _compile(self, req: GenerationRequest, proposal: dict, upstream: dict, hashes: dict) -> GenerationResult:
        tid = proposal["template_id"]
        template = self.templates[tid]
        allowed_parameters = {key for item in template["goals"] for key in item["params"]}
        if set(req.task_parameters) - allowed_parameters:
            raise GenerationError("LOCKED_CONFLICT", "User task parameter does not apply to selected template")
        task_id = "task-" + hashlib.sha256(canonical([req.request_id, proposal])).hexdigest()[:16]
        scene_input = dict(req.scene_constraints or {})
        if scene_input.get("raw_text") and "target_side" not in scene_input:
            scene_input["target_side"] = SIMPLE_SCENE_TEXT[scene_input["raw_text"].strip().lower()]
        scene = {"schema_version": SCHEMA, "task_id": task_id, "raw_text": scene_input.get("raw_text"),
                 "target_side": scene_input.get("target_side"), "workspace_box_m": scene_input.get("workspace_box_m"),
                 "forbidden_regions": scene_input.get("forbidden_regions", []), "support_relation": scene_input.get("support_relation"),
                 "asset_ids": scene_input.get("asset_ids", []), "field_sources": {}}
        for key in ("raw_text", "target_side", "workspace_box_m", "forbidden_regions", "support_relation", "asset_ids"):
            scene["field_sources"]["/" + key] = {"source": "USER_PROVIDED" if key in scene_input else "DEFAULT_CONFIG", "source_ref": ("/scene_constraints/raw_text" if key == "target_side" and key not in (req.scene_constraints or {}) else "/scene_constraints/" + key) if key in scene_input else self.catalog["catalog_version"]}
        target = {"schema_version": SCHEMA, "task_id": task_id, "asset_class": proposal["target_object"],
                  "semantic_attributes": {}, "hard_attributes": {}, "field_sources": {"/asset_class": {"source": "USER_PROVIDED" if req.target_object else "AGENT_GENERATED", "source_ref": "/target_object" if req.target_object else f"model:{PROMPT_VERSION}"}}}
        for key in ("semantic_attributes", "hard_attributes"):
            target["field_sources"]["/" + key] = {"source": "DEFAULT_CONFIG", "source_ref": self.catalog["catalog_version"]}
        predicates = []
        criteria = []
        for index, item in enumerate(template["goals"]):
            param_key = next(iter(item["params"]), None)
            chosen = dict(item["params"])
            source = "DEFAULT_CONFIG"
            if param_key in req.task_parameters:
                chosen[param_key] = req.task_parameters[param_key]
                source = "USER_PROVIDED"
            pid = f"goal-{index+1}"
            predicates.append({"id": pid, "type": item["type"], "args": item["args"], "params": chosen, "frame": item["frame"]})
            threshold = chosen[param_key] if param_key else item["threshold"]
            criteria.append({"predicate_id": pid, "metric": item["metric"], "comparator": item["comparator"],
                             "threshold": threshold, "unit": item["unit"], "frame": item["frame"],
                             "hold_s": item["hold_s"], "evidence": item["evidence"], "missing_policy": "UNKNOWN",
                             "threshold_source": {"source": source, "source_ref": f"/task_parameters/{param_key}" if source == "USER_PROVIDED" else f"templates.json#{tid}@{template['version']}/goals/{index}"}})
        initial = [{"id": f"initial-{i+1}", **p} for i, p in enumerate(template["initial"])]
        language_source = "USER_PROVIDED" if req.language_task else "AGENT_GENERATED"
        task = {"schema_version": SCHEMA, "task_id": task_id, "template_id": tid, "template_version": template["version"],
                "original_language": req.language_task, "normalized_language": proposal["language_task"],
                "roles": template["roles"], "initial_predicates": initial, "goal_predicates": predicates,
                "process_constraints": [], "required_capabilities": template["required_capabilities"],
                "required_skills": template["required_skills"], "time_limit_s": None,
                "sampling_bounds": {"task_count": req.task_count},
                "field_sources": {"/original_language": {"source": language_source, "source_ref": "/language_task" if req.language_task else f"model:{PROMPT_VERSION}"},
                                  "/normalized_language": {"source": language_source, "source_ref": "/language_task" if req.language_task else f"model:{PROMPT_VERSION}"},
                                  "/roles": {"source": "DEFAULT_CONFIG", "source_ref": f"templates.json#{tid}@{template['version']}"}},
                "locked_fields": (["/original_language", "/normalized_language"] if req.language_task else [])}
        for key in ("template_id", "template_version", "initial_predicates", "goal_predicates", "process_constraints", "required_capabilities", "required_skills", "time_limit_s"):
            task["field_sources"]["/" + key] = {"source": "DEFAULT_CONFIG", "source_ref": f"templates.json#{tid}@{template['version']}"}
        task["field_sources"]["/sampling_bounds/task_count"] = {"source": "USER_PROVIDED", "source_ref": "/task_count"}
        for i, criterion in enumerate(criteria):
            task["field_sources"][f"/goal_predicates/{i}/params"] = criterion["threshold_source"]
        success = {"schema_version": SCHEMA, "task_id": task_id, "criteria": criteria}
        locked = []
        for key in ("language_task", "target_object", "template_id"):
            if getattr(req, key) is not None:
                locked.append("/" + key)
        locked += ["/scene_constraints/" + k for k in scene_input]
        locked += ["/task_parameters/" + k for k in req.task_parameters]
        intent = {"schema_version": SCHEMA, "intent_id": "intent-" + task_id, "request_id": req.request_id,
                  "task_id": task_id, "target_object_spec_ref": "#/target_object_spec", "scene_constraint_spec_ref": "#/scene_constraint_spec",
                  "task_spec_ref": "#/task_spec", "success_spec_ref": "#/success_spec",
                  "input_hash": hashlib.sha256(canonical(asdict(req))).hexdigest(), "field_sources": {}, "locked_fields": locked}
        intent["field_sources"] = {"/language_task": task["field_sources"]["/original_language"],
                                   "/target_object": target["field_sources"]["/asset_class"],
                                   "/scene_constraints": {"source": "USER_PROVIDED" if scene_input else "DEFAULT_CONFIG", "source_ref": "/scene_constraints" if scene_input else self.catalog["catalog_version"]},
                                   "/template_id": {"source": "USER_PROVIDED" if req.template_id else "AGENT_GENERATED", "source_ref": "/template_id" if req.template_id else f"model:{PROMPT_VERSION}"}}
        report = {"schema_version": SCHEMA, "report_id": "report-" + task_id, "request_id": req.request_id,
                  "status": "PRODUCED", "template_id": tid, "generated_fields": [k for k in ("language_task", "target_object") if getattr(req, k) is None],
                  "normalized_fields": ["task_spec", "success_spec"], "conflicts": [],
                  "evidence_refs": [asdict(req.embodiment_refs)], "upstream_status": upstream["status"],
                  "upstream_missing": upstream.get("missing", []), "input_artifact_hashes": hashes,
                  "unverified_assumptions": ["Asset availability, task feasibility and Gate A remain unverified", "Template thresholds require downstream asset compatibility"],
                  "template_catalog_version": self.catalog["catalog_version"], "template_catalog_sha256": self.catalog_hash}
        result = GenerationResult("PRODUCED", intent, task, success, target, scene, report)
        self.validate(result, req)
        return result

    def validate(self, result: GenerationResult, req: GenerationRequest) -> None:
        task, success, target, scene = result.task_spec, result.success_spec, result.target_object_spec, result.scene_constraint_spec
        if not all(isinstance(x, dict) for x in (task, success, target, scene, result.completed_generation_intent)):
            raise GenerationError("VALIDATION_FAILED", "Missing output structure")
        if set(result.to_dict()) != {"status", "completed_generation_intent", "task_spec", "success_spec", "target_object_spec", "scene_constraint_spec", "task_generation_report"}:
            raise GenerationError("VALIDATION_FAILED", "Output structure mismatch")
        if any(x.get("schema_version") != SCHEMA for x in (task, success, target, scene, result.completed_generation_intent, result.task_generation_report)):
            raise GenerationError("VALIDATION_FAILED", "Output schema version mismatch")
        if len({task["task_id"], success["task_id"], target["task_id"], scene["task_id"], result.completed_generation_intent["task_id"]}) != 1:
            raise GenerationError("VALIDATION_FAILED", "Task IDs disagree")
        template = self.templates.get(task["template_id"])
        if not template or task["template_version"] != template["version"] or task["roles"] != template["roles"] or task["required_skills"] != template["required_skills"] or task["required_capabilities"] != template["required_capabilities"]:
            raise GenerationError("VALIDATION_FAILED", "Template contract mismatch")
        if req.language_task is not None and (task["original_language"] != req.language_task or task["normalized_language"] != req.language_task):
            raise GenerationError("VALIDATION_FAILED", "Locked language changed")
        if req.target_object is not None and target["asset_class"] != req.target_object:
            raise GenerationError("VALIDATION_FAILED", "Locked target changed")
        for k, v in (req.scene_constraints or {}).items():
            if scene[k] != v:
                raise GenerationError("VALIDATION_FAILED", "Locked scene constraint changed")
        ids = set()
        for p in task["initial_predicates"] + task["goal_predicates"] + task["process_constraints"]:
            if set(p) != {"id", "type", "args", "params", "frame"} or p["id"] in ids or not set(p["args"]) <= set(task["roles"]) or p["frame"] not in task["roles"]:
                raise GenerationError("VALIDATION_FAILED", "Invalid predicate ID, role or frame")
            ids.add(p["id"])
        if len(task["goal_predicates"]) != len(template["goals"]) or len(success["criteria"]) != len(template["goals"]):
            raise GenerationError("VALIDATION_FAILED", "Missing success condition")
        for i, (p, c, defined) in enumerate(zip(task["goal_predicates"], success["criteria"], template["goals"])):
            if (p["type"], p["args"], p["frame"]) != (defined["type"], defined["args"], defined["frame"]) or set(p["params"]) != set(defined["params"]):
                raise GenerationError("VALIDATION_FAILED", "Goal does not match template")
            expected = next(iter(p["params"].values()), defined["threshold"])
            if c["predicate_id"] != p["id"] or c["threshold"] != expected or c["metric"] != defined["metric"] or c["comparator"] != defined["comparator"] or c["unit"] != defined["unit"] or c["frame"] != p["frame"] or c["hold_s"] != defined["hold_s"] or c["evidence"] != defined["evidence"] or c["missing_policy"] != "UNKNOWN":
                raise GenerationError("VALIDATION_FAILED", "Success condition is not traceable")
            if set(c) != {"predicate_id", "metric", "comparator", "threshold", "unit", "frame", "hold_s", "evidence", "missing_policy", "threshold_source"} or c["comparator"] not in {">=", "<=", "=="} or c["hold_s"] < 0:
                raise GenerationError("VALIDATION_FAILED", "Invalid success criterion structure")
            source = c["threshold_source"]
            if source["source"] not in SOURCE or (source["source"] == "USER_PROVIDED") != (next(iter(defined["params"]), None) in req.task_parameters):
                raise GenerationError("VALIDATION_FAILED", "Threshold provenance mismatch")
        if any(p["type"] != q["type"] or p["args"] != q["args"] for p, q in zip(task["initial_predicates"], template["initial"])):
            raise GenerationError("VALIDATION_FAILED", "Initial predicates mismatch")
        if not all(isinstance(c["threshold"], (int, float)) and c["threshold"] == c["threshold"] for c in success["criteria"]):
            raise GenerationError("VALIDATION_FAILED", "Invalid threshold")
        for obj in (task, target, scene, result.completed_generation_intent):
            if any(item.get("source") not in SOURCE or not item.get("source_ref") for item in obj["field_sources"].values()):
                raise GenerationError("VALIDATION_FAILED", "Invalid field provenance")

    def run(self, req: GenerationRequest, audit_path: str | None = None) -> GenerationResult:
        audit: dict[str, Any] = {"prompt_version": PROMPT_VERSION, "model_requested": self.model,
                                 "started_at_utc": datetime.now(timezone.utc).isoformat(),
                                 "request_parameters": {"response_format": {"type": "json_object"}},
                                 "attempts": []}
        try:
            self._validate_request(req)
            upstream, profile, validation, hashes = self._load(req.embodiment_refs)
            audit["input_artifact_hashes"] = hashes
            audit["generation_request_sha256"] = hashlib.sha256(canonical(asdict(req))).hexdigest()
            audit["template_catalog_sha256"] = self.catalog_hash
            language_template = self._language_template(req.language_task)
            if req.template_id and language_template and req.template_id != language_template:
                raise GenerationError("LOCKED_CONFLICT", "Template conflicts with locked language")
            facts = {"rotor_count": len(profile.get("quadrotor", {}).get("rotors", [])),
                     "arm_joint_names": profile.get("manipulator", {}).get("joint_order", []),
                     "body_frame": profile.get("quadrotor", {}).get("body_frame"),
                     "max_thrust_known": profile.get("quadrotor", {}).get("max_thrust") is not None,
                     "rated_payload_known": profile.get("end_effector", {}).get("rated_payload") is not None,
                     "upstream_status": upstream["status"], "missing": upstream.get("missing", [])}
            system = ("You propose task semantics for a phase-one aerial manipulation intent. Return ONLY a JSON object "
                      "with exactly template_id, language_task, target_object. Pick one enabled template. "
                      "Copy every provided language_task and target_object byte-for-byte; fill only null fields. "
                      "Do not claim physical feasibility, asset existence, thrust, payload or Gate A. "
                      "Do not include numeric physics or success thresholds. Unknown robot facts remain unknown.")
            payload = {"request": {"language_task": req.language_task, "target_object": req.target_object,
                                   "template_id": req.template_id, "scene_constraints": req.scene_constraints},
                       "suggested_template_from_language": language_template, "enabled_templates": list(self.templates),
                       "confirmed_or_unknown_robot_facts": facts}
            messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
            audit["request_messages"] = messages.copy()
            audit["prompt_sha256"] = hashlib.sha256(canonical(messages)).hexdigest()
            for attempt in range(self.max_revisions + 1):
                raw, metadata = self.proposer(messages, audit["request_parameters"])
                item = {"raw_response": raw, "response_metadata": metadata}
                audit["attempts"].append(item)
                try:
                    proposal = json.loads(raw)
                    self._validate_proposal(proposal, req, language_template)
                    result = self._compile(req, proposal, upstream, hashes)
                    item["validation"] = "PASS"
                    audit["status"] = "PRODUCED"
                    audit["output_sha256"] = hashlib.sha256(canonical(result.to_dict())).hexdigest()
                    return result
                except (ValueError, TypeError, GenerationError) as exc:
                    item["validation"] = "FAIL"
                    item["error"] = str(exc)
                    if attempt == self.max_revisions:
                        raise GenerationError("MODEL_INVALID", f"Model response failed validation after {attempt+1} attempts: {exc}") from exc
                    messages.extend([{"role": "assistant", "content": raw}, {"role": "user", "content": f"Validation failed: {exc}. Return a corrected JSON object with the same locked user values."}])
            raise AssertionError("unreachable")
        except (GenerationError, OSError, json.JSONDecodeError) as exc:
            code = exc.code if isinstance(exc, GenerationError) else "INPUT_ERROR"
            audit["status"] = code
            result = GenerationResult(code, None, None, None, None, None,
                {"schema_version": SCHEMA, "request_id": req.request_id, "status": code, "conflicts": [str(exc)],
                 "generated_fields": [], "normalized_fields": [], "evidence_refs": []})
            return result
        except Exception as exc:
            code = "MODEL_CALL_FAILED"
            audit["status"] = code
            audit["error_type"] = type(exc).__name__
            return GenerationResult(code, None, None, None, None, None,
                {"schema_version": SCHEMA, "request_id": req.request_id, "status": code,
                 "conflicts": [f"Model call or internal processing failed: {type(exc).__name__}"],
                 "generated_fields": [], "normalized_fields": [], "evidence_refs": []})
        finally:
            if audit_path:
                path = Path(audit_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def replay(audit_path: str, req: GenerationRequest) -> GenerationResult:
    """Validate the frozen original response without an API call."""
    audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    rows = iter(audit["attempts"])
    agent = TaskGenerationAgent(audit["model_requested"], proposer=lambda _m, _p: (lambda row: (row["raw_response"], row.get("response_metadata", {})))(next(rows)), max_revisions=len(audit["attempts"])-1)
    result = agent.run(req)
    if result.status == "PRODUCED" and hashlib.sha256(canonical(result.to_dict())).hexdigest() != audit.get("output_sha256"):
        raise GenerationError("REPLAY_MISMATCH", "Frozen response no longer compiles to same result")
    return result
