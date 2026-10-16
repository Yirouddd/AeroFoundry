import argparse
import json
from pathlib import Path
from .pipeline import run

parser = argparse.ArgumentParser(description="Realize a Task Generation result in Isaac Sim")
parser.add_argument("task_result", type=Path)
parser.add_argument("robot_profile", type=Path)
parser.add_argument("--isaac-root", type=Path, required=True)
parser.add_argument("--output-root", type=Path, default=Path("outputs"))
parser.add_argument("--embodiment-result", type=Path)
parser.add_argument("--validation-report", type=Path)
parser.add_argument("--cached-archive", type=Path)
args = parser.parse_args()
result = run(args.task_result, args.robot_profile, args.isaac_root, args.output_root,
             embodiment_result=args.embodiment_result, validation_report=args.validation_report,
             cached_archive=args.cached_archive)
print(json.dumps({"status": result["status"], "task_id": result["task_id"], "failure": result["failure"]}, ensure_ascii=False))
raise SystemExit(0 if result["status"] == "PASSED" else 1)
