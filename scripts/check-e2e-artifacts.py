#!/usr/bin/env python3
"""Refuse to leave an AgentRank credential in anything a browser test run retains.

Browser tests sign a real merchant in, and the artifacts a browser test run leaves behind are
files nobody thinks of as credential stores: a Playwright trace records the arguments of every
action, the DOM around it and the network it caused, and `retain-on-failure` writes that out
whenever anything breaks. The harness keeps the credential out of them by starting each trace
after sign in, and this is what proves it actually did.

Two kinds of match are refused, and both matter:

- any string shaped like a merchant API key. `agentrank_api.auth.tokens` mints exactly one
  shape and says why it is recognisable, so a scanner can act on it without decoding anything.
  This catches a credential nobody told this script about, including one minted by a future
  test.
- the exact credentials this run used, and their secret halves, passed through
  `AGENTRANK_E2E_SECRETS`. Exact strings rather than a pattern, so there is nothing to be
  clever about and no false positive to explain away.

A trace is a zip and its entries are compressed, so scanning the file bytes would find nothing
and report success. Every zip is opened and every entry is read out before it is searched.

Nothing this script prints is the value it found. A tool that reports a leaked credential by
printing it has moved the leak rather than found it, so a finding names the file, the entry
inside it and which rule matched.

Exit codes: 0 when every artifact is clean, 1 when one is not, and 1 when there was nothing to
scan at all. The last one is deliberate: a check that passes because it found no files is a
check that would keep passing after somebody stopped producing them.
"""

import argparse
import os
import re
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

# The exact shape `agentrank_api.auth.tokens` mints, unanchored so it is found anywhere inside a
# larger document. Kept in step with that module by `tests/test_e2e_artifact_scan.py`.
TOKEN = re.compile(rb"ar_(?:live|dev)_[0-9a-f]{32}_[0-9a-f]{64}")

ZIP_MAGIC = b"PK\x03\x04"

SECRETS_VARIABLE = "AGENTRANK_E2E_SECRETS"


def configured_secrets() -> list[bytes]:
    """Every exact string this run must not have left behind.

    Read from the environment rather than from the command line, because an argument vector is
    readable by every process on the machine and a credential in one is a credential in `ps`.

    A merchant API key's secret half is scanned for on its own as well as inside the whole
    token. A leak that carried only the half that matters would otherwise pass.
    """
    secrets: list[bytes] = []
    for value in os.environ.get(SECRETS_VARIABLE, "").split():
        secrets.append(value.encode())
        half = value.rsplit("_", 1)[-1]
        if half and half != value:
            secrets.append(half.encode())
    return secrets


def streams(path: Path) -> Iterator[tuple[str, bytes]]:
    """Every byte stream inside one file, named for a person reading a failure.

    A zip yields one stream per entry, decompressed. Anything else yields itself. A zip this
    cannot open is yielded whole rather than skipped: refusing to read something is not the same
    as reading it and finding nothing.
    """
    try:
        head = path.open("rb").read(len(ZIP_MAGIC))
    except OSError as unreadable:
        raise SystemExit(f"{path}: could not be read: {unreadable.strerror}") from unreadable
    if head != ZIP_MAGIC:
        yield str(path), path.read_bytes()
        return
    try:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                with archive.open(entry) as opened:
                    yield f"{path}::{entry.filename}", opened.read()
    except zipfile.BadZipFile, OSError:
        yield str(path), path.read_bytes()


def findings(name: str, blob: bytes, secrets: list[bytes]) -> list[str]:
    """What is wrong with one byte stream, in words that never quote the value."""
    found = []
    if TOKEN.search(blob) is not None:
        found.append(f"{name}: contains a string shaped like a merchant API key")
    for index, secret in enumerate(secrets):
        if secret in blob:
            found.append(f"{name}: contains configured secret {index + 1}")
    return found


def files_under(paths: list[Path]) -> list[Path]:
    """Every regular file under the given paths. A path that does not exist contributes none."""
    collected: list[Path] = []
    for path in paths:
        if path.is_file():
            collected.append(path)
        elif path.is_dir():
            collected.extend(sorted(item for item in path.rglob("*") if item.is_file()))
    return collected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="files or directories to scan")
    parser.add_argument(
        "--require",
        default=None,
        help=(
            "text that must appear in at least one scanned stream. Proves the artifact really"
            " captured the session rather than being empty, so a clean result means something"
        ),
    )
    arguments = parser.parse_args()

    secrets = configured_secrets()
    scanned = files_under(arguments.paths)
    if not scanned:
        print(f"no browser test artifacts found under {' '.join(map(str, arguments.paths))}")
        return 1

    problems: list[str] = []
    required = arguments.require is None
    marker = None if arguments.require is None else arguments.require.encode()
    entries = 0
    for path in scanned:
        for name, blob in streams(path):
            entries += 1
            problems.extend(findings(name, blob, secrets))
            if marker is not None and marker in blob:
                required = True

    if problems:
        print(f"credential material found in {len(problems)} place(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1
    if not required:
        print(f"scanned {entries} stream(s) and none contained {arguments.require!r}")
        return 1
    print(
        f"{len(scanned)} artifact(s), {entries} stream(s) scanned:"
        " no credential material and no key shaped string"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
