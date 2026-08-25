"""The check that refuses to leave a credential in a browser test artifact.

Browser tests sign a real merchant in, and a Playwright trace records the arguments of every
action, the DOM around it and the network it caused. The harness keeps the credential out of
those by starting each trace after sign in; `scripts/check-e2e-artifacts.py` is what proves it,
and this is what proves the check itself works.

Three properties, and each is a way the check could pass while being useless:

- a trace is a zip and its entries are compressed, so a scanner that searched the file bytes
  would find nothing and report success. The leak here is deflated, and a plain substring search
  over the same file is asserted to miss it.
- a scan that found no files would pass forever after somebody stopped producing them.
- a tool that reports a leaked credential by printing it has moved the leak rather than found
  it.
"""

import importlib.util
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from agentrank_api.auth.console import CONSOLE_SESSION_SCHEME, is_console_session_verifier
from agentrank_api.auth.tokens import SECRET_BYTES, TokenMarker

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY_ROOT / "scripts" / "check-e2e-artifacts.py"

# One well formed development credential, generated here rather than issued: nothing in this file
# authenticates, and a token that came from the real minter would be a real secret in a test.
CREDENTIAL = f"ar_{TokenMarker.DEVELOPMENT.value}_{'0' * 32}_{'a' * (SECRET_BYTES * 2)}"
SECRET_HALF = CREDENTIAL.rsplit("_", 1)[-1]

# One well formed console session verifier and one well formed console session cookie. The
# verifier authenticates every merchant endpoint and must never survive in an artifact; the cookie
# is inert without the console's deployment secret and is expected to be in every signed in trace.
SESSION_VERIFIER = f"{CONSOLE_SESSION_SCHEME}_{'b' * 64}"
SESSION_COOKIE_VALUE = f"arc_{'c' * 64}"

# Enough repetition that a compressor has something to do, so the deflated entry genuinely does
# not contain the plain bytes.
PADDING = "navigated to /compiler and read the review queue " * 60

# The same volume with nothing a caller would demand be present, for the artifact that captured
# a session in which nothing happened.
QUIET = "idle idle idle " * 60


def scanner() -> ModuleType:
    """The script, imported by path because its name is not an identifier."""
    specification = importlib.util.spec_from_file_location("check_e2e_artifacts", SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def trace(path: Path, body: str, *, network: str = PADDING) -> Path:
    """One file shaped like a Playwright trace: a zip whose entries are compressed."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("trace.trace", body)
        archive.writestr("trace.network", network)
    return path


def run(*arguments: str, secrets: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  the interpreter and the script are this repository's
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "AGENTRANK_E2E_SECRETS": secrets},
        check=False,
    )


def test_the_scanned_shape_is_the_one_this_application_mints() -> None:
    """A pattern that drifted from the minter would stop recognising real credentials."""
    pattern = re.compile(scanner().TOKEN.pattern.decode())
    assert pattern.fullmatch(CREDENTIAL) is not None
    assert pattern.search(f"fill(value={CREDENTIAL}) then click") is not None
    # A digest is not a credential, and this repository is full of digests.
    assert pattern.search("sha256:" + "f" * 64) is None


def test_the_scanned_session_shape_is_the_one_the_api_accepts() -> None:
    """A console session credential authenticates as the merchant, so it is scanned for too."""
    pattern = re.compile(scanner().CONSOLE_SESSION.pattern.decode())
    assert is_console_session_verifier(SESSION_VERIFIER)
    assert pattern.fullmatch(SESSION_VERIFIER) is not None
    assert pattern.search(f"authorization: Bearer {SESSION_VERIFIER}") is not None
    # A merchant API key is caught by the other rule, not by this one.
    assert pattern.search(CREDENTIAL) is None


def test_a_console_session_credential_in_an_artifact_is_refused(tmp_path: Path) -> None:
    artifact = trace(tmp_path / "trace.zip", f"Bearer {SESSION_VERIFIER} {PADDING}")

    result = run(str(artifact))

    assert result.returncode == 1
    assert "shaped like a console session credential" in result.stdout
    assert SESSION_VERIFIER not in result.stdout + result.stderr


def test_the_console_session_cookie_is_not_treated_as_a_credential(tmp_path: Path) -> None:
    """The one documented exception, pinned so it cannot widen by accident.

    A trace of a signed in browser necessarily carries the session cookie. It is not a credential
    the API would accept: the console derives the verifier above from it under a deployment
    secret the browser test generates per run and never writes down. What must stay true is that
    the exception covers this value and nothing the API accepts, which the test above asserts
    from the other side.
    """
    artifact = trace(tmp_path / "trace.zip", f"cookie: ar_console_session={SESSION_COOKIE_VALUE}")

    result = run(str(artifact))

    assert result.returncode == 0
    assert not is_console_session_verifier(SESSION_COOKIE_VALUE)


def test_a_compressed_credential_is_found_where_a_substring_search_would_miss_it(
    tmp_path: Path,
) -> None:
    artifact = trace(tmp_path / "trace.zip", f"fill value {CREDENTIAL} {PADDING}")

    assert CREDENTIAL.encode() not in artifact.read_bytes()

    result = run(str(artifact))

    assert result.returncode == 1
    assert "shaped like a merchant API key" in result.stdout


def test_an_exact_secret_is_found_even_when_it_is_not_token_shaped(tmp_path: Path) -> None:
    """A leak of the half that matters, without the recognisable wrapper around it."""
    artifact = trace(tmp_path / "trace.zip", f"cookie=abc {SECRET_HALF} {PADDING}")

    unconfigured = run(str(artifact))
    configured = run(str(artifact), secrets=CREDENTIAL)

    assert unconfigured.returncode == 0
    assert configured.returncode == 1
    assert "configured secret" in configured.stdout


def test_a_finding_never_prints_the_value_it_found(tmp_path: Path) -> None:
    artifact = trace(tmp_path / "trace.zip", f"fill value {CREDENTIAL} {PADDING}")

    result = run(str(artifact), secrets=CREDENTIAL)

    assert result.returncode == 1
    printed = result.stdout + result.stderr
    assert CREDENTIAL not in printed
    assert SECRET_HALF not in printed
    # It still says enough to find the leak: which file, which entry inside it, which rule.
    assert "trace.zip::trace.trace" in printed


def test_a_clean_artifact_passes_and_names_what_it_scanned(tmp_path: Path) -> None:
    artifact = trace(tmp_path / "trace.zip", PADDING)

    result = run(str(artifact), secrets=CREDENTIAL)

    assert result.returncode == 0
    assert "2 stream(s) scanned" in result.stdout


def test_nothing_to_scan_is_a_failure_rather_than_a_pass(tmp_path: Path) -> None:
    """A check that passes because it found no files keeps passing after they stop existing."""
    result = run(str(tmp_path / "absent"))

    assert result.returncode == 1
    assert "no browser test artifacts found" in result.stdout


def test_a_required_marker_refuses_an_artifact_that_captured_nothing(tmp_path: Path) -> None:
    """A clean trace of an empty session proves nothing, so the caller may demand content."""
    artifact = trace(tmp_path / "trace.zip", QUIET, network=QUIET)

    missing = run(str(artifact), "--require", "/compiler")
    present = run(str(trace(tmp_path / "real.zip", PADDING)), "--require", "/compiler")

    assert missing.returncode == 1
    assert "none contained" in missing.stdout
    assert present.returncode == 0


@pytest.mark.parametrize("name", ["trace.zip", "screenshot.png"])
def test_every_file_under_a_directory_is_scanned(tmp_path: Path, name: str) -> None:
    """Artifacts are not only traces, and a scan of one kind would miss the others."""
    directory = tmp_path / "test-results" / "a-test"
    directory.mkdir(parents=True)
    if name.endswith(".zip"):
        trace(directory / name, f"{CREDENTIAL} {PADDING}")
    else:
        (directory / name).write_text(f"{CREDENTIAL} {PADDING}")

    result = run(str(tmp_path / "test-results"))

    assert result.returncode == 1
    assert "shaped like a merchant API key" in result.stdout
