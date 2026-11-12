"""64×64 块的本地文件存储。

设计要点
========
* 只保存“至少有一个已填格”的块，空块不落盘 —— 逻辑网格不预分配。
* 块文件不可变（immutable）：每次块内容变化都写到一个新的唯一名文件，
  再由 SQLite 事务原子地把 blocks 表指向新文件。旧文件进入 obsolete_files，
  进程启动时统一回收。进行中的旧快照仍可安全读取旧文件。
* 写入采用 tmp 目录 fsync + os.replace 原子改名 + 目录 fsync，
  进程崩溃只会留下垃圾临时文件（启动时清理），绝不会出现半截块文件。
* 文件格式（小端）：
    magic(4s = b'EBLK') | version(uint16=1) | br(uint32) | bc(uint32)
    | bitmap(512 字节，4096 位，bit=1 表示该格已填)
    | values(4096 × int32，未填位置为 0)
    | crc32(uint32，覆盖 magic..values 的全部字节)
"""
from __future__ import annotations

import os
import re
import struct
import uuid
import zlib
from typing import Dict, Iterator, List, Optional, Tuple

from .config import Config

MAGIC = b"EBLK"
VERSION = 1
_HEADER = struct.Struct("<4sHII")
_CRC = struct.Struct("<I")
_BITMAP_BYTES = 512  # 64 * 64 / 8
_CELLS = 64 * 64
_BLOCK_FILENAME = re.compile(r"b(\d+)-(\d+)-[0-9a-fA-F]{32}\.blk\Z")


class BlockFormatError(Exception):
    """块文件损坏或格式不符。"""


def _bitmap_index(local_r: int, local_c: int) -> Tuple[int, int]:
    pos = local_r * 64 + local_c
    return pos >> 3, 1 << (pos & 7)


def block_filename(br: int, bc: int) -> str:
    # UUID4 的 hex 为 32 个十六进制字符，无横线，保证多进程并发唯一。
    return f"b{br}-{bc}-{uuid.uuid4().hex}.blk"


def is_block_filename(name: str) -> bool:
    return _BLOCK_FILENAME.match(name) is not None


def encode_block(br: int, bc: int,
                 cells: Dict[Tuple[int, int], int]) -> bytes:
    """把块内 {局部行列: 值} 编码为块文件字节。cells 非空。"""
    bitmap = bytearray(_BITMAP_BYTES)
    values = [0] * _CELLS
    for (lr, lc), value in cells.items():
        if not (0 <= lr < 64 and 0 <= lc < 64):
            raise ValueError("块内局部坐标越界")
        if not (-(2 ** 31) <= value <= 2 ** 31 - 1):
            raise ValueError("高程值超出 int32 范围")
        byte_idx, bit = _bitmap_index(lr, lc)
        bitmap[byte_idx] |= bit
        values[lr * 64 + lc] = value

    body = _HEADER.pack(MAGIC, VERSION, br, bc) + bytes(bitmap)
    body += struct.pack("<%di" % _CELLS, *values)
    return body + _CRC.pack(zlib.crc32(body) & 0xFFFFFFFF)


def decode_block(data: bytes,
                 expect_br: Optional[int] = None,
                 expect_bc: Optional[int] = None
                 ) -> Dict[Tuple[int, int], int]:
    """校验并解码块文件，返回 {局部行列: int32 值}（仅含已填格）。"""
    need = _HEADER.size + _BITMAP_BYTES + _CELLS * 4 + _CRC.size
    if len(data) != need:
        raise BlockFormatError(f"块文件长度异常: {len(data)} != {need}")
    body, crc_bytes = data[:-_CRC.size], data[-_CRC.size:]
    (stored_crc,) = _CRC.unpack(crc_bytes)
    if (zlib.crc32(body) & 0xFFFFFFFF) != stored_crc:
        raise BlockFormatError("块文件 CRC32 校验失败")
    magic, version, br, bc = _HEADER.unpack(body[:_HEADER.size])
    if magic != MAGIC or version != VERSION:
        raise BlockFormatError("块文件 magic/version 不符")
    if expect_br is not None and (br, bc) != (expect_br, expect_bc):
        raise BlockFormatError(
            f"块坐标 ({br},{bc}) 与索引 ({expect_br},{expect_bc}) 不符")
    offset = _HEADER.size
    bitmap = body[offset:offset + _BITMAP_BYTES]
    offset += _BITMAP_BYTES
    raw_values = body[offset:offset + _CELLS * 4]
    values = struct.unpack("<%di" % _CELLS, raw_values)

    cells: Dict[Tuple[int, int], int] = {}
    for byte_idx, byte in enumerate(bitmap):
        if byte == 0:
            continue
        for bit_no in range(8):
            if byte & (1 << bit_no):
                pos = byte_idx * 8 + bit_no
                cells[(pos // 64, pos % 64)] = values[pos]
    return cells


class BlockStore:
    """块文件目录上的原子写 / 不可变读。"""

    def __init__(self, config: Config):
        self.config = config
        self.blocks_dir = config.blocks_dir
        self.tmp_dir = config.tmp_dir

    # ---------- 写 ----------
    def write_block(self, br: int, bc: int,
                    cells: Dict[Tuple[int, int], int]) -> str:
        """原子写入一个新版本块，返回最终文件名（不含目录）。"""
        payload = encode_block(br, bc, cells)
        name = block_filename(br, bc)
        tmp_path = os.path.join(self.tmp_dir, name + ".tmp")
        final_path = os.path.join(self.blocks_dir, name)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, final_path)
            self._fsync_dir(self.blocks_dir)
        except BaseException:
            # 崩溃/失败时清理临时文件；final_path 若已 replace 成功则保持不动。
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
        return name

    # ---------- 读 ----------
    def read_block(self, br: int, bc: int, filename: str
                   ) -> Dict[Tuple[int, int], int]:
        path = os.path.join(self.blocks_dir, filename)
        with open(path, "rb") as fh:
            data = fh.read()
        return decode_block(data, expect_br=br, expect_bc=bc)

    def block_path(self, filename: str) -> str:
        return os.path.join(self.blocks_dir, filename)

    # ---------- 启动期垃圾回收 ----------
    def iter_disk_files(self) -> Iterator[str]:
        with os.scandir(self.blocks_dir) as it:
            for entry in it:
                if entry.is_file():
                    yield entry.name

    def remove_file(self, filename: str) -> None:
        try:
            os.unlink(os.path.join(self.blocks_dir, filename))
        except FileNotFoundError:
            pass

    def cleanup_tmp(self) -> List[str]:
        """清理 tmp 目录中崩溃遗留的临时文件，返回被清理的文件名列表。"""
        removed: List[str] = []
        with os.scandir(self.tmp_dir) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(".tmp"):
                    try:
                        os.unlink(entry.path)
                        removed.append(entry.name)
                    except FileNotFoundError:
                        pass
        if removed:
            self._fsync_dir(self.tmp_dir)
        return removed

    @staticmethod
    def _fsync_dir(path: str) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # 某些文件系统不支持目录 fsync，rename 的原子性仍由内核保证。
            pass
