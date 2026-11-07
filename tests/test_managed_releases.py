"""Official-main and fixed-release validation without any network or pip calls."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from mediaworker.managed.common import ManagedError, write_json
from mediaworker.managed.releases import ORIGIN, PROFILE, Releases


class ReleaseValidationTests(unittest.TestCase):
    def source(self, directory):
        root = Path(directory)
        source = root / "source"
        (source / ".git").mkdir(parents=True)
        config = ("[core]\nrepositoryformatversion=0\nbare=false\n"
                  f'[remote "origin"]\nurl={ORIGIN}\nfetch=+refs/heads/main:refs/remotes/origin/main\n'
                  '[branch "main"]\nremote=origin\nmerge=refs/heads/main\n')
        (source / ".git/config").write_text(config)
        release = Releases.__new__(Releases)
        release.root, release.data, release.source, release.base = root, root, source, root / "releases"
        release._git = Mock(side_effect=lambda *args, **kwargs: "main" if args[0] == "branch" else "")
        return release, source / ".git/config", config

    def test_unexpected_origin_refspec_and_config_are_rejected_before_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            release, path, config = self.source(directory)
            for text in (config.replace(ORIGIN, "https://unreviewed.test/source.git"),
                         config.replace("+refs/heads/main:refs/remotes/origin/main", "+refs/heads/*:refs/remotes/origin/*"),
                         config + "[include]\npath=unreviewed\n"):
                path.write_text(text)
                with self.assertRaises(ManagedError):
                    release.verify_source()
                release._git.assert_not_called()

    def test_dirty_source_stops_without_fetching(self):
        with tempfile.TemporaryDirectory() as directory:
            release, path, config = self.source(directory)
            release._git.side_effect = ["main", " M mediaworker/runner.py"]
            with self.assertRaisesRegex(ManagedError, "DIRTY_SOURCE_CACHE"):
                release.latest("a" * 40)
            self.assertFalse(any(call.args[0] == "fetch" for call in release._git.call_args_list))

    def test_divergent_history_never_selects_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            release, path, config = self.source(directory)
            def git(*args, **kwargs):
                if args[0] == "branch": return "main"
                if args[0] == "rev-parse": return "b" * 40
                if args[0] == "merge-base": raise ManagedError("RELEASE_VALIDATION_FAILED")
                return ""
            release._git.side_effect = git
            with self.assertRaises(ManagedError):
                release.latest("a" * 40)
            self.assertIn(("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"),
                          [call.args for call in release._git.call_args_list])

    def test_modified_or_extra_python_in_fixed_release_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            release, _, _ = self.source(directory)
            revision = "a" * 40
            root = release.release(revision)
            source = root / "mediaworker/fixture.py"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"pass\n")
            write_json(root / "release.json", {"commit": revision, "profile": PROFILE,
                       "files": {"mediaworker/fixture.py": hashlib.sha256(source.read_bytes()).hexdigest()}})
            release.verify_fixed(revision)
            source.with_name("unreviewed.py").write_bytes(b"pass\n")
            with self.assertRaisesRegex(ManagedError, "FIXED_RELEASE_MODIFIED"):
                release.verify_fixed(revision)

    def test_cpu_profile_never_replaces_imported_gpu_environment(self):
        release = Releases.__new__(Releases)
        with self.assertRaisesRegex(ManagedError, "DEPENDENCY_PROFILE_CHANGE_REQUIRES_REVIEW"):
            release.prepare("a" * 40, profile="windows-directml")


if __name__ == "__main__":
    unittest.main()
