"""Validated input, live catalog lookup, provenance and Isaac Sim handoff."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

TEMPLATES = ("pick_place", "press", "push", "pull", "rotate", "insert", "wipe")
KENNEY_PAGE = "https://kenney.nl/assets/factory-kit"
CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
AGENT_VERSION = "0.1.0"
ISAAC_PYTHON_ENV = "AERIAL_VLA_ISAAC_PYTHON"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AssetSceneAgent/0.1 (research simulator)"})
    with urllib.request.urlopen(req, timeout=40) as response:
        return response.read()


def validate(task: dict, profile: dict) -> tuple[str, str]:
    if task.get("status") != "PRODUCED":
        raise ValueError("Task Generation status is not PRODUCED")
    fields = ("completed_generation_intent", "task_spec", "success_spec", "target_object_spec", "scene_constraint_spec")
    if any(not isinstance(task.get(key), dict) for key in fields):
        raise ValueError("Required Task Generation artifact is absent")
    tid = task["completed_generation_intent"]["task_id"]
    if any(task[key].get("task_id") != tid for key in fields[1:]):
        raise ValueError("Task artifact task_id mismatch")
    template = task["task_spec"]["template_id"]
    if template not in TEMPLATES:
        raise ValueError(f"Unsupported template: {template}")
    if profile.get("model_entry") is None or profile.get("stage", {}).get("meters_per_unit", {}).get("value") is None:
        raise ValueError("RobotProfile has no model entry or stage units")
    return tid, template


def discover_factory_kit() -> dict:
    html = fetch(KENNEY_PAGE).decode("utf-8", errors="replace")
    if not re.search(r"Creative Commons CC0", html, flags=re.I):
        raise RuntimeError("Kenney page does not confirm CC0")
    match = re.search(r"(/media/pages/assets/factory-kit/[^\"'<> ]+\.zip)", html)
    if not match:
        raise RuntimeError("No direct Factory Kit archive URL on source page")
    return {"page": KENNEY_PAGE, "download_url": "https://kenney.nl" + match.group(1),
            "provider": "Kenney", "license": "CC0-1.0", "license_url": CC0_URL,
            "version": "3.0", "retrieved_at": datetime.now(timezone.utc).isoformat()}


def load_cached_catalog(archive: Path) -> dict:
    """Use previously recorded source metadata, verifying the cached bytes."""
    metadata_path = archive.with_suffix(".json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    required = {"page", "download_url", "provider", "license", "license_url",
                "version", "retrieved_at", "sha256", "bytes"}
    if not required <= set(metadata) or metadata["page"] != KENNEY_PAGE or metadata["license"] != "CC0-1.0":
        raise ValueError("Cached asset metadata is incomplete or does not match the source")
    if sha256(archive) != metadata["sha256"] or archive.stat().st_size != metadata["bytes"]:
        raise ValueError("Cached asset archive failed provenance hash/size check")
    return metadata


def _expanded_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def resolve_isaac_python(isaac_root: Path | None = None,
                         isaac_python: Path | str | None = None) -> Path:
    """Resolve the Python interpreter that owns Isaac Sim and Isaac Lab."""
    explicit = isaac_python or os.environ.get(ISAAC_PYTHON_ENV)
    if explicit:
        path = _expanded_path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"Isaac Lab Python interpreter does not exist: {path}")
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise PermissionError(f"Isaac Lab Python interpreter is not executable: {path}")
        return path

    if isaac_root:
        root = _expanded_path(isaac_root)
        if root.is_file():
            candidates = [root]
        elif os.name == "nt":
            candidates = [root / "python.bat", root / "python.exe", root / "bin" / "python.exe"]
        else:
            candidates = [root / "python.sh", root / "bin" / "python", root / "python"]
        for path in candidates:
            if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
                return path.resolve()

    if importlib.util.find_spec("isaaclab") and importlib.util.find_spec("isaacsim"):
        return Path(sys.executable).resolve()

    raise FileNotFoundError(
        f"Isaac Lab runtime not found. Set {ISAAC_PYTHON_ENV} to the executable from "
        "the Isaac Lab environment, or pass isaac_python in the request."
    )


def isaac_subprocess_environment(isaac_python: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Set Linux runtime libraries from the selected Isaac conda environment first."""
    environment = dict(os.environ if base is None else base)
    if os.name != "nt":
        prefix = isaac_python.resolve().parent.parent
        library_dir = prefix / "lib"
        if library_dir.is_dir():
            existing = [item for item in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item]
            library = str(library_dir)
            if library in existing:
                existing.remove(library)
            environment["LD_LIBRARY_PATH"] = os.pathsep.join([library, *existing])
    return environment


class AssetSceneAgent:
    def __init__(self, isaac_root: Path | None, output_root: Path,
                 isaac_python: Path | str | None = None):
        self.isaac_root = Path(isaac_root) if isaac_root else None
        self.isaac_python = isaac_python
        self.output_root = Path(output_root)

    def run(self, task_result: Path, robot_profile: Path, embodiment_result: Path | None = None,
            validation_report: Path | None = None, cached_archive: Path | None = None) -> dict:
        task_result, robot_profile = Path(task_result), Path(robot_profile)
        task, profile = json.loads(task_result.read_text(encoding="utf-8")), json.loads(robot_profile.read_text(encoding="utf-8"))
        tid, template = validate(task, profile)
        scene_dir = (self.output_root / "scenes" / tid).resolve()
        scene_dir.mkdir(parents=True, exist_ok=True)
        result_path = scene_dir / "result.json"
        result = {"schema_version": "asset-scene-0.1", "task_id": tid, "status": "FAILED",
                  "agent_version": AGENT_VERSION, "template_id": template,
                  "inputs": {"task_result": {"path": str(task_result), "sha256": sha256(task_result)},
                             "robot_profile": {"path": str(robot_profile), "sha256": sha256(robot_profile)}},
                  "object_spec": {"task_id": tid, "asset_class": task["target_object_spec"]["asset_class"],
                                  "semantic_attributes": task["target_object_spec"]["semantic_attributes"],
                                  "hard_attributes": task["target_object_spec"]["hard_attributes"]},
                  "asset_match_report": {"task_id": tid, "candidates": [], "selected": None},
                  "target_object_instance": None, "scene_spec": None, "realized_scene": None,
                  "initialization_log": [], "asset_versions": [], "scene_version": None,
                  "dependencies": [], "verification": {}, "failure": None,
                  "gate_a": "NOT_EVALUATED", "robot_flight_reachability": "UNKNOWN"}
        stage_name = "input_validation"
        for name, path in (("embodiment_result", embodiment_result), ("validation_report", validation_report)):
            if path:
                path = Path(path)
                result["inputs"][name] = {"path": str(path), "sha256": sha256(path)}
        try:
            stage_name = "template_adapter"
            if template != "press" or task["target_object_spec"]["asset_class"].lower() not in ("button", "push button"):
                raise NotImplementedError(f"No verified asset adapter for {template}/{task['target_object_spec']['asset_class']}")
            constraints = task["scene_constraint_spec"]
            unsupported_constraints = [key for key in ("raw_text", "target_side", "workspace_box_m", "forbidden_regions",
                                                      "support_relation", "asset_ids") if constraints.get(key) not in (None, [], "")]
            if unsupported_constraints:
                raise NotImplementedError(f"Scene constraint adapter missing for: {', '.join(unsupported_constraints)}")
            if task["target_object_spec"].get("hard_attributes"):
                raise NotImplementedError("Hard target attributes require a verified matching adapter")
            stage_name = "asset_discovery"
            catalog = load_cached_catalog(Path(cached_archive)) if cached_archive else discover_factory_kit()
            result["initialization_log"].append({"step": "cached_catalog" if cached_archive else "live_discovery",
                                                 "url": catalog["page"], "time": catalog["retrieved_at"]})
            archive = scene_dir / "assets" / "kenney_factory-kit_3.0.zip"
            stage_name = "asset_download"
            archive.parent.mkdir(parents=True, exist_ok=True)
            if cached_archive:
                shutil.copy2(cached_archive, archive)
                catalog["download_method"] = "verified_cached_archive"
            else:
                archive.write_bytes(fetch(catalog["download_url"]))
                catalog["download_method"] = "live_download"
            catalog["sha256"] = sha256(archive)
            catalog["bytes"] = archive.stat().st_size
            with zipfile.ZipFile(archive) as bundle:
                stage_name = "license_and_archive_verification"
                license_text = bundle.read("License.txt").decode("utf-8")
                if "Creative Commons Zero" not in license_text or "commercial purposes" not in license_text:
                    raise RuntimeError("Archive license did not verify CC0 and commercial use")
                for filename in ("License.txt", "Models/OBJ format/button-floor-round.obj",
                                 "Models/OBJ format/button-floor-round.mtl", "Models/GLB format/button-floor-round.glb"):
                    (archive.parent / Path(filename).name).write_bytes(bundle.read(filename))
            candidate = {**catalog, "id": "kenney-factory-kit-3.0-button-floor-round",
                         "asset_url": catalog["download_url"], "format": "OBJ/GLB",
                         "original_file": str(archive.parent / "button-floor-round.obj"),
                         "original_sha256": sha256(archive.parent / "button-floor-round.obj"),
                         "matched_attributes": ["button", "round", "floor mounted", "press surface appearance"],
                         "unmatched_attributes": ["authored prismatic travel", "spring return", "measured force"],
                         "selection_reason": "CC0 source, geometric button and explicit free download; mechanism can be built separately"}
            result["asset_match_report"]["candidates"].append(candidate)
            result["asset_match_report"]["candidates"].append({
                "id": "kenney-factory-kit-3.0-button-floor-square", "page": catalog["page"],
                "download_url": catalog["download_url"], "provider": "Kenney", "license": "CC0-1.0",
                "format": "OBJ/GLB", "version": "3.0", "matched_attributes": ["button", "floor mounted"],
                "unmatched_attributes": ["authored prismatic travel", "spring return", "measured force"],
                "selection_reason": "Valid candidate; round button chosen for symmetric contact surface"})
            result["asset_match_report"]["selected"] = candidate["id"]
            result["asset_versions"].append({"id": candidate["id"], "source_version": "3.0", "sha256": candidate["original_sha256"]})
            model_entry = _expanded_path(profile["model_entry"])
            if not model_entry.is_file():
                raise FileNotFoundError(f"RobotProfile model_entry is unavailable on this host: {model_entry}")
            robot_src = model_entry.parent
            stage_name = "robot_dependency_copy"
            robot_dst = scene_dir / "dependencies" / "robot_asset"
            shutil.copytree(robot_src, robot_dst, dirs_exist_ok=True)
            robot_asset = robot_dst / model_entry.name
            scene_path = scene_dir / "scene.usd"
            payload = {"task_id": tid, "task": task, "profile": profile,
                       "scene_dir": str(scene_dir), "scene_path": str(scene_path),
                       "robot_asset": str(robot_asset),
                       "source_obj": str(archive.parent / "button-floor-round.obj")}
            job = scene_dir / "isaac_job.json"
            write_json(job, payload)
            runner = Path(__file__).resolve().parent / "isaac_runner.py"
            isaac_python = resolve_isaac_python(self.isaac_root, self.isaac_python)
            stage_name = "isaac_sim_build_and_reopen"
            command = [str(isaac_python), str(runner), str(job), "--headless", "--device", "cuda:0"]
            with (scene_dir / "isaac_stdout.log").open("w", encoding="utf-8") as stdout, (scene_dir / "isaac_stderr.log").open("w", encoding="utf-8") as stderr:
                environment = isaac_subprocess_environment(isaac_python)
                environment["WARP_CACHE_PATH"] = str(scene_dir / "cache" / "warp")
                completed = subprocess.run(command, stdout=stdout, stderr=stderr,
                                           env=environment, timeout=900)
            evidence_file = scene_dir / "isaac_evidence.json"
            if completed.returncode or not evidence_file.exists():
                raise RuntimeError(f"Isaac Lab runner exited {completed.returncode}; see isaac_stdout.log and isaac_stderr.log")
            evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
            result["initialization_log"].append({"step": "isaac_sim_build_and_reopen", "evidence": str(evidence_file),
                                                  "required_checks_passed": evidence.get("verification", {}).get("required_checks_passed")})
            result.update({k: evidence.get(k) for k in ("target_object_instance", "scene_spec", "realized_scene", "verification")})
            result["scene_version"] = {"agent": AGENT_VERSION, "isaac_sim": evidence.get("isaac_version"),
                                       "isaac_lab": evidence.get("isaac_lab_version"),
                                       "scene_sha256": sha256(scene_path) if scene_path.exists() else None}
            result["dependencies"] = [{"path": str(p.relative_to(scene_dir)), "sha256": sha256(p)}
                                      for p in scene_dir.rglob("*") if p.is_file() and p not in (result_path, job)]
            if not evidence.get("verification", {}).get("required_checks_passed"):
                raise RuntimeError("Isaac Lab structural validation did not pass required checks")
            result["status"] = "PASSED"
        except Exception as exc:
            result["failure"] = {"type": type(exc).__name__, "message": str(exc),
                                 "stage": stage_name}
        write_json(result_path, result)
        return result


def run(task_result: Path, robot_profile: Path, isaac_root: Path | None, output_root: Path,
        isaac_python: Path | str | None = None, **kwargs) -> dict:
    return AssetSceneAgent(isaac_root, output_root, isaac_python).run(task_result, robot_profile, **kwargs)
