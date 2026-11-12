"""核心业务服务：事务化导入与窗口取数（与 Web 框架无关）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import BinaryIO, Dict, List, Optional, Tuple

from .blockstore import BlockStore, BlockFormatError
from .config import Config
from .database import Database, IntegrityError
from .errors import (ConflictError, OutOfBoundsError, StorageError,
                     ValidationError)
from .parsing import parse_int_grid


@dataclass
class ImportResult:
    commit_id: int
    rows: int
    cols: int
    row_offset: int
    col_offset: int
    cell_count: int                 # 本次 CSV 的格数
    new_cells: int                  # 实际新增的已填格
    blocks_written: int             # 新写出的块版本数
    blocks_unchanged: int           # 全部命中等值复用、未重写的块
    replaced_files: List[str] = field(default_factory=list)


@dataclass
class WindowResult:
    row_offset: int
    col_offset: int
    rows: int
    cols: int
    values: List[List[Optional[int]]]
    filled_cells: int
    snapshot_commit_id: int
    blocks_read: int


class ElevationService:
    def __init__(self, config: Config):
        self.config = config
        self.store = BlockStore(config)
        self.db = Database(config, self.store)

    def initialize(self) -> Dict[str, List[str]]:
        return self.db.initialize()

    # ================= 导入 =================
    def import_csv(self, stream: BinaryIO,
                   row_offset: int, col_offset: int) -> ImportResult:
        cfg = self.config
        grid, _bytes = parse_int_grid(
            stream, cfg.MAX_UPLOAD_ROWS, cfg.MAX_UPLOAD_COLS, cfg.MAX_BODY_BYTES)
        return self.import_grid(grid, row_offset, col_offset)

    def import_grid(self, grid: List[List[int]],
                    row_offset: int, col_offset: int) -> ImportResult:
        cfg = self.config
        if row_offset < 0 or col_offset < 0:
            raise ValidationError("左上行列偏移必须为非负整数（从 0 起）")
        if not grid or not grid[0]:
            raise ValidationError("上传网格不能为空，至少需要 1×1")
        up_rows, up_cols = len(grid), len(grid[0])
        if any(len(row) != up_cols for row in grid):
            raise ValidationError("上传网格各行列数不一致")
        if up_rows > cfg.MAX_UPLOAD_ROWS or up_cols > cfg.MAX_UPLOAD_COLS:
            raise ValidationError(
                f"上传网格最大 {cfg.MAX_UPLOAD_ROWS}×{cfg.MAX_UPLOAD_COLS}，"
                f"收到 {up_rows}×{up_cols}")
        if row_offset + up_rows > cfg.GRID_ROWS or \
                col_offset + up_cols > cfg.GRID_COLS:
            raise OutOfBoundsError(
                f"上传范围 [{row_offset}..{row_offset + up_rows}) × "
                f"[{col_offset}..{col_offset + up_cols}) 越出网格 "
                f"{cfg.GRID_ROWS}×{cfg.GRID_COLS}，整次拒绝")

        # 先在内存按块分组（单次最多 256×256，天然有界；不触碰全量索引）。
        grouped: Dict[Tuple[int, int], Dict[Tuple[int, int], int]] = {}
        for lr, row in enumerate(grid):
            gr = row_offset + lr
            for lc, value in enumerate(row):
                gc = col_offset + lc
                key = (gr // cfg.BLOCK_SIZE, gc // cfg.BLOCK_SIZE)
                grouped.setdefault(key, {})[(gr % cfg.BLOCK_SIZE, gc % cfg.BLOCK_SIZE)] = value

        new_files: List[str] = []
        replaced_files: List[str] = []
        merged_blocks: List[Tuple[int, int, str, int, int]] = []
        # 元组: (br, bc, 新文件名, 新块已填格数, 新增格数)
        unchanged_blocks = 0
        total_new_cells = 0

        try:
            with self.db.write_transaction() as conn:
                for (br, bc), incoming in grouped.items():
                    existing_row = self.db.find_block(conn, br, bc)
                    existing_cells: Dict[Tuple[int, int], int] = {}
                    if existing_row is not None:
                        try:
                            existing_cells = self.store.read_block(
                                br, bc, existing_row["filename"])
                        except (OSError, BlockFormatError) as exc:
                            raise StorageError(f"读取已有块 ({br},{bc}) 失败: {exc}") from exc

                    # 冲突检测：重叠格等值复用，异值整次拒绝。
                    for (lr, lc), new_value in incoming.items():
                        old_value = existing_cells.get((lr, lc))
                        if old_value is not None and old_value != new_value:
                            gr, gc = br * cfg.BLOCK_SIZE + lr, bc * cfg.BLOCK_SIZE + lc
                            raise ConflictError(
                                f"格 ({gr},{gc}) 已存在值 {old_value}，"
                                f"本次提交值 {new_value} 不一致，整次拒绝")

                    merged = dict(existing_cells)
                    added = 0
                    for coord, value in incoming.items():
                        if coord not in merged:
                            added += 1
                        merged[coord] = value  # 等值覆盖结果相同

                    if added == 0 and existing_row is not None:
                        # 全部为等值复用：不产生新版本，保留原块文件。
                        unchanged_blocks += 1
                        continue

                    filename = self.store.write_block(br, bc, merged)
                    new_files.append(filename)
                    if existing_row is not None:
                        replaced_files.append(existing_row["filename"])
                    merged_blocks.append((br, bc, filename, len(merged), added))
                    total_new_cells += added

                if not merged_blocks:
                    # 纯等值复用提交：仍记录一条 commit，便于审计；不改动任何块。
                    commit_id = self.db.next_commit_id(conn)
                    conn.execute(
                        "INSERT INTO commits(commit_id, row_offset, col_offset, "
                        "rows, cols, cell_count) VALUES(?,?,?,?,?,?)",
                        (commit_id, row_offset, col_offset, up_rows, up_cols,
                         up_rows * up_cols))
                    self._bump_meta(conn, 0)
                else:
                    commit_id = self.db.next_commit_id(conn)
                    for br, bc, filename, filled, _added in merged_blocks:
                        conn.execute(
                            "INSERT INTO blocks(br, bc, filename, filled_cells, commit_id) "
                            "VALUES(?,?,?,?,?) "
                            "ON CONFLICT(br, bc) DO UPDATE SET "
                            "filename=excluded.filename, "
                            "filled_cells=excluded.filled_cells, "
                            "commit_id=excluded.commit_id",
                            (br, bc, filename, filled, commit_id))
                    for old_name in replaced_files:
                        conn.execute(
                            "INSERT INTO obsolete_files(filename, created_commit) "
                            "VALUES(?, ?) ON CONFLICT(filename) DO NOTHING",
                            (old_name, commit_id))
                    conn.execute(
                        "INSERT INTO commits(commit_id, row_offset, col_offset, "
                        "rows, cols, cell_count) VALUES(?,?,?,?,?,?)",
                        (commit_id, row_offset, col_offset, up_rows, up_cols,
                         up_rows * up_cols))
                    self._bump_meta(conn, total_new_cells)
        except ConflictError:
            self._rollback_files(new_files)
            raise
        except IntegrityError as exc:  # SQLite 索引/磁盘完整性问题
            self._rollback_files(new_files)
            raise StorageError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 任何写盘/提交失败都保证无部分更新
            self._rollback_files(new_files)
            if isinstance(exc, (ValidationError, OutOfBoundsError)):
                raise
            raise StorageError(f"写入失败，已回滚本次全部改动: {exc}") from exc

        return ImportResult(
            commit_id=commit_id, rows=up_rows, cols=up_cols,
            row_offset=row_offset, col_offset=col_offset,
            cell_count=up_rows * up_cols, new_cells=total_new_cells,
            blocks_written=len(merged_blocks),
            blocks_unchanged=unchanged_blocks,
            replaced_files=replaced_files)

    @staticmethod
    def _bump_meta(conn, delta: int) -> None:
        conn.execute(
            "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT) "
            "WHERE key = 'filled_cells'", (delta,))

    def _rollback_files(self, filenames: List[str]) -> None:
        """事务回滚后清理本次已落盘但无人引用的新块文件。"""
        for name in filenames:
            try:
                os.unlink(self.store.block_path(name))
            except FileNotFoundError:
                pass

    # ================= 窗口取数 =================
    def read_window(self, row_offset: int, col_offset: int,
                    rows: int, cols: int) -> WindowResult:
        cfg = self.config
        if row_offset < 0 or col_offset < 0:
            raise ValidationError("窗口左上行列偏移必须为非负整数")
        if rows <= 0 or cols <= 0:
            raise ValidationError("窗口行列数必须为正整数")
        if rows > cfg.MAX_WINDOW_ROWS or cols > cfg.MAX_WINDOW_COLS:
            raise ValidationError(
                f"窗口最大 {cfg.MAX_WINDOW_ROWS}×{cfg.MAX_WINDOW_COLS}")
        if row_offset + rows > cfg.GRID_ROWS or col_offset + cols > cfg.GRID_COLS:
            raise OutOfBoundsError(
                f"窗口 [{row_offset}..{row_offset + rows}) × "
                f"[{col_offset}..{col_offset + cols}) 越出网格 "
                f"{cfg.GRID_ROWS}×{cfg.GRID_COLS}")

        # 输出矩阵固定窗口大小，未填格保持 None（下载时渲染为空字段）。
        values: List[List[Optional[int]]] = [[None] * cols for _ in range(rows)]
        filled = 0
        blocks_read = 0

        with self.db.read_transaction() as conn:
            snapshot = self.db.next_commit_id(conn) - 1  # 当前最大已提交 commit
            br_lo, br_hi = row_offset // cfg.BLOCK_SIZE, \
                (row_offset + rows - 1) // cfg.BLOCK_SIZE
            bc_lo, bc_hi = col_offset // cfg.BLOCK_SIZE, \
                (col_offset + cols - 1) // cfg.BLOCK_SIZE

            for row in self.db.iter_window_blocks(conn, br_lo, br_hi, bc_lo, bc_hi):
                br, bc = row["br"], row["bc"]
                try:
                    cells = self.store.read_block(br, bc, row["filename"])
                except (OSError, BlockFormatError) as exc:
                    raise StorageError(f"读取块 ({br},{bc}) 失败: {exc}") from exc
                blocks_read += 1
                g_r0, g_c0 = br * cfg.BLOCK_SIZE, bc * cfg.BLOCK_SIZE
                for (lr, lc), value in cells.items():
                    gr, gc = g_r0 + lr, g_c0 + lc
                    if row_offset <= gr < row_offset + rows and \
                            col_offset <= gc < col_offset + cols:
                        values[gr - row_offset][gc - col_offset] = value
                        filled += 1

        return WindowResult(
            row_offset=row_offset, col_offset=col_offset, rows=rows, cols=cols,
            values=values, filled_cells=filled,
            snapshot_commit_id=max(snapshot, 0), blocks_read=blocks_read)

    # ================= 统计 =================
    def stats(self) -> Dict[str, int]:
        return self.db.stats()

    def recent_commits(self, limit: int = 10):
        return self.db.recent_commits(limit)
