"""故障与边界：配置漂移防护、写盘失败回滚、提交失败回滚。"""
import os
import sqlite3
import unittest
from contextlib import contextmanager

from elevation.database import IntegrityError
from elevation.errors import StorageError
from elevation.service import ElevationService
from tests.helpers import make_config


class ConfigDriftTest(unittest.TestCase):
    def test_grid_dimension_mismatch_refuses_start(self):
        cfg = make_config(grid_rows=200, grid_cols=200)
        try:
            ElevationService(cfg).initialize()
            # 同目录换一套网格尺寸 -> 拒绝启动，防止越界写入旧数据
            cfg.GRID_ROWS = 500
            with self.assertRaises(IntegrityError):
                ElevationService(cfg).initialize()
        finally:
            from tests.helpers import cleanup_config
            cleanup_config(cfg)

    def test_block_size_mismatch_refuses_start(self):
        cfg = make_config(grid_rows=200, grid_cols=200)
        try:
            ElevationService(cfg).initialize()
            cfg.BLOCK_SIZE = 32
            with self.assertRaises(IntegrityError):
                ElevationService(cfg).initialize()
        finally:
            from tests.helpers import cleanup_config
            cleanup_config(cfg)


class WriteFailureTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(grid_rows=200, grid_cols=200)
        self.svc = ElevationService(self.cfg)
        self.svc.initialize()

    def tearDown(self):
        from tests.helpers import cleanup_config
        cleanup_config(self.cfg)

    def test_disk_write_failure_rolls_back_entire_upload(self):
        # 先放一块已有数据
        self.svc.import_grid([[1, 2], [3, 4]], 0, 0)

        original_write = self.svc.store.write_block
        calls = {"n": 0}

        def flaky_write(br, bc, cells):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("模拟磁盘已满")
            return original_write(br, bc, cells)

        self.svc.store.write_block = flaky_write
        # 跨 4 块上传，第二个块写盘失败 -> 整次拒绝、无残留
        grid = [[100 + r * 4 + c for c in range(4)] for r in range(4)]
        with self.assertRaises(StorageError):
            self.svc.import_grid(grid, 62, 62)

        self.svc.store.write_block = original_write
        stats = self.svc.stats()
        self.assertEqual(stats["filled_cells"], 4)
        self.assertEqual(stats["stored_blocks"], 1)
        self.assertEqual(stats["commit_count"], 1)
        win = self.svc.read_window(0, 0, 2, 2)
        self.assertEqual(win.values, [[1, 2], [3, 4]])
        # 第一个已落盘的新块也必须被清理
        on_disk = [n for n in os.listdir(self.cfg.blocks_dir) if n.endswith(".blk")]
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(os.listdir(self.cfg.tmp_dir), [])

    def test_commit_failure_cleans_new_block_files(self):
        svc = self.svc

        class _DelegatingConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, params=()):
                return self._real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._real, name)

        @contextmanager
        def failing_tx():
            conn = svc.db._new_connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield _DelegatingConn(conn)
                # 模拟 fsync/提交阶段故障：块文件已写但事务必须回滚
                raise sqlite3.OperationalError("模拟提交失败")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

        svc.db.write_transaction = failing_tx
        with self.assertRaises(StorageError):
            svc.import_grid([[9]], 80, 80)

        self.assertEqual(svc.stats()["filled_cells"], 0)
        on_disk = [n for n in os.listdir(self.cfg.blocks_dir) if n.endswith(".blk")]
        self.assertEqual(on_disk, [])
        self.assertEqual(os.listdir(self.cfg.tmp_dir), [])


if __name__ == "__main__":
    unittest.main()
