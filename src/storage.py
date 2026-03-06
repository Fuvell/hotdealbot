import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "bot_state.db"
DB_BUSY_TIMEOUT_MS = 5000


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    return conn


def validate_db_file_path_or_raise() -> None:
    resolved_base = BASE_DIR.resolve()
    resolved_db_dir = DB_FILE.resolve().parent
    if resolved_db_dir != resolved_base:
        raise RuntimeError(
            "DB file path is outside project base directory. "
            f"db_dir={resolved_db_dir}, base_dir={resolved_base}"
        )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [str(row["name"]) for row in rows]


def _ensure_registered_channels_schema(conn: sqlite3.Connection) -> None:
    target_columns = ["guild_id", "channel_id", "guild_name"]

    if not _table_exists(conn, "registered_channels"):
        conn.execute(
            """
            CREATE TABLE registered_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL UNIQUE,
                guild_name TEXT NOT NULL DEFAULT ''
            )
            """
        )
        return

    current_columns = _table_columns(conn, "registered_channels")
    if current_columns == target_columns:
        return

    conn.execute(
        """
        CREATE TABLE registered_channels_new (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL UNIQUE,
            guild_name TEXT NOT NULL DEFAULT ''
        )
        """
    )
    if "guild_id" in current_columns and "channel_id" in current_columns:
        guild_name_expr = (
            "COALESCE(CAST(guild_name AS TEXT), '')"
            if "guild_name" in current_columns
            else "''"
        )
        conn.execute(
            f"""
            INSERT OR IGNORE INTO registered_channels_new (guild_id, channel_id, guild_name)
            SELECT DISTINCT
                CAST(guild_id AS INTEGER),
                CAST(channel_id AS INTEGER),
                {guild_name_expr}
            FROM registered_channels
            WHERE guild_id IS NOT NULL AND channel_id IS NOT NULL
            """
        )
    elif "channel_id" in current_columns:
        conn.execute(
            """
            INSERT OR IGNORE INTO registered_channels_new (guild_id, channel_id, guild_name)
            SELECT DISTINCT
                CAST(channel_id AS INTEGER),
                CAST(channel_id AS INTEGER),
                ''
            FROM registered_channels
            WHERE channel_id IS NOT NULL
            """
        )
    conn.execute("DROP TABLE registered_channels")
    conn.execute(
        "ALTER TABLE registered_channels_new RENAME TO registered_channels"
    )


def _ensure_sent_deals_schema(conn: sqlite3.Connection) -> None:
    target_columns = ["deal_id", "created_at"]

    if not _table_exists(conn, "sent_deals"):
        conn.execute(
            """
            CREATE TABLE sent_deals (
                deal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        return

    current_columns = _table_columns(conn, "sent_deals")
    if current_columns == target_columns:
        return

    conn.execute(
        """
        CREATE TABLE sent_deals_new (
            deal_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    created_at_expr = "created_at" if "created_at" in current_columns else "CURRENT_TIMESTAMP"
    if "deal_id" in current_columns:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO sent_deals_new (deal_id, created_at)
            SELECT DISTINCT
                CAST(deal_id AS TEXT),
                COALESCE(CAST({created_at_expr} AS TEXT), CURRENT_TIMESTAMP)
            FROM sent_deals
            WHERE deal_id IS NOT NULL
            """
        )
    conn.execute("DROP TABLE sent_deals")
    conn.execute("ALTER TABLE sent_deals_new RENAME TO sent_deals")


def _ensure_guild_excluded_categories_schema(conn: sqlite3.Connection) -> None:
    target_columns = ["guild_id", "category_norm"]

    if not _table_exists(conn, "guild_excluded_categories"):
        conn.execute(
            """
            CREATE TABLE guild_excluded_categories (
                guild_id INTEGER NOT NULL,
                category_norm TEXT NOT NULL,
                PRIMARY KEY (guild_id, category_norm),
                FOREIGN KEY (guild_id)
                    REFERENCES registered_channels(guild_id)
                    ON DELETE CASCADE
            )
            """
        )
        return

    current_columns = _table_columns(conn, "guild_excluded_categories")
    if current_columns == target_columns:
        return

    conn.execute(
        """
        CREATE TABLE guild_excluded_categories_new (
            guild_id INTEGER NOT NULL,
            category_norm TEXT NOT NULL,
            PRIMARY KEY (guild_id, category_norm),
            FOREIGN KEY (guild_id)
                REFERENCES registered_channels(guild_id)
                ON DELETE CASCADE
        )
        """
    )

    if "guild_id" in current_columns and "category_norm" in current_columns:
        conn.execute(
            """
            INSERT OR IGNORE INTO guild_excluded_categories_new (
                guild_id,
                category_norm
            )
            SELECT DISTINCT
                CAST(guild_id AS INTEGER),
                CAST(category_norm AS TEXT)
            FROM guild_excluded_categories
            WHERE guild_id IS NOT NULL
              AND category_norm IS NOT NULL
            """
        )

    conn.execute("DROP TABLE guild_excluded_categories")
    conn.execute(
        "ALTER TABLE guild_excluded_categories_new RENAME TO guild_excluded_categories"
    )


def _ensure_guild_excluded_sites_schema(conn: sqlite3.Connection) -> None:
    target_columns = ["guild_id", "site_code"]

    if not _table_exists(conn, "guild_excluded_sites"):
        conn.execute(
            """
            CREATE TABLE guild_excluded_sites (
                guild_id INTEGER NOT NULL,
                site_code TEXT NOT NULL,
                PRIMARY KEY (guild_id, site_code),
                FOREIGN KEY (guild_id)
                    REFERENCES registered_channels(guild_id)
                    ON DELETE CASCADE
            )
            """
        )
        return

    current_columns = _table_columns(conn, "guild_excluded_sites")
    if current_columns == target_columns:
        return

    conn.execute(
        """
        CREATE TABLE guild_excluded_sites_new (
            guild_id INTEGER NOT NULL,
            site_code TEXT NOT NULL,
            PRIMARY KEY (guild_id, site_code),
            FOREIGN KEY (guild_id)
                REFERENCES registered_channels(guild_id)
                ON DELETE CASCADE
        )
        """
    )

    if "guild_id" in current_columns and "site_code" in current_columns:
        conn.execute(
            """
            INSERT OR IGNORE INTO guild_excluded_sites_new (
                guild_id,
                site_code
            )
            SELECT DISTINCT
                CAST(guild_id AS INTEGER),
                CAST(site_code AS TEXT)
            FROM guild_excluded_sites
            WHERE guild_id IS NOT NULL
              AND site_code IS NOT NULL
            """
        )

    conn.execute("DROP TABLE guild_excluded_sites")
    conn.execute("ALTER TABLE guild_excluded_sites_new RENAME TO guild_excluded_sites")


def _ensure_user_alert_keywords_schema(conn: sqlite3.Connection) -> None:
    target_columns = ["user_id", "keyword_norm", "keyword_raw", "created_at"]

    if not _table_exists(conn, "user_alert_keywords"):
        conn.execute(
            """
            CREATE TABLE user_alert_keywords (
                user_id INTEGER NOT NULL,
                keyword_norm TEXT NOT NULL,
                keyword_raw TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, keyword_norm)
            )
            """
        )
        return

    current_columns = _table_columns(conn, "user_alert_keywords")
    if current_columns == target_columns:
        return

    conn.execute(
        """
        CREATE TABLE user_alert_keywords_new (
            user_id INTEGER NOT NULL,
            keyword_norm TEXT NOT NULL,
            keyword_raw TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, keyword_norm)
        )
        """
    )

    if "user_id" in current_columns and "keyword_norm" in current_columns:
        keyword_raw_expr = "keyword_raw"
        if "keyword_raw" not in current_columns:
            keyword_raw_expr = "keyword_norm"
        created_at_expr = "created_at"
        if "created_at" not in current_columns:
            created_at_expr = "CURRENT_TIMESTAMP"

        conn.execute(
            f"""
            INSERT OR IGNORE INTO user_alert_keywords_new (
                user_id,
                keyword_norm,
                keyword_raw,
                created_at
            )
            SELECT DISTINCT
                CAST(user_id AS INTEGER),
                CAST(keyword_norm AS TEXT),
                CAST({keyword_raw_expr} AS TEXT),
                CAST({created_at_expr} AS TEXT)
            FROM user_alert_keywords
            WHERE user_id IS NOT NULL
              AND keyword_norm IS NOT NULL
            """
        )

    conn.execute("DROP TABLE user_alert_keywords")
    conn.execute("ALTER TABLE user_alert_keywords_new RENAME TO user_alert_keywords")


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sent_deals_created_at
        ON sent_deals(created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_alert_keywords_user_id
        ON user_alert_keywords(user_id)
        """
    )


def init_db() -> None:
    with _connect() as conn:
        _ensure_registered_channels_schema(conn)
        _ensure_sent_deals_schema(conn)
        _ensure_guild_excluded_categories_schema(conn)
        _ensure_guild_excluded_sites_schema(conn)
        _ensure_user_alert_keywords_schema(conn)
        _ensure_indexes(conn)
        conn.execute("DROP TABLE IF EXISTS guild_enabled_sites")
        conn.commit()


def load_registered_channels() -> dict[int, int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT guild_id, channel_id FROM registered_channels"
        ).fetchall()
    return {
        int(row["guild_id"]): int(row["channel_id"])
        for row in rows
    }


def load_excluded_categories_by_guild() -> dict[int, set[str]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, category_norm
            FROM guild_excluded_categories
            """
        ).fetchall()

    data: dict[int, set[str]] = {}
    for row in rows:
        guild_id = int(row["guild_id"])
        category_norm = str(row["category_norm"])
        if guild_id not in data:
            data[guild_id] = set()
        data[guild_id].add(category_norm)
    return data


def load_excluded_sites_by_guild() -> dict[int, set[str]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, site_code
            FROM guild_excluded_sites
            """
        ).fetchall()

    data: dict[int, set[str]] = {}
    for row in rows:
        guild_id = int(row["guild_id"])
        site_code = str(row["site_code"])
        if guild_id not in data:
            data[guild_id] = set()
        data[guild_id].add(site_code)
    return data


def replace_excluded_categories_for_guild(guild_id: int, category_norms: set[str]) -> None:
    guild_id_int = int(guild_id)
    normalized_values = sorted({str(v).strip() for v in category_norms if str(v).strip()})

    with _connect() as conn:
        conn.execute(
            "DELETE FROM guild_excluded_categories WHERE guild_id = ?",
            (guild_id_int,),
        )
        if normalized_values:
            conn.executemany(
                """
                INSERT OR IGNORE INTO guild_excluded_categories (
                    guild_id,
                    category_norm
                ) VALUES (?, ?)
                """,
                [(guild_id_int, value) for value in normalized_values],
            )
        conn.commit()


def replace_excluded_sites_for_guild(guild_id: int, site_codes: set[str]) -> None:
    guild_id_int = int(guild_id)
    normalized_values = sorted({str(v).strip() for v in site_codes if str(v).strip()})

    with _connect() as conn:
        conn.execute(
            "DELETE FROM guild_excluded_sites WHERE guild_id = ?",
            (guild_id_int,),
        )
        if normalized_values:
            conn.executemany(
                """
                INSERT OR IGNORE INTO guild_excluded_sites (
                    guild_id,
                    site_code
                ) VALUES (?, ?)
                """,
                [(guild_id_int, value) for value in normalized_values],
            )
        conn.commit()


def upsert_registered_channel(guild_id: int, channel_id: int, guild_name: str = "") -> str:
    guild_id_int = int(guild_id)
    channel_id_int = int(channel_id)
    guild_name_text = str(guild_name or "").strip()

    with _connect() as conn:
        # Keep channel ownership consistent if stale/corrupt data exists.
        conn.execute(
            """
            DELETE FROM registered_channels
            WHERE channel_id = ? AND guild_id != ?
            """,
            (channel_id_int, guild_id_int),
        )

        row = conn.execute(
            """
            SELECT channel_id, guild_name
            FROM registered_channels
            WHERE guild_id = ?
            """,
            (guild_id_int,),
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO registered_channels (guild_id, channel_id, guild_name)
                VALUES (?, ?, ?)
                """,
                (guild_id_int, channel_id_int, guild_name_text),
            )
            conn.commit()
            return "inserted"

        existing_channel_id = int(row["channel_id"])
        existing_guild_name = str(row["guild_name"] or "")
        if existing_channel_id == channel_id_int and existing_guild_name == guild_name_text:
            return "unchanged"

        conn.execute(
            """
            UPDATE registered_channels
            SET channel_id = ?, guild_name = ?
            WHERE guild_id = ?
            """,
            (channel_id_int, guild_name_text, guild_id_int),
        )
        conn.commit()
    return "updated"


def remove_registered_channel_by_channel_id(channel_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM registered_channels WHERE channel_id = ?",
            (int(channel_id),),
        )
        conn.commit()
    return cur.rowcount > 0


def remove_registered_channel_by_guild_id(guild_id: int) -> int | None:
    guild_id_int = int(guild_id)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT channel_id
            FROM registered_channels
            WHERE guild_id = ?
            """,
            (guild_id_int,),
        ).fetchone()
        if row is None:
            return None

        conn.execute(
            "DELETE FROM registered_channels WHERE guild_id = ?",
            (guild_id_int,),
        )
        conn.commit()
    return int(row["channel_id"])


def load_sent_deal_ids() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT deal_id FROM sent_deals").fetchall()
    return {str(row["deal_id"]) for row in rows}


def mark_deal_sent(deal_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sent_deals (deal_id) VALUES (?)",
            (deal_id,),
        )
        conn.commit()
    return cur.rowcount > 0


def replace_sent_deal_ids(deal_ids: set[str]) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sent_deals")
        conn.executemany(
            "INSERT OR IGNORE INTO sent_deals (deal_id) VALUES (?)",
            [(deal_id,) for deal_id in sorted(deal_ids)],
        )
        conn.commit()


def purge_sent_deals_older_than(days: int) -> int:
    keep_days = max(1, int(days))
    with _connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM sent_deals
            WHERE created_at < datetime('now', ?)
            """,
            (f"-{keep_days} days",),
        )
        conn.commit()
    return int(cur.rowcount)


def load_user_alert_keywords() -> dict[int, dict[str, str]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, keyword_norm, keyword_raw
            FROM user_alert_keywords
            ORDER BY user_id ASC, created_at ASC
            """
        ).fetchall()

    data: dict[int, dict[str, str]] = {}
    for row in rows:
        user_id = int(row["user_id"])
        keyword_norm = str(row["keyword_norm"])
        keyword_raw = str(row["keyword_raw"])
        if user_id not in data:
            data[user_id] = {}
        data[user_id][keyword_norm] = keyword_raw
    return data


def upsert_user_alert_keyword(user_id: int, keyword_norm: str, keyword_raw: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_alert_keywords (
                user_id,
                keyword_norm,
                keyword_raw
            ) VALUES (?, ?, ?)
            ON CONFLICT(user_id, keyword_norm) DO UPDATE SET
                keyword_raw = excluded.keyword_raw
            """,
            (int(user_id), str(keyword_norm), str(keyword_raw)),
        )
        conn.commit()


def remove_user_alert_keyword(user_id: int, keyword_norm: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM user_alert_keywords
            WHERE user_id = ? AND keyword_norm = ?
            """,
            (int(user_id), str(keyword_norm)),
        )
        conn.commit()
    return cur.rowcount > 0


def clear_user_alert_keywords(user_id: int) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_alert_keywords WHERE user_id = ?",
            (int(user_id),),
        )
        conn.commit()
    return int(cur.rowcount)
