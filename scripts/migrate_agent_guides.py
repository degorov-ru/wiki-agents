"""Move legacy project instructions into AGENTS.md after a dry run."""
from __future__ import annotations

import argparse
import re
from pathlib import Path


POINTER = re.compile(r"<!-- wiki-agents:pointer-start -->.*?<!-- wiki-agents:pointer-end -->", re.S)


def is_pointer(text: str) -> bool:
    body = POINTER.sub("", text).strip()
    return len(body) < 500 and "## " not in body and (
        "AGENT_GUIDE.md" in body or
        ("AGENTS.md" in body and ("Read `AGENTS.md`" in body or "points to" in body))
    )


def migrate(project: Path, apply: bool) -> str:
    agents = project / "AGENTS.md"
    legacy = [project / name for name in ("AGENT_GUIDE.md", "CLAUDE.md")]
    legacy = [path for path in legacy if path.is_file() and not path.is_symlink()]
    if not legacy:
        return "skip"
    if agents.is_symlink():
        raise ValueError(f"symlinked AGENTS.md: {project}")
    current = agents.read_text(encoding="utf-8") if agents.exists() else ""
    sections = []
    for path in legacy:
        content = path.read_text(encoding="utf-8")
        content = POINTER.sub("", content).strip()
        if content and not is_pointer(content) and content not in current:
            sections.append((path.name, content))
    if is_pointer(current):
        current = ""
    merged = current.strip()
    for name, content in sections:
        merged += ("\n\n" if merged else "") + (f"## Rules from {name}\n\n" if merged else "") + content
    merged = merged.replace("AGENT_GUIDE.md", "AGENTS.md")
    if not merged:
        raise ValueError(f"no instruction content: {project}")
    if apply:
        agents.write_text(merged + "\n", encoding="utf-8")
        if agents.read_text(encoding="utf-8") != merged + "\n":
            raise RuntimeError(f"write verification failed: {project}")
        for path in legacy:
            path.unlink()
    return f"{'apply' if apply else 'plan'}: {project} ({len(sections)} sections)"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("projects", nargs="+", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for project in args.projects:
        print(migrate(project, args.apply))


if __name__ == "__main__":
    main()
