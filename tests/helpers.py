"""单元测试共用工具：临时数据目录 + 小网格配置。"""
import os
import shutil
import tempfile

from elevation.config import Config


def make_config(grid_rows=200, grid_cols=200, max_upload=256,
                max_window=256, max_body=4 * 1024 * 1024) -> Config:
    tmp = tempfile.mkdtemp(prefix="elev-test-")
    cfg = Config.__new__(Config)
    cfg.GRID_ROWS = grid_rows
    cfg.GRID_COLS = grid_cols
    cfg.BLOCK_SIZE = 64
    cfg.MAX_UPLOAD_ROWS = max_upload
    cfg.MAX_UPLOAD_COLS = max_upload
    cfg.MAX_WINDOW_ROWS = max_window
    cfg.MAX_WINDOW_COLS = max_window
    cfg.MAX_BODY_BYTES = max_body
    cfg.BUSY_TIMEOUT_MS = 10_000
    cfg.REAP_LOCK_WAIT_MS = 10_000
    cfg.DATA_DIR = tmp
    cfg.ensure_dirs()
    return cfg


def make_config_at(data_dir, grid_rows=200, grid_cols=200):
    """指向**已有共享数据目录**的配置（多进程/多实例共用目录测试用）。

    与 :func:`make_config` 相同的小网格参数，但不新建临时目录，
    可被 pickle（只传字符串路径），适合 spawn 出来的子进程重建。
    """
    cfg = Config.__new__(Config)
    cfg.GRID_ROWS = grid_rows
    cfg.GRID_COLS = grid_cols
    cfg.BLOCK_SIZE = 64
    cfg.MAX_UPLOAD_ROWS = 256
    cfg.MAX_UPLOAD_COLS = 256
    cfg.MAX_WINDOW_ROWS = 256
    cfg.MAX_WINDOW_COLS = 256
    cfg.MAX_BODY_BYTES = 4 * 1024 * 1024
    cfg.BUSY_TIMEOUT_MS = 10_000
    cfg.REAP_LOCK_WAIT_MS = 10_000
    cfg.DATA_DIR = data_dir
    cfg.ensure_dirs()
    return cfg


def cleanup_config(cfg: Config) -> None:
    shutil.rmtree(cfg.DATA_DIR, ignore_errors=True)


def grid_csv(rows, cols, start=0):
    lines = []
    for r in range(rows):
        lines.append(",".join(str(start + r * cols + c) for c in range(cols)))
    return ("\n".join(lines) + "\n").encode("utf-8")


def csv_stream(payload: bytes):
    from io import BytesIO
    return BytesIO(payload)
