"""Unified command-line compatibility manager for the three CLI platforms."""

from __future__ import annotations

import argparse
import subprocess
from typing import List, Optional

from agent import GatewayManager, ManagementJob, PLATFORMS, _default_config
import server as unified_server


JOB_ACTIONS = {"deps", "install-cli", "login", "login-status"}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manage a local CLI gateway")
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("action", nargs="?", default="serve")
    args, rest = parser.parse_known_args(argv)
    profile = _default_config()["profiles"][args.platform]
    if args.action in JOB_ACTIONS:
        command = ManagementJob()._command(args.platform, args.action, profile)
        env = GatewayManager()._child_env({**profile, "platform": args.platform})
        return subprocess.call(command, env=env)
    backend_args = rest
    if args.platform == "antigravity" and args.action == "serve":
        backend_args = ["serve", *rest]
    elif args.action != "serve":
        backend_args = [args.action, *rest]
    return unified_server.main(["--platform", args.platform, *backend_args])


if __name__ == "__main__":
    raise SystemExit(main())
