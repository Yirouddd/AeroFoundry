import argparse
import json
from .agent import EmbodimentRequest, run

parser = argparse.ArgumentParser(description="Parse a USD robot into RobotProfile and ModelValidationReport")
parser.add_argument("model_directory")
parser.add_argument("output_directory")
parser.add_argument("--entry-file")
parser.add_argument("--job-id", default="standalone-job")
parser.add_argument("--request-id", default="standalone-request")
parser.add_argument("--interface-version", default="1.0")
args = parser.parse_args()
result = run(EmbodimentRequest(args.interface_version, args.job_id, args.request_id,
                                args.model_directory, args.output_directory, args.entry_file))
print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
raise SystemExit(2 if result.status == "ERROR" else 0)
