"""框架无关的请求处理：Flask 入口与零依赖 WSGI 入口共用本模块。"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .errors import ServiceError
from .http_types import (Request, Response, csv_response, html_response,
                         json_response, text_response)
from .parsing import render_window_csv
from .service import ElevationService
from .templates import index_page, window_page


class Handlers:
    def __init__(self, service: ElevationService):
        self.service = service

    # ---------- 工具 ----------
    @staticmethod
    def _int_param(req: Request, name: str, default: Optional[int] = None) -> int:
        raw = req.query.get(name) if hasattr(req, "query") else None
        if raw is None:
            raw = req.form.get(name)
        if raw is None or raw == "":
            if default is not None:
                return default
            raise ServiceError(f"缺少参数 {name}", 400)
        try:
            return int(raw, 10)
        except ValueError:
            raise ServiceError(f"参数 {name}={raw!r} 不是整数", 400) from None

    def _dispatch(self, req: Request) -> Response:
        path, method = req.path, req.method
        if method == "GET" and path == "/health":
            return text_response("ok\n")
        if method == "GET" and path == "/api/stats":
            return json_response(self.service.stats())
        if method == "GET" and path == "/":
            return self._index(flash=req.query.get("flash"),
                               flash_kind=req.query.get("flash_kind", "ok"))
        if method == "GET" and path == "/window":
            return self._window_page(req)
        if method == "GET" and path == "/download":
            return self._download(req)
        if method == "GET" and path == "/sample.csv":
            return self._sample()
        if method == "POST" and path == "/upload":
            return self._upload(req)
        return text_response("Not Found\n", 404)

    def handle(self, req: Request) -> Response:
        try:
            return self._dispatch(req)
        except ServiceError as exc:
            accept = req.query.get("_accept", "")  # 适配器也可注入 Accept 处理
            if req.path.startswith("/api/"):
                return json_response({"error": str(exc)}, exc.status)
            return text_response(f"请求被拒绝（HTTP {exc.status}）：{exc}\n",
                                 exc.status)
        except sqlite3.OperationalError as exc:
            # 典型为写锁等待超时（并发上传压力下）。
            return text_response(f"数据库忙，稍后重试: {exc}\n", 503)

    # ---------- 页面 ----------
    def _index(self, flash: Optional[str] = None,
               flash_kind: str = "ok") -> Response:
        stats = self.service.stats()
        commits = [dict(r) for r in self.service.recent_commits(10)]
        flash_tuple = (flash_kind, flash) if flash else None
        return html_response(index_page(stats, commits, flash_tuple))

    def _window_page(self, req: Request) -> Response:
        result = self._read_window(req)
        return html_response(window_page(result))

    def _download(self, req: Request) -> Response:
        result = self._read_window(req)
        csv_text = render_window_csv(result.values)
        name = (f"window_r{result.row_offset}_c{result.col_offset}_"
                f"{result.rows}x{result.cols}.csv")
        return csv_response(csv_text, name)

    def _read_window(self, req: Request):
        row = self._int_param(req, "row")
        col = self._int_param(req, "col")
        rows = self._int_param(req, "rows")
        cols = self._int_param(req, "cols")
        return self.service.read_window(row, col, rows, cols)

    def _sample(self) -> Response:
        sample = (
            "100,101,102,103\n"
            "110,111,112,113\n"
            "120,121,122,123\n"
        )
        return csv_response(sample, "sample_elevation.csv")

    # ---------- 上传 ----------
    def _upload(self, req: Request) -> Response:
        uploaded = req.files.get("csv")
        if uploaded is None:
            raise ServiceError("上传表单中缺少字段 csv 的文件", 400)
        row_offset = self._int_param(req, "row_offset")
        col_offset = self._int_param(req, "col_offset")
        result = self.service.import_csv(uploaded.stream, row_offset, col_offset)
        msg = (f"提交成功 commit #{result.commit_id}：{result.rows}×{result.cols} "
               f"共 {result.cell_count} 格，新增已填 {result.new_cells} 格，"
               f"写出块 {result.blocks_written} 个，等值复用块 {result.blocks_unchanged} 个")
        # 浏览器表单提交后走 303 重定向回首页展示结果（PRG 模式）。
        from urllib.parse import quote
        location = "/?flash=" + quote(msg) + "&flash_kind=ok"
        return Response(303, [("Location", location)], b"")
