"""Compatibility import for the unified Grok ACP implementation."""

import sys
from pathlib import Path

CLI_ROOT = Path(__file__).resolve().parents[1]
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from cli_gateway.backends import grok_acp as _implementation

globals().update({key: value for key, value in vars(_implementation).items() if not key.startswith("__")})
