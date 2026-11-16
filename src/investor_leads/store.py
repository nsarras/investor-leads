"""Local SQLite store: every post seen, every classification, and cached query plans.

Keeping this between runs is what makes the tool cheap to re-run (posts are classified
once) and lets it answer for windows longer than a single collection.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Classification, Post, QueryPlan

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    url TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    post_id TEXT NOT NULL,
    text TEXT NOT NULL,
    posted_at TEXT NOT NULL DEFAULT '',
    author_name TEXT NOT NULL DEFAULT '',
    author_handle TEXT NOT NULL DEFAULT '',
    author_headline TEXT NOT NULL DEFAULT '',
    author_url TEXT NOT NULL DEFAULT '',
    author_followers INTEGER,
    engagement INTEGER,
    matched_query TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classifications (
    url TEXT NOT NULL,
    target TEXT NOT NULL,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    model TEXT NOT NULL,
    classified_at TEXT NOT NULL,
    PRIMARY KEY (url, target)
);
CREATE TABLE IF NOT EXISTS plans (
    target TEXT PRIMARY KEY,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

POST_COLUMNS = (
    "url, platform, post_id, text, posted_at, author_name, author_handle, author_headline, "
    "author_url, author_followers, engagement, matched_query"
)

# A post's date for window filtering: its own date, or the day we first saw it when the
# platform did not provide one.
POST_DAY = "COALESCE(NULLIF(substr(p.posted_at, 1, 10), ''), substr(p.first_seen, 1, 10))"


def now_stamp() -> str:
    """The current UTC time in the form stored in the database. Sorts correctly as text."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_now = now_stamp


def _post(row: sqlite3.Row) -> Post:
    return Post(
        platform=row["platform"],
        post_id=row["post_id"],
        url=row["url"],
        text=row["text"],
        posted_at=row["posted_at"],
        author_name=row["author_name"],
        author_handle=row["author_handle"],
        author_headline=row["author_headline"],
        author_url=row["author_url"],
        author_followers=row["author_followers"],
        engagement=row["engagement"],
        matched_query=row["matched_query"],
    )


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._drop_outdated_tables()
        self._db.executescript(SCHEMA)

    def _drop_outdated_tables(self) -> None:
        """Classifications from before version 0.2 used a column per field. Drop that table;
        the posts are kept and are simply classified again on the next run."""
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(classifications)")}
        if columns and "data" not in columns:
            with self._db:
                self._db.execute("DROP TABLE classifications")

    def close(self) -> None:
        self._db.close()

    def add_posts(self, posts: list[Post]) -> int:
        """Insert posts not seen before. Returns how many were new."""
        new = 0
        with self._db:
            for p in posts:
                cursor = self._db.execute(
                    f"INSERT OR IGNORE INTO posts ({POST_COLUMNS}, first_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        p.url, p.platform, p.post_id, p.text, p.posted_at, p.author_name, p.author_handle,
                        p.author_headline, p.author_url, p.author_followers, p.engagement, p.matched_query, _now(),
                    ),
                )
                new += cursor.rowcount
        return new

    def unclassified(self, target: str, since_date: str, version: int) -> list[Post]:
        """Posts inside the window with no classification for this target, or an outdated one."""
        rows = self._db.execute(
            f"SELECT p.* FROM posts p LEFT JOIN classifications c ON c.url = p.url AND c.target = ? "
            f"WHERE (c.url IS NULL OR c.version < ?) AND {POST_DAY} >= ? ORDER BY p.posted_at DESC",
            (target, version, since_date),
        ).fetchall()
        return [_post(r) for r in rows]

    def save_classifications(self, target: str, model: str, version: int, items: list[Classification]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO classifications VALUES (?, ?, ?, ?, ?, ?)",
                [(c.url, target, version, json.dumps(c.to_dict()), model, _now()) for c in items],
            )

    def classified(
        self, target: str, since_date: str, first_seen_from: str | None = None
    ) -> list[tuple[Post, Classification]]:
        """Every classified post inside the window, newest first.

        With `first_seen_from` (a UTC timestamp as stored in `first_seen`), only posts
        collected for the first time at or after that moment are returned.
        """
        rows = self._db.execute(
            f"SELECT p.*, c.data FROM posts p JOIN classifications c ON c.url = p.url AND c.target = ? "
            f"WHERE {POST_DAY} >= ? AND p.first_seen >= ? ORDER BY p.posted_at DESC",
            (target, since_date, first_seen_from or ""),
        ).fetchall()
        return [(_post(r), Classification.from_dict(json.loads(r["data"]))) for r in rows]

    def get_plan(self, target: str, version: int) -> QueryPlan | None:
        """The cached search plan, unless it was made under older planning rules."""
        row = self._db.execute("SELECT plan FROM plans WHERE target = ?", (target,)).fetchone()
        if not row:
            return None
        data = json.loads(row["plan"])
        if data.get("version", 1) != version:
            return None
        return QueryPlan(
            x_queries=list(data.get("x_queries", [])),
            linkedin_keywords=list(data.get("linkedin_keywords", [])),
            source="cache",
            requested_x=int(data.get("requested_x", 0)),
            requested_linkedin=int(data.get("requested_linkedin", 0)),
        )

    def save_plan(self, target: str, plan: QueryPlan, version: int) -> None:
        payload = json.dumps(
            {
                "version": version,
                "x_queries": plan.x_queries,
                "linkedin_keywords": plan.linkedin_keywords,
                "requested_x": plan.requested_x,
                "requested_linkedin": plan.requested_linkedin,
            }
        )
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO plans VALUES (?, ?, ?)", (target, payload, _now()))
