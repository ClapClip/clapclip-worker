"""Execute the POSIX installer with local fake tools under a temporary root."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


class BootstrapShellTests(unittest.TestCase):
    def shell(self):
        if os.name == "nt":
            path = Path("D:/Tools/msys64/usr/bin/bash.exe")
            if not path.exists():
                self.skipTest("A local bash is needed for isolated shell execution")
            return str(path)
        return "/bin/sh"

    def fixture(self, directory, exit_code):
        base = Path(directory).resolve()
        root, fakebin = base / "host", base / "bin"
        root.mkdir(); fakebin.mkdir()
        def shell_path(path):
            value = path.as_posix()
            return "/" + path.drive[0].lower() + value[2:] if os.name == "nt" else value
        def executable(path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n" + text, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        for name, text in {"uname": 'case "$1" in -s) echo Linux;; -m) echo x86_64;; esac\n',
                           "id": "echo 0\n", "ldd": "echo 'GNU libc 2.39'\n", "systemctl": "exit 0\n",
                           "ps": "exit 0\n", "stat": 'case "$2" in %u) echo 0;; %a) echo 755;; esac\n'}.items():
            executable(fakebin / name, text)
        (root / "bootstrap-owner.txt").write_text("clapclip-bootstrap-v1\n")
        executable(root / "micromamba", "exit 77\n")
        digest = hashlib.sha256((root / "micromamba").read_bytes()).hexdigest()
        (root / "tools/conda-meta").mkdir(parents=True)
        (root / "tools/conda-meta/history").write_text("synthetic tools already installed")
        (root / "installer-source").mkdir()
        executable(root / "tools/bin/git", 'case "$*" in *remote.origin.url*) echo https://github.com/ClapClip/clapclip-worker.git;; esac\n')
        executable(root / "tools/bin/python", 'if [ "$1" = -m ] && [ "$2" = venv ]; then\n'
            '  mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; chmod 755 "$3/bin/python"\n'
            'elif [ "$1" = -B ]; then\n'
            f'  touch "{shell_path(base)}/entered"\n'
            f'  while [ ! -f "{shell_path(base)}/release" ]; do sleep 0.05; done\n'
            f'  exit {exit_code}\nfi\nexit 0\n')
        script = Path(__file__).parents[1].joinpath("scripts/install-worker.sh").read_text()
        script = script.replace("root=/var/lib/clapclip-worker", f"root='{shell_path(root)}'")
        script = script.replace("701f39c06f8e6a670a0e186c8479b9e5c0502a2f0cda6cef65d83bdd2875e8bd", digest)
        target = base / "installer.sh"
        target.write_text(script, encoding="utf-8", newline="\n")
        env = os.environ.copy()
        if os.name == "nt":
            # MSYS bash requires POSIX PATH separators; Windows paths remain
            # valid for quoted file arguments in the actual script.
            fake_path = "/" + fakebin.drive[0].lower() + fakebin.as_posix()[2:]
            env["PATH"] = fake_path + ":/usr/bin:/bin"
            env["MSYS2_ARG_CONV_EXCL"] = "*"
        else:
            env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
        return target, root, env

    def test_success_failure_and_competitor_all_release_only_their_own_lock(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                script, root, env = self.fixture(directory, exit_code)
                command = [self.shell(), str(script)]
                first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 10
                    while not Path(directory).joinpath("entered").exists() and first.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue(Path(directory).joinpath("entered").exists(), first.communicate(timeout=1) if first.poll() is not None else "bootstrap did not reach fixture")
                    competitor = subprocess.run(command, env=env, capture_output=True, timeout=5)
                    self.assertNotEqual(competitor.returncode, 0)
                    self.assertTrue((root / ".bootstrap-lock").exists())
                    Path(directory).joinpath("release").touch()
                    stdout, stderr = first.communicate(timeout=10)
                    self.assertEqual(first.returncode, exit_code, stderr.decode(errors="replace"))
                    self.assertFalse((root / ".bootstrap-lock").exists())
                    retry = subprocess.run(command, env=env, capture_output=True, timeout=10)
                    self.assertEqual(retry.returncode, exit_code, retry.stderr.decode(errors="replace"))
                    self.assertFalse((root / ".bootstrap-lock").exists())
                finally:
                    if first.poll() is None:
                        Path(directory).joinpath("release").touch()
                        first.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
