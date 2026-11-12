"""用最小 Flask 桩验证 app.py（生产 Flask 入口）的路由与错误处理接线。"""
import io
import sys
import unittest
from pathlib import Path

# 让 `import flask` 命中测试桩（不影响 elevation 包本身）
STUB = str(Path(__file__).parent / "flask_stub")
if STUB not in sys.path:
    sys.path.insert(0, STUB)

import flask as flask_stub  # noqa: E402  来自 tests/flask_stub 的契约桩
from tests.helpers import grid_csv, make_config  # noqa: E402


class FlaskEntryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = make_config(grid_rows=200, grid_cols=200)
        import app as flask_app_module
        cls.module = flask_app_module

    @classmethod
    def tearDownClass(cls):
        from tests.helpers import cleanup_config
        cleanup_config(cls.cfg)

    def setUp(self):
        # 用桩 app 替换模块级 app（共享同一数据目录的新服务）
        self.flask = self.module.create_app(self.cfg)
        from flask import request  # 桩模块（sys.path 中已注入 tests/flask_stub）
        self.request = request

    def _call_route(self, rule, method="GET", **kwargs):
        return self.flask.invoke(rule, method, **kwargs)

    def test_health_and_stats_routes(self):
        body, status, headers = self._call_route("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok\n")
        body, status, _ = self._call_route("/api/stats")
        self.assertEqual(status, 200)
        self.assertIn("filled_cells", body.decode())

    def test_upload_route_with_conflict(self):
        from flask import request  # 桩
        f1 = flask_stub._File("a.csv", io.BytesIO(b"1,2\n3,4\n"), 8)
        request.reset("POST", "/upload",
                      form={"row_offset": "0", "col_offset": "0"},
                      files={"csv": f1}, body_len=8)
        body, status, headers = self._call_route("/upload", "POST")
        self.assertEqual(status, 303)  # PRG 模式重定向
        self.assertIn("/?flash=", headers["Location"])

        f2 = flask_stub._File("b.csv", io.BytesIO(b"1,99\n"), 6)
        request.reset("POST", "/upload",
                      form={"row_offset": "0", "col_offset": "0"},
                      files={"csv": f2}, body_len=6)
        from elevation.errors import ConflictError
        with self.assertRaises(ConflictError):
            self._call_route("/upload", "POST")

    def test_window_and_download_routes(self):
        from flask import request
        f = flask_stub._File("a.csv", io.BytesIO(grid_csv(3, 3)), 20)
        request.reset("POST", "/upload",
                      form={"row_offset": "10", "col_offset": "10"},
                      files={"csv": f}, body_len=20)
        self._call_route("/upload", "POST")

        request.reset("GET", "/window",
                      args={"row": "10", "col": "10", "rows": "4", "cols": "4"})
        html, status, headers = self._call_route("/window")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("<table".encode(), html)

        request.reset("GET", "/download",
                      args={"row": "10", "col": "10", "rows": "4", "cols": "4"})
        body, status, headers = self._call_route("/download")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        # 4 列窗口最后一行全未填：4 个空字段 = 3 个逗号
        self.assertEqual(body.split(b"\n")[-2], b",,,")

    def test_error_handlers_registered(self):
        from elevation.errors import ServiceError, ValidationError
        # 400 类错误处理器
        out = self.flask.invoke_error(ValidationError("bad"))
        self.assertEqual(out[1], 400)
        out = self.flask.invoke_error(ServiceError("x", 409))
        self.assertEqual(out[1], 409)
        out = self.flask.invoke_error(KeyError("csv"))
        self.assertEqual(out[1], 400)
        out = self.flask.invoke_error(ValueError("nope"))
        self.assertEqual(out[1], 400)
        # 注册了 ServiceError / 413 / KeyError / ValueError 四个处理器
        self.assertEqual(len(self.flask._error_handlers), 4)

    def test_sample_route(self):
        body, status, headers = self._call_route("/sample.csv")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
