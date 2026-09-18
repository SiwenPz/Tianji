#!/usr/bin/env python3
"""Publish this tree to the public repository.

The public repository is a curated export rather than a mirror, because this
working tree holds two kinds of document: what the people building Tianji need,
and what the people using it need. The split lives in exactly one place --
tools/public-exclude.txt -- so keeping an internal note internal stays a
one-line change that shows up in review, instead of a habit of remembering.

The export is a single commit built from the current HEAD tree, so a clone gets
the current version rather than the road that led to it. The working tree and
the index are left untouched: the commit is assembled with git plumbing and
pushed straight to the public remote.

    python tools/publish_public.py --dry-run
    python tools/publish_public.py
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_FILE = ROOT / "tools" / "public-exclude.txt"
PUBLIC_REMOTE = "public"
PUBLIC_BRANCH = "refs/heads/main"

MESSAGE = """\
chore: publish the current version

Assembled by tools/publish_public.py from the development tree as a single
commit: what the project is, without the working material it is not.
"""


def git(*args, env=None, check=True):
    result = subprocess.run(
        ("git",) + args, cwd=ROOT, env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        sys.exit("FAIL: git {}\n{}".format(" ".join(args), result.stderr.strip()))
    return result


def exclusion_patterns():
    lines = EXCLUDE_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines
            if line.strip() and not line.lstrip().startswith("#")]


def split(files, patterns):
    """(publish, held_back, unmatched) -- the split itself, no policy.

    An unmatched pattern is returned rather than raised, so a caller can decide
    what to do about it and a test can assert on it.
    """
    held = set()
    unmatched = []
    for pattern in patterns:
        matches = [path for path in files
                   if fnmatch.fnmatch(path, pattern)
                   or path.startswith(pattern.rstrip("/") + "/")]
        if not matches:
            unmatched.append(pattern)
            continue
        held.update(matches)
    return [path for path in files if path not in held], sorted(held), unmatched


def build_commit(publish, message):
    """A commit carrying exactly *publish*, built off to one side of the tree."""
    wanted = set(publish)
    with tempfile.TemporaryDirectory(prefix="tianji-publish-") as temporary:
        message_path = Path(temporary) / "message"
        message_path.write_text(message, encoding="utf-8")
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / "index"))

        git("read-tree", "HEAD", env=env)
        for path in git("ls-files", env=env).stdout.splitlines():
            if path and path not in wanted:
                git("update-index", "--force-remove", path, env=env)
        tree = git("write-tree", env=env).stdout.strip()
        return git("commit-tree", tree, "-F", str(message_path), env=env).stdout.strip()


def main():
    parser = argparse.ArgumentParser(
        description="Publish the current tree to the public repository")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the split and push nothing")
    args = parser.parse_args()

    files = [path for path in git("ls-files").stdout.splitlines() if path]
    publish, held, unmatched = split(files, exclusion_patterns())
    for pattern in unmatched:
        print(f"FAIL: exclusion pattern matches nothing: {pattern}", file=sys.stderr)
    if unmatched:
        return 1

    print(f"publish:   {len(publish)} files")
    print(f"held back: {len(held)} files")
    for path in held:
        print(f"  - {path}")
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("note: this exports HEAD, so uncommitted changes are not published")

    if args.dry_run:
        return 0

    commit = build_commit(publish, MESSAGE)
    git("push", "--force", PUBLIC_REMOTE, f"{commit}:{PUBLIC_BRANCH}")
    exported = [path for path in
                git("ls-tree", "-r", "--name-only", commit).stdout.splitlines() if path]
    if sorted(exported) != sorted(publish):
        print("FAIL: the pushed commit does not match the planned export", file=sys.stderr)
        return 1
    print(f"published {commit[:12]} to {PUBLIC_REMOTE} {PUBLIC_BRANCH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
