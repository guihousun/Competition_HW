#!/usr/bin/env python3
"""Competition entry point (original sample name/layout, plus diagnostics).

``root`` is exactly ``Path(__file__).resolve().parent`` — the same assumption the
original sample makes — so ``src/`` and the nested runtime resolve unchanged. No
upward asset scan: the bundle keeps ``web/`` and ``docs/`` at the archive root,
which the existing ``parents[4]`` calculations already reach.
"""
import logging
import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python main3.py <port>")
    port = int(sys.argv[1])
    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
    )

    from agent import diagnostics, telemetry

    diagnostics.utf8_stdout()
    identity = diagnostics.emit_startup_identity(entry=sys.argv[0] or "main3.py", root=root)
    trace = telemetry.configure(root.parent.parent, identity)

    from agent.server import serve

    logging.info("listening on 0.0.0.0:%d", port)
    try:
        serve(port)
    finally:
        if trace is not None:
            trace.close()


if __name__ == "__main__":
    main()
