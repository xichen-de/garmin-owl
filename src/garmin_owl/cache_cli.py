"""Developer/user CLI utilities for the local normalized cache."""

from __future__ import annotations

import argparse

from .database import GarminDatabase


def info_main() -> None:
    info = GarminDatabase().info()
    print(f"Path:           {info.path}")
    print(f"Schema version: {info.schema_version}")
    print(f"Size:           {info.size_bytes} bytes")
    print(f"Date range:     {info.first_date or '-'} to {info.last_date or '-'}")
    print("Rows:")
    for table, count in info.table_rows.items():
        print(f"  {table + ':':24s}{count:8d}")


def clear_main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear normalized Garmin history. Authentication tokens are never touched."
    )
    parser.add_argument("--yes", action="store_true", help="skip confirmation")
    parser.add_argument("--vacuum", action="store_true", help="compact the SQLite file")
    args = parser.parse_args()
    database = GarminDatabase()
    if not args.yes:
        answer = input(f"Clear normalized cache at {database.path}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cache unchanged.")
            return
    database.clear(vacuum=args.vacuum)
    print("Normalized cache cleared. Garmin authentication tokens were not changed.")
