"""Compare legacy and structured memory on a fixed synthetic corpus."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _prompt(cases: list[dict], mode: str) -> str:
    payload = [{"id": case["id"], "question": case["question"],
                "options": case["options"], "memory": case[mode]} for case in cases]
    return (
        "Answer each synthetic memory question by choosing exactly one option key. "
        "Return one JSON object mapping every id to A, B, or C; no prose.\n\n" +
        json.dumps(payload, ensure_ascii=False)
    )


def _answers(text: str, ids: set[str]) -> dict[str, str]:
    parsed = json.loads(text)
    if not isinstance(parsed, dict) or set(parsed) != ids:
        raise ValueError("engine response has wrong case IDs")
    if any(value not in {"A", "B", "C"} for value in parsed.values()):
        raise ValueError("engine response has invalid option")
    return parsed


def evaluate(engine: str, output: Path) -> dict:
    from model_client import _attempt

    cases = json.loads((ROOT / "evaluation/semantic-cases.json").read_text(encoding="utf-8"))
    ids = {case["id"] for case in cases}
    report = {"engine": engine, "created_at": datetime.now(timezone.utc).isoformat(), "runs": {}}
    for mode in ("old", "new"):
        prompt = _prompt(cases, mode)
        result = _attempt(engine, prompt, ROOT, 2, ())
        if not result.get("ok"):
            raise RuntimeError(f"{engine}/{mode}: {result.get('error')}")
        answers = _answers(str(result["text"]), ids)
        rows = [{"id": case["id"], "split": case["split"], "answer": answers[case["id"]],
                 "expected": case["expected"], "correct": answers[case["id"]] == case["expected"]}
                for case in cases]
        report["runs"][mode] = {
            "answers": rows,
            "correct": sum(row["correct"] for row in rows),
            "cases": len(rows),
            "input_chars": len(prompt),
            "approx_input_tokens": math.ceil(len(prompt) / 4),
            "reported_tokens": result.get("total_tokens"),
            "duration_s": result.get("duration_s"),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("haiku", "luna", "grok"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = evaluate(args.engine, args.output.resolve())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}))
        return 1
    print(json.dumps({"status": "OK", "output": str(args.output),
                      "scores": {name: run["correct"] for name, run in report["runs"].items()}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
