"""Exercise publication guards with real temporary Git history; no registry writes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/docker-release-channel.sh"


class ReleaseChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("commit", "--allow-empty", "-m", "base")
        self.git("remote", "add", "origin", str(self.repo))
        self.git("tag", "1.2.3")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    def run_guard(self, ref, version, channel=None):
        output = self.root / "output"
        output.write_text("")
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.repo,
            env={**os.environ, "GITHUB_REF": ref, "PACKAGE_VERSION": version,
                 "GITHUB_OUTPUT": str(output)},
            capture_output=True,
            text=True,
        )
        if channel is None:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(output.read_text(), "")
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), f"channel={channel}\n")

    def test_release_tag(self):
        self.run_guard("refs/tags/1.2.3", "1.2.3", "main")

    def test_v_release_tag(self):
        self.git("tag", "v1.2.3")
        self.run_guard("refs/tags/v1.2.3", "1.2.3", "main")

    def test_manual_tagged_main(self):
        self.run_guard("refs/heads/main", "1.2.3", "main")

    def test_untagged_main(self):
        self.git("commit", "--allow-empty", "-m", "untagged")
        self.run_guard("refs/heads/main", "1.2.4.dev1")

    def test_prerelease_rejected(self):
        self.git("tag", "1.2.4.dev1")
        self.run_guard("refs/tags/1.2.4.dev1", "1.2.4.dev1")

    def test_version_mismatch_rejected(self):
        self.run_guard("refs/tags/1.2.3", "1.2.3.dev348")

    def test_tag_on_non_main_commit_rejected(self):
        self.git("checkout", "-b", "dev")
        self.git("commit", "--allow-empty", "-m", "dev only")
        self.git("tag", "1.2.4")
        self.run_guard("refs/tags/1.2.4", "1.2.4")

    def test_tag_points_elsewhere_rejected(self):
        self.git("commit", "--allow-empty", "-m", "new main")
        self.run_guard("refs/tags/1.2.3", "1.2.3")

    def test_dev_unchanged(self):
        self.run_guard("refs/heads/dev", "1.2.4.dev7", "dev")

    def test_feature_manual_does_not_publish_main(self):
        self.run_guard("refs/heads/fix/example", "1.2.4.dev7", "")


if __name__ == "__main__":
    unittest.main()
