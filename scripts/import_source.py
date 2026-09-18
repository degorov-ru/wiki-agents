"""Import one Markdown or text source with stable provenance."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from config import PROJECT_DIR, SOURCES_DIR, STATE_DIR
from utils import file_hash
from pending import project_lock


ALLOWED_SUFFIXES = {".md", ".txt"}


def import_source(source: Path, project_dir: Path = PROJECT_DIR) -> dict:
    source = source.resolve()
    if source.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("only .md and .txt sources are supported")
    if not source.is_file():
        raise ValueError("source file does not exist")
    project_dir = project_dir.resolve()
    sources = project_dir / "sources"
    if sources.is_symlink():
        raise ValueError("sources directory cannot be a symlink")
    destination = sources / source.name
    digest = file_hash(source)
    manifest_path = project_dir / ".cmc" / "source-imports.json"
    with project_lock(project_dir, "sources"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        existing = manifest.get(destination.name)
        if destination.exists():
            if destination.is_symlink() or file_hash(destination) != digest:
                raise ValueError(f"source name already has different content: {destination.name}")
            if existing and existing.get("hash") == digest:
                return {"imported": False, "path": destination, "hash": digest}
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest[destination.name] = {"hash": digest, "origin": str(source)}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=manifest_path.parent, delete=False) as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
            temporary = Path(fh.name)
        os.replace(temporary, manifest_path)
        return {"imported": not bool(existing), "path": destination, "hash": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import one Markdown or text source")
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    result = import_source(args.source)
    print(f"{'Imported' if result['imported'] else 'Already imported'}: {result['path']}")


if __name__ == "__main__":
    main()
