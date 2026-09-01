from __future__ import annotations

import argparse

from app.db import close_pool, open_pool
from app.ingestion import (
    create_or_get_run,
    import_boards_file,
    reclassify_existing_jobs,
    run_ingestion,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="JobHunt India operations CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")
    sub.add_parser("reclassify")
    import_parser = sub.add_parser("import-boards")
    import_parser.add_argument("path")
    import_parser.add_argument("--via", default="bootstrap_import")

    ingest_parser = sub.add_parser("ingest")
    ingest_parser.add_argument(
        "--mode",
        choices=("incremental", "refresh_recent", "full_discovery", "smoke"),
        default="incremental",
    )
    ingest_parser.add_argument("--limit-per-ats", type=int)

    args = parser.parse_args()
    open_pool()
    try:
        if args.command == "init-db":
            print("database initialized")
        elif args.command == "reclassify":
            result = reclassify_existing_jobs()
            print(
                "reclassified "
                f"{result['scanned']} jobs; updated {result['updated']}; "
                f"closed {result['closed']}"
            )
        elif args.command == "import-boards":
            count = import_boards_file(args.path, args.via)
            print(f"imported {count} board records")
        elif args.command == "ingest":
            run_id, created = create_or_get_run(args.mode)
            if not created:
                raise SystemExit(f"ingestion already active: {run_id}")
            run_ingestion(run_id, args.mode, args.limit_per_ats)
            print(f"ingestion finished: {run_id}")
    finally:
        close_pool()


if __name__ == "__main__":
    main()
