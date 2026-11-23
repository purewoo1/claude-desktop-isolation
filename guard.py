#!/usr/bin/env python3
"""Launch Claude Desktop with a private display, home, and proxy-only network."""
import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import select
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
INNER = "/opt/claude-guard"
PRIVATE_HOME = "/home/claude"
PROXY_PORT = 18080
DEFAULT_PACKAGE = Path("/usr/lib/claude-desktop")


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with self.server.dial() as upstream:
                upstream.settimeout(None)
                channels = {self.request: upstream, upstream: self.request}
                pending = set(channels)
                while pending:
                    ready, _, _ = select.select(list(pending), [], [], 300)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        target = channels[source]
                        if data:
                            target.sendall(data)
                        else:
                            pending.remove(source)
                            try:
                                target.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass
        except OSError:
            pass


class UnixRelay(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class LoopbackRelay(socketserver.ThreadingTCPServer):
    daemon_threads = True


@contextmanager
def serve(server, dial):
    server.dial = dial
    with server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join()


def connect_unix(path):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(path)
    except OSError:
        connection.close()
        raise
    return connection


@contextmanager
def process(args, **kwargs):
    with subprocess.Popen(args, **kwargs) as child:
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def private_profile(mode):
    state = Path.home() / ".local/share/claude-desktop-isolation"
    profile = state / {"desktop": "profile", "browser": "browser-profile", "cli": "cli-profile"}[mode]
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (state, profile):
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RuntimeError(f"Expected a private, user-owned directory at {path}")
    return profile


def workspace_path(path):
    source = path.expanduser().resolve(strict=True)
    home = Path.home().resolve()
    private = (home / name for name in (".ssh", ".gnupg", ".aws", ".config", ".local", ".codex",
                                       ".claude", ".cache", ".mozilla", ".password-store", ".kube"))
    if (not source.is_dir() or source == home or source in home.parents
            or source in (Path("/mnt"), Path("/media"), Path("/data"), Path("/opt"))):
        raise RuntimeError("Choose one existing project directory.")
    if any(source == root or source.is_relative_to(root) for root in private):
        raise RuntimeError("A private host directory cannot be mounted as a project.")
    if any(source == Path(root) or source.is_relative_to(Path(root))
           for root in ("/etc", "/usr", "/proc", "/sys", "/dev", "/run", "/var", "/tmp")):
        raise RuntimeError("Choose a project directory outside system paths.")
    return source


def sandbox(temporary, relay_path):
    uid, gid = os.getuid(), os.getgid()
    passwd = temporary / "passwd"
    group = temporary / "group"
    passwd.write_text(f"claude:x:{uid}:{gid}:Claude:{PRIVATE_HOME}:/bin/bash\n")
    group.write_text(f"claude:x:{gid}:\n")
    cmd = ["/usr/bin/bwrap", "--unshare-user", "--unshare-net", "--unshare-pid",
           "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL", "--die-with-parent",
           "--new-session", "--clearenv", "--ro-bind", "/usr", "/usr",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
           "--dir", "/run", "--dir", "/work", "--dir", "/home", "--dir", PRIVATE_HOME,
           "--dir", "/run/guard", "--chmod", "0700", "/run/guard",
           "--ro-bind", str(passwd), "/etc/passwd", "--ro-bind", str(group), "/etc/group",
           "--ro-bind", str(relay_path), "/run/proxy.sock"]
    for path in ("/lib", "/lib64", "/etc/ld.so.cache", "/etc/ssl", "/etc/fonts",
                 "/etc/machine-id", "/etc/os-release"):
        if Path(path).exists():
            cmd += ["--ro-bind", path, path]
    cmd += ["--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin"]
    env = {
        "HOME": PRIVATE_HOME, "USER": "claude", "LOGNAME": "claude",
        "PATH": f"{INNER}/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": "/run/guard", "XDG_CONFIG_HOME": f"{PRIVATE_HOME}/.config",
        "XDG_DATA_HOME": f"{PRIVATE_HOME}/.local/share", "XDG_CACHE_HOME": f"{PRIVATE_HOME}/.cache",
        "XDG_DATA_DIRS": f"{INNER}/share:/usr/share", "BROWSER": f"{INNER}/bin/xdg-open",
        "HTTP_PROXY": f"http://127.0.0.1:{PROXY_PORT}", "HTTPS_PROXY": f"http://127.0.0.1:{PROXY_PORT}",
        "http_proxy": f"http://127.0.0.1:{PROXY_PORT}", "https_proxy": f"http://127.0.0.1:{PROXY_PORT}",
        "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1",
    }
    for key, value in env.items():
        cmd += ["--setenv", key, value]
    return cmd + ["--chdir", "/work"]


def file_mounts(args):
    command = []
    if args.workspace is not None:
        command += ["--bind" if args.workspace_write else "--ro-bind",
                    str(workspace_path(args.workspace)), "/work"]
    if args.cuda:
        devices = [p for p in Path("/dev").glob("nvidia*") if p.is_char_device() and
                   (p.name[6:].isdigit() or p.name in ("nvidiactl", "nvidia-uvm", "nvidia-uvm-tools"))]
        if not any(p.name[6:].isdigit() for p in devices):
            raise RuntimeError("No NVIDIA compute devices found.")
        command += ["--ro-bind", "/sys", "/sys"]
        for path in devices:
            command += ["--dev-bind", str(path), str(path)]
    if args.conda_root:
        root = args.conda_root.expanduser().resolve(strict=True)
        if root == Path.home().resolve() or root in Path.home().resolve().parents:
            raise RuntimeError("The Conda root must be a specific installation.")
        prefix = root if args.conda_env == "base" else root / "envs" / args.conda_env
        if not (root / "bin/conda").is_file() or not (prefix / "bin/python").is_file():
            raise RuntimeError("The selected Conda installation/environment is unavailable.")
        command += ["--bind" if args.conda_write else "--ro-bind", str(root), str(root),
                    "--setenv", "CONDA_PREFIX", str(prefix), "--setenv", "CONDA_DEFAULT_ENV", args.conda_env]
    prefix = f"{prefix}/bin:" if args.conda_root else ""
    command += ["--setenv", "PATH", f"{INNER}/bin:{prefix}/usr/bin:/bin"]
    return command


def inner(command):
    with serve(LoopbackRelay(("127.0.0.1", PROXY_PORT), RelayHandler),
               lambda: connect_unix("/run/proxy.sock")):
        return subprocess.call(command)


def start_xephyr(stack, temporary, resize):
    from shutil import which
    for name in ("Xephyr", "xauth", "xdpyinfo", "openbox"):
        if not which(name):
            raise RuntimeError(f"Missing dependency: {name}. See README.")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("Run from a graphical session with X11 or Xwayland available.")
    number = next((n for n in range(80, 100)
                   if not Path(f"/tmp/.X11-unix/X{n}").exists()
                   and not Path(f"/tmp/.X{n}-lock").exists()), None)
    if number is None:
        raise RuntimeError("No free nested display.")
    display = f":{number}"
    x_socket = Path(f"/tmp/.X11-unix/X{number}")
    auth = temporary / "Xauthority"
    auth.touch(mode=0o600)
    # Feed the cookie on stdin and wildcard only the address family of the private display.
    subprocess.run(["xauth", "-f", str(auth)], input=f"add {display} . {secrets.token_hex(16)}\n",
                   text=True, check=True)
    records = subprocess.check_output(["xauth", "-f", str(auth), "nlist", display], text=True)
    subprocess.run(["xauth", "-f", str(auth), "nmerge", "-"],
                   input="".join("ffff" + line[4:] + "\n" for line in records.splitlines()),
                   text=True, check=True)
    title = f"Claude Desktop (Isolated) [{number}]"
    server = stack.enter_context(process(["Xephyr", display, "-screen", "1280x900",
                        *(["-resizeable"] if resize else []),
                        "-noreset", "-no-host-grab", "-nolisten", "tcp",
                        "-auth", str(auth), "-title", title]))
    for _ in range(60):
        if server.poll() is not None:
            raise RuntimeError("Xephyr failed to start.")
        if x_socket.is_socket():
            return server, display, x_socket, auth, title
        time.sleep(0.1)
    raise RuntimeError("Timed out starting the nested display.")


def launch(args, stack, temporary, relay_path):
    profile = private_profile(args.mode)
    lock = stack.enter_context((profile.parent / f"{args.mode}.lock").open("a"))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise RuntimeError("This isolated profile is already in use.") from error
    command = sandbox(temporary, relay_path) + file_mounts(args)
    command += [
        "--ro-bind", str(HERE / "guard.py"), f"{INNER}/guard.py",
        "--ro-bind", str(HERE / "session.py"), f"{INNER}/session.py",
        "--ro-bind", str(HERE / "bin"), f"{INNER}/bin",
        "--ro-bind", str(HERE / "share"), f"{INNER}/share",
        "--bind", str(profile), PRIVATE_HOME,
        "--ro-bind", str(HERE / "share/managed-settings.json"), "/etc/claude-code/managed-settings.json",
        "--setenv", "CLAUDE_GUARD_SCALE", str(args.scale),
    ]
    if args.connections is not None:
        from connections import connection_mounts
        excluded = [profile.resolve(), HERE]
        excluded += [directory.expanduser().resolve() for directory in
                     (args.workspace, args.conda_root, args.app_dir if args.mode == "desktop" else None)
                     if directory is not None]
        command += stack.enter_context(connection_mounts(
            args.connections, temporary, args.proxy_port, excluded))
    if args.usage_panel:
        directory = profile / ".claude/skills/private-usage-panel"
        directory.mkdir(parents=True, exist_ok=True)
        command += ["--ro-bind", str(HERE / "plugins/private-usage-panel"),
                    f"{PRIVATE_HOME}/.claude/skills/private-usage-panel"]
    if args.mode == "cli":
        if args.cli_bin is None:
            raise RuntimeError("Pass the official native CLI binary as --cli-bin.")
        binary = args.cli_bin.expanduser().resolve(strict=True)
        with binary.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise RuntimeError("--cli-bin must point to the native Linux executable.")
        command += ["--ro-bind", str(binary), "/opt/claude-cli",
                    "--setenv", "BROWSER", f"{INNER}/bin/no-browser",
                    "--", "/usr/bin/python3", f"{INNER}/guard.py",
                    "_inner", "/opt/claude-cli", *args.cli_args]
        if sys.stdin.isatty() and sys.stdout.isatty():
            # Interactive tools get a dedicated PTY, never the host shell's terminal.
            command.remove("--new-session")
            return subprocess.call(["/usr/bin/script", "-qefc", shlex.join(command), "/dev/null"],
                                   env={**os.environ, "SHELL": "/bin/sh"})
        return subprocess.call(command)

    if args.mode == "desktop":
        if not (args.app_dir / "claude-desktop").is_file():
            raise RuntimeError("Install the official desktop package or set --app-dir.")
        command += ["--ro-bind", str(args.app_dir.resolve()), "/opt/claude-desktop"]
    chrome = Path("/opt/google/chrome")
    if not (chrome / "chrome").is_file():
        raise RuntimeError("Install Google Chrome for the private browser. See README.")
    command += ["--ro-bind", str(chrome), str(chrome)]
    display_server, display, x_socket, auth, title = start_xephyr(stack, temporary, args.resize)
    command += [
        "--ro-bind", str(x_socket), str(x_socket), "--ro-bind", str(auth), "/run/display.auth",
        "--setenv", "DISPLAY", display, "--setenv", "XAUTHORITY", "/run/display.auth",
        "--setenv", "GTK_THEME", "Adwaita:dark",
    ]
    if args.rime:
        command += ["--setenv", "CLAUDE_GUARD_RIME", "1", "--setenv", "GTK_IM_MODULE", "ibus",
                    "--setenv", "QT_IM_MODULE", "ibus", "--setenv", "XMODIFIERS", "@im=ibus",
                    "--setenv", "IBUS_ADDRESS", "unix:path=/run/guard/ibus"]
    command += ["--", "/usr/bin/python3", f"{INNER}/guard.py", "_inner",
                "/usr/bin/dbus-run-session", f"--config-file={INNER}/share/session.conf",
                "--", "/usr/bin/python3", f"{INNER}/session.py", args.mode]
    with process(command) as desktop:
        if args.resize:
            stack.enter_context(process(["/usr/bin/python3", str(HERE / "geometry.py"),
                                         "--display", display, "--authority", str(auth), "--title", title,
                                         "--app-class", "Google-chrome" if args.mode == "browser" else "com.anthropic.Claude"]))
        if args.clipboard:
            stack.enter_context(process(["/usr/bin/python3", str(HERE / "clipboard.py"),
                                         "--inner-display", display, "--authority", str(auth)]))
        while desktop.poll() is None and display_server.poll() is None:
            time.sleep(0.25)
        return desktop.returncode if desktop.returncode is not None else 0


class ProbeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if self.request.recv(64) == b"probe":
            self.request.sendall(b"proxy-only")


def check(stack, temporary, relay_path):
    listener = LoopbackRelay(("127.0.0.1", 0), ProbeHandler)
    address = listener.server_address
    stack.enter_context(serve(UnixRelay(str(relay_path), RelayHandler),
                              lambda: socket.create_connection(address, timeout=3)))
    # Copy no profile into the test environment.
    command = sandbox(temporary, relay_path) + [
        "--ro-bind", str(HERE / "guard.py"), f"{INNER}/guard.py",
        "--", "/usr/bin/python3", f"{INNER}/guard.py", "_inner",
        "/usr/bin/python3", f"{INNER}/guard.py", "_probe", str(address[1])]
    with serve(listener, None):
        subprocess.run([*command, "up"], timeout=15, check=True)
    subprocess.run([*command, "down"], timeout=15, check=True)
    return 0


def inner_probe(host_port, state):
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("A host network interface is visible")
    for address in (("127.0.0.1", int(host_port)), ("192.0.2.1", 443)):
        try:
            with socket.create_connection(address, timeout=1):
                raise RuntimeError("A direct connection bypassed isolation")
        except OSError:
            pass
    with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=3) as conn:
        try:
            conn.sendall(b"probe")
            response = conn.recv(64)
        except ConnectionResetError:
            response = b""
        if response != (b"proxy-only" if state == "up" else b""):
            raise RuntimeError(f"Unexpected relay response with upstream {state}")
    if any(name in os.environ for name in ("SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS")):
        raise RuntimeError("Host environment leaked into the sandbox")
    print(f"PASS [{state}]: private network, no direct route, scoped relay, host agent/bus absent")
    return 0


def main():
    os.umask(0o077)
    if sys.argv[1:2] == ["_inner"]:
        return inner(sys.argv[2:])
    if sys.argv[1:2] == ["_probe"]:
        return inner_probe(*sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="offline namespace and mock-relay test")
    parser.add_argument("--proxy-port", type=int, help="host loopback HTTP or mixed proxy port")
    parser.add_argument("--app-dir", type=Path, default=DEFAULT_PACKAGE, help="official package directory")
    parser.add_argument("--workspace", type=Path, help="one optional local project, mounted as /work")
    parser.add_argument("--workspace-write", action="store_true", help="allow edits to the mounted project")
    parser.add_argument("--scale", type=float, default=1.25, help="Claude window scale (default 1.25)")
    parser.add_argument("--mode", choices=("desktop", "browser", "cli"), default="desktop")
    parser.add_argument("--cli-bin", type=Path)
    parser.add_argument("--clipboard", action="store_true", help="share the X11 text/image clipboard")
    parser.add_argument("--resize", action="store_true", help="fit Claude to the outer window")
    parser.add_argument("--rime", action="store_true", help="enable private IBus Rime")
    parser.add_argument("--usage-panel", action="store_true", help="load the optional local usage mod")
    parser.add_argument("--cuda", action="store_true", help="mount the host NVIDIA compute devices")
    parser.add_argument("--conda-root", type=Path)
    parser.add_argument("--conda-env", default="base")
    parser.add_argument("--conda-write", action="store_true")
    parser.add_argument("--connections", type=Path, help="private Git and cluster config file")
    argv = sys.argv[1:]
    cli_args = []
    if "--" in argv:
        delimiter = argv.index("--")
        argv, cli_args = argv[:delimiter], argv[delimiter + 1:]
    args = parser.parse_args(argv)
    args.cli_args = cli_args
    if os.getuid() == 0 or not shutil.which("bwrap"):
        parser.error("Run unprivileged with bubblewrap installed.")
    if not 0.75 <= args.scale <= 2 or (args.workspace_write and args.workspace is None):
        parser.error("Use a scale from 0.75 to 2. Workspace write requires --workspace.")
    if not args.conda_env.replace("-", "").replace("_", "").isalnum() or (args.conda_write and not args.conda_root):
        parser.error("Specify a valid Conda env name. Conda write requires --conda-root.")
    if args.mode == "cli" and (args.clipboard or args.resize or args.rime):
        parser.error("Clipboard, resize and IBus require a graphical mode.")
    if cli_args and args.mode != "cli":
        parser.error("Arguments after -- are for CLI mode only.")
    with ExitStack() as stack:
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="claude-guard-", dir="/tmp")))
        relay = temp / "proxy.sock"
        if args.check:
            return check(stack, temp, relay)
        if args.proxy_port is None or not 1 <= args.proxy_port <= 65535:
            parser.error("Specify your own local HTTP/mixed proxy port.")
        address = ("127.0.0.1", args.proxy_port)
        with socket.create_connection(address, timeout=3):
            pass
        stack.enter_context(serve(UnixRelay(str(relay), RelayHandler),
                                  lambda: socket.create_connection(address, timeout=5)))
        return launch(args, stack, temp, relay)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"claude-guard: {error}")
