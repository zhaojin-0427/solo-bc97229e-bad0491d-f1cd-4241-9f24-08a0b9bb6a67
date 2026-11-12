"""Flask 生产入口工厂：``gunicorn --factory app:create_app``。

Flask 只负责协议适配，所有业务逻辑在 elevation 包内，
与零依赖入口 elevation/wsgi_stdlib.py 共用 Handlers。
模块导入不产生副作用（不访问数据目录），便于测试与多配置部署。
"""
from __future__ import annotations

from urllib.parse import quote

from flask import Flask, jsonify, redirect, request, send_file  # type: ignore

from elevation.config import CONFIG
from elevation.errors import ServiceError
from elevation.handlers import Handlers
from elevation.parsing import render_window_csv
from elevation.service import ElevationService
from elevation.templates import window_page


def create_app(config=CONFIG) -> Flask:
    service = ElevationService(config)
    report = service.initialize()
    if any(report.values()):
        import sys
        print("[startup] 崩溃恢复回收:", report, file=sys.stderr)

    handlers = Handlers(service)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_BODY_BYTES

    @app.errorhandler(ServiceError)
    def _service_error(exc: ServiceError):
        if request.path.startswith("/api/"):
            return jsonify({"error": str(exc)}), exc.status
        return (f"请求被拒绝（HTTP {exc.status}）：{exc}\n",
                exc.status, {"Content-Type": "text/plain; charset=utf-8"})

    @app.errorhandler(413)
    def _too_large(_exc):
        return (f"请求体超过上限 {config.MAX_BODY_BYTES} 字节\n",
                413, {"Content-Type": "text/plain; charset=utf-8"})

    @app.errorhandler(KeyError)
    def _missing_param(exc: KeyError):
        return (f"缺少必需参数/表单字段: {exc.args[0]}\n",
                400, {"Content-Type": "text/plain; charset=utf-8"})

    @app.errorhandler(ValueError)
    def _bad_value(exc: ValueError):
        return (f"参数格式错误: {exc}\n",
                400, {"Content-Type": "text/plain; charset=utf-8"})

    @app.get("/health")
    def health():
        return "ok\n"

    @app.get("/api/stats")
    def api_stats():
        return jsonify(service.stats())

    @app.get("/")
    def index():
        flash = request.args.get("flash")
        kind = request.args.get("flash_kind", "ok")
        resp = handlers._index(flash=flash, flash_kind=kind)  # noqa: SLF001
        return resp.body, resp.status, dict(resp.headers)

    @app.get("/window")
    def window():
        result = service.read_window(
            int(request.args["row"]), int(request.args["col"]),
            int(request.args["rows"]), int(request.args["cols"]))
        html = window_page(result)
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.get("/download")
    def download():
        result = service.read_window(
            int(request.args["row"]), int(request.args["col"]),
            int(request.args["rows"]), int(request.args["cols"]))
        import io
        csv_text = render_window_csv(result.values)
        name = (f"window_r{result.row_offset}_c{result.col_offset}_"
                f"{result.rows}x{result.cols}.csv")
        return send_file(
            io.BytesIO(csv_text.encode("utf-8")),
            mimetype="text/csv; charset=utf-8",
            as_attachment=True, download_name=name)

    @app.get("/sample.csv")
    def sample():
        resp = handlers._sample()  # noqa: SLF001
        return resp.body, resp.status, dict(resp.headers)

    @app.post("/upload")
    def upload():
        f = request.files.get("csv")
        if f is None:
            raise ServiceError("上传表单中缺少字段 csv 的文件", 400)
        row_offset = int(request.form["row_offset"])
        col_offset = int(request.form["col_offset"])
        result = service.import_csv(f.stream, row_offset, col_offset)
        msg = (f"提交成功 commit #{result.commit_id}：{result.rows}×{result.cols} "
               f"共 {result.cell_count} 格，新增已填 {result.new_cells} 格，"
               f"写出块 {result.blocks_written} 个，等值复用块 {result.blocks_unchanged} 个")
        return redirect("/?flash=" + quote(msg) + "&flash_kind=ok", code=303)

    return app
