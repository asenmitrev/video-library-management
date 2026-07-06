"""Command-line interface for testing the core module.

    python -m backend.cli index /path/to/videos
    python -m backend.cli search "a dog catching a frisbee" -k 12
    python -m backend.cli status
    python -m backend.cli reset
"""

import argparse
import logging
import shutil
import sys
import time

from . import config, db


def cmd_index(args):
    from . import indexer

    t0 = time.time()
    progress = indexer.index_folder(args.folder)
    dt = time.time() - t0
    print(
        f"Indexed {progress.done_files} file(s), skipped {progress.skipped_files} "
        f"unchanged, {progress.failed_files} failed, in {dt:.1f}s"
    )
    for err in progress.errors:
        print(f"  ! {err}", file=sys.stderr)


def cmd_search(args):
    from . import search as search_mod

    t0 = time.time()
    results, has_more = search_mod.search(args.query, limit=args.k)
    dt = time.time() - t0
    if not results:
        print("No results (is anything indexed yet?)")
        return
    for r in results:
        print(
            f"{r['score']:.3f}  {r['path']}  "
            f"@ {r['start_sec']:.1f}s–{r['end_sec']:.1f}s"
        )
    more = ", more available with a larger -k" if has_more else ""
    print(f"({len(results)} results in {dt:.2f}s{more})")


def cmd_status(args):
    conn = db.connect()
    try:
        s = db.stats(conn)
    finally:
        conn.close()
    print(f"Database: {s['db_path']}")
    print(f"Indexed files: {s['files']}, segments: {s['segments']}")


def cmd_reset(args):
    config.db_path().unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        config.db_path().with_name(config.db_path().name + suffix).unlink(
            missing_ok=True
        )
    shutil.rmtree(config.thumbnails_dir(), ignore_errors=True)
    print("Database and thumbnails cleared.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="backend.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="index a folder of videos")
    p.add_argument("folder")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="search indexed videos")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=config.DEFAULT_SEARCH_LIMIT)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("status", help="show database stats")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("reset", help="delete the database and thumbnails")
    p.set_defaults(fn=cmd_reset)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
