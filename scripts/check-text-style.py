#!/usr/bin/env python3
"""Fail when an em dash or an emoji appears in tracked project text.

Both characters are written as escapes here on purpose. Spelling either literally would
make this file fail its own check.

Emoji are refused for the same reason the em dash is: this project's text is read in
terminals, diffs, log lines and HTTP responses, where an emoji is a rendering question
rather than a word. The ranges below are the pictographic blocks plus the variation
selector that turns an ordinary character into one. Ordinary symbols that happen to live
near them, the arrows and the box drawing characters this repository uses in its own
diagrams, are deliberately outside every range.

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

# Inclusive codepoint ranges that are emoji rather than punctuation. Variation selector 16
# is included because it is what promotes a text glyph to an emoji presentation, so a bare
# one is an emoji this file would otherwise have to spell out to catch.
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # pictographs, symbols, transport, faces, flags, extended
    (0x1F004, 0x1F0CF),  # mahjong and playing cards
    (0x2600, 0x27BF),  # miscellaneous symbols and dingbats
    (0xFE0F, 0xFE0F),  # variation selector 16
    (0x1F1E6, 0x1F1FF),  # regional indicators, which pair into flags
)

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


def is_emoji(character: str) -> bool:
    point = ord(character)
    return any(low <= point <= high for low, high in EMOJI_RANGES)


def offences(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError, FileNotFoundError:
        # Binary, or staged for deletion. Neither is project text.
        return []

    found = []
    for number, line in enumerate(content.splitlines(), start=1):
        for column, character in enumerate(line, start=1):
            if character == EM_DASH:
                found.append(f"{path}:{number}:{column}: em dash: {line.strip()}")
            elif is_emoji(character):
                name = f"U+{ord(character):04X}"
                found.append(f"{path}:{number}:{column}: emoji {name}: {line.strip()}")
    return found


def main() -> int:
    failures = [
        offence
        for path in tracked_files()
        if path.as_posix() not in EXCLUDED
        for offence in offences(path)
    ]

    if failures:
        print("disallowed characters in tracked project text:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("\nUse ordinary punctuation and ordinary words instead.", file=sys.stderr)
        return 1

    print("text style clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
