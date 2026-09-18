"""Fail-closed helpers for project-owned runtime paths."""

import os
import tempfile
from pathlib import Path


MANAGED_DIRS = (".cmc", ".codex", "daily", "sources", "wiki")


def managed_dir(project: Path, name: str, *, create: bool = False) -> Path:
    project = project.resolve()
    if not project.is_dir() or name not in MANAGED_DIRS:
        raise ValueError("invalid managed project directory")
    path = project / name
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"unsafe managed directory: {name}")
    if create:
        path.mkdir(parents=False, exist_ok=True)
    if path.exists() and not path.resolve().is_relative_to(project):
        raise ValueError(f"managed directory escapes project: {name}")
    return path


def validate_layout(project: Path, *, create_state: bool = False) -> Path:
    project = project.resolve()
    for name in MANAGED_DIRS:
        managed_dir(project, name, create=create_state and name == ".cmc")
    return project


def contained_file(path: Path, root: Path, *, must_exist: bool = True) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("unsafe managed root")
    if path.is_symlink():
        raise ValueError("path crosses symlink")
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("path escapes managed root") from exc
    current = resolved_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("path crosses symlink")
    if must_exist and not resolved_path.is_file():
        raise ValueError("expected regular file")
    if path.exists() and not resolved_path.is_relative_to(resolved_root):
        raise ValueError("path escapes managed root")
    return resolved_path


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a file without trusting a predictable temporary leaf."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".cmc-write-", delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def append_text(path: Path, content: str) -> None:
    """Append without following a pre-existing symlink leaf."""
    if path.is_symlink():
        raise ValueError("append target is a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(content)
