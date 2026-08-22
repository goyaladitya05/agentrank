"""Can a benchmark worker obtain the answer key. Asserted by having a real process try.

The attack this file is about was demonstrated by an independent test audit at the end of Phase
2B-R: a worker process, in exactly the environment and working directory the runner gives it,
ran `import agentrank_api.benchmark.voltedge` and read every mission's expected outcome indexed
by the mission key it had just been handed. The process boundary removed the database, every
secret and every other mission, and did not remove the authored suite, because the suite was
Python in the package the worker runs from.

These are mechanism tests rather than source greps, and the difference matters. A grep asserts
that a particular string is absent from a file somebody remembered to check. What is asserted
here is that a process started the way the runner starts one cannot reach the authored world by
any ordinary runtime route: the import system, the installed distribution, `sys.path`, the
working directory or the environment.

The probe is proved capable before it is believed. The same probe, run in this test process's
own environment and working directory, finds the authored world immediately, so a probe that
finds nothing in the worker's environment is evidence rather than a test that cannot fail.

What is deliberately not claimed is operating system sandboxing. The worker is an ordinary
process on the same filesystem, and in a developer checkout the repository is a readable
directory. The threat model is a model that is handed a brief and a tool schema, not hostile
native code, and docs/security.md says so in those words rather than implying more.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from benchmark_support import VOLTEDGE, VOLTEDGE_DIRECTORY

from agentrank_api.benchmark.endpoint import RequestLedger
from agentrank_api.benchmark.isolation import IsolatedMissionExecutor

# Two strings that appear in the authored world and nowhere in the package that is installed.
# A mission key is what an executor is handed and is the index into the answer key; a SKU is the
# catalog half of the same secret. Prose in the package mentions `voltedge` and a suite label, so
# neither of those would be evidence of anything.
MISSION_MARKER = "desktop-charger-on-a-budget"
CATALOG_MARKER = "VE-PWR-20K-NAV"

# The attack, as the audit performed it, plus every other ordinary way a process could look for
# the same document. It reports what it found rather than deciding anything, so one probe answers
# every question below and the assertions stay readable.
PROBE = """
import json, os, sys
from pathlib import Path

MARKERS = ("desktop-charger-on-a-budget", "VE-PWR-20K-NAV")
found = {"module": None, "path_files": [], "package_files": [], "cwd": [], "environment": []}

try:
    import agentrank_api.benchmark.voltedge as authored
    found["module"] = sorted(name for name in dir(authored) if not name.startswith("_"))
except ImportError:
    found["module"] = None

def carries(path):
    try:
        if path.stat().st_size > 1_000_000:
            return False
        return any(marker in path.read_text(encoding="utf-8", errors="replace")
                   for marker in MARKERS)
    except OSError:
        return False

for entry in sys.path:
    root = Path(entry or ".")
    if not root.is_dir():
        continue
    for candidate in root.rglob("*.json"):
        if carries(candidate):
            found["path_files"].append(str(candidate))

import agentrank_api
package = Path(agentrank_api.__file__).parent
for candidate in package.rglob("*"):
    if candidate.is_file() and carries(candidate):
        found["package_files"].append(str(candidate.relative_to(package)))

found["cwd"] = sorted(str(entry) for entry in Path.cwd().rglob("*"))
found["environment"] = sorted(os.environ)

print(json.dumps(found))
"""


def _probe(environment: dict[str, str], cwd: Path) -> dict[str, Any]:
    """Run the probe in one environment and working directory, and read what it found."""
    finished = subprocess.run(  # noqa: S603  the interpreter and the source are this repository's
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
        timeout=180,
        check=True,
    )
    reported: dict[str, Any] = json.loads(finished.stdout)
    return reported


def _in_the_workers_environment() -> dict[str, Any]:
    """The probe, in exactly what `IsolatedMissionExecutor` gives a worker.

    The environment comes from the executor rather than from a copy of its rule, and the working
    directory is an empty temporary one, which is what `_carry_out` creates before every mission.
    """
    executor = IsolatedMissionExecutor(
        base_url="http://127.0.0.1:1", token="unused", served=RequestLedger()
    )
    with tempfile.TemporaryDirectory(prefix="agentrank-oracle-probe-") as sandbox:
        return _probe(executor.environment, Path(sandbox))


# The probe is capable of finding what it is looking for.


def test_the_probe_finds_the_authored_world_from_an_ordinary_process() -> None:
    """The positive control, and the reason the refusals below are evidence.

    This process is trusted operator side code: it has the repository as its working directory
    and the developer's environment, which is exactly the situation the authored world is not
    hidden from. Finding it here is what proves a probe that finds nothing in the worker's
    environment has actually looked.
    """
    found = _probe(dict(os.environ), VOLTEDGE_DIRECTORY.parent.parent)

    assert any(Path(name).name in {"suite.json", "catalog.json"} for name in found["path_files"])


# What a worker can reach.


def test_a_worker_cannot_import_the_authored_suite() -> None:
    """The exact attack an independent audit demonstrated, run again and refused.

    `agentrank_api.benchmark.voltedge` was a module in the installed package and is now two JSON
    documents at the top of the repository. There is no module for this import to find, whatever
    the worker does with it.
    """
    found = _in_the_workers_environment()

    assert found["module"] is None


def test_a_worker_finds_no_authored_world_anywhere_it_can_import_from() -> None:
    """Not only the module. Anything on the import path that carries the answer key.

    `sys.path` in a worker is the interpreter's own entries plus whatever the installation puts
    there, which for an editable install is the source directory this package is built from. The
    authored world is a sibling of that directory rather than inside it, so nothing the import
    system can see holds a mission key or a catalog SKU.

    This is asserted over the package tree rather than over its source files, which found
    something the first time it ran: a checkout that had the old module still held the compiled
    copy of it under `__pycache__`, carrying every mission key and every expected outcome. No
    import can reach a cached module whose source is gone, and the file is still readable, so
    removing stale bytecode is part of removing the module rather than housekeeping.
    """
    found = _in_the_workers_environment()

    assert found["path_files"] == []
    assert found["package_files"] == []


def test_a_worker_has_nothing_to_read_in_its_working_directory_or_environment() -> None:
    """The two routes that are not the import system.

    The working directory is a fresh temporary one, so a relative path finds nothing. The
    environment is an allowlist that names no repository, no checkout and no benchmark
    directory, so there is no variable to resolve one from either.
    """
    found = _in_the_workers_environment()

    assert found["cwd"] == []
    named = [name for name in found["environment"] if name not in {"PATH", "HOME", "TMPDIR"}]
    assert not [name for name in named if "AGENTRANK" in name or "PYTHONPATH" in name]


# The oracle is still the evaluator's, and the brief is still all a buyer is handed.


def test_the_answer_key_the_attack_wanted_is_in_the_authored_world_the_worker_cannot_reach() -> (
    None
):
    """The other half of the claim, which is that there is something worth hiding.

    A test that only asserted the worker finds nothing would pass just as well if the markers
    were nonsense. These are the mission key and the SKU an executor would look up, they are in
    the authored documents, and the documents answer exactly the question a buyer must not be
    able to ask.
    """
    suite = (VOLTEDGE_DIRECTORY / "suite.json").read_text(encoding="utf-8")
    catalog = (VOLTEDGE_DIRECTORY / "catalog.json").read_text(encoding="utf-8")

    assert MISSION_MARKER in suite
    assert CATALOG_MARKER in catalog
    assert VOLTEDGE.suite.mission(MISSION_MARKER).oracle.expected_outcome is not None
    assert "expected_outcome" not in json.dumps(
        VOLTEDGE.suite.mission(MISSION_MARKER).brief.to_payload()
    )
