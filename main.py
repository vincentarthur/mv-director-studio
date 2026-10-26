#!/usr/bin/env python3
"""Main entrypoint for MV Multimodal Evaluation, Storyline & Generation Studio (v2.3.0)."""
from __future__ import annotations

import argparse
import os
import uvicorn

from app.server import app

__all__ = ["app", "main"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MV Multimodal Evaluation, Storyline & Generation Studio v2.3.0"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Host interface to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="Port to listen on (default: $PORT or 8080)",
    )
    args = parser.parse_args()
    print(
        f"🚀 MV Multimodal Evaluation, Storyline & Generation Studio v2.3.0 listening on http://{args.host}:{args.port}",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
