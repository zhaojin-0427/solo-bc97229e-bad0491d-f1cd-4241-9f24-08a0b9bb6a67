"""多进程共用数据目录的跨进程回收协调（基于 ``flock(2)``）。

为什么需要它
============
多个服务进程可能共用同一个数据目录。启动期垃圾回收若只依据
“块文件不在 SQLite 索引里 / 在 obsolete_files 里”就删除，会撞上两类活体：

1. **进行中的上传**：块文件已 ``os.replace`` 落盘，但索引事务还没提交，
   此时它对回收者表现为“无引用孤儿”，删掉后上传仍会报成功，随后读取即缺文件。
2. **进行中的窗口读取**：读事务用延迟 ``BEGIN`` 固定了一次 WAL 快照，
   替换产生的旧块已登记为 obsolete，但若在该窗口结束前被删，固定快照就读不到旧块。

本模块用两类内核协调对象解决，且全部基于 ``flock`` —— 锁由内核持有，
进程异常退出（含 ``SIGKILL``、断电）时自动释放，不会留下永久锁：

* ``reaper.lock`` 上的 **读写锁**：窗口读取持共享锁（可多读并存），
  启动回收持排他锁（与任何在读窗口互斥）。回收因此只会在没有活体读快照时进行。
* 每次上传一个 ``upload-*.guard`` 守护文件，上传全程持排他锁，并在写任何
  块/临时文件**之前**把文件名登记进去。回收者识别出仍被持有的守护即跳过其文件。
  进程退出后锁释放、守护文件残留，下一次回收可安全判定其为“死上传”并清理产物。
"""
from __future__ import annotations

import fcntl
import os
import time
import uuid
from contextlib import contextmanager
from typing import Iterator, List, Optional, Set, Tuple

from .config import Config

# flock 常量（Linux 上 LOCK_SH/EX/NB 均可用）。
_LOCK_SH = fcntl.LOCK_SH
_LOCK_EX = fcntl.LOCK_EX
_LOCK_NB = fcntl.LOCK_NB
_UNLOCK = fcntl.LOCK_UN


class ReapLockBusy(Exception):
    """在限定等待时间内拿不到回收排他锁（仍有窗口在读取）。"""


def _try_lock(fd: int, flags: int) -> bool:
    try:
        fcntl.flock(fd, flags)
        return True
    except BlockingIOError:
        return False


# ---------------- 回收读写锁 ----------------
@contextmanager
def read_lock(config: Config) -> Iterator[None]:
    """窗口读取期间持有共享锁；与回收排他锁互斥，读之间共享。

    锁在整个读窗口（含固定快照与块文件读取）期间持有，保证快照需要的
    旧块不会被并发回收删除。
    """
    config.ensure_dirs()
    fd = os.open(config.reap_lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # 共享锁仅与排他回收互斥；正常情况下获取很快，用阻塞等待即可。
        fcntl.flock(fd, _LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(fd, _UNLOCK)
        finally:
            os.close(fd)


@contextmanager
def reap_lock(config: Config, wait_ms: Optional[int] = None) -> Iterator[None]:
    """启动回收期间持有排他锁，等待所有在读窗口退出。

    ``wait_ms`` 为最长等待（默认取配置）。超时抛 :class:`ReapLockBusy`，
    调用方据此跳过本次删除而不是强行回收（绝不误删活体快照）。
    """
    config.ensure_dirs()
    if wait_ms is None:
        wait_ms = config.REAP_LOCK_WAIT_MS
    deadline = time.monotonic() + max(wait_ms, 0) / 1000.0
    fd = os.open(config.reap_lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        while True:
            if _try_lock(fd, _LOCK_EX | _LOCK_NB):
                break
            if time.monotonic() >= deadline:
                raise ReapLockBusy(
                    f"等待回收安全时机超过 {wait_ms}ms，仍有窗口在读取，"
                    "本次启动跳过文件回收")
            time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, _UNLOCK)
        finally:
            os.close(fd)


# ---------------- 上传守护 ----------------
class UploadGuard:
    """一次上传的跨进程“在途”证明。

    用法::

        with UploadGuard(config) as guard:
            guard.claim(filename)          # 写块/tmp 之前先登记
            store.write_block(...)         # 随后落盘
            ... 索引事务提交 ...

    守护文件在 ``locks/`` 下，全程持排他锁；退出时删除。进程崩溃后文件残留，
    但锁已被内核释放，回收者据此区分“活上传”与“死上传遗留”。
    """

    def __init__(self, config: Config):
        self.config = config
        self.guard_id = uuid.uuid4().hex
        self.path = os.path.join(config.locks_dir, f"upload-{self.guard_id}.guard")
        self._fd: Optional[int] = None
        self._claimed: Set[str] = set()

    def __enter__(self) -> "UploadGuard":
        self.config.ensure_dirs()
        # O_EXCL 防止守护名碰撞（uuid 已唯一，这里再兜底）。
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
        # 排他锁：回收者非阻塞尝试失败即说明本上传仍活着。
        fcntl.flock(self._fd, _LOCK_EX)
        self._write_claims()
        return self

    def claim(self, name: str) -> None:
        """登记一个即将/已经写出的文件名（块名或 tmp 名），使其免于被回收。"""
        if name in self._claimed:
            return
        self._claimed.add(name)
        self._write_claims()

    def _write_claims(self) -> None:
        assert self._fd is not None
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        payload = ("\n".join(sorted(self._claimed)) + "\n").encode("utf-8")
        os.write(self._fd, payload)
        os.fsync(self._fd)

    @property
    def claimed_files(self) -> Set[str]:
        return set(self._claimed)

    def __exit__(self, exc_type, exc, tb) -> None:
        fd, self._fd = self._fd, None
        try:
            if fd is not None:
                try:
                    fcntl.flock(fd, _UNLOCK)
                finally:
                    os.close(fd)
        finally:
            # 守护文件删除本身不要求原子给回收者看：锁释放后回收者即便看到
            # 残留文件也会因拿得到锁而判定为死上传并按索引/obsolete 规则处理。
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def scan_live_claims(config: Config) -> Set[str]:
    """扫描所有**仍被活体进程持有**的上传守护，汇总其登记文件名。

    判断活性：对守护文件做非阻塞排他 ``flock``。拿得到锁 => 持锁进程已退出
    （死上传，其产物按孤儿/obsolete 规则回收）；拿不到 => 上传仍在进行，
    其登记的全部文件受保护。
    """
    live: Set[str] = set()
    for name, fd in _iter_guards(config):
        try:
            if not _try_lock(fd, _LOCK_EX | _LOCK_NB):
                # 锁被活体上传持有：读取其已 fsync 的登记清单。
                live.update(_read_claims(os.path.join(config.locks_dir, name)))
                continue
            # 拿到锁 => 没有活体持有者，属于死上传残留；立即释放，不保护其文件。
            fcntl.flock(fd, _UNLOCK)
        finally:
            os.close(fd)
    return live


def remove_stale_guards(config: Config) -> List[str]:
    """删除持锁进程已退出的死守护文件，返回被清理的守护文件名。

    须在回收排他锁保护下调用：能对守护文件加上排他锁即证明没有活体上传持有
    它（``flock`` 随进程退出由内核自动释放），删除安全。
    """
    removed: List[str] = []
    for name, fd in _iter_guards(config):
        path = os.path.join(config.locks_dir, name)
        locked = False
        try:
            if _try_lock(fd, _LOCK_EX | _LOCK_NB):
                locked = True
                fcntl.flock(fd, _UNLOCK)
        finally:
            os.close(fd)
        if locked:
            try:
                os.unlink(path)
                removed.append(name)
            except FileNotFoundError:
                pass
    return removed


def _iter_guards(config: Config) -> Iterator[Tuple[str, int]]:
    """遍历守护文件，逐个 yield (文件名, 已打开的 fd)；调用方负责关闭 fd。"""
    try:
        entries = os.listdir(config.locks_dir)
    except FileNotFoundError:
        return
    for name in entries:
        if not name.startswith("upload-") or not name.endswith(".guard"):
            continue
        try:
            fd = os.open(os.path.join(config.locks_dir, name), os.O_RDWR)
        except FileNotFoundError:
            continue
        yield name, fd


def _read_claims(path: str) -> List[str]:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    return [line.decode("utf-8") for line in data.splitlines() if line]
