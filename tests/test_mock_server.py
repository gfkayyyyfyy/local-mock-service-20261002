"""mock_server 回归测试：规则文件 -> 命令行入口 -> 本地 HTTP 响应。

仅依赖 Python 3 标准库，可重复执行：

    python -m unittest discover -s tests
    python tests/test_mock_server.py

测试自行准备 UTF-8 规则文件与可用端口，通过 `python -m mock_server`
子进程启动服务，只连接 127.0.0.1；结束后释放进程、连接与临时文件，
不修改项目自带的 rules.json。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

try:
    from selectors import DefaultSelector, EVENT_READ
except ImportError:  # pragma: no cover - 标准库必有，仅为静态检查占位
    DefaultSelector = None
    EVENT_READ = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    # 允许直接以 `python tests/test_mock_server.py` 运行
    sys.path.insert(0, str(PROJECT_ROOT))

CONTENT_TYPE = "application/json; charset=utf-8"
STARTUP_MARKER = "mock_server listening"
STARTUP_TIMEOUT = 10.0
REQUEST_TIMEOUT = 10.0


def free_port():
    """向系统申请一个当前可用的 127.0.0.1 端口（不长期占用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_rules(directory, name, routes):
    """在临时目录写入一份独立的 UTF-8 规则文件，返回其路径。"""
    path = Path(directory) / name
    payload = json.dumps(
        {"routes": routes}, ensure_ascii=False, indent=2
    ).encode("utf-8")
    path.write_bytes(payload)
    return path


class ServerProcess:
    """以现有命令行入口启动的 mock_server 子进程。"""

    def __init__(self, rules_path, port):
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mock_server",
                "--rules",
                str(rules_path),
                "--port",
                str(port),
            ],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.port = port
        self._wait_until_listening()

    def _wait_until_listening(self):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        selector = DefaultSelector()
        selector.register(self.proc.stdout, EVENT_READ)
        try:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    out, err = self.proc.communicate(timeout=5)
                    raise AssertionError(
                        f"mock_server 提前退出（退出码 {self.proc.returncode}）；"
                        f"stdout={out!r} stderr={err!r}"
                    )
                for key, _ in selector.select(timeout=0.2):
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    if STARTUP_MARKER in line:
                        return
            self.stop()
            raise AssertionError(
                f"mock_server 在 {STARTUP_TIMEOUT}s 内未输出监听提示"
            )
        finally:
            selector.close()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        # 关闭管道并取走残余输出，避免资源泄漏
        try:
            self.proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError):
            self.proc.kill()
            self.proc.communicate(timeout=5)


def request(port, method, target, body=None):
    """发起一次本地 HTTP 请求，返回 (状态码, 响应头, 原始响应体字节)。"""
    conn = HTTPConnection("127.0.0.1", port, timeout=REQUEST_TIMEOUT)
    try:
        conn.request(method, target, body=body)
        resp = conn.getresponse()
        raw = resp.read()
        headers = {k: v for k, v in resp.getheaders()}
        return resp.status, headers, raw
    finally:
        conn.close()


class MockServerRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        # 独立的 UTF-8 规则文件，与项目自带 rules.json 完全隔离
        cls.rules_path = write_rules(
            cls._tmp.name,
            "rules_main.json",
            [
                {"method": "GET", "path": "/hello",
                 "body": {"message": "你好"}},
                {"method": "POST", "path": "/fail", "status": 503,
                 "body": {"error": "demo_failure"}},
                {"method": "GET", "path": "/custom404", "status": 404,
                 "body": {"error": "configured_missing"}},
            ],
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def _check_response(self, label, method, target, expected_status,
                        expected_body, request_body=None):
        status, headers, raw = request(
            self.port, method, target, body=request_body
        )
        self.assertEqual(
            status, expected_status,
            f"样例 {label}: 状态码应为 {expected_status}，实际 {status}",
        )
        content_type = headers.get("Content-Type")
        self.assertEqual(
            content_type, CONTENT_TYPE,
            f"样例 {label}: Content-Type 应为 {CONTENT_TYPE!r}，"
            f"实际 {content_type!r}",
        )
        content_length = headers.get("Content-Length")
        self.assertIsNotNone(content_length, f"样例 {label}: 缺少 Content-Length")
        self.assertEqual(
            int(content_length), len(raw),
            f"样例 {label}: Content-Length={content_length} "
            f"与实际响应体字节数 {len(raw)} 不符",
        )
        # 中文内容必须能按 UTF-8 正确解析
        decoded = raw.decode("utf-8")
        actual_body = json.loads(decoded)
        self.assertEqual(
            actual_body, expected_body,
            f"样例 {label}: 响应 JSON 应为 {expected_body}，实际 {actual_body}",
        )
        return actual_body

    def test_configured_routes(self):
        # status 缺省 -> 200；带查询串仍应命中（匹配忽略查询字符串）
        self._check_response(
            "GET /hello（未填写 status）", "GET", "/hello",
            200, {"message": "你好"},
        )
        self._check_response(
            "GET /hello?z=1（查询串不影响匹配）", "GET", "/hello?z=1",
            200, {"message": "你好"},
        )
        # POST 携带请求体，验证服务读掉请求体后仍正确响应
        self._check_response(
            "POST /fail（status=503）", "POST", "/fail",
            503, {"error": "demo_failure"},
            request_body=b'{"ignored": true}',
        )

    def test_configured_404_distinct_from_route_not_found(self):
        # 配置的 404：命中规则，返回配置的 body
        self._check_response(
            "GET /custom404（配置的 404）", "GET", "/custom404",
            404, {"error": "configured_missing"},
        )
        # 未命中的 404：返回统一的 route_not_found
        self._check_response(
            "GET /missing（未命中的 404）", "GET", "/missing",
            404, {"error": "route_not_found"},
        )

    def test_utf8_chinese_body_bytes(self):
        # 直接核对中文的 UTF-8 字节与紧凑 JSON 序列化一致
        status, headers, raw = request(self.port, "GET", "/hello")
        self.assertEqual(status, 200)
        self.assertEqual(raw, '{"message":"你好"}'.encode("utf-8"))
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_keep_alive_connection_reuse(self):
        # HTTP/1.1 下同一连接连续发起多个请求（含带体 POST），均应成功
        conn = HTTPConnection("127.0.0.1", self.port, timeout=REQUEST_TIMEOUT)
        try:
            sequence = [
                ("GET", "/hello", None, 200),
                ("POST", "/fail", b"payload", 503),
                ("GET", "/custom404", None, 404),
                ("GET", "/missing", None, 404),
            ]
            for method, target, body, expected in sequence:
                with self.subTest(request=f"{method} {target}"):
                    conn.request(method, target, body=body)
                    resp = conn.getresponse()
                    raw = resp.read()
                    self.assertEqual(resp.status, expected)
                    self.assertEqual(
                        resp.getheader("Content-Type"), CONTENT_TYPE
                    )
                    self.assertEqual(
                        int(resp.getheader("Content-Length")), len(raw)
                    )
                    json.loads(raw.decode("utf-8"))
        finally:
            conn.close()


class StatusBoundaryTests(unittest.TestCase):
    """合法 status 边界：200、400、599 均可加载并原样返回。"""

    def test_boundary_statuses_load_and_return(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            routes = [
                {"method": "GET", "path": f"/s{status}",
                 "status": status, "body": {"status": status}}
                for status in (200, 400, 599)
            ]
            rules_path = write_rules(tmp, "rules_boundary.json", routes)
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                for status in (200, 400, 599):
                    with self.subTest(status=status):
                        code, headers, raw = request(
                            port, "GET", f"/s{status}"
                        )
                        self.assertEqual(code, status)
                        self.assertEqual(
                            headers.get("Content-Type"), CONTENT_TYPE
                        )
                        self.assertEqual(
                            int(headers["Content-Length"]), len(raw)
                        )
                        self.assertEqual(
                            json.loads(raw.decode("utf-8")),
                            {"status": status},
                        )
            finally:
                server.stop()


INVALID_STATUSES = [
    ("整数 199（低于下限）", 199),
    ("整数 201（2xx 仅接受 200）", 201),
    ("整数 399（3xx 不接受）", 399),
    ("整数 600（高于上限）", 600),
    ("布尔 true", True),
    ("布尔 false", False),
    ("null", None),
    ('字符串 "503"', "503"),
    ("浮点数 503.0", 503.0),
    ("空数组 []", []),
    ("空对象 {}", {}),
]


class InvalidStatusTests(unittest.TestCase):
    """每份非法 status 配置经命令行入口启动，都应以退出码 2 失败。"""

    def test_invalid_statuses_rejected(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, bad_status) in enumerate(INVALID_STATUSES):
                with self.subTest(illegal_status=label):
                    routes = [{
                        "method": "GET",
                        "path": "/x",
                        "status": bad_status,
                        "body": {},
                    }]
                    rules_path = write_rules(
                        tmp, f"rules_invalid_{index}.json", routes
                    )
                    proc = subprocess.run(
                        [
                            sys.executable, "-m", "mock_server",
                            "--rules", str(rules_path),
                            "--port", str(free_port()),
                        ],
                        cwd=str(PROJECT_ROOT),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=STARTUP_TIMEOUT,
                    )
                    self.assertEqual(
                        proc.returncode, 2,
                        f"样例 {label}: 期望退出码 2，实际 "
                        f"{proc.returncode}；stdout={proc.stdout!r} "
                        f"stderr={proc.stderr!r}",
                    )
                    self.assertIn(
                        "status",
                        proc.stderr.lower(),
                        f"样例 {label}: 标准错误应包含 status 配置错误原因，"
                        f"实际 stderr={proc.stderr!r}",
                    )
                    self.assertNotIn(
                        STARTUP_MARKER, proc.stdout,
                        f"样例 {label}: 校验失败时标准输出不应出现监听提示，"
                        f"实际 stdout={proc.stdout!r}",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
