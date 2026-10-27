"""Durable submission identity only. The study JobStore remains lifecycle authority."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.tools.coder.remote_contract import HttpReceipt, RemoteCoderError, bare


@dataclass(frozen=True)
class RemoteBinding:
    request_digest: str
    task_id: str | None


class RemoteSubmissionStore:
    def __init__(self, root: Path, *, max_receipts: int, max_receipt_bytes: int) -> None:
        self.root = root
        for value in (max_receipts, max_receipt_bytes):
            if type(value) is not int or value <= 0:
                raise RemoteCoderError("remote_receipt_budget_invalid")
        self.max_receipts = max_receipts
        self.max_receipt_bytes = max_receipt_bytes

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        path = self.root / "remote-coder.sqlite3"
        if path.is_symlink() or self.root.is_symlink():
            raise RemoteCoderError("remote_binding_store_unsafe")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=5)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise RemoteCoderError("remote_binding_store_version")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS submissions (
                        scope TEXT PRIMARY KEY, request_digest TEXT NOT NULL, identity_json TEXT NOT NULL,
                        task_id TEXT);
                    CREATE TABLE IF NOT EXISTS receipts (
                        scope TEXT NOT NULL REFERENCES submissions(scope), operation TEXT NOT NULL,
                        status_code INTEGER NOT NULL, digest TEXT NOT NULL, body BLOB NOT NULL,
                        UNIQUE(scope,operation,status_code,digest));
                    PRAGMA user_version=1;
                """)
                with connection:
                    yield connection
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as exc:
            raise RemoteCoderError("remote_binding_store_unavailable") from exc

    @staticmethod
    def scope(study_id: str, attempt_id: str, call_id: str) -> str:
        return sha256_hex(canonical_json([bare(study_id), bare(attempt_id), bare(call_id)]))

    def reserve(self, scope: str, identity: dict[str, object]) -> tuple[RemoteBinding, bool]:
        encoded = canonical_json(identity).decode("utf-8")
        request_digest = sha256_hex(encoded)
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO submissions VALUES (?,?,?,NULL) ON CONFLICT(scope) DO NOTHING",
                (scope, request_digest, encoded))
            created = cursor.rowcount == 1
            row = connection.execute("SELECT * FROM submissions WHERE scope=?", (scope,)).fetchone()
            assert row is not None
            if row["request_digest"] != request_digest or row["identity_json"] != encoded:
                raise RemoteCoderError("remote_request_identity_conflict")
            return RemoteBinding(request_digest, row["task_id"]), created

    def get(self, scope: str) -> RemoteBinding:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM submissions WHERE scope=?", (scope,)).fetchone()
            if row is None:
                raise RemoteCoderError("remote_binding_not_found")
            if sha256_hex(row["identity_json"]) != row["request_digest"]:
                raise RemoteCoderError("remote_binding_digest_mismatch")
            return RemoteBinding(row["request_digest"], row["task_id"])

    def bind(self, scope: str, request_digest: str, task_id: str) -> None:
        bare(task_id)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE submissions SET task_id=? WHERE scope=? AND request_digest=? AND task_id IS NULL",
                (task_id, scope, request_digest))
            if cursor.rowcount != 1:
                row = connection.execute("SELECT * FROM submissions WHERE scope=?", (scope,)).fetchone()
                if row is None or row["request_digest"] != request_digest or row["task_id"] != task_id:
                    raise RemoteCoderError("remote_task_binding_conflict")

    def receipt(self, scope: str, receipt: HttpReceipt) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute(
                "SELECT 1 FROM receipts WHERE scope=? AND operation=? AND status_code=? AND digest=?",
                (scope, receipt.operation, receipt.status_code, receipt.sha256)).fetchone()
            if old is not None:
                return
            count, size = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(body)),0) FROM receipts WHERE scope=?", (scope,)).fetchone()
            if count >= self.max_receipts or size + len(receipt.body) > self.max_receipt_bytes:
                raise RemoteCoderError("remote_receipt_budget_exhausted")
            connection.execute("INSERT INTO receipts VALUES (?,?,?,?,?)",
                               (scope, receipt.operation, receipt.status_code, receipt.sha256, receipt.body))

    def receipts(self, scope: str) -> tuple[HttpReceipt, ...]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM receipts WHERE scope=? ORDER BY rowid", (scope,)).fetchall()
            receipts = []
            for row in rows:
                receipt = HttpReceipt(row["operation"], row["status_code"], row["body"])
                if receipt.sha256 != row["digest"]:
                    raise RemoteCoderError("remote_receipt_digest_mismatch")
                receipts.append(receipt)
            return tuple(receipts)
