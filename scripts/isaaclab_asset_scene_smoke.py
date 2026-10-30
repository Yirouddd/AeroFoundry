"""Exercise AeroFoundry's Asset & Scene runner with the available P5 robot USD."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asset_scene.pipeline import AssetSceneAgent, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-usd", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--isaac-python", type=Path, required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output_root = args.output_root.expanduser().resolve()
    if output_root.exists():
        parser.error(f"Output already exists; choose a new path: {output_root}")
    output_root.mkdir(parents=True)
    inputs = output_root / "smoke_inputs"
    inputs.mkdir()

    task_result = root / "example_run" / "task_generation" / "GenerationResult.json"
    profile = json.loads((root / "tests" / "fixtures" / "embodiment" / "RobotProfile.json").read_text())
    profile["model_entry"] = str(args.robot_usd.expanduser().resolve(strict=True))
    profile_path = inputs / "RobotProfile.json"
    write_json(profile_path, profile)

    result = AssetSceneAgent(
        isaac_root=None,
        output_root=output_root / "asset_scene",
        isaac_python=args.isaac_python,
    ).run(
        task_result,
        profile_path,
        cached_archive=root / "assets" / "cache" / "kenney_factory-kit_3.0.zip",
    )
    report = {
        "kind": "aerofoundry_asset_scene_isaaclab_backend_smoke",
        "status": result["status"],
        "task_id": result["task_id"],
        "robot_asset": profile["model_entry"],
        "result": result,
        "interpretation": [
            "Uses the existing press-button candidate and the P5 M500 USD solely to test scene-runner integration.",
            "The fixture profile is not a validated P5 RobotProfile.",
            "A PASSED scene check is not Gate A, HQP execution, robot contact, or task success.",
        ],
    }
    evidence_path = args.evidence.expanduser().resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "evidence": str(evidence_path), "failure": result["failure"]}))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
