"""多进程共用数据目录的回收竞态回归测试。

这些用例用真正的子进程（``multiprocessing`` spawn/fork）+ ``multiprocessing.Event``
精确编排交错时刻，覆盖题面要求的三类保证：

1. 启动清理保护**进行中上传**已写出但尚未提交索引的块/tmp 文件，
   上传成功后数据完整可读；
2. 启动清理保护**窗口固定快照**仍需要的旧块，窗口始终返回原快照；
3. 进程退出后留下的无引用文件（孤儿块/tmp/死守护）在后续启动仍被回收。

子进程通过 ``sys.path`` 注入项目根、用字符串数据目录重建配置，
不依赖父进程内存状态；所有同步原语均为可跨进程序列化的 ``Event``。
"""
import os
import sys
import time
import unittest
import multiprocessing as mp

# 让 spawn 出的子进程也能 import 项目包与 tests.helpers。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests.helpers import make_config, make_config_at, cleanup_config  # noqa: E402


def _wait(ev: "mp.synchronize.Event", timeout=15.0):
    if not ev.wait(timeout):
        raise RuntimeError("测试同步事件等待超时，子进程可能已异常退出")


# ================= 子进程入口（必须是模块级、可 pickle 的函数） =================

def child_live_upload(data_dir, block_written, allow_finish, result_q,
                      row=70, col=70, value=77):
    """子进程 A：上传在块落盘后、索引提交前被暂停。"""
    try:
        from elevation.service import ElevationService
        cfg = make_config_at(data_dir)
        svc = ElevationService(cfg)
        svc.initialize()

        # 在“块文件已落盘、索引事务尚未提交”处插入确定性停顿。
        original_write = svc.store.write_block

        def paused_write(br, bc, cells, guard=None):
            name = original_write(br, bc, cells, guard=guard)
            block_written.set()       # 通知主进程：在途块已可见
            _wait(allow_finish)       # 等主进程在此期间跑完启动回收
            return name

        svc.store.write_block = paused_write
        res = svc.import_grid([[value]], row, col)
        result_q.put(("ok", res.commit_id))
    except BaseException as exc:  # noqa: BLE001 - 回传到主进程断言
        result_q.put(("error", repr(exc)))


def child_live_upload_with_tmp(data_dir, tmp_visible, allow_finish, result_q):
    """子进程 A：在 tmp 文件已建、rename 之前暂停（验证 tmp 也受保护）。"""
    try:
        import elevation.blockstore as bs
        from elevation.service import ElevationService
        cfg = make_config_at(data_dir)
        svc = ElevationService(cfg)
        svc.initialize()
        original_replace = os.replace

        def paused_replace(src, dst):
            tmp_visible.set()
            _wait(allow_finish)
            return original_replace(src, dst)

        bs.os.replace = paused_replace  # 模块级 os.replace，write_block 内部命中
        try:
            res = svc.import_grid([[88]], 70, 70)
        finally:
            bs.os.replace = original_replace
        result_q.put(("ok", res.commit_id))
    except BaseException as exc:  # noqa: BLE001
        result_q.put(("error", repr(exc)))


def child_slow_window(data_dir, window_started, allow_finish, result_q,
                      make_obsolete_after_snapshot=False):
    """子进程 R：窗口读取在固定快照后、读块文件期间被放慢。

    ``make_obsolete_after_snapshot`` 为真时，在固定旧快照之后、读块之前，
    先把同块替换出一个新版本（旧版进入 obsolete_files），用于验证旧快照
    仍指向并读取旧块。
    """
    try:
        from elevation.service import ElevationService
        cfg = make_config_at(data_dir)
        svc = ElevationService(cfg)
        svc.initialize()
        if make_obsolete_after_snapshot:
            svc.import_grid([[1]], 0, 0)  # 先建旧版本
        original_read = svc.store.read_block
        state = {"armed": True}

        def slow_read(br, bc, filename):
            # 只对窗口本身的第一次块读取放慢；import_grid 内部的读取直接放行，
            # 否则替换流程会递归进入本函数造成自死锁。
            if state["armed"]:
                state["armed"] = False
                if make_obsolete_after_snapshot:
                    # 固定旧快照后再替换：旧块此刻成为 obsolete，但本窗口仍要读它。
                    svc.import_grid([[1, 2]], 0, 0)
                window_started.set()  # 共享回收锁已持有、快照固定、替换已完成
                _wait(allow_finish)   # 期间主进程尝试启动回收
            return original_read(br, bc, filename)

        svc.store.read_block = slow_read
        win = svc.read_window(0, 0, 1, 2)
        result_q.put(("ok", [row for row in win.values]))
    except BaseException as exc:  # noqa: BLE001
        result_q.put(("error", repr(exc)))


def child_hold_inflight_file(data_dir, br, bc, ready, allow_finish, result_q):
    """子进程：持有上传守护并已写出块文件（文件阶段），但不提交索引。

    这精确刻画“块已落盘、索引未提交”的在途状态，且不占用 SQLite 写锁，
    因此多个这样的进程可以真正同时存在，用于验证回收对多个在途文件的保护。
    """
    try:
        from elevation.service import ElevationService
        from elevation.locking import UploadGuard
        cfg = make_config_at(data_dir)
        svc = ElevationService(cfg)
        svc.initialize()
        guard = UploadGuard(cfg).__enter__()
        name = svc.store.write_block(br, bc, {(0, 0): 1000 + br * 10 + bc},
                                     guard=guard)
        result_q.put(("name", name))
        ready.set()
        _wait(allow_finish)
        guard.__exit__(None, None, None)
        result_q.put(("done", name))
    except BaseException as exc:  # noqa: BLE001
        result_q.put(("error", repr(exc)))


def child_crash_mid_upload(data_dir, block_written, result_q):
    """子进程 C：写出在途块后直接退出（模拟进程崩溃），留下死守护+孤儿块。"""
    try:
        from elevation.service import ElevationService
        from elevation.locking import UploadGuard
        cfg = make_config_at(data_dir)
        svc = ElevationService(cfg)
        svc.initialize()
        guard = UploadGuard(cfg).__enter__()
        name = svc.store.write_block(2, 2, {(0, 0): 55}, guard=guard)
        result_q.put(("name", name))
        block_written.set()
        # 不释放守护、不提交索引，直接硬退出，模拟崩溃。
        os._exit(0)
    except BaseException:  # noqa: BLE001
        os._exit(1)


class MultiProcessReaperTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(grid_rows=200, grid_cols=200)
        self.data_dir = self.cfg.DATA_DIR
        self.ctx = mp.get_context("fork")
        self.queue = self.ctx.Queue()

    def tearDown(self):
        cleanup_config(self.cfg)

    def _new_service(self):
        from elevation.service import ElevationService
        return ElevationService(make_config_at(self.data_dir))

    # ---- 竞态 1：启动清理不得删除进行中上传的在途块 ----
    def test_startup_reap_protects_inflight_upload_block(self):
        block_written = self.ctx.Event()
        allow_finish = self.ctx.Event()
        proc = self.ctx.Process(
            target=child_live_upload,
            args=(self.data_dir, block_written, allow_finish, self.queue))
        proc.start()
        try:
            _wait(block_written)
            # 在途块已落盘、索引未提交：另一个进程此刻启动并做启动回收。
            reaper = self._new_service()
            report = reaper.initialize()
            # 关键断言：在途块没有被当孤儿删除，回收清单为空。
            self.assertEqual(report["orphan"], [])
            self.assertEqual(report["obsolete"], [])
            self.assertEqual(report["tmp"], [])
            blocks_on_disk = [n for n in os.listdir(self.cfg.blocks_dir)
                              if n.endswith(".blk")]
            self.assertEqual(len(blocks_on_disk), 1)

            allow_finish.set()  # 放行上传完成提交
            kind, payload = self.queue.get(timeout=15)
            self.assertEqual(kind, "ok", msg=f"子进程上传失败: {payload}")
        finally:
            allow_finish.set()
            proc.join(timeout=15)
        self.assertEqual(proc.exitcode, 0)

        # 上传报告成功后，数据必须完整可读（修复前这里会因文件缺失而 500）。
        svc = self._new_service()
        svc.initialize()
        win = svc.read_window(70, 70, 1, 1)
        self.assertEqual(win.values, [[77]])
        # 上传结束后守护文件应已自行清理。
        guards = [n for n in os.listdir(self.cfg.locks_dir)
                  if n.startswith("upload-")]
        self.assertEqual(guards, [])

    # ---- 竞态 1b：进行中上传的 tmp 文件同样受保护 ----
    def test_startup_reap_protects_inflight_tmp_file(self):
        tmp_visible = self.ctx.Event()
        allow_finish = self.ctx.Event()
        proc = self.ctx.Process(
            target=child_live_upload_with_tmp,
            args=(self.data_dir, tmp_visible, allow_finish, self.queue))
        proc.start()
        try:
            _wait(tmp_visible)
            tmp_files = [n for n in os.listdir(self.cfg.tmp_dir)
                         if n.endswith(".tmp")]
            self.assertEqual(len(tmp_files), 1)
            # 另一进程启动回收：tmp 属于活体上传，不得删除。
            report = self._new_service().initialize()
            self.assertEqual(report["tmp"], [])
            self.assertTrue(
                os.path.exists(os.path.join(self.cfg.tmp_dir, tmp_files[0])))

            allow_finish.set()
            kind, payload = self.queue.get(timeout=15)
            self.assertEqual(kind, "ok", msg=f"子进程上传失败: {payload}")
        finally:
            allow_finish.set()
            proc.join(timeout=15)
        self.assertEqual(proc.exitcode, 0)
        # 上传成功后 rename 完成，tmp 已消失，块可读。
        svc = self._new_service()
        svc.initialize()
        self.assertEqual(svc.read_window(70, 70, 1, 1).values, [[88]])
        self.assertEqual(
            [n for n in os.listdir(self.cfg.tmp_dir) if n.endswith(".tmp")], [])

    # ---- 竞态 2：窗口固定旧快照，替换+启动回收不得删其旧块 ----
    def test_startup_reap_waits_for_live_window_and_snapshot_stable(self):
        # 先建立 commit #1 的块，窗口将固定这个旧快照。
        seed = self._new_service()
        seed.initialize()
        seed.import_grid([[1]], 0, 0)

        window_started = self.ctx.Event()
        allow_finish = self.ctx.Event()
        proc = self.ctx.Process(
            target=child_slow_window,
            args=(self.data_dir, window_started, allow_finish, self.queue))
        proc.start()
        try:
            _wait(window_started)  # 子进程已持共享锁并固定旧快照

            # 同块出新版本：旧块进入 obsolete_files（新快照为 [[1,2]]）。
            seed.import_grid([[1, 2]], 0, 0)
            obsolete_before = os.listdir(self.cfg.blocks_dir)
            self.assertEqual(len(obsolete_before), 2)  # 新旧两版都在盘上

            # 启动回收必须等待窗口结束，期间不能删旧块。
            t0 = time.monotonic()
            report = self._new_service().initialize()
            waited = time.monotonic() - t0

            # 窗口仍在进行（还没放行），排他锁拿不到 -> 本轮不删除。
            self.assertEqual(report.get("deferred", []) != [], True)
            self.assertEqual(report["obsolete"], [])
            self.assertEqual(len(os.listdir(self.cfg.blocks_dir)), 2)
            self.assertGreaterEqual(waited, 0.0)

            allow_finish.set()  # 窗口读完，返回固定的旧快照
            kind, payload = self.queue.get(timeout=15)
            self.assertEqual(kind, "ok", msg=f"窗口读取失败: {payload}")
            self.assertEqual(payload, [[1, None]])  # 旧快照，不含新写入的 2
        finally:
            allow_finish.set()
            proc.join(timeout=15)
        self.assertEqual(proc.exitcode, 0)

        # 窗口结束后再启动：obsolete 旧块被正常回收，新快照可读。
        later = self._new_service()
        report2 = later.initialize()
        self.assertEqual(len(report2["obsolete"]), 1)
        self.assertEqual(len(os.listdir(self.cfg.blocks_dir)), 1)
        self.assertEqual(later.read_window(0, 0, 1, 2).values, [[1, 2]])

    # ---- 等待安全时机：窗口很快结束时，启动回收应等到锁后真正回收 ----
    def test_startup_reap_waits_then_succeeds_after_window_closes(self):
        window_started = self.ctx.Event()
        allow_finish = self.ctx.Event()
        q = self.ctx.Queue()
        # 子进程在持快照期间自行把旧块替换为 obsolete，避免其启动期先回收掉。
        proc = self.ctx.Process(
            target=child_slow_window,
            args=(self.data_dir, window_started, allow_finish, q, True))
        proc.start()
        try:
            _wait(window_started)
            # 此刻子进程已固定旧快照并完成替换：盘上有新旧两块，旧块 obsolete。
            self.assertEqual(len(os.listdir(self.cfg.blocks_dir)), 2)
            allow_finish.set()  # 窗口立即结束 -> 共享锁很快释放
            kind, payload = q.get(timeout=15)
            self.assertEqual(kind, "ok")
            self.assertEqual(payload, [[1, None]])  # 窗口读到的是固定旧快照
            proc.join(timeout=15)
        finally:
            allow_finish.set()
            proc.join(timeout=15)
        self.assertEqual(proc.exitcode, 0)

        # 此刻无活体窗口：启动回收应拿到排他锁并真正删掉 obsolete 块（非 deferred）。
        report = self._new_service().initialize()
        self.assertEqual(len(report["obsolete"]), 1)
        self.assertNotIn("deferred", report)
        self.assertEqual(len(os.listdir(self.cfg.blocks_dir)), 1)

    # ---- 退出后回收 1：崩溃进程的死守护 + 孤儿块在后续启动被清 ----
    def test_orphan_and_dead_guard_reaped_after_process_exit(self):
        block_written = self.ctx.Event()
        crash_q = self.ctx.Queue()
        proc = self.ctx.Process(
            target=child_crash_mid_upload,
            args=(self.data_dir, block_written, crash_q))
        proc.start()
        _wait(block_written)
        proc.join(timeout=10)
        self.assertEqual(proc.exitcode, 0)

        kind, orphan_name = crash_q.get(timeout=10)
        self.assertEqual(kind, "name")
        # 崩溃现场：孤儿块 + 死守护文件残留，索引无引用。
        self.assertTrue(os.path.exists(
            os.path.join(self.cfg.blocks_dir, orphan_name)))
        guards = [n for n in os.listdir(self.cfg.locks_dir)
                  if n.startswith("upload-")]
        self.assertEqual(len(guards), 1)

        # 新进程启动：锁已随崩溃释放，孤儿块与死守护产物应被回收。
        svc = self._new_service()
        report = svc.initialize()
        self.assertIn(orphan_name, report["orphan"])
        self.assertFalse(os.path.exists(
            os.path.join(self.cfg.blocks_dir, orphan_name)))
        # 死守护文件被识别为无主并清理，locks 目录不残留。
        self.assertEqual(len(report["stale_guards"]), 1)
        leftover_guards = [n for n in os.listdir(self.cfg.locks_dir)
                           if n.startswith("upload-")]
        self.assertEqual(leftover_guards, [])
        # 索引可读、服务正常。
        self.assertEqual(svc.stats()["stored_blocks"], 0)

    # ---- 退出后回收 2：无守护的崩溃 tmp / 孤儿块照常回收 ----
    def test_plain_crash_leftovers_still_reaped(self):
        svc = self._new_service()
        svc.initialize()
        svc.import_grid([[1]], 0, 0)
        # 手工伪造崩溃遗留：一个 tmp、一个无引用孤儿块（无守护）。
        from elevation.blockstore import BlockStore
        orphan = BlockStore(self.cfg).write_block(3, 3, {(0, 0): 7})
        tmp_path = os.path.join(self.cfg.tmp_dir, "b0-0-deadbeef.tmp")
        with open(tmp_path, "wb") as fh:
            fh.write(b"partial")

        svc2 = self._new_service()
        report = svc2.initialize()
        self.assertIn(orphan, report["orphan"])
        self.assertIn("b0-0-deadbeef.tmp", report["tmp"])
        self.assertFalse(os.path.exists(
            os.path.join(self.cfg.blocks_dir, orphan)))
        self.assertFalse(os.path.exists(tmp_path))
        # 正常数据不受影响。
        self.assertEqual(svc2.read_window(0, 0, 1, 1).values, [[1]])

    # ---- 并发交错：多个在途上传与回收同时存在，互不破坏，最终一致 ----
    def test_concurrent_inflight_files_and_reapers_converge(self):
        seed = self._new_service()
        seed.initialize()

        procs = []
        queues = []
        # 多个进程各自持有守护 + 在途块（文件阶段、不占库写锁），可真正同时在途。
        targets = [(4, 4), (5, 5), (6, 6)]
        for br, bc in targets:
            q = self.ctx.Queue()
            queues.append(q)
            ready, allow_finish = self.ctx.Event(), self.ctx.Event()
            p = self.ctx.Process(
                target=child_hold_inflight_file,
                args=(self.data_dir, br, bc, ready, allow_finish, q))
            procs.append((p, ready, allow_finish))
            p.start()
        inflight_names = set()
        for q in queues:
            kind, name = q.get(timeout=15)
            self.assertEqual(kind, "name")
            inflight_names.add(name)
        for _p, ready, _af in procs:
            _wait(ready)
        self.assertEqual(len(inflight_names), 3)
        for name in inflight_names:
            self.assertTrue(os.path.exists(
                os.path.join(self.cfg.blocks_dir, name)))

        # 多个在途文件并存时，连续两个进程做启动回收：全部受保护，无一被删。
        for _ in range(2):
            rep = self._new_service().initialize()
            self.assertEqual(rep["orphan"], [])
            self.assertEqual(rep["tmp"], [])
            self.assertTrue(rep.get("deferred"))
        for name in inflight_names:
            self.assertTrue(os.path.exists(
                os.path.join(self.cfg.blocks_dir, name)))

        # 放行后进程正常退出（守护释放、文件自行处置）。
        for _p, _ready, af in procs:
            af.set()
        for p, _ready, _af in procs:
            p.join(timeout=20)
            self.assertEqual(p.exitcode, 0)
        for q in queues:
            kind, _name = q.get(timeout=10)
            self.assertEqual(kind, "done")

        # 这些在途块从未提交索引 -> 进程退出后成为无引用孤儿，
        # 下一次启动必须把它们回收（题面要求：退出后的遗留仍须回收）。
        svc = self._new_service()
        report = svc.initialize()
        self.assertTrue(inflight_names.issubset(set(report["orphan"])))
        for name in inflight_names:
            self.assertFalse(os.path.exists(
                os.path.join(self.cfg.blocks_dir, name)))
        self.assertEqual(svc.stats()["stored_blocks"], 0)


if __name__ == "__main__":
    unittest.main()
