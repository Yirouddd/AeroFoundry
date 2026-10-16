"""Deterministic USD embodiment extraction. Requires Pixar usd-core (pxr)."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
SUFFIXES = {".usd", ".usda", ".usdc"}


@dataclass(frozen=True)
class EmbodimentRequest:
    interface_version: str
    job_id: str
    request_id: str
    model_directory: str
    output_directory: str
    entry_file: str | None = None


@dataclass
class EmbodimentResult:
    interface_version: str
    job_id: str
    request_id: str
    status: str
    artifact_refs: list[dict[str, Any]]
    evidence_refs: list[str]
    missing: list[str]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "GetReal") and hasattr(value, "GetImaginary"):
        return {"real": float(value.GetReal()), "imaginary": _jsonable(value.GetImaginary())}
    if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        return [_jsonable(value[i]) for i in range(len(value))]
    if isinstance(value, (tuple, list)) or hasattr(value, "__iter__"):
        try:
            return [_jsonable(v) for v in value]
        except TypeError:
            pass
    return str(value)


def _source(prop: Any) -> dict[str, str] | None:
    stack = prop.GetPropertyStack()
    if not stack:
        return None
    spec = stack[0]
    return {"file": str(Path(spec.layer.realPath or spec.layer.identifier).resolve()),
            "field_path": str(spec.path)}


def _field(prim: Any, name: str, unit: str | None = None) -> dict[str, Any] | None:
    attr = prim.GetAttribute(name)
    if not attr or not attr.HasAuthoredValueOpinion():
        return None
    value = _jsonable(attr.Get())
    if value is None:
        return None
    return {"value": value, "unit": unit, "source": _source(attr)}


def _relation(prim: Any, name: str) -> dict[str, Any] | None:
    rel = prim.GetRelationship(name)
    if not rel or not rel.HasAuthoredTargets():
        return None
    return {"targets": [str(p) for p in rel.GetTargets()], "source": _source(rel)}


def _evidence(report: dict, category: str, path: str, detail: str,
              source: dict | None = None) -> None:
    report[category].append({"field": path, "detail": detail, "source": source})


def _entry(root: Path, explicit: str | None) -> Path:
    if explicit:
        path = (root / explicit).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in SUFFIXES:
            raise ValueError("entry_file must be an existing USD file inside model_directory")
        return path
    choices = sorted(root.glob("*.usd*"))
    if len(choices) != 1:
        raise ValueError("Exactly one top-level USD entry is required; provide entry_file")
    return choices[0].resolve()


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run(request: EmbodimentRequest) -> EmbodimentResult:
    """Produce both JSON artifacts, including on parser/input failure when output is writable."""
    out = Path(request.output_directory).resolve()
    out.mkdir(parents=True, exist_ok=True)
    result = EmbodimentResult(SCHEMA_VERSION, request.job_id, request.request_id,
                              "ERROR", [], [], [], [])
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "job_id": request.job_id,
        "request_id": request.request_id, "stage_opened": False, "variant_checks": [],
        "confirmed": [], "invalid": [], "missing_or_ambiguous": [],
        "optional_unset": [], "errors": []}
    profile: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "job_id": request.job_id,
        "request_id": request.request_id, "model_entry": None, "files": [],
        "stage": None, "quadrotor": {"body_frame": None, "rotors": [],
            "max_thrust": None, "hover_capability": None},
        "manipulator": {"mount_joint": None, "joint_order": [], "joints": []},
        "end_effector": {"mount_joint": None, "joints": [], "tcp": None,
            "rated_payload": None}, "links": [], "collisions": [], "sensors": []}
    try:
        if request.interface_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported interface_version: {request.interface_version}")
        if not request.job_id or not request.request_id:
            raise ValueError("job_id and request_id are required")
        root = Path(request.model_directory).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("model_directory is not a directory")
        entry = _entry(root, request.entry_file)
        profile["model_entry"] = str(entry)
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUFFIXES)
        profile["files"] = [{"path": str(p.resolve()), "sha256": _sha256(p),
                             "bytes": p.stat().st_size} for p in files]
        try:
            from pxr import Usd, UsdGeom, UsdPhysics
        except Exception as exc:
            raise RuntimeError(f"USD_RUNTIME_UNAVAILABLE: {exc}") from exc
        stage = Usd.Stage.Open(str(entry), load=Usd.Stage.LoadAll)
        if not stage or not stage.GetDefaultPrim():
            raise RuntimeError("USD_STAGE_OPEN_FAILED: no composed default prim")
        report["stage_opened"] = True
        root_prim = stage.GetDefaultPrim()
        used = {str(Path(layer.realPath).resolve()) for layer in stage.GetUsedLayers() if layer.realPath}
        inventoried = {item["path"] for item in profile["files"]}
        for path_text in sorted(used - inventoried):
            dependency = Path(path_text)
            if dependency.is_file():
                profile["files"].append({"path": path_text, "sha256": _sha256(dependency),
                                         "bytes": dependency.stat().st_size})
        for item in profile["files"]:
            item["used_in_selected_composition"] = item["path"] in used
        profile["stage"] = {"default_prim": str(root_prim.GetPath()),
            "meters_per_unit": {"value": UsdGeom.GetStageMetersPerUnit(stage), "unit": "m",
                                "source": {"file": str(entry), "field_path": "layer metadata:metersPerUnit"}},
            "up_axis": str(UsdGeom.GetStageUpAxis(stage)),
            "kilograms_per_unit": {"value": UsdPhysics.GetStageKilogramsPerUnit(stage),
                                   "unit": "kg", "source": {"file": str(entry),
                                   "field_path": "composed layer metadata:kilogramsPerUnit"}}}
        _evidence(report, "confirmed", "stage", "USD stage and default prim composed")
        for variant_set in root_prim.GetVariantSets().GetNames():
            if variant_set != "Physics":
                continue
            vset = root_prim.GetVariantSets().GetVariantSet(variant_set)
            selected = vset.GetVariantSelection()
            for name in vset.GetVariantNames():
                vset.SetVariantSelection(name)  # session edit; source files stay unchanged
                stage.Load()
                broken = [str(x) for x in stage.GetCompositionErrors()]
                report["variant_checks"].append({"name": name, "readable": not broken,
                    "composition_errors": broken})
                if broken:
                    _evidence(report, "invalid", f"Physics variant:{name}",
                              "USD composition errors: " + "; ".join(broken))
            vset.SetVariantSelection(selected)
            stage.Load()
        errors = [str(x) for x in stage.GetCompositionErrors()]
        if errors:
            raise RuntimeError("USD_COMPOSITION_ERROR: " + "; ".join(errors))
        report["selected_variant"] = root_prim.GetVariantSets().GetVariantSet("Physics").GetVariantSelection()
        profile["stage"]["selected_physics_variant"] = report["selected_variant"]
        for prim in stage.Traverse():
            path = str(prim.GetPath())
            if prim.IsA(UsdPhysics.Joint):
                joint = {"name": prim.GetName(), "path": path, "type": prim.GetTypeName(),
                    "source": str(Path(prim.GetPrimStack()[0].layer.realPath).resolve()) if prim.GetPrimStack() else None,
                    "body0": _relation(prim, "physics:body0"),
                    "body1": _relation(prim, "physics:body1"),
                    "axis": _field(prim, "physics:axis"),
                    "lower_limit": _field(prim, "physics:lowerLimit", "degree" if "Revolute" in prim.GetTypeName() else "m"),
                    "upper_limit": _field(prim, "physics:upperLimit", "degree" if "Revolute" in prim.GetTypeName() else "m"),
                    "local_pos0": _field(prim, "physics:localPos0", "m"),
                    "local_rot0": _field(prim, "physics:localRot0", "quaternion"),
                    "local_pos1": _field(prim, "physics:localPos1", "m"),
                    "local_rot1": _field(prim, "physics:localRot1", "quaternion")}
                if prim.GetName().startswith("rotor_"):
                    profile["quadrotor"]["rotors"].append(joint)
                elif prim.GetName() == "swan_uav_arm_joint":
                    profile["manipulator"]["mount_joint"] = joint
                elif prim.GetName() == "swan_gripper_joint":
                    profile["end_effector"]["mount_joint"] = joint
                elif prim.GetName() in ("joint1", "joint2"):
                    profile["end_effector"]["joints"].append(joint)
                else:
                    profile["manipulator"]["joints"].append(joint)
                for side in ("body0", "body1"):
                    if not joint[side] or len(joint[side]["targets"]) != 1 or not stage.GetPrimAtPath(joint[side]["targets"][0]):
                        _evidence(report, "invalid", path + "." + side, "Joint body target missing or unresolved")
                lo, hi = joint["lower_limit"], joint["upper_limit"]
                if lo and hi and lo["value"] > hi["value"]:
                    _evidence(report, "invalid", path + ".limits", "lower limit exceeds upper limit", lo["source"])
                elif "Fixed" not in prim.GetTypeName() and (not lo or not hi):
                    _evidence(report, "missing_or_ambiguous", path + ".limits", "One or both joint limits are unauthored")
            if prim.HasAPI(UsdPhysics.MassAPI):
                link = {"path": path, "mass": _field(prim, "physics:mass", "kg"),
                    "center_of_mass": _field(prim, "physics:centerOfMass", "m"),
                    "diagonal_inertia": _field(prim, "physics:diagonalInertia", "kg*m^2"),
                    "principal_axes": _field(prim, "physics:principalAxes", "quaternion")}
                profile["links"].append(link)
                mass = link["mass"]
                if mass and (not isinstance(mass["value"], (int, float)) or not math.isfinite(mass["value"]) or mass["value"] <= 0):
                    _evidence(report, "invalid", path + ".physics:mass", "Mass must be finite and positive", mass["source"])
                elif mass:
                    _evidence(report, "confirmed", path + ".physics:mass", "Authored mass", mass["source"])
                else:
                    _evidence(report, "missing_or_ambiguous", path + ".physics:mass", "Mass not authored")
                inertia = link["diagonal_inertia"]
                if inertia and (len(inertia["value"]) != 3 or any(not math.isfinite(float(x)) or float(x) <= 0 for x in inertia["value"])):
                    _evidence(report, "invalid", path + ".physics:diagonalInertia", "Diagonal inertia must be finite and positive", inertia["source"])
                elif not inertia:
                    _evidence(report, "missing_or_ambiguous", path + ".physics:diagonalInertia", "Inertia not authored")
            if prim.HasAPI(UsdPhysics.CollisionAPI):
                profile["collisions"].append({"path": path, "geometry_type": prim.GetTypeName(),
                    "enabled": _field(prim, "physics:collisionEnabled"),
                    "size": _field(prim, "size", "m"),
                    "radius": _field(prim, "radius", "m"),
                    "height": _field(prim, "height", "m"),
                    "extent": _field(prim, "extent", "m"),
                    "mesh_point_count": len(prim.GetAttribute("points").Get()) if prim.GetTypeName() == "Mesh" and prim.GetAttribute("points") and prim.GetAttribute("points").Get() is not None else None,
                    "source": str(Path(prim.GetPrimStack()[0].layer.realPath).resolve()) if prim.GetPrimStack() else None})
            if prim.IsA(UsdGeom.Camera):
                profile["sensors"].append({"path": path, "type": "Camera",
                    "source": str(Path(prim.GetPrimStack()[0].layer.realPath).resolve()) if prim.GetPrimStack() else None})
        # Joint order is the authored robotJoints relationship, with chain order retained separately.
        order = _relation(root_prim, "isaac:physics:robotJoints")
        profile["manipulator"]["joint_order"] = [p.rsplit("/", 1)[-1] for p in order["targets"] if p.rsplit("/", 1)[-1] in {j["name"] for j in profile["manipulator"]["joints"]}] if order else []
        profile["stage"]["authored_robot_joint_order"] = [p.rsplit("/", 1)[-1] for p in order["targets"]] if order else []
        profile["quadrotor"]["body_frame"] = next((x["body0"]["targets"][0] for x in profile["quadrotor"]["rotors"] if x["body0"]), None)
        if not order:
            _evidence(report, "missing_or_ambiguous", "isaac:physics:robotJoints", "Authored joint order absent")
        _evidence(report, "optional_unset", "end_effector.tcp",
                  "No fixed TCP is specified in this USD; it is not required for embodiment extraction")
        for field, detail in (("quadrotor.max_thrust", "No rated thrust value identified"),
                              ("end_effector.rated_payload", "No rated gripper payload identified")):
            _evidence(report, "missing_or_ambiguous", field, detail)
        if len(profile["quadrotor"]["rotors"]) != 4:
            _evidence(report, "missing_or_ambiguous", "quadrotor.rotors", "Expected four named rotor joints")
        if len(profile["manipulator"]["joints"]) != 6:
            _evidence(report, "missing_or_ambiguous", "manipulator.joints", "Expected six arm joints")
        result.status = "ERROR" if report["invalid"] else "NEEDS_INFORMATION" if report["missing_or_ambiguous"] else "PRODUCED"
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        result.errors = report["errors"][:]
    result.missing = [x["field"] for x in report["missing_or_ambiguous"]]
    for filename, obj, kind in (("RobotProfile.json", profile, "RobotProfile"),
                                ("ModelValidationReport.json", report, "ModelValidationReport")):
        path = out / filename
        _write(path, obj)
        result.artifact_refs.append({"artifact_type": kind, "schema_version": SCHEMA_VERSION,
                                     "path": str(path), "sha256": _sha256(path)})
    result.evidence_refs = [result.artifact_refs[1]["path"]]
    return result
