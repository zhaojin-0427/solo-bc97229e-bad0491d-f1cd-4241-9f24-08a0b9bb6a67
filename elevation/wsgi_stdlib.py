"""零依赖 WSGI 入口：没有 Flask/gunicorn 的环境（或离线演示）也能直接运行。

与 Flask 入口共享 handlers.Handlers 中的全部业务逻辑，本文件只负责
WSGI 协议、URL 编码解析与 multipart/form-data 解析。
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Dict, Tuple
from urllib.parse import parse_qs, unquote
from wsgiref.simple_server import WSGIRequestHandler, make_server

from .config import CONFIG, Config
from .handlers import Handlers
from .http_types import Request, Response, UploadedFile
from .service import ElevationService


def parse_query(query_string: str) -> Dict[str, str]:
    parsed = parse_qs(query_string, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def parse_multipart(body: bytes, content_type: str,
                    max_body: int) -> Tuple[Dict[str, str], Dict[str, UploadedFile]]:
    """最小可用的 multipart/form-data 解析器（RFC 2046 结构，无嵌套）。"""
    marker = "boundary="
    idx = content_type.find(marker)
    if idx < 0:
        raise ValueError("multipart 请求缺少 boundary")
    boundary = content_type[idx + len(marker):].strip().strip('"').encode("utf-8")
    delimiter = b"--" + boundary
    form: Dict[str, str] = {}
    files: Dict[str, UploadedFile] = {}

    # 按 boundary 切分；首尾分别是前导空段与 -- 结束段。
    parts = body.split(delimiter)
    for part in parts[1:-1]:
        if part[:2] == b"--":
            break  # 结束标记
        if part[:2] == b"\r\n":
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_blob, _, content = part.partition(b"\r\n\r\n")
        if not _:
            continue
        headers = header_blob.decode("latin-1", errors="replace")
        disp = next((line for line in headers.split("\r\n")
                     if line.lower().startswith("content-disposition:")), "")
        match = re.search(r'name="([^"]*)"(?:;\s*filename="([^"]*)")?', disp)
        if not match:
            continue
        name = unquote(match.group(1))
        filename = match.group(2)
        if len(content) > max_body:
            raise ValueError("part 超过请求体上限")
        if filename is not None:
            files[name] = UploadedFile(
                filename=filename, stream=BytesIO(content), size=len(content))
        else:
            form[name] = content.decode("utf-8", errors="replace")
    return form, files


def build_request(env: dict, max_body: int) -> Request:
    method = env.get("REQUEST_METHOD", "GET").upper()
    path = env.get("PATH_INFO", "/")
    query = parse_query(env.get("QUERY_STRING", ""))
    form: Dict[str, str] = {}
    files: Dict[str, UploadedFile] = {}
    length: int | None = None

    if method in ("POST", "PUT", "PATCH"):
        raw_length = env.get("CONTENT_LENGTH")
        if raw_length:
            length = int(raw_length)
            if length > max_body:
                raise _BodyTooLarge()
        body = env["wsgi.input"].read(length if length is not None else max_body)
        if length is None and body:
            # 无 Content-Length 的流式上传不支持，避免无界读取。
            raise ValueError("POST 请求必须带 Content-Length")
        ctype = env.get("CONTENT_TYPE", "")
        if ctype.startswith("multipart/form-data"):
            form, files = parse_multipart(body, ctype, max_body)
        elif ctype.startswith("application/x-www-form-urlencoded"):
            form = parse_query(body.decode("utf-8", errors="replace"))
        else:
            raise ValueError(f"不支持的请求 Content-Type: {ctype or '(空)'}")

    return Request(method=method, path=path, query=query, form=form,
                   files=files, content_length=length)


class _BodyTooLarge(Exception):
    pass


class StdlibApp:
    def __init__(self, handlers: Handlers, config: Config):
        self.handlers = handlers
        self.config = config

    def __call__(self, environ: dict, start_response):
        try:
            req = build_request(environ, self.config.MAX_BODY_BYTES)
        except _BodyTooLarge:
            resp = Response(413, [("Content-Type", "text/plain; charset=utf-8")],
                            "请求体超过上限\n".encode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            resp = Response(400, [("Content-Type", "text/plain; charset=utf-8")],
                            f"请求解析失败: {exc}\n".encode("utf-8"))
        else:
            resp = self.handlers.handle(req)

        default_headers = [("Content-Length", str(len(resp.body)))]
        start_response(_status_line(resp.status),
                       default_headers + resp.headers)
        return [resp.body]


_STATUS_TEXT = {
    200: "200 OK", 303: "303 See Other", 400: "400 Bad Request",
    404: "404 Not Found", 409: "409 Conflict", 413: "413 Payload Too Large",
    422: "422 Unprocessable Entity", 500: "500 Internal Server Error",
    503: "503 Service Unavailable",
}


def _status_line(status: int) -> str:
    return _STATUS_TEXT.get(status, f"{status} Status")


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):  # 访问日志走 stderr 但精简格式
        import sys
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def create_app(config: Config | None = None) -> StdlibApp:
    cfg = config or CONFIG
    service = ElevationService(cfg)
    report = service.initialize()
    if any(report.values()):
        import sys
        print("[startup] 崩溃恢复回收:", report, file=sys.stderr)
    return StdlibApp(Handlers(service), cfg)


def main() -> None:
    import os
    host = os.environ.get("ELEV_HOST", "0.0.0.0")
    port = int(os.environ.get("ELEV_PORT", "8000"))
    app = create_app()
    httpd = make_server(host, port, app, handler_class=_QuietHandler)
    print(f"零依赖演示服务已启动: http://{host}:{port}  数据目录={CONFIG.DATA_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
