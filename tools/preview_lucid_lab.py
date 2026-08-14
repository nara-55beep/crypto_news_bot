"""Serve only the read-only Lucid Strategy Lab for local visual checks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lucid_lab.web import routes


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes(routes())
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8123, type=int)
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
