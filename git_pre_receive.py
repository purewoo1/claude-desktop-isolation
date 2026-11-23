#!/usr/bin/python3
"""Publish only append-only updates under the operator's review branch prefix."""
import os
import re
import subprocess
import sys

GIT = "/usr/bin/git"
ZERO = "0" * 40
OID = re.compile("[0-9a-f]{40}")


def git(*args):
    return subprocess.run([GIT, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=60, check=True)


def publish():
    prefix = "refs/heads/" + os.environ["GUARD_GIT_PREFIX"]
    remote = os.environ["GUARD_GIT_REMOTE"]
    refs = []
    seen = set()
    for line in sys.stdin:
        if len(line) > 1100 or len(refs) >= 32:
            raise ValueError
        old, new, ref = line.split()
        if (not OID.fullmatch(old) or not OID.fullmatch(new) or new == ZERO
                or not ref.startswith(prefix) or ref == prefix or ref in seen):
            raise ValueError
        git("check-ref-format", ref)
        git("cat-file", "-e", new + "^{commit}")
        if old != ZERO:
            git("merge-base", "--is-ancestor", old, new)
        seen.add(ref)
        refs.append(f"{new}:{ref}")
    if not refs:
        raise ValueError
    git("push", "--atomic", "--porcelain", "--", remote, *refs)


if __name__ == "__main__":
    try:
        publish()
    except (KeyError, OSError, ValueError, subprocess.SubprocessError):
        sys.exit("Review push rejected by policy or upstream. Fetch and verify the target branch.")
