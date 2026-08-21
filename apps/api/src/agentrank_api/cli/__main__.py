"""Entry point for `python -m agentrank_api.cli`.

`raise SystemExit` rather than `sys.exit`, and the exit code comes back from `main` rather
than being set inside it, so that a test can call `main` and read the number instead of
catching an exception.
"""

from agentrank_api.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
