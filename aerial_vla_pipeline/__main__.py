import argparse
import json
import os
from pathlib import Path

from .pipeline import PipelineRequest, run


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Embodiment then Task Generation")
    parser.add_argument("request", help="PipelineRequest JSON")
    parser.add_argument("--model", help="GPT model ID; otherwise OPENAI_MODEL")
    parser.add_argument("--proposal-file", help="Offline proposal JSON for integration tests; no API call")
    args = parser.parse_args()
    data = json.loads(Path(args.request).read_text(encoding="utf-8"))
    request = PipelineRequest(**data)
    proposer = None
    if args.proposal_file:
        proposal = Path(args.proposal_file).read_text(encoding="utf-8")
        proposer = lambda _messages, _parameters: (proposal, {"source": "offline-proposal-file"})
    model = args.model or os.getenv("OPENAI_MODEL")
    if not proposer and not model:
        parser.error("Provide --model / OPENAI_MODEL or --proposal-file")
    result = run(request, model=model, proposer=proposer)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status in {"TASK_INTENT_PRODUCED", "SCENE_BUILT"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
