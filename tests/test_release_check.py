from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
import tarfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_check.py"
SPEC = importlib.util.spec_from_file_location("release_check_test", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ReleaseCheckTests(unittest.TestCase):
    def test_rejects_private_runtime_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "safe.md").write_text("portable", encoding="utf-8")
            (root / "ACCESS.md").write_text("private", encoding="utf-8")
            result = CHECK.check(root)
            self.assertEqual("FAILED", result["status"])
            self.assertEqual("ACCESS.md", result["issues"][0]["file"])

    def test_rejects_private_path_from_git_history_even_when_ignored_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            private = root / "daily/secret.md"
            private.parent.mkdir()
            private.write_text("synthetic")
            subprocess.run(["git", "add", "daily/secret.md"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "old"], cwd=root, check=True)
            private.unlink()
            (root / ".gitignore").write_text("daily/\n")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "remove"], cwd=root, check=True)
            result = CHECK.check(root)
            self.assertEqual("FAILED", result["status"])
            self.assertIn("Git history", result["issues"][0]["issue"])

    def test_builds_artifact_from_verified_candidate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "safe.md").write_text("portable")
            output = Path(tmp) / "release.tar.gz"
            result = CHECK.build_artifact(root, output)
            self.assertEqual("OK", result["status"])
            with tarfile.open(output) as archive:
                self.assertEqual(["wiki-agents/safe.md"], archive.getnames())

    def test_rejects_symlink_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            outside = Path(tmp) / "outside.md"
            outside.write_text("secret")
            (root / "link.md").symlink_to(outside)
            result = CHECK.check(root)
            self.assertEqual("FAILED", result["status"])
            self.assertIn("regular file", result["issues"][0]["issue"])


if __name__ == "__main__":
    unittest.main()
