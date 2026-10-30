"""Import the project's P5 URDF and step it with Isaac Lab, without HQP control."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from isaaclab.app import AppLauncher

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--urdf", type=Path, required=True)
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--evidence", type=Path, required=True)
AppLauncher.add_app_launcher_args(parser)
args = parser.parse_args()

app_launcher = AppLauncher(args)
simulation_app = app_launcher.app

import isaacsim.core.utils.prims as prim_utils
import isaacsim.core.utils.stage as stage_utils
import omni.usd
from isaaclab.sim import SimulationCfg, SimulationContext
from isaaclab.sim.converters import UrdfConverter, UrdfConverterCfg
from pxr import UsdGeom, UsdPhysics


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def main() -> int:
    urdf = args.urdf.expanduser().resolve(strict=True)
    output_dir = args.output_dir.expanduser().resolve()
    evidence_path = args.evidence.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)

    mesh_paths = []
    for mesh in ET.parse(urdf).iter("mesh"):
        mesh_path = (urdf.parent / mesh.attrib["filename"]).resolve(strict=True)
        mesh_paths.append({"path": str(mesh_path), "sha256": sha256(mesh_path)})

    result = {
        "status": "FAILED",
        "backend": "Isaac Lab",
        "runtime": {
            "python": sys.executable,
            "isaac_sim": package_version("isaacsim"),
            "isaac_lab": package_version("isaaclab"),
            "device": args.device,
        },
        "source": {"urdf": str(urdf), "sha256": sha256(urdf), "meshes": mesh_paths},
        "conversion": {
            "fix_base": True,
            "merge_fixed_joints": False,
            "joint_drive": None,
            "note": "Fixed base isolates import/articulation compatibility; it does not model UAV flight.",
        },
        "checks": {},
        "limitations": [
            "HQP controller was not invoked.",
            "No EE TCP or frame transform was inferred or changed.",
            "No UAV flight, contact, task tracking, or real-time constraint was tested.",
        ],
    }
    sim = None

    def checkpoint(phase: str) -> None:
        result["phase"] = phase
        evidence_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"[smoke] {phase}", flush=True)

    try:
        checkpoint("new_stage")
        stage_utils.create_new_stage()
        cfg = UrdfConverterCfg(
            asset_path=str(urdf),
            usd_dir=str(output_dir),
            usd_file_name="p5_m500_import_smoke.usd",
            force_usd_conversion=True,
            fix_base=True,
            merge_fixed_joints=False,
            joint_drive=None,
        )
        converter = UrdfConverter(cfg)
        converted_usd = Path(converter.usd_path).resolve(strict=True)
        checkpoint("urdf_converted")

        stage_utils.create_new_stage()
        prim_utils.create_prim("/World/P5", usd_path=str(converted_usd))
        current_stage = omni.usd.get_context().get_stage()
        if current_stage is None:
            raise RuntimeError("Isaac Lab did not provide an active USD stage")
        UsdGeom.SetStageMetersPerUnit(current_stage, 1.0)
        checkpoint("usd_composed")

        sim = SimulationContext(SimulationCfg(
            physics_prim_path="/World/PhysicsScene",
            dt=1.0 / 240.0,
            render_interval=1,
            device=args.device,
            gravity=(0.0, 0.0, -9.81),
        ))
        checkpoint("simulation_context_created")
        sim.reset()
        checkpoint("simulation_reset")
        for _ in range(120):
            sim.step(render=False)
            if _ in (0, 29, 59, 89, 119):
                checkpoint(f"simulation_step_{_ + 1}")

        robot_prim = current_stage.GetPrimAtPath("/World/P5")
        prim_names = [prim.GetName() for prim in current_stage.Traverse()]
        revolute_names = sorted({
            prim.GetName()
            for prim in current_stage.Traverse()
            if prim.IsA(UsdPhysics.RevoluteJoint)
        })
        expected_arm_joints = ["A", "B", "C", "D", "E", "F"]
        physics_step_index = sim.current_time_step_index
        physics_time_s = sim.current_time
        checks = {
            "converted_usd_exists": converted_usd.is_file(),
            "robot_reference_composed": robot_prim.IsValid() and len(prim_names) > 10,
            "six_arm_joints_preserved": all(name in revolute_names for name in expected_arm_joints),
            "f_link_preserved": "F_Link" in prim_names,
            "simulation_reset": True,
            "simulation_clock_advanced": physics_step_index >= 120 and physics_time_s > 0.0,
            "simulation_steps": 120,
        }
        result["checks"] = checks
        result["physics"] = {"step_index": physics_step_index, "sim_time_s": physics_time_s,
                             "dt_s": 1.0 / 240.0}
        result["conversion"].update({"converted_usd": str(converted_usd), "converted_usd_sha256": sha256(converted_usd)})
        result["robot"] = {
            "composed_prim_count": len(prim_names),
            "revolute_joint_names": revolute_names,
            "expected_arm_joint_names": expected_arm_joints,
            "end_link_name": "F_Link",
        }
        result["status"] = "PASSED" if all(
            value is True for key, value in checks.items() if key != "simulation_steps"
        ) and checks["simulation_steps"] == 120 else "FAILED"
    except Exception as exc:
        result["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if sim is not None:
            try:
                sim.clear_all_callbacks()
            except Exception:
                pass
            SimulationContext.clear_instance()
        evidence_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        simulation_app.close(wait_for_replicator=False)

    print(json.dumps({"status": result["status"], "evidence": str(evidence_path), "checks": result["checks"]}))
    return 0 if result["status"] == "PASSED" else 1


raise SystemExit(main())
