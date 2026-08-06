"""Unified CLI gateway entrypoint.

Examples:
    python server.py
    python server.py --platform codex --port 8120
    python server.py --platform grok --port 8100
    python server.py --platform antigravity serve --port 8110
"""

from __future__ import annotations

import argparse
from importlib import import_module
import json
import time
from typing import List, Optional

from cli_gateway import SUPPORTED_PLATFORMS


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Unified CLI -> OpenAI gateway")
    parser.add_argument("--platform", choices=SUPPORTED_PLATFORMS, help="internal/legacy single-platform backend")
    args, backend_args = parser.parse_known_args(argv)
    if not args.platform:
        from agent import UnifiedGatewayFleet, _load_config

        config = _load_config()
        config["enabled"] = True
        fleet = UnifiedGatewayFleet()
        fleet.apply(config)
        print(json.dumps(fleet.status(), ensure_ascii=False), flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            fleet.stop()
        return 0
    backend = import_module(f"cli_gateway.backends.{args.platform}")
    result = backend.main(backend_args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
