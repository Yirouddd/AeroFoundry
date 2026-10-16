import argparse
import json
from pathlib import Path

from .agent import EmbodimentRefs, GenerationRequest, TaskGenerationAgent, replay


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a validated phase-one task intent")
    parser.add_argument("request", help="GenerationRequest JSON path")
    parser.add_argument("--model", help="GPT model ID; otherwise OPENAI_MODEL environment variable")
    parser.add_argument("--output", required=True, help="Result JSON path")
    parser.add_argument("--audit", required=True, help="Audit JSON path")
    parser.add_argument("--replay", help="Use frozen audit response, without a network call")
    args = parser.parse_args()
    import os
    data = json.loads(Path(args.request).read_text(encoding="utf-8"))
    data["embodiment_refs"] = EmbodimentRefs(**data["embodiment_refs"])
    request = GenerationRequest(**data)
    if args.replay:
        result = replay(args.replay, request)
    else:
        model = args.model or os.getenv("OPENAI_MODEL")
        if not model:
            parser.error("Set --model or OPENAI_MODEL")
        result = TaskGenerationAgent(model).run(request, args.audit)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(result.status)
    return 0 if result.status == "PRODUCED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
