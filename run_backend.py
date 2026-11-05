#!/usr/bin/env python3
"""
Entry point: start the backend API + dashboard.

    python run_backend.py
    python run_backend.py --host 0.0.0.0 --port 8080

Then open http://127.0.0.1:8000 in a browser.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import load_config                              # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Urban Intelligence backend")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    host = args.host or config.get("backend.host", "127.0.0.1")
    port = args.port or int(config.get("backend.port", 8000))

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed.  pip install fastapi uvicorn")
        return 1

    print(f"\n  Dashboard : http://{host}:{port}/")
    print(f"  API docs  : http://{host}:{port}/docs\n")
    uvicorn.run(
        "backend.app:app", host=host, port=port,
        reload=args.reload, log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
