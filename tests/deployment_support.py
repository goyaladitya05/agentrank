"""Starting the real supported topology from a test, as an orchestrator and an operator would.

Shared by every smoke test that needs processes rather than an ASGI transport. The pieces here
are the ones that have nothing to do with what is being proved: how a process is started, how it
is waited for, how it is stopped, and how an operator command is run and read.

What is deliberately not here is the environment. A deployment's environment is the thing each
smoke test is making a statement about, so each one builds its own rather than inheriting a
shared one that would quietly decide what they are testing.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx2

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# How long a started process gets to answer before this gives up on it, and how often it is asked.
BOOT_TIMEOUT_SECONDS = 60.0
BOOT_POLL_SECONDS = 0.2

# One operator command should never take this long. Bounded so a hung subprocess is a named
# failure rather than a job timeout with no test attached to it.
COMMAND_TIMEOUT_SECONDS = 180.0


def operator(environment: dict[str, str], *arguments: str) -> dict[str, object]:
    """Run one operator command as a shell would, and return the JSON it printed.

    A failure raises with the command's own stderr attached, because the useful thing about a
    bootstrap step that did not work is what it said.
    """
    finished = subprocess.run(  # noqa: S603  the interpreter and the module are this repository's
        [sys.executable, "-m", "agentrank_api.cli", *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if finished.returncode != 0:
        raise AssertionError(
            f"operator command {' '.join(arguments)} exited {finished.returncode}:"
            f" {finished.stderr.strip()}"
        )
    parsed: dict[str, object] = json.loads(finished.stdout)
    return parsed


def wait_for(url: str, *, expect: int = 200) -> httpx2.Response:
    """Poll one endpoint until it answers as expected, or fail naming what it did instead."""
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    last = "never answered"
    while time.monotonic() < deadline:
        try:
            response = httpx2.get(url, timeout=2.0)
        except httpx2.HTTPError as unreachable:
            last = type(unreachable).__name__
        else:
            if response.status_code == expect:
                return response
            last = f"HTTP {response.status_code}"
        time.sleep(BOOT_POLL_SECONDS)
    raise AssertionError(f"{url} did not answer {expect} within the boot timeout: {last}")


class Deployment:
    """One API process, started and stopped as an orchestrator would.

    Its output is captured rather than discarded, for two reasons that pull the same way. A
    process that fails to boot otherwise produces only "did not answer within the boot timeout",
    with the reason it gave thrown away. And its startup log is itself under test: the line that
    names this deployment's capabilities, and the absence of any configured value beside them,
    are the things the configuration work added and the only place to read them is here.
    """

    def __init__(self, environment: dict[str, str], port: int, log: Path) -> None:
        self._environment = environment
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._log = log
        self._process: subprocess.Popen[bytes] | None = None

    def output(self) -> str:
        """Everything this deployment's processes have written, across restarts."""
        return self._log.read_text(errors="replace") if self._log.exists() else ""

    def start(self) -> None:
        self._stream = self._log.open("ab")
        self._process = subprocess.Popen(  # noqa: S603  this repository's own module
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agentrank_api.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=REPOSITORY_ROOT,
            env=self._environment,
            stdout=self._stream,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for(f"{self.base_url}/health")
        except AssertionError as unhealthy:
            # What the process said about why, rather than only that it never answered.
            raise AssertionError(f"{unhealthy}\n--- process output ---\n{self.output()}") from None

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # A process ignoring SIGTERM must not leak a listener on the port and take the next
            # start down with it.
            self._process.kill()
            self._process.wait(timeout=30)
        self._process = None
        self._stream.close()

    def restart(self) -> None:
        """A process boundary an orchestrator would create, made for real.

        Not a new object in one process. The old process is signalled, waited for and gone, and
        what comes up afterwards has an empty heap, a new connection pool and no knowledge of
        anything the first one did that it did not write down.
        """
        self.stop()
        self.start()
