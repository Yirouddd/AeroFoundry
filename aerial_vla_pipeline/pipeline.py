"""Thin integration layer; neither agent owns the other's implementation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from embodiment_agent import EmbodimentRequest, EmbodimentResult, run as run_embodiment
from task_generation import EmbodimentRefs, GenerationRequest, TaskGenerationAgent
from asset_scene import AssetSceneAgent


@dataclass(frozen=True)
class PipelineRequest:
    job_id: str
    request_id: str
    model_directory: str
    output_directory: str
    entry_file: str | None = None
    embodiment_directory: str | None = None
    isaac_root: str | None = None
    cached_asset_archive: str | None = None
    language_task: str | None = None
    target_object: str | None = None
    scene_constraints: dict[str, Any] | None = None
    template_id: str | None = None
    task_parameters: dict[str, float] = field(default_factory=dict)
    task_count: int = 1


@dataclass
class PipelineResult:
    status: str
    embodiment_status: str
    task_status: str | None
    scene_status: str | None
    artifact_refs: dict[str, dict[str, str]]
    missing: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _ref(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _reuse_embodiment(source: Path, destination: Path, request: PipelineRequest) -> EmbodimentResult:
    """Copy a prior verified agent result without modifying its original files."""
    saved = json.loads((source / "EmbodimentResult.json").read_text(encoding="utf-8"))
    if saved.get("job_id") != request.job_id or saved.get("request_id") != request.request_id:
        raise ValueError("Existing embodiment job_id/request_id do not match the pipeline request")
    if saved.get("status") not in {"PRODUCED", "NEEDS_INFORMATION", "ERROR"}:
        raise ValueError("Existing embodiment result has an unsupported status")
    expected = {item.get("artifact_type"): item.get("sha256") for item in saved.get("artifact_refs", [])}
    for name, kind in (("RobotProfile.json", "RobotProfile"),
                       ("ModelValidationReport.json", "ModelValidationReport")):
        original = source / name
        if kind not in expected or _ref(original)["sha256"] != expected[kind]:
            raise ValueError(f"Existing embodiment artifact failed hash check: {kind}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("RobotProfile.json", "ModelValidationReport.json"):
        (destination / name).write_bytes((source / name).read_bytes())
    saved["artifact_refs"] = [
        {"artifact_type": kind, "schema_version": "1.0", **_ref(destination / name)}
        for name, kind in (("RobotProfile.json", "RobotProfile"),
                           ("ModelValidationReport.json", "ModelValidationReport"))
    ]
    saved["evidence_refs"] = [str((destination / "ModelValidationReport.json").resolve())]
    return EmbodimentResult(**saved)


def run(request: PipelineRequest, *, model: str | None = None,
        proposer: Callable | None = None) -> PipelineResult:
    """Produce a task intent from one USD model, preserving both agents' results.

    A supplied proposer supports offline tests. Otherwise ``model`` is passed to
    TaskGenerationAgent for a real model call. This function never runs Gate A.
    """
    if not request.job_id or not request.request_id:
        raise ValueError("job_id and request_id are required")
    if proposer is None and not model:
        raise ValueError("model is required unless a proposer is supplied")

    root = Path(request.output_directory).resolve()
    embodiment_dir = root / "embodiment"
    task_dir = root / "task_generation"
    if request.embodiment_directory:
        embodiment = _reuse_embodiment(Path(request.embodiment_directory), embodiment_dir, request)
    else:
        embodiment = run_embodiment(EmbodimentRequest(
            interface_version="1.0", job_id=request.job_id,
            request_id=request.request_id, model_directory=request.model_directory,
            output_directory=str(embodiment_dir), entry_file=request.entry_file,
        ))
    embodiment_result_path = embodiment_dir / "EmbodimentResult.json"
    _save(embodiment_result_path, embodiment.to_dict())
    refs = {
        "EmbodimentResult": _ref(embodiment_result_path),
        "RobotProfile": _ref(embodiment_dir / "RobotProfile.json"),
        "ModelValidationReport": _ref(embodiment_dir / "ModelValidationReport.json"),
    }
    if embodiment.status == "ERROR":
        result = PipelineResult("EMBODIMENT_ERROR", embodiment.status, None, None, refs, embodiment.missing)
    else:
        generation_request = GenerationRequest(
            schema_version="phase1.1", request_id=request.request_id,
            robot_bundle_ref=str(Path(request.model_directory).resolve()),
            embodiment_refs=EmbodimentRefs(
                result=str(embodiment_result_path),
                profile=str(embodiment_dir / "RobotProfile.json"),
                validation_report=str(embodiment_dir / "ModelValidationReport.json"),
            ),
            language_task=request.language_task,
            target_object=request.target_object,
            scene_constraints=request.scene_constraints,
            template_id=request.template_id,
            task_parameters=request.task_parameters,
            task_count=request.task_count,
        )
        _save(task_dir / "GenerationRequest.json", asdict(generation_request))
        task = TaskGenerationAgent(model or "offline-proposal", proposer=proposer).run(
            generation_request, str(task_dir / "audit.json"))
        task_result_path = task_dir / "GenerationResult.json"
        _save(task_result_path, task.to_dict())
        refs.update({
            "GenerationRequest": _ref(task_dir / "GenerationRequest.json"),
            "GenerationResult": _ref(task_result_path),
            "TaskAudit": _ref(task_dir / "audit.json"),
        })
        scene_status = None
        status = "TASK_INTENT_PRODUCED" if task.status == "PRODUCED" else "TASK_GENERATION_FAILED"
        if task.status == "PRODUCED" and request.isaac_root:
            scene = AssetSceneAgent(Path(request.isaac_root), root / "asset_scene").run(
                task_result_path, embodiment_dir / "RobotProfile.json",
                embodiment_result=embodiment_result_path,
                validation_report=embodiment_dir / "ModelValidationReport.json",
                cached_archive=Path(request.cached_asset_archive) if request.cached_asset_archive else None,
            )
            scene_status = scene["status"]
            scene_dir = root / "asset_scene" / "scenes" / scene["task_id"]
            refs["AssetSceneResult"] = _ref(scene_dir / "result.json")
            scene_file = scene_dir / "scene.usd"
            if scene_file.is_file():
                refs["SceneUSD"] = _ref(scene_file)
            status = "SCENE_BUILT" if scene_status == "PASSED" else "ASSET_SCENE_FAILED"
        result = PipelineResult(status, embodiment.status, task.status,
                                scene_status, refs, embodiment.missing)
    _save(root / "PipelineResult.json", result.to_dict())
    return result
