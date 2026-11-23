"""Offline regressions for the fixed cluster operations and review push boundary."""
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

from connections import GIT_ENV, ClusterHandler, GitHandler
from guard import UnixRelay, serve

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "review/claude/"


def git(*args, cwd=None, env=None, check=True):
    return subprocess.run(["/usr/bin/git", *args], cwd=cwd, env=env or GIT_ENV,
                          check=check, capture_output=True, timeout=12)


class TestGateways(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.repo = self.temp / "project"
        self.repo.mkdir()
        git("init", "-q", "--initial-branch=main", cwd=self.repo)
        git("config", "user.name", "Fixture", cwd=self.repo)
        git("config", "user.email", "fixture@example.invalid", cwd=self.repo)
        self.source = self.repo / "src"
        self.source.mkdir()
        self.sample = self.source / "sample.py"
        self.sample.write_text("X = 1\n")
        (self.source / "excluded.so").write_text("binary fixture")
        (self.source / "alias.py").symlink_to("/etc/passwd")
        git("add", ".", cwd=self.repo)
        git("commit", "-qm", "Initial fixture", cwd=self.repo)
        self.tip = self.revision("HEAD")

    def revision(self, ref, cwd=None):
        return git("rev-parse", ref, cwd=cwd or self.repo).stdout.decode().strip()

    def script(self, name, body):
        script = self.temp / name
        script.write_text(body)
        script.chmod(0o700)
        return script

    def server(self, name, handler):
        path = self.temp / name
        return path, self.stack.enter_context(serve(UnixRelay(str(path), handler), None))

    def test_cluster(self):
        path, server = self.server("cluster.sock", ClusterHandler)
        policy = {"roots": [str(self.repo)], "mounts": [],
                  "repositories": {"demo": {"root": str(self.repo), "tips": [self.tip], "source_dirs": ["src"]}}}
        shell = self.script("ssh-test", '#!/bin/sh\nexec /bin/sh -c "$1"\n')
        server.hosts = {"worker": ([str(shell)], policy)}
        server.reader = "\n".join((ROOT / name).read_text() for name in ("cluster_status.py", "cluster_reader.py"))

        def query(*args):
            with socket.socket(socket.AF_UNIX) as client, client.makefile("rb") as reader:
                client.settimeout(12)
                client.connect(str(path))
                client.sendall(json.dumps(args).encode() + b"\n")
                header = reader.readline()
                if header.startswith(b"OK "):
                    return reader.read(int(header.split()[1]))
                return header

        self.assertEqual(query("hosts"), b"worker\n")
        self.assertIn("filesystems", json.loads(query("worker", "status")))
        self.assertEqual(query("worker", "read", str(self.sample)), b"X = 1\n")
        self.assertIn(self.tip.encode(), query("worker", "log", "demo"))
        self.assertEqual(query("worker", "read-commit", "demo", self.tip, "src/sample.py"), b"X = 1\n")
        self.assertEqual(json.loads(query("worker", "list-worktree", "demo")),
                         [{"path": "src/sample.py", "bytes": 6}])
        for args in [("read", str(self.source / "alias.py")), ("read", str(self.source / "../.git/config")),
                     ("read-commit", "demo", self.tip, "src/excluded.so"),
                     ("read-commit", "demo", "1" * 40, "src/sample.py"), ("run", "demo")]:
            self.assertTrue(query("worker", *args).startswith(b"ERR "), args)

    def test_git(self):
        path, server = self.server("git.sock", GitHandler)
        upstream = self.temp / "upstream.git"
        git("init", "-q", "--bare", "--initial-branch=main", str(upstream))
        git("push", str(upstream), "HEAD:main", f"HEAD:{PREFIX}existing", cwd=self.repo)
        self.sample.write_text("X = 2\n")
        git("commit", "-qam", "Update fixture", cwd=self.repo)
        remote = "fixture/project.git"
        url = "git@github.com:" + remote
        backend = self.script("ssh-test", rf"""#!/bin/sh
for argument; do command=$argument; done
case "$command" in
  git-upload-pack\ *) operation=upload-pack ;;
  git-receive-pack\ *) operation=receive-pack ;;
  *) exit 1 ;;
esac
exec /usr/bin/env -i PATH=/usr/bin:/bin /usr/bin/git "$operation" "{upstream}"
""")
        transport_env = {**GIT_ENV, "GIT_SSH_COMMAND": str(backend)}
        server.gateway = ([str(backend)], remote, PREFIX, transport_env)
        server.tempdir = self.temp
        helper = self.script("git-helper", (ROOT / "bin/git-ssh").read_text().replace(
            "/run/guard/git.sock", str(path)))
        client_env = {**GIT_ENV, "GIT_SSH_VARIANT": "simple", "GIT_SSH_COMMAND": str(helper),
                      "CLAUDE_GUARD_REPOSITORY": remote}
        self.assertIn(b"refs/heads/main", git("ls-remote", url, env=client_env, cwd=self.repo).stdout)
        self.assertNotEqual(git("ls-remote", "git@github.com:fixture/other.git", cwd=self.repo,
                                env=client_env, check=False).returncode, 0)
        for branch in ("new", "existing"):
            result = git("push", url, f"HEAD:refs/heads/{PREFIX}{branch}", cwd=self.repo,
                         env=client_env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        for ref in ("HEAD:refs/heads/main", "HEAD:refs/tags/forbidden", f":refs/heads/{PREFIX}existing",
                    f"+{self.tip}:refs/heads/{PREFIX}existing"):
            self.assertNotEqual(git("push", url, ref, env=client_env, cwd=self.repo, check=False).returncode, 0, ref)
        self.assertEqual(self.revision("refs/heads/main", upstream), self.tip)
        self.assertEqual(self.revision(f"refs/heads/{PREFIX}existing", upstream), self.revision("HEAD"))
