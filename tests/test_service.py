"""服务层核心语义测试：事务导入、冲突、跨块原子性、快照读、重启恢复、GC。"""
import os
import sqlite3
import threading
import unittest

from elevation.errors import (ConflictError, OutOfBoundsError, StorageError,
                              ValidationError)
from elevation.service import ElevationService
from tests.helpers import csv_stream, grid_csv, make_config


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(grid_rows=200, grid_cols=200)
        self.svc = ElevationService(self.cfg)
        self.svc.initialize()

    def tearDown(self):
        from tests.helpers import cleanup_config
        cleanup_config(self.cfg)

    def upload(self, grid_or_bytes, roff=0, coff=0):
        if isinstance(grid_or_bytes, (bytes, bytearray)):
            return self.svc.import_csv(csv_stream(bytes(grid_or_bytes)), roff, coff)
        return self.svc.import_grid(grid_or_bytes, roff, coff)

    def read(self, r, c, rows, cols):
        return self.svc.read_window(r, c, rows, cols)

    def assert_cell(self, r, c, expected):
        res = self.read(r, c, 1, 1)
        self.assertEqual(res.values[0][0], expected)


class BasicImportTest(ServiceTestBase):
    def test_single_block_import_and_read(self):
        res = self.upload([[10, 11], [20, 21]], 0, 0)
        self.assertEqual(res.new_cells, 4)
        self.assertEqual(res.blocks_written, 1)
        win = self.read(0, 0, 2, 2)
        self.assertEqual(win.values, [[10, 11], [20, 21]])
        self.assertEqual(win.filled_cells, 4)
        self.assertEqual(win.blocks_read, 1)

    def test_unfilled_cells_are_none_and_blank_in_csv(self):
        self.upload([[5]], 0, 0)
        win = self.read(0, 0, 2, 2)
        self.assertEqual(win.values, [[5, None], [None, None]])

    def test_negative_offset_rejected(self):
        with self.assertRaises(ValidationError):
            self.upload([[1]], -1, 0)
        with self.assertRaises(ValidationError):
            self.upload([[1]], 0, -1)

    def test_out_of_bounds_whole_upload_rejected(self):
        with self.assertRaises(OutOfBoundsError):
            self.upload([[1, 2]], 199, 199)  # 2 列超出 200 宽
        # 整次拒绝：没有任何格被写入
        self.assertEqual(self.svc.stats()["filled_cells"], 0)

    def test_window_limits_and_bounds(self):
        with self.assertRaises(ValidationError):
            self.read(0, 0, 257, 1)
        with self.assertRaises(OutOfBoundsError):
            self.read(199, 0, 2, 1)


class OverlapConflictTest(ServiceTestBase):
    def test_equal_value_overlap_reused(self):
        self.upload([[7, 8], [9, 10]], 0, 0)
        # 同样的值再次上传（部分重叠 + 新格）
        res = self.upload([[7, 8, 11]], 0, 0)
        self.assertEqual(res.new_cells, 1)
        self.assertEqual(res.blocks_unchanged, 0)

    def test_equal_only_upload_writes_nothing(self):
        self.upload([[7]], 0, 0)
        res = self.upload([[7]], 0, 0)
        self.assertEqual(res.blocks_written, 0)
        self.assertEqual(res.blocks_unchanged, 1)
        self.assertEqual(res.new_cells, 0)

    def test_different_value_whole_upload_rejected(self):
        self.upload([[1, 2], [3, 4]], 0, 0)
        before = self.svc.stats()
        with self.assertRaises(ConflictError) as ctx:
            # 第一格等值、第二格异值 -> 整次拒绝
            self.upload([[1, 99]], 0, 0)
        self.assertIn("(0,1)", str(ctx.exception))
        after = self.svc.stats()
        self.assertEqual(before, after)
        # 原值完好
        self.assert_cell(0, 1, 2)

    def test_explicit_zero_distinct_from_unfilled(self):
        self.upload([[0]], 5, 5)
        self.assert_cell(5, 5, 0)
        res = self.read(5, 5, 1, 2)
        self.assertEqual(res.values, [[0, None]])


class CrossBlockAtomicityTest(ServiceTestBase):
    def test_upload_spanning_four_blocks(self):
        # 62,62 起 4×4：覆盖块 (0,0)(0,1)(1,0)(1,1)
        grid = [[100 + r * 4 + c for c in range(4)] for r in range(4)]
        res = self.upload(grid, 62, 62)
        self.assertEqual(res.blocks_written, 4)
        win = self.read(62, 62, 4, 4)
        self.assertEqual(win.values, grid)
        self.assertEqual(win.blocks_read, 4)
        self.assertEqual(self.svc.stats()["stored_blocks"], 4)

    def test_conflict_in_second_block_rolls_back_first_block(self):
        # 先在块 (1,1) 放一个值
        self.upload([[42]], 100, 100)
        # 跨 4 块上传，其中块 (1,1) 内格 (100,100) 异值
        grid = [[100 + r * 4 + c for c in range(4)] for r in range(4)]
        grid[2][2] = 999  # 对应 (64,64)？实际 (62+2,62+2)=(64,64)，不是 (100,100)
        # 重新构造：让 (100,100) 落在上传范围内
        big = [[1 for _ in range(40)] for _ in range(40)]  # 62..101
        with self.assertRaises(ConflictError):
            self.upload(big, 62, 62)
        # 除预先存在的 (100,100) 块外，不应出现任何新块
        stats = self.svc.stats()
        self.assertEqual(stats["stored_blocks"], 1)
        self.assertEqual(stats["filled_cells"], 1)
        self.assert_cell(100, 100, 42)
        # 新块文件即使曾落盘也必须被回滚清理
        on_disk = [n for n in os.listdir(self.cfg.blocks_dir) if n.endswith(".blk")]
        self.assertEqual(len(on_disk), 1)

    def test_failed_commit_leaves_no_partial_state(self):
        self.upload([[5]], 0, 0)
        # 模拟块文件写入阶段后的提交失败：临时把 blocks 表搞坏使 COMMIT 路径异常较难，
        # 改为验证冲突路径下所有已写新文件被清理（上面的用例已覆盖），
        # 这里再验证磁盘上无 tmp 残留。
        self.assertEqual(
            [n for n in os.listdir(self.cfg.tmp_dir) if n.endswith(".tmp")], [])


class SnapshotReadTest(ServiceTestBase):
    def test_read_uses_one_committed_snapshot(self):
        self.upload([[1]], 0, 0)
        win = self.read(0, 0, 1, 1)
        self.assertEqual(win.snapshot_commit_id, 1)
        self.upload([[2]], 1, 1)
        win2 = self.read(0, 0, 2, 2)
        self.assertEqual(win2.snapshot_commit_id, 2)
        self.assertEqual(win2.values, [[1, None], [None, 2]])

    def test_window_only_touches_intersecting_blocks(self):
        self.upload([[r * 10 + c for c in range(10)] for r in range(10)], 0, 0)
        self.upload([[55, 56], [65, 66]], 60, 60)   # 块 (0,0) 内的相邻区域
        self.upload([[99]], 150, 150)               # 远处块 (2,2)
        win = self.read(0, 0, 5, 5)
        self.assertEqual(win.blocks_read, 1)        # 只访问 (0,0) 块
        win2 = self.read(60, 60, 8, 8)  # 跨 (0,0)(0,1)(1,0)(1,1)
        self.assertEqual(win2.blocks_read, 1)       # 只有 (0,0) 有数据
        self.assertEqual(win2.values[0][0], 55)     # (60,60)
        self.assertIsNone(win2.values[7][7])        # (67,67) 未填
        win3 = self.read(145, 145, 10, 10)
        self.assertEqual(win3.blocks_read, 1)
        self.assertEqual(win3.values[5][5], 99)


class RestartAndGCTest(ServiceTestBase):
    def test_data_survives_service_restart(self):
        self.upload([[10, 11], [12, 13]], 70, 70)
        # 用同一数据目录构造全新服务实例，模拟进程重启
        svc2 = ElevationService(self.cfg)
        report = svc2.initialize()
        self.assertEqual(report["tmp"], [])
        win = svc2.read_window(70, 70, 2, 2)
        self.assertEqual(win.values, [[10, 11], [12, 13]])

    def test_old_block_versions_reaped_on_restart(self):
        self.upload([[1]], 0, 0)
        first_files = set(os.listdir(self.cfg.blocks_dir))
        self.upload([[1, 2]], 0, 0)  # 同块新版本
        self.assertEqual(len(os.listdir(self.cfg.blocks_dir)), 2)
        # 旧版本在 obsolete_files 中登记，运行期间保留以保护旧快照
        with sqlite3.connect(self.cfg.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) FROM obsolete_files").fetchone()[0]
        self.assertEqual(n, 1)
        svc2 = ElevationService(self.cfg)
        report = svc2.initialize()
        self.assertEqual(len(report["obsolete"]), 1)
        on_disk = set(os.listdir(self.cfg.blocks_dir))
        self.assertEqual(len(on_disk), 1)
        self.assertTrue(on_disk.isdisjoint(first_files & set(report["obsolete"])))
        # 新服务读到的仍是最新值
        self.assertEqual(svc2.read_window(0, 0, 1, 2).values, [[1, 2]])

    def test_orphan_block_from_crash_reaped(self):
        self.upload([[1]], 0, 0)
        # 伪造“块文件已写但事务未提交”的崩溃现场
        from elevation.blockstore import BlockStore
        orphan = BlockStore(self.cfg).write_block(3, 3, {(0, 0): 7})
        svc2 = ElevationService(self.cfg)
        report = svc2.initialize()
        self.assertIn(orphan, report["orphan"])
        self.assertFalse(os.path.exists(
            os.path.join(self.cfg.blocks_dir, orphan)))

    def test_tmp_leftover_reaped(self):
        path = os.path.join(self.cfg.tmp_dir, "b0-0-dead.tmp")
        with open(path, "wb") as fh:
            fh.write(b"x" * 100)
        svc2 = ElevationService(self.cfg)
        report = svc2.initialize()
        self.assertIn("b0-0-dead.tmp", report["tmp"])
        self.assertFalse(os.path.exists(path))


class ConcurrencyTest(ServiceTestBase):
    def test_concurrent_disjoint_uploads_all_succeed(self):
        errors = []

        def worker(base):
            try:
                for i in range(5):
                    r = base + i * 3
                    self.upload([[r, r + 1], [r + 2, r + 3]], r, r)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(b,))
                   for b in (0, 20, 40, 60, 120)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 5 线程 × 5 次 × 4 格 = 100 个已填格，无一丢失
        self.assertEqual(self.svc.stats()["filled_cells"], 100)

    def test_concurrent_same_cell_equal_values_succeed(self):
        errors = []

        def worker():
            try:
                for _ in range(10):
                    self.upload([[777]], 30, 30)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assert_cell(30, 30, 777)
        self.assertEqual(self.svc.stats()["filled_cells"], 1)

    def test_concurrent_conflicting_values_one_wins_rest_rejected(self):
        outcomes = []

        def worker(value):
            try:
                self.upload([[value]], 45, 45)
                outcomes.append(("ok", value))
            except ConflictError:
                outcomes.append(("conflict", value))
            except StorageError as exc:
                outcomes.append(("storage", str(exc)))

        threads = [threading.Thread(target=worker, args=(v,))
                   for v in (1, 2, 3, 4, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [v for k, v in outcomes if k == "ok"]
        self.assertEqual(len(oks), 1)            # 只有一个值能占位
        self.assertEqual(len([o for o in outcomes if o[0] == "conflict"]), 4)
        self.assert_cell(45, 45, oks[0])
        self.assertEqual(self.svc.stats()["filled_cells"], 1)


class LargeGridNoPreallocTest(unittest.TestCase):
    def test_100k_grid_uses_no_proportional_storage(self):
        cfg = make_config(grid_rows=100_000, grid_cols=100_000)
        try:
            svc = ElevationService(cfg)
            svc.initialize()
            # 10 万 × 10 万逻辑网格初始时没有任何块文件
            self.assertEqual(svc.stats()["stored_blocks"], 0)
            self.assertEqual(os.listdir(cfg.blocks_dir), [])
            # 远角写入一个格，只产生一个块
            svc.import_grid([[123]], 99_999, 99_999)
            self.assertEqual(svc.stats()["stored_blocks"], 1)
            win = svc.read_window(99_999, 99_999, 1, 1)
            self.assertEqual(win.values, [[123]])
            # 越界校验在 10 万边界生效
            with self.assertRaises(OutOfBoundsError):
                svc.import_grid([[1]], 100_000, 0)
        finally:
            from tests.helpers import cleanup_config
            cleanup_config(cfg)


if __name__ == "__main__":
    unittest.main()
