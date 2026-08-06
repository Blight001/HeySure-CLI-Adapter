"""Compatibility entrypoint; implementation lives in cli_gateway.backends.codex."""

import sys
from pathlib import Path

CLI_ROOT = Path(__file__).resolve().parents[1]
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from cli_gateway.backends import codex as _implementation

globals().update({key: value for key, value in vars(_implementation).items() if not key.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(main())
