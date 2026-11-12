"""最小 Flask 契约桩：仅用于离线环境验证 app.py 的接线，不参与交付镜像。

实现本项目用到的 Flask 子集：Flask 路由装饰器、request 对象、
jsonify/redirect/send_file、错误处理器注册。
"""
from __future__ import annotations

import io
import json
from urllib.parse import urlencode


class _File:
    def __init__(self, filename, stream, content_length):
        self.filename = filename
        self.stream = stream
        self.content_length = content_length


class RequestProxy:
    def __init__(self):
        self.method = "GET"
        self.path = "/"
        self.args = {}
        self.form = {}
        self.files = {}
        self.content_length = 0

    def reset(self, method, path, args=None, form=None, files=None, body_len=0):
        self.method = method
        self.path = path
        self.args = args or {}
        self.form = form or {}
        self.files = files or {}
        self.content_length = body_len


request = RequestProxy()


class _Rule:
    def __init__(self, rule, endpoint, methods):
        self.rule = rule
        self.endpoint = endpoint
        self.methods = methods


class Flask:
    def __init__(self, name):
        self.name = name
        self.config = {}
        self._routes = {}
        self._error_handlers = {}

    def _add(self, rule, methods):
        def deco(fn):
            self._routes[(rule, tuple(sorted(methods)))] = fn
            return fn
        return deco

    def get(self, rule):
        return self._add(rule, ["GET"])

    def post(self, rule):
        return self._add(rule, ["POST"])

    def errorhandler(self, exc_type):
        def deco(fn):
            self._error_handlers[exc_type] = fn
            return fn
        return deco

    def invoke(self, rule, method="GET", **kwargs):
        for (r, methods), fn in self._routes.items():
            if r == rule and method in methods:
                out = fn(**kwargs)
                # 模拟 Flask：裸字符串 -> (body, 200, {})；str 体编码为 bytes
                if isinstance(out, str):
                    out = (out.encode("utf-8"), 200, {})
                if isinstance(out, tuple) and isinstance(out[0], str):
                    out = (out[0].encode("utf-8"),) + out[1:]
                return out
        raise AssertionError(f"无路由 {rule} {method}")

    def invoke_error(self, exc):
        # 模拟 Flask：按类型继承链匹配错误处理器
        for exc_type in type(exc).__mro__:
            handler = self._error_handlers.get(exc_type)
            if handler is not None:
                out = handler(exc)
                if isinstance(out, tuple) and isinstance(out[0], str):
                    out = (out[0].encode("utf-8"),) + out[1:]
                return out
        raise AssertionError(f"无 {type(exc)} 的错误处理器")


def jsonify(payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return body, 200, {"Content-Type": "application/json"}


def redirect(location, code=302):
    return b"", code, {"Location": location}


def send_file(buf: io.BytesIO, mimetype=None, as_attachment=False,
              download_name=None):
    return (buf.getvalue(), 200,
            {"Content-Type": mimetype,
             "Content-Disposition": f'attachment; filename="{download_name}"'})
