#!/usr/bin/env python3
"""Fail when the em dash character appears in tracked project text.

The character is written as an escape here on purpose. Spelling it literally would make
this file fail its own check.

Scanning is driven by git: tracked files plus untracked files that are not ignored. That
is what "relevant project text" means in practice, since git already excludes .git, .venv,
node_modules, .next, coverage, build output and the local docs directory. Including
untracked files matters because this check runs before `git add`, so a brand new file must
be caught on its first run rather than on the next one.
"""

import shutil
import subprocess
import sys
from pathlib import Path

EM_DASH = "\u2014"

# Generated dependency manifests are not project authored text. Their contents come from
# package registries and may legitimately contain any character.
EXCLUDED = frozenset({"uv.lock", "pnpm-lock.yaml"})


def tracked_files() -> list[Path]:
    # Resolved rather than invoked by bare name, so a missing git fails with a clear
    # message instead of relying on whatever PATH happens to contain.
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is required to determine which files to scan")

    # S603 is suppressed because every argument below is a literal. No caller supplied
    # value reaches this call, so there is nothing to sanitize.
    completed = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
        text=True,
    )
    return [Path(name) for name in completed.stdout.split("\0") if name]


def offences(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError, FileNotFoundError:
        # Binary, or staged for deletion. Neither is project text.
        return []

    found = []
    for number, line in enumerate(content.splitlines(), start=1):
        column = line.find(EM_DASH)
        if column >= 0:
            found.append(f"{path}:{number}:{column + 1}: {line.strip()}")
    return found


def main() -> int:
    failures = [
        offence
        for path in tracked_files()
        if path.as_posix() not in EXCLUDED
        for offence in offences(path)
    ]

    if failures:
        print("em dash found in tracked project text:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("\nUse ordinary punctuation instead.", file=sys.stderr)
        return 1

    print("text style clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
