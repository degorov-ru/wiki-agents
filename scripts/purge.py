"""Preview/apply exact daily-event removal and invalidate local derived memory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from pending import project_lock
from path_safety import atomic_write_text


EVENT_ID = re.compile(r"m-\d{8}-[a-f0-9]{10}")
EVENT_HEADER = re.compile(r"^### (m-\d{8}-[a-f0-9]{10})(?=\s|$)", re.M)
PRESERVED = {"claude-capture-state.json", "codex-last-flush.json", "last-flush.json", "purge-paused"}


def _plan(project: Path, event_ids: list[str]) -> tuple[dict, dict[Path, str | None]]:
    project = project.resolve()
    ids = sorted(set(event_ids))
    if not ids or any(not EVENT_ID.fullmatch(value) for value in ids):
        raise ValueError("exact event IDs are required")
    for layer in ("daily", "wiki", ".cmc"):
        root = project / layer
        if root.is_symlink() or not root.is_dir():
            raise ValueError("purge requires local daily/wiki/.cmc directories")
    changes: dict[Path, str | None] = {}
    found: set[str] = set()
    for path in sorted((project / "daily").glob("*.md")):
        if path.is_symlink():
            raise ValueError("symlinks are not purge targets")
        content = path.read_text(encoding="utf-8")
        headers = list(EVENT_HEADER.finditer(content))
        spans = []
        for index, header in enumerate(headers):
            if header[1] in ids:
                found.add(header[1])
                spans.append((header.start(), headers[index + 1].start() if index + 1 < len(headers) else len(content)))
        if spans:
            for start, end in reversed(spans):
                content = content[:start] + content[end:]
            changes[path] = content
    if found != set(ids):
        raise ValueError("one or more event IDs were not found")
    # ponytail: legacy summaries lack event provenance; invalidate derived layers wholesale.
    for layer in ("wiki", ".cmc"):
        for path in sorted((project / layer).rglob("*")):
            if path.is_symlink():
                raise ValueError("symlinks are not purge targets")
            if not path.is_file():
                continue
            if layer == ".cmc" and (path.suffix == ".lock" or (path.parent == project / ".cmc" and path.name in PRESERVED)):
                continue
            changes[path] = None
    entries = [{"path": str(path.relative_to(project)),
                "action": "delete" if content is None else "rewrite",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
               for path, content in sorted(changes.items())]
    plan = {"project": str(project), "event_ids": ids, "changes": entries,
            "derived_scope": "all wiki and runtime artifacts; capture/dedupe cursors retained",
            "after_apply": "capture and model publication paused; external transcripts untouched"}
    plan["confirmation"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    return plan, changes


def preview(project: Path, event_ids: list[str]) -> dict:
    return _plan(project, event_ids)[0]


def apply(project: Path, event_ids: list[str], confirmation: str) -> dict:
    project = project.resolve()
    with project_lock(project, "flush"), project_lock(project), project_lock(project, "wiki"):
        if (project / ".cmc" / "compile.lock").exists():
            raise ValueError("compilation is active; retry purge after it finishes")
        plan, changes = _plan(project, event_ids)
        if confirmation != plan["confirmation"]:
            raise ValueError("preview changed or confirmation is invalid; preview again")
        (project / ".cmc" / "purge-paused").write_text("local purge; explicit review required before resuming\n")
        for path, content in changes.items():
            if content is None:
                path.unlink()
            else:
                atomic_write_text(path, content)
        return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--event-id", action="append", required=True)
    parser.add_argument("--apply", metavar="PREVIEW_CONFIRMATION")
    args = parser.parse_args()
    try:
        result = apply(args.project, args.event_id, args.apply) if args.apply else preview(args.project, args.event_id)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"purge refused: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
