"""Atomic bounded SQLite backend for memory and deterministic JSON artifacts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from pydantic import ValidationError

from semantic_terraform_agent.cache.models import VerifiedFailureEntry


CACHE_DATABASE_NAME = "semantic-terraform-agent-v1.sqlite3"
MAX_MEMORY_ENTRY_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_MEMORY_ENTRIES = 500
MAX_ARTIFACT_ENTRIES = 500


class CacheStoreError(Exception):
    pass


def validate_cache_directory(path: Path, repository_root: Path | None = None) -> Path:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise ValueError("cache directory must not contain traversal segments")
    if expanded.exists() and expanded.is_symlink():
        raise ValueError("cache directory must not be a symbolic link")
    resolved = expanded.resolve(strict=False)
    forbidden = {Path("/").resolve(), Path.home().resolve()}
    if repository_root is not None:
        forbidden.add(repository_root.resolve())
    if resolved in forbidden:
        raise ValueError("cache directory must not be a filesystem, home, or repository root")
    if (resolved / ".git").exists():
        raise ValueError("cache directory must not be a Git repository root")
    created = not resolved.exists()
    resolved.mkdir(parents=True, exist_ok=True)
    if not resolved.is_dir():
        raise ValueError("cache directory is not a directory")
    if created:
        resolved.chmod(0o700)
    return resolved


class LocalCacheStore:
    def __init__(self, cache_dir: Path, *, repository_root: Path | None = None) -> None:
        self.cache_dir = validate_cache_directory(cache_dir, repository_root)
        self.database_path = self.cache_dir / CACHE_DATABASE_NAME
        if self.database_path.exists() and self.database_path.is_symlink():
            raise ValueError("cache database must not be a symbolic link")
        self._initialize()
        self.database_path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS failure_memory (
                        fingerprint TEXT PRIMARY KEY,
                        repository_scope TEXT NOT NULL,
                        entry_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        rejection_count INTEGER NOT NULL DEFAULT 0,
                        last_rejection_reason TEXT
                    );
                    CREATE TABLE IF NOT EXISTS artifacts (
                        kind TEXT NOT NULL,
                        cache_key TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(kind, cache_key)
                    );
                    CREATE TABLE IF NOT EXISTS counters (
                        name TEXT PRIMARY KEY,
                        value INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
        except sqlite3.DatabaseError as exc:
            raise CacheStoreError("cache database could not be initialized") from exc

    def get_failure(self, fingerprint: str) -> VerifiedFailureEntry | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT entry_json, rejection_count FROM failure_memory WHERE fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                self._increment(connection, "failure_memory_lookups")
                if row is None:
                    return None
                self._increment(connection, "failure_memory_hits")
            entry = VerifiedFailureEntry.model_validate_json(row[0])
            return entry.model_copy(update={"rejection_count": row[1]})
        except (sqlite3.DatabaseError, ValidationError, ValueError) as exc:
            raise CacheStoreError("verified failure memory could not be read") from exc

    def put_failure(self, entry: VerifiedFailureEntry) -> bool:
        encoded = entry.model_dump_json()
        if len(encoded.encode("utf-8")) > MAX_MEMORY_ENTRY_BYTES:
            raise CacheStoreError("verified failure memory entry exceeds size limit")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO failure_memory "
                    "(fingerprint, repository_scope, entry_json, created_at) VALUES (?, ?, ?, ?)",
                    (
                        entry.fingerprint,
                        entry.repository_scope,
                        encoded,
                        entry.created_at,
                    ),
                )
                self._increment(connection, "failure_memory_writes")
                self._prune(connection, "failure_memory", MAX_MEMORY_ENTRIES)
                return cursor.rowcount == 1
        except sqlite3.DatabaseError as exc:
            raise CacheStoreError("verified failure memory could not be written") from exc

    def record_rejection(self, fingerprint: str, reason: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE failure_memory SET rejection_count = rejection_count + 1, "
                    "last_rejection_reason = ? WHERE fingerprint = ?",
                    (reason[:200], fingerprint),
                )
                self._increment(connection, "failure_memory_rejections")
        except sqlite3.DatabaseError as exc:
            raise CacheStoreError("memory rejection could not be recorded") from exc

    def get_artifact(self, kind: str, key: str) -> dict | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM artifacts WHERE kind = ? AND cache_key = ?",
                    (kind, key),
                ).fetchone()
            if row is None:
                return None
            value = json.loads(row[0])
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (sqlite3.DatabaseError, json.JSONDecodeError, ValueError) as exc:
            raise CacheStoreError("deterministic cache artifact could not be read") from exc

    def put_artifact(self, kind: str, key: str, payload: dict) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise CacheStoreError("deterministic cache artifact exceeds size limit")
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO artifacts(kind, cache_key, payload_json) "
                    "VALUES (?, ?, ?)",
                    (kind, key, encoded),
                )
                self._prune(connection, "artifacts", MAX_ARTIFACT_ENTRIES)
        except sqlite3.DatabaseError as exc:
            raise CacheStoreError("deterministic cache artifact could not be written") from exc

    def stats(self) -> dict[str, int]:
        try:
            with self._connect() as connection:
                memory = connection.execute(
                    "SELECT COUNT(*) FROM failure_memory"
                ).fetchone()[0]
                artifacts = connection.execute(
                    "SELECT COUNT(*) FROM artifacts"
                ).fetchone()[0]
                counters = dict(connection.execute("SELECT name, value FROM counters"))
            return {
                "failure_memory_entries": memory,
                "artifact_entries": artifacts,
                "database_bytes": self.database_path.stat().st_size,
                **counters,
            }
        except (sqlite3.DatabaseError, OSError) as exc:
            raise CacheStoreError("cache statistics could not be read") from exc

    def clear(self) -> dict[str, int]:
        before = self.stats()
        try:
            with self._connect() as connection:
                connection.execute("DELETE FROM failure_memory")
                connection.execute("DELETE FROM artifacts")
                connection.execute("DELETE FROM counters")
        except sqlite3.DatabaseError as exc:
            raise CacheStoreError("cache could not be cleared") from exc
        return before

    @staticmethod
    def _increment(connection: sqlite3.Connection, name: str) -> None:
        connection.execute(
            "INSERT INTO counters(name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1",
            (name,),
        )

    @staticmethod
    def _prune(connection: sqlite3.Connection, table: str, maximum: int) -> None:
        if table == "failure_memory":
            connection.execute(
                "DELETE FROM failure_memory WHERE fingerprint IN "
                "(SELECT fingerprint FROM failure_memory ORDER BY created_at DESC "
                "LIMIT -1 OFFSET ?)",
                (maximum,),
            )
        else:
            connection.execute(
                "DELETE FROM artifacts WHERE rowid IN "
                "(SELECT rowid FROM artifacts ORDER BY created_at DESC "
                "LIMIT -1 OFFSET ?)",
                (maximum,),
            )
