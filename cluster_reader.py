"""Fixed read-only cluster operations. The host supplies the policy, not clients."""
from contextlib import contextmanager
import itertools
import json
import os
from pathlib import PurePosixPath
import re
import stat
import subprocess
import sys

MAX_BYTES = 2 * 1024 * 1024
PAGE = 200
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
SOURCE_TYPES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".inl", ".cu", ".cuh",
    ".py", ".pyi", ".sh", ".yaml", ".yml", ".toml", ".json", ".md", ".rst", ".cmake",
}
BUILD_DIRS = {"build", "dist", "checkpoints", "datasets", "outputs", "__pycache__", "weights"}
PRIVATE_NAMES = re.compile(r"(^|[._ -])(?:credentials?|secrets?|tokens?|passwords?|"
                           r"private[-_ ]?key|authorized_keys|id_(?:rsa|ecdsa|ed25519))($|[._ -])", re.I)
SECRET_TEXT = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
                         rb"(?:gh[pousr]_|github_pat_|sk-ant-|sk-proj-|hf_|xox[baprs]-)[A-Za-z0-9_-]{16,}"
                         rb"|AKIA[A-Z0-9]{16}|(?im:^[ \t]*(?:password|api[_-]?key|"
                         rb"access[_-]?token)[ \t]*[:=][ \t]*[\x22\x27][^\x22\x27\r\n]{8,}[\x22\x27])")
COMMIT = re.compile("[0-9a-f]{40}")


def visible(name):
    return (0 < len(name) <= 255 and name.isprintable() and "/" not in name
            and not name.startswith(".") and not PRIVATE_NAMES.search(name)
            and PurePosixPath(name).suffix.casefold() not in {".pem", ".key", ".p12", ".ppk", ".kdbx"})


def relative_parts(path):
    parts = path.split("/")
    if len(path) > 1024 or len(parts) > 48 or not all(visible(part) for part in parts):
        raise ValueError("Path outside read scope")
    return parts


def approved_root(path, roots):
    for root in roots:
        if path == root:
            return root, []
        if path.startswith(root + "/"):
            return root, relative_parts(path[len(root) + 1:])
    raise ValueError("Path outside read scope")


def matches_source(path, scopes):
    try:
        parts = relative_parts(path)
    except ValueError:
        return False
    return (any(path.startswith(scope + "/") for scope in scopes)
            and not BUILD_DIRS.intersection(p.casefold() for p in parts)
            and (PurePosixPath(parts[-1]).suffix in SOURCE_TYPES or parts[-1] == "CMakeLists.txt"))


def offset(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 6:
        raise ValueError("Invalid page offset")
    return int(value)


def validate(args, policy):
    if not isinstance(args, list) or not args or not all(isinstance(arg, str) for arg in args):
        raise ValueError("Invalid request")
    op = args[0]
    if args in (["status"], ["roots"], ["repos"]):
        return
    if op in ("list", "stat", "read"):
        if len(args) != 2 and not (op == "list" and len(args) == 3):
            raise ValueError("Invalid file query")
        _, parts = approved_root(args[1], policy["roots"])
        if op == "read" and not parts:
            raise ValueError("A file path is required")
        if len(args) == 3:
            offset(args[2])
        return
    if len(args) < 2 or args[1] not in policy["repositories"]:
        raise ValueError("Unknown repository")
    repo = policy["repositories"][args[1]]
    if op in ("log", "list-worktree") and len(args) == 2:
        return
    if op == "log" and len(args) == 3:
        offset(args[2])
        return
    if op in ("list-commit", "read-commit"):
        count = 3 if op == "list-commit" else 4
        if len(args) == count and COMMIT.fullmatch(args[2]):
            if op == "list-commit" or matches_source(args[3], repo["source_dirs"]):
                return
    if op == "read-worktree" and len(args) == 3 and matches_source(args[2], repo["source_dirs"]):
        return
    raise ValueError("Operation outside the read-only interface")


@contextmanager
def directory(parts, base=None, owner=None):
    fd = os.open("/", DIR_FLAGS) if base is None else os.dup(base)
    try:
        for part in parts:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            if owner is not None and os.fstat(fd).st_uid != owner:
                raise ValueError("Directory owner is outside the account scope")
        yield fd
    finally:
        os.close(fd)


def file_info(info):
    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else None
    return {"type": kind, "bytes": info.st_size, "mtime_ns": info.st_mtime_ns} if kind else None


def plain_text(data):
    if len(data) > MAX_BYTES or b"\0" in data or SECRET_TEXT.search(data):
        raise ValueError("Only bounded text without recognized credential patterns is allowed")
    data.decode("utf-8")
    return data


def checked_read(parts, base, owner=None):
    with directory(parts[:-1], base, owner) as parent:
        fd = os.open(parts[-1], os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES
                    or (owner is not None and info.st_uid != owner)):
                raise ValueError("File is outside the read scope")
            with open(f"/proc/self/fd/{fd}", "rb") as stream:
                return plain_text(stream.read(MAX_BYTES + 1))
        finally:
            os.close(fd)


def entries(fd, owner=None):
    with os.scandir(fd) as iterator:
        for entry in iterator:
            if visible(entry.name):
                info = entry.stat(follow_symlinks=False)
                value = file_info(info)
                if value is not None and (owner is None or info.st_uid == owner):
                    yield {"name": entry.name, **value}


def account_query(args, policy):
    op, path = args[:2]
    root, parts = approved_root(path, policy["roots"])
    owner = os.geteuid()
    with directory(root.strip("/").split("/")) as base:
        if os.fstat(base).st_uid != owner:
            raise ValueError("Root owner is outside the account scope")
        if op == "read":
            return checked_read(parts, base, owner)
        if op == "list":
            skip = offset(args[2]) if len(args) == 3 else 0
            with directory(parts, base, owner) as fd:
                page = list(itertools.islice(entries(fd, owner), skip, skip + PAGE + 1))
                return {"entries": page[:PAGE], "next_offset": skip + PAGE if len(page) > PAGE else None}
        if not parts:
            return file_info(os.fstat(base))
        with directory(parts[:-1], base, owner) as fd:
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            value = file_info(info)
            if info.st_uid != owner or value is None:
                raise ValueError("Path is outside the read scope")
            return value


def git(repo, *args):
    path = repo["root"]
    return subprocess.check_output(
        ["/usr/bin/git", "--no-pager", "--no-optional-locks", "-c", f"safe.directory={path}",
         "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.allow=never",
         "-C", path, f"--git-dir={path}/.git", *args], timeout=6, stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
             "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1"},
    )


def approved_commit(repo, commit):
    try:
        git(repo, "cat-file", "-e", commit + "^{commit}")
    except subprocess.CalledProcessError:
        return False
    for tip in repo["tips"]:
        try:
            git(repo, "merge-base", "--is-ancestor", commit, tip)
            return True
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
    return False


def committed_entries(repo, commit, path=None):
    selection = ["--", *(f":(literal){scope}" for scope in repo["source_dirs"])]
    if path is not None:
        selection = ["--", f":(literal){path}"]
    data = git(repo, "ls-tree", "-r", "-l", "-z", commit, *selection)
    for entry in filter(None, data.split(b"\0")):
        header, raw_path = entry.split(b"\t", 1)
        mode, kind, oid, size = header.split()
        if mode not in (b"100644", b"100755") or kind != b"blob" or int(size) > MAX_BYTES:
            continue
        name = raw_path.decode("utf-8", errors="replace")
        if matches_source(name, repo["source_dirs"]):
            yield name, oid.decode("ascii"), int(size)


def worktree_entries(fd, scopes, prefix, depth=0):
    for entry in entries(fd):
        path = prefix + "/" + entry["name"]
        if entry["type"] == "directory" and depth < 12 and entry["name"].casefold() not in BUILD_DIRS:
            with directory([entry["name"]], fd) as child:
                yield from worktree_entries(child, scopes, path, depth + 1)
        elif entry["type"] == "file" and entry["bytes"] <= MAX_BYTES and matches_source(path, scopes):
            yield {"path": path, "bytes": entry["bytes"]}


def repository_query(args, repo):
    op = args[0]
    tips = repo["tips"]
    with directory(repo["root"].strip("/").split("/")) as base:
        if op == "read-worktree":
            return checked_read(relative_parts(args[2]), base)
        if op == "list-worktree":
            result = []
            for scope in repo["source_dirs"]:
                with directory(relative_parts(scope), base) as fd:
                    result.extend(itertools.islice(worktree_entries(fd, repo["source_dirs"], scope), 1001))
            if len(result) > 1000:
                raise ValueError("Worktree inventory exceeds the limit")
            return result
        # Refuse a symlink or gitdir pointer for an approved repository.
        with directory([".git"], base):
            if op == "log":
                skip = offset(args[2]) if len(args) == 3 else 0
                return git(repo, "log", "--no-notes", "--no-show-signature", f"--skip={skip}",
                           f"--max-count={PAGE}", "--format=%H %cI", *tips)
            commit = args[2]
            if not approved_commit(repo, commit):
                raise ValueError("Commit outside approved history")
            if op == "list-commit":
                return [{"path": name, "bytes": size} for name, _, size in committed_entries(repo, commit)]
            for name, oid, size in committed_entries(repo, commit, args[3]):
                if name == args[3]:
                    data = git(repo, "cat-file", "blob", oid)
                    if len(data) != size:
                        raise ValueError("Object changed during read")
                    return plain_text(data)
            raise ValueError("Source file missing from selected commit")


def query(args, policy):
    validate(args, policy)
    op = args[0]
    if op == "status":
        # The source sent to the cluster includes the fixed collector before this module.
        return node_status(policy["mounts"])
    if op == "roots":
        return policy["roots"]
    if op == "repos":
        return sorted(policy["repositories"])
    if op in ("list", "stat", "read"):
        return account_query(args, policy)
    return repository_query(args, policy["repositories"][args[1]])


if __name__ == "__main__":
    try:
        result = query(sys.argv[1:], SERVER_POLICY)
        output = result if isinstance(result, bytes) else (json.dumps(result, ensure_ascii=True) + "\n").encode()
        if len(output) > MAX_BYTES:
            raise ValueError("Result exceeds maximum response size")
        sys.stdout.buffer.write(output)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit(1)
