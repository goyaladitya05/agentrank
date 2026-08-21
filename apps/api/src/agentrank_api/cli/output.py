"""How an operator command writes a machine readable answer.

Its own module so that every command group prints `--json` the same way rather than each one
having a private copy that drifts.
"""

import json
from collections.abc import Mapping
from typing import Any, TextIO


def write_json(out: TextIO, payload: Mapping[str, Any]) -> None:
    """One JSON document and nothing else, with keys in a stable order.

    Sorted rather than insertion ordered, so a script diffing two runs sees a difference only
    when something actually differs.
    """
    print(json.dumps(payload, indent=2, sort_keys=True), file=out)
