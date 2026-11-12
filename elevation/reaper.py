"""启动期垃圾回收：在跨进程锁保护下安全回收，兼容多进程共用数据目录。

回收时机的三条安全不变量
========================
1. **进行中上传的文件不删**：每次上传持一个加锁的 ``upload-*.guard`` 并预先
   登记其 tmp/块文件名。回收者只把“拿得到锁的死守护”遗留产物当垃圾；仍被
   活体持有的守护，其登记文件全部跳过。
2. **在读窗口快照需要的旧块不删**：窗口读取持 ``reaper.lock`` 共享锁，回收持
   排他锁。排他锁获取成功意味着已无活体读窗口，此时删除 obsolete 旧块才安全。
3. **进程退出后的无引用遗留仍须回收**：``flock`` 由内核在进程退出时释放，
   崩溃残留的守护文件不再挡路，其孤儿块 / tmp / obsolete 文件在后续启动照常回收。

回收可以**等待**安全时机（``ELEV_REAP_LOCK_WAIT_MS`` 有界等待），但既不会
停用回收，也不会拒绝共用目录；等待超时只是跳过本轮删除，服务照常启动，
下一次启动再尝试。
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Set

from .blockstore import BlockStore
from .config import Config
from .database import Database
from .locking import (ReapLockBusy, reap_lock, remove_stale_guards,
                      scan_live_claims)


def startup_reap(config: Config, db: Database, store: BlockStore
                 ) -> Dict[str, List[str]]:
    """进程启动时调用的安全回收，返回回收报告（含未回收原因）。

    返回键：``tmp`` / ``obsolete`` / ``orphan`` 为实际删除的文件名；
    ``deferred`` 为因安全时机未到而跳过本轮回收的说明（非文件清单）。

    两阶段判定，确保绝不与活体上传/读窗口冲突：

    * 先在回收排他锁下扫描活体上传守护。**只要存在任何活体上传**，本轮就不触碰
      SQLite（其写事务正持有库锁），也不删除任何块/tmp —— 在途文件全部由守护
      保护，等上传结束后的下一次启动再回收。
    * 没有活体上传时，才在单个 ``BEGIN IMMEDIATE`` 事务内回收孤儿/obsolete 块。
    """
    report: Dict[str, List[str]] = {
        "tmp": [], "obsolete": [], "orphan": [], "stale_guards": []}
    config.ensure_dirs()
    try:
        with reap_lock(config):
            # 排他回收锁在手：此刻没有活体窗口读取（共享锁全部退出）。
            protected = scan_live_claims(config)

            if protected:
                # 仍有进程在上传：其写事务可能正持有 SQLite 锁，且所有在途
                # 块/tmp 都在 protected 中。本轮不做任何删除，避免锁竞争与误删。
                msg = (f"检测到 {len(protected)} 个进行中上传的在途文件，"
                       "本轮启动跳过回收，待上传结束后的下次启动处理")
                print(f"[startup] {msg}", file=sys.stderr)
                report["deferred"] = [msg]
                return report

            # 无活体上传：崩溃遗留 tmp 可安全清理。
            report["tmp"] = _reap_tmp(store, protected)
            # blocks 目录：obsolete 旧版本 + 无引用孤儿（单个写事务内判定删除）。
            block_report = db.reap_once(protected)
            report["obsolete"] = block_report["obsolete"]
            report["orphan"] = block_report["orphan"]
            # 死守护（持锁进程已退出）一并清掉，避免 locks 目录无限堆积。
            report["stale_guards"] = remove_stale_guards(config)
    except ReapLockBusy as exc:
        # 仍有窗口在读取：本轮不删除任何文件，等下次启动再回收。
        # 服务不因此启动失败，回收能力也未被停用。
        print(f"[startup] {exc}", file=sys.stderr)
        report["deferred"] = [str(exc)]
    return report


def _reap_tmp(store: BlockStore, protected: Set[str]) -> List[str]:
    """删除 tmp 目录中的崩溃遗留，跳过活体上传登记的临时文件。"""
    removed: List[str] = []
    with os.scandir(store.tmp_dir) as entries:
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".tmp"):
                continue
            if entry.name in protected:
                continue
            try:
                os.unlink(entry.path)
                removed.append(entry.name)
            except FileNotFoundError:
                pass
    if removed:
        store._fsync_dir(store.tmp_dir)  # noqa: SLF001
    return removed
