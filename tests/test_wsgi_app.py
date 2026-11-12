"""零依赖 WSGI 入口的端到端 HTTP 测试（不启动真实 socket）。"""
import io
import json
import unittest
from urllib.parse import urlencode

from elevation.handlers import Handlers
from elevation.service import ElevationService
from elevation.wsgi_stdlib import StdlibApp
from tests.helpers import grid_csv, make_config


def multipart_body(fields, files):
    """构造 multipart/form-data 请求体。files: {name: (filename, bytes)}。"""
    boundary = "----elevtestboundary123"
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(str(value).encode())
        buf.write(b"\r\n")
    for name, (filename, content) in files.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'.encode())
        buf.write(b"Content-Type: text/csv\r\n\r\n")
        buf.write(content)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return boundary, buf.getvalue()


class WsgiAppTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(grid_rows=200, grid_cols=200)
        svc = ElevationService(self.cfg)
        svc.initialize()
        self.app = StdlibApp(Handlers(svc), self.cfg)

    def tearDown(self):
        from tests.helpers import cleanup_config
        cleanup_config(self.cfg)

    def request(self, method, path, body=b"", content_type=""):
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(headers)

        env = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path.split("?")[0],
            "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": content_type,
            "wsgi.input": io.BytesIO(body),
        }
        payload = b"".join(self.app(env, start_response))
        return captured["status"], captured["headers"], payload

    def upload(self, content, roff=0, coff=0):
        boundary, body = multipart_body(
            {"row_offset": roff, "col_offset": coff},
            {"csv": ("grid.csv", content)})
        return self.request(
            "POST", "/upload", body, f"multipart/form-data; boundary={boundary}")

    def test_health_and_stats(self):
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok\n")
        status, headers, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        stats = json.loads(body)
        self.assertEqual(stats["grid_rows"], 200)
        self.assertEqual(stats["filled_cells"], 0)

    def test_index_renders_html(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("高程网格".encode(), body)

    def test_upload_then_preview_and_download(self):
        status, headers, _ = self.upload(grid_csv(4, 5), roff=10, coff=20)
        self.assertEqual(status, 303)
        self.assertIn("/?flash=", headers["Location"])

        qs = urlencode({"row": 10, "col": 20, "rows": 5, "cols": 6})
        status, headers, body = self.request("GET", "/window?" + qs)
        self.assertEqual(status, 200)
        self.assertIn(b"<table", body)
        # 未填格渲染为 ·
        self.assertIn("·".encode(), body)

        status, headers, body = self.request("GET", "/download?" + qs)
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        lines = body.decode().strip().split("\n")
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0].split(","), ["0", "1", "2", "3", "4", ""])
        self.assertEqual(lines[4], ",,,,,")  # 最后一行全未填

    def test_conflict_returns_409_and_no_partial_write(self):
        self.upload(b"1,2\n3,4\n")
        status, _, body = self.upload(b"1,99\n")
        self.assertEqual(status, 409)
        self.assertIn("(0,1)", body.decode())
        status, _, body = self.request(
            "GET", "/download?" + urlencode({"row": 0, "col": 0, "rows": 1, "cols": 2}))
        self.assertEqual(body.decode().strip(), "1,2")  # 原值未被覆盖

    def test_out_of_bounds_returns_422(self):
        status, _, body = self.upload(b"1\n", roff=200, coff=0)
        self.assertEqual(status, 422)

    def test_missing_file_returns_400(self):
        boundary, body = multipart_body(
            {"row_offset": 0, "col_offset": 0}, {})
        status, _, _ = self.request(
            "POST", "/upload", body, f"multipart/form-data; boundary={boundary}")
        self.assertEqual(status, 400)

    def test_body_too_large_413(self):
        big = grid_csv(10, 10)
        self.cfg.MAX_BODY_BYTES = 32
        boundary, body = multipart_body(
            {"row_offset": 0, "col_offset": 0}, {"csv": ("g.csv", big)})
        status, _, _ = self.request(
            "POST", "/upload", body, f"multipart/form-data; boundary={boundary}")
        self.assertEqual(status, 413)

    def test_bad_query_params_400(self):
        status, _, _ = self.request(
            "GET", "/window?row=0&col=0&rows=abc&cols=2")
        self.assertEqual(status, 400)

    def test_unknown_route_404(self):
        status, _, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertIn(b"Not Found", body)

    def test_sample_csv(self):
        status, headers, body = self.request("GET", "/sample.csv")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(len(body.decode().strip().split("\n")), 3)


if __name__ == "__main__":
    unittest.main()
