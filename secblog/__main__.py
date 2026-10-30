"""CLI: python -m secblog {collect,backfill,build,serve,all}"""
from __future__ import annotations

import argparse
import asyncio
import http.server
import socketserver
import sys
from functools import partial
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="secblog", description="Security research blog archive")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="Poll all live feeds (incremental, conditional requests)")
    c.add_argument("--concurrency", type=int, default=16)
    c.add_argument("--only", nargs="*", help="Limit to these source ids")

    b = sub.add_parser("backfill", help="Resumable historical backfill")
    b.add_argument("--budget", type=int, default=35 * 60, help="Seconds to spend this run")
    b.add_argument("--concurrency", type=int, default=10)
    b.add_argument("--only", nargs="*", help="Limit to these source ids")
    b.add_argument("--prefer-dead", action="store_true", help="Prioritize Wayback backfill for dead blogs")

    s = sub.add_parser("build", help="Build the static site into dist/")
    s.add_argument("--no-pagefind", action="store_true")

    sv = sub.add_parser("serve", help="Serve dist/ locally under /secblog/")
    sv.add_argument("--port", type=int, default=8080)

    a = sub.add_parser("all", help="collect + backfill + build")
    a.add_argument("--budget", type=int, default=35 * 60)
    a.add_argument("--concurrency", type=int, default=16)

    args = p.parse_args(argv)

    if args.cmd == "collect":
        from .collect import run
        result = asyncio.run(run(concurrency=args.concurrency, only=args.only))
        print(result["summary"])
        if result["errors"]:
            print(f"{len(result['errors'])} sources with errors (see data/state/last_run.json)")
        return 0

    if args.cmd == "backfill":
        from .backfill import run
        result = asyncio.run(run(budget_s=args.budget, concurrency=args.concurrency, only=args.only, prefer_dead=args.prefer_dead))
        print(result)
        return 0

    if args.cmd == "build":
        from .site import build
        stats = build(pagefind=not args.no_pagefind)
        print(stats)
        return 0

    if args.cmd == "serve":
        from .site import DIST, BASE_PATH
        if not DIST.exists():
            print("dist/ missing — run build first", file=sys.stderr)
            return 1
        # Serve so that /secblog/* maps to dist/*
        class Handler(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path: str) -> str:
                if path.startswith(BASE_PATH):
                    path = path[len(BASE_PATH):] or "/"
                return super().translate_path(path)

            def log_message(self, fmt, *a):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

        handler = partial(Handler, directory=str(DIST))
        with socketserver.TCPServer(("", args.port), handler) as httpd:
            print(f"Serving {DIST} at http://127.0.0.1:{args.port}{BASE_PATH}/")
            httpd.serve_forever()
        return 0

    if args.cmd == "all":
        from .collect import run as collect_run
        from .backfill import run as backfill_run
        from .site import build
        print("== collect ==")
        print(asyncio.run(collect_run(concurrency=args.concurrency))["summary"])
        print("== backfill ==")
        print(asyncio.run(backfill_run(budget_s=args.budget, concurrency=max(6, args.concurrency // 2))))
        print("== build ==")
        print(build())
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
