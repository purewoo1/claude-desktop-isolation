"""Optional host-side Git and cluster gateways with fixed operations."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import socketserver
import stat
import subprocess
import tempfile
import time

import cluster_reader
from guard import INNER, UnixRelay, serve

HERE = Path(__file__).resolve().parent
GIT = "/usr/bin/git"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\.git")
REF_PREFIX = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_.-]*/)+")
GIT_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_VARIANT": "ssh"}
CLUSTER_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


def local_file(value, excluded, private):
    if not isinstance(value, str) or not value or not Path(value).expanduser().is_absolute():
        raise ValueError("Use an absolute local path or a path starting with ~/")
    path = Path(value).expanduser().resolve(strict=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or (private and (
            info.st_uid != os.getuid() or info.st_mode & 0o077)):
        raise ValueError("Use owner-only regular files for private connection settings and keys")
    if any(path.is_relative_to(parent) for parent in excluded):
        raise ValueError("Keep host connection files outside mounted directories")
    if any(character in str(path) for character in '\r\n"\\%'):
        raise ValueError("Unsupported character in local connection file path")
    return str(path)


def ssh_command(settings, excluded):
    key = local_file(settings["key"], excluded, private=True)
    known_hosts = local_file(settings["known_hosts"], excluded, private=False)
    return ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none", "-o", "ClearAllForwardings=yes",
            "-o", "ForwardAgent=no", "-o", "UpdateHostKeys=no", "-o", "ControlMaster=no",
            "-o", "ConnectTimeout=7", "-o", f'UserKnownHostsFile="{known_hosts}"',
            "-o", "GlobalKnownHostsFile=/dev/null", "-i", key]


def cluster_path(path):
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Cluster roots and mounts must be absolute paths")
    if not path.isprintable() or ".." in path.split("/") or len(path) > 1024:
        raise ValueError("Invalid cluster path")
    return str(PurePosixPath(path))


def config_cluster(settings, excluded):
    if not isinstance(settings, dict):
        raise ValueError("Invalid cluster settings")
    hosts = {}
    for alias, node in settings.items():
        if not NAME.fullmatch(alias) or not all(NAME.fullmatch(node[k]) for k in ("host", "user")):
            raise ValueError("Invalid cluster alias, host or account name")
        port = node.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid cluster SSH port")
        repositories = {}
        for name, repo in node.get("repositories", {}).items():
            if (not NAME.fullmatch(name) or not isinstance(repo["source_dirs"], list)
                    or not 1 <= len(repo["source_dirs"]) <= 32):
                raise ValueError("Invalid cluster repository scope")
            for directory in repo["source_dirs"]:
                cluster_reader.relative_parts(directory)
            root = cluster_path(repo["path"])
            if root == "/":
                raise ValueError("A cluster repository must have a dedicated path")
            tips = repo["tips"]
            if (not isinstance(tips, list) or not 1 <= len(tips) <= 16
                    or not all(cluster_reader.COMMIT.fullmatch(tip) for tip in tips)):
                raise ValueError("Cluster repository history requires approved commit IDs")
            repositories[name] = {"root": root, "source_dirs": repo["source_dirs"], "tips": tips}
        for field in ("roots", "mounts"):
            if not isinstance(node.get(field, []), list):
                raise ValueError("Cluster roots and mounts must be lists")
        policy = {"roots": [cluster_path(p) for p in node.get("roots", [])],
                  "mounts": [cluster_path(p) for p in node.get("mounts", [])],
                  "repositories": repositories}
        if "/" in policy["roots"]:
            raise ValueError("The system root cannot be a cluster read scope")
        command = ssh_command(node, excluded) + ["-p", str(port), "--", f'{node["user"]}@{node["host"]}']
        hosts[alias] = (command, policy)
    return hosts


def cluster_output(command):
    limit = cluster_reader.MAX_BYTES
    deadline = time.monotonic() + 18
    result = bytearray()
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=CLUSTER_ENV) as child:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while len(result) <= limit:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError
                    data = os.read(child.stdout.fileno(), min(65536, limit + 1 - len(result)))
                    if not data:
                        if child.wait(timeout=max(0.01, deadline - time.monotonic())) == 0:
                            return bytes(result)
                        break
                    result.extend(data)
            raise RuntimeError("Cluster command failed or exceeded the read limit")
        finally:
            if child.poll() is None:
                child.kill()


class ClusterHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(6)
            raw = self.rfile.readline(8193)
            if not raw.endswith(b"\n") or len(raw) > 8192:
                raise ValueError("Invalid cluster request")
            args = json.loads(raw)
            if args == ["hosts"]:
                result = ("\n".join(sorted(self.server.hosts)) + "\n").encode()
            else:
                if not isinstance(args, list) or len(args) < 2 or not isinstance(args[0], str):
                    raise ValueError("Use a configured cluster alias and a read-only operation")
                if args[0] not in self.server.hosts:
                    raise ValueError("Unknown cluster alias")
                command, policy = self.server.hosts[args[0]]
                cluster_reader.validate(args[1:], policy)
                program = f"SERVER_POLICY = {policy!r}\n" + self.server.reader
                remote = shlex.join(["/usr/bin/python3", "-I", "-B", "-S", "-c", program, *args[1:]])
                result = cluster_output([*command, remote])
            self.wfile.write(f"OK {len(result)}\n".encode() + result)
        except (TypeError, ValueError):
            self.wfile.write(b"ERR Invalid request or path outside the configured scope\n")
        except (RuntimeError, TimeoutError, subprocess.SubprocessError):
            self.wfile.write(b"ERR Cluster read failed or timed out\n")
        except OSError:
            return


class GitHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(6)
            command = bytearray()
            for _ in range(14):
                data = self.request.recv(1)
                if not data or data == b"\n":
                    break
                command.extend(data)
            self.request.settimeout(None)
            ssh, remote, prefix, env = self.server.gateway
            if command == b"upload" and data == b"\n":
                argv = [*ssh, "--", "git@ssh.github.com", f"git-upload-pack {shlex.quote(remote)}"]
                subprocess.run(argv, stdin=self.request, stdout=self.request, stderr=subprocess.DEVNULL,
                               timeout=180, env=CLUSTER_ENV, check=True)
            elif command == b"receive" and data == b"\n" and prefix:
                self.receive(remote, prefix, env)
            else:
                self.reject("Operation outside the configured Git scope")
        except (OSError, subprocess.SubprocessError):
            try:
                self.reject("Host Git gateway could not complete the request")
            except OSError:
                pass

    def reject(self, message):
        packet = ("ERR " + message + "\n").encode()
        self.request.sendall(f"{len(packet) + 4:04x}".encode() + packet)

    def receive(self, remote, prefix, env):
        with tempfile.TemporaryDirectory(prefix="guard-git-", dir=self.server.tempdir) as directory:
            ref = f"refs/heads/{prefix}*"
            url = "git@github.com:" + remote
            options = {"env": env, "cwd": directory, "check": True, "timeout": 120,
                       "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            subprocess.run([GIT, "init", "--bare", "--template="], **options)
            subprocess.run([GIT, "fetch", "--no-tags", "--no-recurse-submodules", url,
                            f"+{ref}:{ref}"], **options)
            # Use the current upstream refs as the receive-pack baseline. The pre-receive
            # hook publishes first, so a local success is reported only after upstream accepts.
            hook_dir = Path(directory) / "hooks"
            hook_dir.mkdir()
            (hook_dir / "pre-receive").symlink_to(HERE / "git_pre_receive.py")
            command = [GIT, "-c", "receive.denyDeletes=true", "-c", "receive.denyNonFastForwards=true",
                       "-c", "receive.fsckObjects=true", "receive-pack", "."]
            hide = ["-c", "transfer.hideRefs=refs", "-c", f"transfer.hideRefs=!refs/heads/{prefix}"]
            subprocess.run([command[0], *hide, *command[1:]], cwd=directory,
                           env={**env, "GUARD_GIT_REMOTE": url, "GUARD_GIT_PREFIX": prefix},
                           stdin=self.request, stdout=self.request, stderr=subprocess.DEVNULL,
                           timeout=180, check=True)


@contextmanager
def connection_mounts(path, temporary, proxy_port, excluded):
    try:
        filename = local_file(str(path.expanduser().absolute()), excluded, private=True)
        settings = json.loads(Path(filename).read_text())
        if not isinstance(settings, dict) or not settings.keys() <= {"git", "clusters"}:
            raise ValueError("Unknown connection settings")
        with ExitStack() as stack:
            args = []
            if "clusters" in settings:
                path = temporary / "cluster.sock"
                hosts = config_cluster(settings["clusters"], excluded)
                server = stack.enter_context(serve(UnixRelay(str(path), ClusterHandler), None))
                server.hosts = hosts
                server.reader = "\n".join((HERE / file).read_text() for file in
                                          ("cluster_status.py", "cluster_reader.py"))
                args += ["--ro-bind", str(path), "/run/guard/cluster.sock"]
            if "git" in settings:
                git = settings["git"]
                remote = git["repository"]
                prefix = git.get("review_prefix")
                if not REPO.fullmatch(remote) or (prefix is not None and (
                        not REF_PREFIX.fullmatch(prefix)
                        or subprocess.run([GIT, "check-ref-format", f"refs/heads/{prefix}example"],
                                          env=GIT_ENV).returncode)):
                    raise ValueError("Invalid Git repository or review branch prefix")
                ssh = ssh_command(git, excluded) + ["-p", "443", "-o", "HostKeyAlias=github.com"]
                if git.get("via_proxy", True):
                    if not Path("/usr/bin/nc.openbsd").is_file():
                        raise ValueError("Install netcat-openbsd for Git via the proxy")
                    ssh += ["-o", f"ProxyCommand=/usr/bin/nc.openbsd -X connect -x 127.0.0.1:{proxy_port} %h %p"]
                env = {**GIT_ENV, "GIT_SSH_COMMAND": shlex.join(ssh)}
                path = temporary / "git.sock"
                server = stack.enter_context(serve(UnixRelay(str(path), GitHandler), None))
                server.gateway = (ssh, remote, prefix, env)
                server.tempdir = temporary
                args += ["--ro-bind", str(path), "/run/guard/git.sock",
                         "--setenv", "GIT_SSH_COMMAND", f"{INNER}/bin/git-ssh",
                         "--setenv", "GIT_SSH_VARIANT", "simple",
                         "--setenv", "CLAUDE_GUARD_REPOSITORY", remote]
            yield args
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise RuntimeError(f"Invalid private connection config ({error.__class__.__name__})") from None
