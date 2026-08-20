from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("migrate_identities")


def _normalize_handle(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized.startswith("@"):
        return normalized
    if normalized.startswith("telegram:") or normalized.startswith("guest:"):
        return normalized
    return f"@{normalized}"


def _normalize_username(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().removeprefix("@")
    return normalized or None


def _entity_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "users": int(conn.execute("SELECT count(*) FROM app_users").fetchone()[0]),
        "subscriptions": int(conn.execute("SELECT count(*) FROM user_subscriptions").fetchone()[0]),
        "orders": int(conn.execute("SELECT count(*) FROM subscription_orders").fetchone()[0]),
        "devices": int(conn.execute("SELECT count(*) FROM devices").fetchone()[0]),
    }


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(app_users)").fetchall()
    }
    required_columns = {
        "role": "ALTER TABLE app_users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'",
        "telegram_username": "ALTER TABLE app_users ADD COLUMN telegram_username TEXT",
        "telegram_linked_at": "ALTER TABLE app_users ADD COLUMN telegram_linked_at TEXT",
        "last_seen_at": (
            "ALTER TABLE app_users ADD COLUMN last_seen_at TEXT NOT NULL "
            f"DEFAULT '{datetime.now(UTC).replace(microsecond=0).isoformat()}'"
        ),
    }
    for column_name, sql in required_columns.items():
        if column_name not in existing:
            logger.info("Adding column '%s' to app_users", column_name)
            conn.execute(sql)


def _build_unique_lookup(rows: list[sqlite3.Row], key_builder) -> dict[str, sqlite3.Row | None]:
    lookup: dict[str, sqlite3.Row | None] = {}
    for row in rows:
        key = key_builder(row)
        if not key:
            continue
        if key in lookup:
            lookup[key] = None
        else:
            lookup[key] = row
    return lookup


def migrate(db_path: Path, report_path: Path) -> None:
    if not db_path.exists():
        logger.error("Database not found at %s", db_path)
        sys.exit(1)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        counts_before = _entity_counts(conn)
        _ensure_columns(conn)

        linked_rows = conn.execute(
            "SELECT * FROM app_users WHERE telegram_user_id IS NOT NULL"
        ).fetchall()
        unlinked_rows = conn.execute(
            "SELECT * FROM app_users WHERE telegram_user_id IS NULL"
        ).fetchall()

        duplicate_ids = [
            {
                "telegram_user_id": int(row["telegram_user_id"]),
                "count": int(row["entry_count"]),
            }
            for row in conn.execute(
                """
                SELECT telegram_user_id, count(*) AS entry_count
                FROM app_users
                WHERE telegram_user_id IS NOT NULL
                GROUP BY telegram_user_id
                HAVING count(*) > 1
                """
            ).fetchall()
        ]

        linked_by_handle = _build_unique_lookup(
            linked_rows,
            lambda row: _normalize_handle(row["telegram_handle"]),
        )
        linked_by_username = _build_unique_lookup(
            linked_rows,
            lambda row: _normalize_username(row["telegram_username"]),
        )

        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        migrated: list[dict[str, object]] = []
        unmatched: list[dict[str, object]] = []
        conflicts: list[dict[str, object]] = []

        for row in unlinked_rows:
            user_id = str(row["user_id"])
            handle = _normalize_handle(row["telegram_handle"])
            username = _normalize_username(row["telegram_username"])
            candidate_ids: set[int] = set()
            candidate_usernames: set[str] = set()

            if handle and handle.startswith("telegram:"):
                raw_id = handle.removeprefix("telegram:").strip()
                if raw_id.isdigit():
                    candidate_ids.add(int(raw_id))

            for lookup_key, lookup in ((handle, linked_by_handle), (username, linked_by_username)):
                if not lookup_key:
                    continue
                matched_row = lookup.get(lookup_key)
                if matched_row is None and lookup_key in lookup:
                    conflicts.append(
                        {
                            "user_id": user_id,
                            "reason": "ambiguous_match",
                            "telegram_handle": row["telegram_handle"],
                            "telegram_username": row["telegram_username"],
                            "lookup_key": lookup_key,
                        }
                    )
                    candidate_ids.clear()
                    break
                if matched_row is not None:
                    candidate_ids.add(int(matched_row["telegram_user_id"]))
                    matched_username = _normalize_username(matched_row["telegram_username"])
                    if matched_username:
                        candidate_usernames.add(matched_username)

            if not candidate_ids:
                unmatched.append(
                    {
                        "user_id": user_id,
                        "reason": "no_safe_match",
                        "telegram_handle": row["telegram_handle"],
                        "telegram_username": row["telegram_username"],
                    }
                )
                continue

            if len(candidate_ids) > 1:
                conflicts.append(
                    {
                        "user_id": user_id,
                        "reason": "multiple_candidates",
                        "telegram_handle": row["telegram_handle"],
                        "telegram_username": row["telegram_username"],
                        "candidate_ids": sorted(candidate_ids),
                    }
                )
                continue

            telegram_user_id = next(iter(candidate_ids))
            telegram_username = next(iter(candidate_usernames), username)
            conn.execute(
                """
                UPDATE app_users
                SET telegram_user_id = ?,
                    telegram_username = coalesce(?, telegram_username),
                    telegram_linked_at = coalesce(telegram_linked_at, ?),
                    updated_at = updated_at
                WHERE user_id = ?
                """,
                (telegram_user_id, telegram_username, now, user_id),
            )
            migrated.append(
                {
                    "user_id": user_id,
                    "telegram_user_id": telegram_user_id,
                    "telegram_username": telegram_username,
                }
            )

        index_created = False
        if duplicate_ids:
            logger.warning("Skipping unique Telegram index: duplicate telegram_user_id values exist.")
        else:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_app_users_telegram_user_id_unique
                ON app_users(telegram_user_id)
                WHERE telegram_user_id IS NOT NULL
                """
            )
            index_created = True

        counts_after = _entity_counts(conn)
        if counts_before != counts_after:
            raise RuntimeError(
                f"Entity counts changed during migration: before={counts_before} after={counts_after}"
            )

        report = {
            "generated_at": now,
            "database": str(db_path),
            "counts_before": counts_before,
            "counts_after": counts_after,
            "migrated": migrated,
            "unmatched": unmatched,
            "conflicts": conflicts,
            "duplicate_telegram_ids": duplicate_ids,
            "unique_index_created": index_created,
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        conn.commit()

        logger.info("Migration finished. migrated=%s unmatched=%s conflicts=%s", len(migrated), len(unmatched), len(conflicts))
        logger.info("Report written to %s", report_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Migrate Speedy.VPN identities.")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("speedy_vpn_data.sqlite3"),
        help="Path to the sqlite3 database file.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("migration_report.json"),
        help="Path to save the migration report.",
    )
    args = parser.parse_args()

    migrate(args.db, args.report)
