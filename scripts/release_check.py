"""Validate a source tree before building a release artifact."""

from __future__ import annotations

import json
import argparse
import re
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_ROOTS = {"daily", "wiki", "sources", ".cmc"}
FORBIDDEN_FILES = {"ACCESS.md", "engine.json", "auto-init-roots.json", ".cmc-config.json", ".disabled-by-codex"}
PRIVATE_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/")


def candidate_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in result.stdout.splitlines() if line]


def historical_private_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--all", "--name-only", "--format="],
        cwd=root, capture_output=True, text=True, check=True,
    )
    paths = set(result.stdout.splitlines())
    return sorted(path for path in paths if path and (
        Path(path).parts[0] in FORBIDDEN_ROOTS or Path(path).name in FORBIDDEN_FILES
    ))


def check(root: Path = ROOT, files: list[Path] | None = None) -> dict:
    issues = []
    files = candidate_files(root) if files is None else files
    for path in files:
        rel = path.relative_to(root)
        if path.is_symlink() or not path.is_file():
            issues.append({"file": str(rel), "issue": "candidate is not a regular file"})
            continue
        if rel.parts[0] in FORBIDDEN_ROOTS or rel.name in FORBIDDEN_FILES:
            issues.append({"file": str(rel), "issue": "runtime/private file in artifact"})
            continue
        if path.suffix.lower() in {".py", ".md", ".toml", ".json", ".sh"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            if PRIVATE_PATH.search(text):
                issues.append({"file": str(rel), "issue": "absolute user path"})
    for path in historical_private_paths(root):
        issues.append({"file": path, "issue": "private/runtime path exists in Git history"})
    return {"status": "OK" if not issues else "FAILED", "files": len(files), "issues": issues}


def build_artifact(root: Path, output: Path) -> dict:
    files = candidate_files(root)
    result = check(root, files)
    if result["status"] != "OK":
        raise ValueError("release checks failed")
    hashes = {path: __import__("hashlib").sha256(path.read_bytes()).hexdigest() for path in files}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for path in files:
            if __import__("hashlib").sha256(path.read_bytes()).hexdigest() != hashes[path]:
                raise ValueError(f"candidate changed during build: {path.relative_to(root)}")
            archive.add(path, arcname=f"wiki-agents/{path.relative_to(root)}", recursive=False)
    if candidate_files(root) != files or any(
        __import__("hashlib").sha256(path.read_bytes()).hexdigest() != digest
        for path, digest in hashes.items()
    ):
        output.unlink(missing_ok=True)
        raise ValueError("release candidates changed during build")
    result["artifact"] = str(output)
    result["artifact_bytes"] = output.stat().st_size
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, help="write a verified source tar.gz")
    args = parser.parse_args()
    try:
        result = build_artifact(ROOT, args.build.resolve()) if args.build else check()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        result = {"status": "FAILED", "issues": [{"issue": str(exc)}]}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
