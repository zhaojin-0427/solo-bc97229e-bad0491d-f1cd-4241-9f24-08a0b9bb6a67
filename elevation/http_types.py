"""框架无关的 HTTP 请求/响应抽象，供 Flask 与零依赖 WSGI 入口共用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Dict, List, Optional


@dataclass
class UploadedFile:
    filename: str
    stream: BytesIO
    size: int


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, str] = field(default_factory=dict)
    form: Dict[str, str] = field(default_factory=dict)
    files: Dict[str, UploadedFile] = field(default_factory=dict)
    content_length: Optional[int] = None


@dataclass
class Response:
    status: int = 200
    headers: List[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    def with_header(self, name: str, value: str) -> "Response":
        self.headers.append((name, value))
        return self


def json_response(payload: dict, status: int = 200) -> Response:
    import json
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(status, [("Content-Type", "application/json; charset=utf-8")], body)


def text_response(text: str, status: int = 200,
                  content_type: str = "text/plain; charset=utf-8") -> Response:
    return Response(status, [("Content-Type", content_type)], text.encode("utf-8"))


def html_response(html: str, status: int = 200) -> Response:
    return Response(status, [("Content-Type", "text/html; charset=utf-8")],
                    html.encode("utf-8"))


def csv_response(text: str, download_name: str) -> Response:
    resp = text_response(text, content_type="text/csv; charset=utf-8")
    resp.with_header("Content-Disposition",
                     f'attachment; filename="{download_name}"')
    return resp
