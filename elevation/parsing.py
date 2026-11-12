"""无表头整数 CSV 的流式解析。

约束
====
* 无表头：每个非空行必须是逗号分隔的整数，出现表头/空值/非整数即拒绝。
* 最多 256×256；行列必须等长，空文件、空行拒绝。
* 整个解析过程逐行进行，并且对原始字节数设硬上限，防止恶意上传耗尽内存。
* 高程单位为厘米，按 int32 存储，超出范围拒绝。
"""
from __future__ import annotations

import csv
import io
from typing import BinaryIO, List, Tuple

from .errors import PayloadTooLarge, ValidationError

INT32_MIN = -(2 ** 31)
INT32_MAX = 2 ** 31 - 1


def parse_int_grid(stream: BinaryIO,
                   max_rows: int,
                   max_cols: int,
                   max_bytes: int
                   ) -> Tuple[List[List[int]], int]:
    """解析 CSV 字节流，返回 (二维整数列表, 已读取字节数)。

    任何不满足约束的情况抛 ValidationError / PayloadTooLarge，
    调用方据此整次拒绝（不产生任何块文件与索引改动）。
    """
    text = _read_capped(stream, max_bytes)
    if not text.strip():
        raise ValidationError("上传内容为空：需要至少 1×1 的整数 CSV")

    reader = csv.reader(io.StringIO(text))
    grid: List[List[int]] = []
    width: int | None = None
    for line_no, raw_row in enumerate(reader, start=1):
        if line_no > max_rows:
            raise ValidationError(f"行数超过单次上传上限 {max_rows}")
        if len(raw_row) == 0 or (len(raw_row) == 1 and raw_row[0] == ""):
            raise ValidationError(f"第 {line_no} 行为空行，无表头 CSV 不允许空行")
        if width is None:
            width = len(raw_row)
            if width > max_cols:
                raise ValidationError(f"列数超过单次上传上限 {max_cols}")
        elif len(raw_row) != width:
            raise ValidationError(
                f"第 {line_no} 行有 {len(raw_row)} 列，与首行 {width} 列不一致")

        row: List[int] = []
        for col_no, token in enumerate(raw_row, start=1):
            value = _parse_int(token, line_no, col_no)
            row.append(value)
        grid.append(row)

    if not grid:
        raise ValidationError("CSV 中没有有效数据行")
    return grid, len(text.encode("utf-8"))


def _read_capped(stream: BinaryIO, max_bytes: int) -> str:
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLarge(f"上传体积超过上限 {max_bytes} 字节")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        return raw.decode("utf-8-sig")  # 兼容带 BOM 的导出文件
    except UnicodeDecodeError as exc:
        raise ValidationError(f"CSV 不是合法 UTF-8 文本: {exc}") from exc


def _parse_int(token: str, line_no: int, col_no: int) -> int:
    token = token.strip()
    if token == "":
        raise ValidationError(f"第 {line_no} 行第 {col_no} 列为空，无表头 CSV 只接受整数")
    # int() 接受 +3 / -3 / 007，不接受 3.0 / 1_000 / 空格内嵌。
    try:
        value = int(token, 10)
    except ValueError:
        raise ValidationError(
            f"第 {line_no} 行第 {col_no} 列 {token!r} 不是整数") from None
    if not (INT32_MIN <= value <= INT32_MAX):
        raise ValidationError(
            f"第 {line_no} 行第 {col_no} 列值 {value} 超出 int32 范围 "
            f"[{INT32_MIN}, {INT32_MAX}]")
    return value


def render_window_csv(rows: List[List[int | None]]) -> str:
    """窗口取数渲染：未填格输出空字段，行尾不加多余空白。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if v is None else str(v) for v in row])
    return buf.getvalue()
