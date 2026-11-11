#!/usr/bin/env python3
"""Clean stale bus sequence references after a bus rebuild.

Usage:
  python tools/cutover/clean_stale_bus_sequences.py /home/ts/.agent-comms [--dry-run]

After bus.sqlite3 is rebuilt empty, other stores still reference old sequence numbers.
This script deletes all rows with stale sequences from runtime stores that rebuild on
startup. Non-runtime stores (coordination.sqlite3) get their sequence columns nulled.

Run with --dry-run first to see what it would do.
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def bus_max_seq(root: Path) -> int:
    with sqlite3.connect(root / "bus.sqlite3") as conn:
        row = conn.execute("SELECT MAX(seq) FROM bus").fetchone()
        return row[0] if row[0] is not None else 0


RUNTIME_STORES = [
    "presence.sqlite3",
    "owner_releases.sqlite3",
]

STORES_WITH_SEQ_COLUMNS: list[tuple[str, list[tuple[str, str]]]] = [
    ("coordination.sqlite3", [
        ("response_obligations", "publication_seq"),
        ("assignment_records", "source_seq"),
    ]),
    ("registry.sqlite3", [
        ("thread_text", "bus_seq"),
    ]),
]


def clean_runtime_store(root: Path, name: str, dry_run: bool) -> list[str]:
    path = root / name
    if not path.exists():
        return []
    actions = []
    try:
        with sqlite3.connect(path) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
            for table in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if count > 0:
                    actions.append(f"DELETE FROM {table} ({count} rows)")
                    if not dry_run:
                        conn.execute(f"DELETE FROM {table}")
            if not dry_run:
                conn.commit()
    except sqlite3.DatabaseError as e:
        actions.append(f"ERROR: {e}")
    return actions


def clean_seq_columns(root: Path, name: str, columns: list[tuple[str, str]],
                      max_seq: int, dry_run: bool) -> list[str]:
    path = root / name
    if not path.exists():
        return []
    actions = []
    try:
        with sqlite3.connect(path) as conn:
            for table, column in columns:
                try:
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column} > ?", (max_seq,)
                    ).fetchone()[0]
                except sqlite3.OperationalError:
                    continue
                if count > 0:
                    actions.append(f"UPDATE {table} SET {column}=NULL WHERE {column}>{max_seq} ({count} rows)")
                    if not dry_run:
                        conn.execute(
                            f"UPDATE {table} SET {column}=NULL WHERE {column} > ?", (max_seq,)
                        )
            if not dry_run:
                conn.commit()
    except sqlite3.DatabaseError as e:
        actions.append(f"ERROR: {e}")
    return actions


def scan_all_for_stale(root: Path, max_seq: int) -> None:
    """Scan every sqlite3 for any integer column with values > max_seq."""
    print(f"\nFull scan for stale sequences > {max_seq}:")
    for db_path in sorted(root.rglob("*.sqlite3")):
        if db_path.name == "bus.sqlite3":
            continue
        rel = db_path.relative_to(root)
        try:
            with sqlite3.connect(db_path) as conn:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()]
                for table in tables:
                    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
                    for _cid, col_name, col_type, *_ in info:
                        if col_type.upper() != "INTEGER":
                            continue
                        lower = col_name.lower()
                        if not any(k in lower for k in ("seq", "sequence")):
                            continue
                        try:
                            stale = conn.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE {col_name} > ?", (max_seq,)
                            ).fetchone()[0]
                        except sqlite3.OperationalError:
                            continue
                        if stale > 0:
                            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                            print(f"  {rel}: {table}.{col_name} — {stale}/{total} rows > {max_seq}")
        except sqlite3.DatabaseError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scan-all", action="store_true", help="Scan every db for stale seqs")
    args = parser.parse_args()

    root = args.root
    max_seq = bus_max_seq(root)
    print(f"Bus max sequence: {max_seq}")

    if args.scan_all:
        scan_all_for_stale(root, max_seq)
        return

    prefix = "[DRY RUN] " if args.dry_run else ""

    for name in RUNTIME_STORES:
        actions = clean_runtime_store(root, name, args.dry_run)
        if actions:
            print(f"\n{prefix}{name}:")
            for a in actions:
                print(f"  {a}")

    for name, columns in STORES_WITH_SEQ_COLUMNS:
        actions = clean_seq_columns(root, name, columns, max_seq, args.dry_run)
        if actions:
            print(f"\n{prefix}{name}:")
            for a in actions:
                print(f"  {a}")

    print(f"\nDone. Run with --scan-all to find any remaining stale sequences.")


if __name__ == "__main__":
    main()
