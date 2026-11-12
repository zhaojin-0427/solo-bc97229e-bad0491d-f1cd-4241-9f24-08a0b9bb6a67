"""SQLite 索引层。

职责
====
* 保存网格元信息、提交历史、块坐标 -> 不可变块文件的映射、待回收文件清单。
* WAL 模式 + 显式 ``BEGIN IMMEDIATE``：同一时刻全集群只有一个写事务，
  并发上传在此排队（busy_timeout 内等待），天然满足“异值/越界整次拒绝、
  跨块改动一起可见、不覆盖他人已成功格”的事务要求。
* 读事务使用延迟 ``BEGIN`` 获取一次 WAL 快照，窗口内所有块查询都落在
  同一已提交状态上（快照隔离）。
* 启动时与 BlockStore 协同做崩溃恢复：回收临时文件、孤儿块、旧版本块。

内存约束：索引查询全部走 (br,bc) 主键点查 / 小范围扫描，
任何流程都不会把整张网格或全部已填格索引加载进内存。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

from .blockstore import BlockStore, is_block_filename
from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS commits (
    commit_id  INTEGER PRIMARY KEY,
    row_offset INTEGER NOT NULL,
    col_offset INTEGER NOT NULL,
    rows       INTEGER NOT NULL,
    cols       INTEGER NOT NULL,
    cell_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS blocks (
    br           INTEGER NOT NULL,
    bc           INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    filled_cells INTEGER NOT NULL,
    commit_id    INTEGER NOT NULL,
    PRIMARY KEY (br, bc)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_blocks_commit ON blocks(commit_id);

CREATE TABLE IF NOT EXISTS obsolete_files (
    filename       TEXT PRIMARY KEY,
    created_commit INTEGER NOT NULL
) WITHOUT ROWID;
"""


class IntegrityError(Exception):
    """磁盘块与 SQLite 索引不一致（如索引指向的块文件丢失）。"""


class Database:
    def __init__(self, config: Config, store: BlockStore):
        self.config = config
        self.store = store
        self.db_path = config.db_path

    # ---------- 连接 ----------
    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.config.BUSY_TIMEOUT_MS / 1000.0,
                               isolation_level=None)
        # isolation_level=None：关闭 Python 层自动事务，全部由我们显式控制。
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.config.BUSY_TIMEOUT_MS)}")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    # ---------- 初始化 / 恢复 ----------
    def initialize(self) -> Dict[str, List[str]]:
        """建库建表、写入网格元信息、执行崩溃恢复。返回回收报告。"""
        self.config.ensure_dirs()
        report = {"tmp": [], "obsolete": [], "orphan": []}
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            # FULL 保证 WAL 已落盘后才确认提交，断电也不丢已成功的上传。
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
            self._upsert_meta_once(conn, "grid_rows", str(self.config.GRID_ROWS))
            self._upsert_meta_once(conn, "grid_cols", str(self.config.GRID_COLS))
            self._upsert_meta_once(conn, "block_size", str(self.config.BLOCK_SIZE))
            self._upsert_meta_once(conn, "filled_cells", "0")
            self._check_grid_dims(conn)

        # 崩溃遗留的临时块（rename 前崩溃）直接清掉。
        report["tmp"] = self.store.cleanup_tmp()
        report["obsolete"], report["orphan"] = self._reap_blocks()
        return report

    def _upsert_meta_once(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO NOTHING", (key, value))

    def _check_grid_dims(self, conn: sqlite3.Connection) -> None:
        rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        for key, expected in (("grid_rows", self.config.GRID_ROWS),
                              ("grid_cols", self.config.GRID_COLS),
                              ("block_size", self.config.BLOCK_SIZE)):
            if int(rows[key]) != int(expected):
                raise IntegrityError(
                    f"数据目录中的 {key}={rows[key]} 与当前配置 {expected} 不一致，"
                    "拒绝启动以避免越界写入")

    def _reap_blocks(self) -> Tuple[List[str], List[str]]:
        """删除 obsolete_files 中及任何不被索引引用的块文件。分批处理，避免大 IN 列表。"""
        removed_obsolete: List[str] = []
        removed_orphan: List[str] = []
        with self.connection() as conn:
            referenced: set[str] = set()
            for row in conn.execute("SELECT filename FROM blocks"):
                referenced.add(row["filename"])

            obsolete: set[str] = set()
            for row in conn.execute("SELECT filename FROM obsolete_files"):
                obsolete.add(row["filename"])

            # 索引引用的文件必须存在，否则数据已不可恢复，明确报错而不是静默。
            for name in referenced:
                if not os.path.exists(self.store.block_path(name)):
                    raise IntegrityError(f"索引引用的块文件丢失: {name}")

            for name in self.store.iter_disk_files():
                if name in referenced:
                    continue
                if name in obsolete:
                    self.store.remove_file(name)
                    removed_obsolete.append(name)
                elif is_block_filename(name):
                    # 已写新块文件但事务未提交就崩溃 —— 无人引用，安全删除。
                    self.store.remove_file(name)
                    removed_orphan.append(name)
                else:
                    # 无法识别的文件不臆测，保留不动。
                    pass

            if removed_obsolete:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for i in range(0, len(removed_obsolete), 200):
                        batch = removed_obsolete[i:i + 200]
                        conn.executemany(
                            "DELETE FROM obsolete_files WHERE filename = ?",
                            [(n,) for n in batch])
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        return removed_obsolete, removed_orphan

    # ---------- 事务 ----------
    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 立即拿全库写锁，串行化所有上传。"""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """读事务：延迟 BEGIN 固定一次快照，窗口内全部读取同一提交状态。"""
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                # 只读事务用 ROLLBACK 释放快照即可。
                conn.execute("ROLLBACK")

    # ---------- 元信息 / 统计 ----------
    def get_meta(self, conn: sqlite3.Connection) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}

    def stats(self) -> Dict[str, int]:
        with self.read_transaction() as conn:
            meta = self.get_meta(conn)
            commit_count = conn.execute(
                "SELECT COUNT(*) AS n FROM commits").fetchone()["n"]
            block_count = conn.execute(
                "SELECT COUNT(*) AS n FROM blocks").fetchone()["n"]
            snapshot_commit = conn.execute(
                "SELECT COALESCE(MAX(commit_id), 0) AS c FROM commits").fetchone()["c"]
            return {
                "grid_rows": int(meta["grid_rows"]),
                "grid_cols": int(meta["grid_cols"]),
                "block_size": int(meta["block_size"]),
                "filled_cells": int(meta["filled_cells"]),
                "stored_blocks": block_count,
                "commit_count": commit_count,
                "snapshot_commit_id": snapshot_commit,
            }

    # ---------- 块点查 / 范围查 ----------
    def find_block(self, conn: sqlite3.Connection, br: int, bc: int
                   ) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT br, bc, filename, filled_cells, commit_id "
            "FROM blocks WHERE br = ? AND bc = ?", (br, bc)).fetchone()

    def iter_window_blocks(self, conn: sqlite3.Connection,
                           br_lo: int, br_hi: int,
                           bc_lo: int, bc_hi: int) -> Iterator[sqlite3.Row]:
        """只扫描与窗口相交的块坐标范围（主键范围扫描，不触碰其他块索引）。"""
        return conn.execute(
            "SELECT br, bc, filename, filled_cells, commit_id FROM blocks "
            "WHERE br BETWEEN ? AND ? AND bc BETWEEN ? AND ?",
            (br_lo, br_hi, bc_lo, bc_hi))

    # ---------- 提交记录 ----------
    def recent_commits(self, limit: int = 10) -> List[sqlite3.Row]:
        with self.read_transaction() as conn:
            return list(conn.execute(
                "SELECT commit_id, row_offset, col_offset, rows, cols, "
                "cell_count, created_at FROM commits "
                "ORDER BY commit_id DESC LIMIT ?", (int(limit),)))

    def next_commit_id(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(commit_id), 0) + 1 AS n "
                           "FROM commits").fetchone()
        return int(row["n"])
