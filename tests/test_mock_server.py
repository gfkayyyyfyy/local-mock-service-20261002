"""mock_server 回归测试：规则文件 -> 命令行入口 -> 本地 HTTP 响应。

仅依赖 Python 3 标准库，可重复执行：

    python -m unittest discover -s tests
    python tests/test_mock_server.py

测试自行准备 UTF-8 规则文件与可用端口，通过 `python -m mock_server`
子进程启动服务，只连接 127.0.0.1；结束后释放进程、连接与临时文件，
不修改项目自带的 rules.json。
"""

import json
import math
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


DUPLICATE_ROUTE_CASES = [
    # (说明, routes, 后出现规则的下标, 重复方法, 重复路径)
    (
        "相邻重复：body 与 status 均不同",
        [
            {"method": "GET", "path": "/same", "body": 1},
            {"method": "GET", "path": "/same", "status": 503, "body": 2},
        ],
        1, "GET", "/same",
    ),
    (
        "非相邻重复：中间夹一条合法且不同的路由",
        [
            {"method": "GET", "path": "/same", "body": 1},
            {"method": "POST", "path": "/other", "body": {"ok": True}},
            {"method": "GET", "path": "/same", "status": 503, "body": 2},
        ],
        2, "GET", "/same",
    ),
    (
        "相邻重复：body 与 status 完全相同",
        [
            {"method": "GET", "path": "/same", "body": 1},
            {"method": "GET", "path": "/same", "body": 1},
        ],
        1, "GET", "/same",
    ),
    (
        "相邻重复：仅 body 不同（status 同为缺省 200）",
        [
            {"method": "GET", "path": "/same", "body": 1},
            {"method": "GET", "path": "/same", "body": 2},
        ],
        1, "GET", "/same",
    ),
    (
        "相邻重复：仅 status 不同（body 相同）",
        [
            {"method": "GET", "path": "/same", "body": 1},
            {"method": "GET", "path": "/same", "status": 503, "body": 1},
        ],
        1, "GET", "/same",
    ),
    (
        "POST 相邻重复：body 与 status 均不同",
        [
            {"method": "POST", "path": "/same", "body": {"kind": "first"}},
            {"method": "POST", "path": "/same", "status": 500,
             "body": {"kind": "second"}},
        ],
        1, "POST", "/same",
    ),
    (
        "POST 非相邻重复：中间夹一条合法且不同的 GET 路由",
        [
            {"method": "POST", "path": "/same", "body": {"kind": "first"}},
            {"method": "GET", "path": "/other", "body": {"ok": True}},
            {"method": "POST", "path": "/same", "status": 500,
             "body": {"kind": "second"}},
        ],
        2, "POST", "/same",
    ),
]


def start_and_wait_exit(rules_path, port):
    """经公开启动入口启动子进程并等待其自行退出。

    返回 (returncode, stdout, stderr)。在 STARTUP_TIMEOUT 内未退出则杀掉
    进程并抛出 AssertionError（判失败，而非跳过）。无论正常返回还是超时，
    返回前都回收子进程与管道。
    """
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "mock_server",
            "--rules", str(rules_path),
            "--port", str(port),
        ],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            returncode = proc.poll()
            if returncode is not None:
                stdout, stderr = proc.communicate(timeout=5)
                return returncode, stdout, stderr
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"mock_server 在 {STARTUP_TIMEOUT}s 内未退出，"
                    f"疑似启动期唯一性检查未生效（pid={proc.pid}）"
                )
            time.sleep(0.05)
    finally:
        # 超时分支下进程可能仍在运行：确保杀掉并回收，避免残留进程
        if proc.poll() is None:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError):
            pass


class DuplicateRouteTests(unittest.TestCase):
    """启动期 method + path 唯一性检查：重复规则必须令启动失败。"""

    def test_duplicate_routes_rejected_at_startup(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, routes, later_index, method, path) in enumerate(
                DUPLICATE_ROUTE_CASES
            ):
                with self.subTest(case=label):
                    rules_path = write_rules(
                        tmp, f"rules_dup_{index}.json", routes
                    )
                    returncode, stdout, stderr = start_and_wait_exit(
                        rules_path, free_port()
                    )
                    self.assertEqual(
                        returncode, 2,
                        f"样例 {label!r}: 期望退出码 2，实际 "
                        f"{returncode}；stdout={stdout!r} stderr={stderr!r}",
                    )
                    # 不逐字比较整段消息，只定位关键片段
                    self.assertIn(
                        "duplicate route", stderr,
                        f"样例 {label!r}: 标准错误应包含 'duplicate route'，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertIn(
                        method, stderr,
                        f"样例 {label!r}: 标准错误应标明重复的方法 {method!r}，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertIn(
                        path, stderr,
                        f"样例 {label!r}: 标准错误应标明重复的路径 {path!r}，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertIn(
                        f"routes[{later_index}]", stderr,
                        f"样例 {label!r}: 标准错误应标明后出现规则的下标 "
                        f"routes[{later_index}]，实际 stderr={stderr!r}",
                    )
                    self.assertNotIn(
                        STARTUP_MARKER, stdout,
                        f"样例 {label!r}: 校验失败时标准输出不应出现监听提示，"
                        f"实际 stdout={stdout!r}",
                    )


class DistinctRouteTests(unittest.TestCase):
    """合法对照：唯一性检查不得误拒绝本不相同的路由。"""

    def test_same_path_different_methods_both_load(self):
        # GET 与 POST 共用同一路径是两条不同路由
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            routes = [
                {"method": "GET", "path": "/same",
                 "body": {"method": "GET"}},
                {"method": "POST", "path": "/same", "status": 503,
                 "body": {"method": "POST"}},
            ]
            rules_path = write_rules(tmp, "rules_methods.json", routes)
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/same")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(json.loads(raw.decode("utf-8")),
                                 {"method": "GET"})

                status, headers, raw = request(port, "POST", "/same")
                self.assertEqual(status, 503)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(json.loads(raw.decode("utf-8")),
                                 {"method": "POST"})
            finally:
                server.stop()

    def test_path_literal_variants_loaded_as_distinct(self):
        # 不做大小写折叠、尾斜杠合并或百分号解码：四条 GET 路径互不相同
        variants = [
            ("/same", 200, {"v": "plain"}),
            ("/Same", 400, {"v": "capitalized"}),
            ("/same/", 503, {"v": "trailing_slash"}),
            ("/s%61me", 500, {"v": "percent_encoded"}),
        ]
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            routes = [
                {"method": "GET", "path": path, "status": status, "body": body}
                for path, status, body in variants
            ]
            rules_path = write_rules(tmp, "_rules_literal_paths.json", routes)
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                for path, status, body in variants:
                    with self.subTest(path=path):
                        code, headers, raw = request(port, "GET", path)
                        self.assertEqual(
                            code, status,
                            f"GET {path}: 状态码应为 {status}，实际 {code}",
                        )
                        self.assertEqual(
                            headers.get("Content-Type"), CONTENT_TYPE
                        )
                        self.assertEqual(
                            int(headers["Content-Length"]), len(raw)
                        )
                        self.assertEqual(
                            json.loads(raw.decode("utf-8")), body,
                            f"GET {path}: 各路径必须独立返回各自配置的响应",
                        )
            finally:
                server.stop()


NON_STANDARD_LITERALS = ["NaN", "Infinity", "-Infinity"]


def write_rules_text(directory, name, text):
    """直接写入原始文本规则文件（用于构造 json.dumps 无法产出的非法文本）。"""
    path = Path(directory) / name
    path.write_bytes(text.encode("utf-8"))
    return path


def literal_rules_templates(literal):
    """给出同一非法字面量出现在不同位置的规则文本（均为非法 JSON）。"""
    return {
        "body 顶层值": (
            '{"routes":[{"method":"GET","path":"/value",'
            f'"body":{{"value":{literal}}}}}]'
        ),
        "body 嵌套数组与对象中": (
            '{"routes":[{"method":"GET","path":"/value",'
            f'"body":{{"list":[1,[2,{{"v":{literal}}}]]}}}}]'
        ),
        "被忽略的额外字段中": (
            '{"routes":[{"method":"GET","path":"/ok","body":{}}],'
            f'"extra":{{"note":{literal}}}}}'
        ),
    }


class NonStandardNumberTests(unittest.TestCase):
    """NaN / Infinity / -Infinity 未加引号出现时，整份规则视为非法 JSON。"""

    def test_load_rules_raises_rules_error(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for literal in NON_STANDARD_LITERALS:
                for label, text in literal_rules_templates(literal).items():
                    with self.subTest(literal=literal, 位置=label):
                        rules_path = write_rules_text(
                            tmp, f"rules_lit_{label}.json", text
                        )
                        with self.assertRaises(
                            RulesError,
                            msg=f"{label} 中的 {literal} 应使 load_rules "
                                f"抛出 RulesError",
                        ) as ctx:
                            load_rules(rules_path)
                        self.assertIn(
                            "not valid JSON", str(ctx.exception),
                            f"{label} 中的 {literal}: 错误应属于 JSON 格式错误，"
                            f"实际消息={ctx.exception}",
                        )

    def test_cli_rejects_literals_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for literal in NON_STANDARD_LITERALS:
                for label, text in literal_rules_templates(literal).items():
                    with self.subTest(literal=literal, 位置=label):
                        rules_path = write_rules_text(
                            tmp, f"rules_cli_{label}.json", text
                        )
                        returncode, stdout, stderr = start_and_wait_exit(
                            rules_path, free_port()
                        )
                        self.assertEqual(
                            returncode, 2,
                            f"{label} 中的 {literal}: 期望退出码 2，实际 "
                            f"{returncode}；stdout={stdout!r} stderr={stderr!r}",
                        )
                        self.assertIn(
                            "not valid JSON", stderr,
                            f"{label} 中的 {literal}: 标准错误应包含 "
                            f"'not valid JSON'，实际 stderr={stderr!r}",
                        )
                        self.assertNotIn(
                            "Traceback", stderr,
                            f"{label} 中的 {literal}: 不应出现 Python 异常回溯，"
                            f"实际 stderr={stderr!r}",
                        )
                        self.assertNotIn(
                            STARTUP_MARKER, stdout,
                            f"{label} 中的 {literal}: 标准输出不应出现监听提示，"
                            f"实际 stdout={stdout!r}",
                        )

    def test_port_reusable_after_failed_start(self):
        # 加载失败的进程不得占用端口：同一端口随后启动合法规则应成功
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            bad_path = write_rules_text(
                tmp, "rules_bad.json",
                '{"routes":[{"method":"GET","path":"/value",'
                '"body":{"value":NaN}}]}',
            )
            good_path = write_rules(
                tmp, "rules_good.json",
                [{"method": "GET", "path": "/value", "body": {"ok": True}}],
            )
            port = free_port()
            returncode, stdout, stderr = start_and_wait_exit(bad_path, port)
            self.assertEqual(returncode, 2)
            self.assertIn("not valid JSON", stderr)

            server = ServerProcess(good_path, port)
            try:
                status, headers, raw = request(port, "GET", "/value")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8")), {"ok": True})
            finally:
                server.stop()


OVERFLOW_NUMBER_TEMPLATES = {
    # (说明, 规则文本)：解析后得到 inf/-inf 的合法 JSON 数字，
    # 无论位于何处都应令整份规则加载失败
    "1e400 位于 body 顶层": (
        '{"routes":[{"method":"GET","path":"/value",'
        '"body":{"n":1e400}}]}'
    ),
    "-1e400 位于 body 顶层": (
        '{"routes":[{"method":"GET","path":"/value",'
        '"body":{"n":-1e400}}]}'
    ),
    "1E+400 大写指数": (
        '{"routes":[{"method":"GET","path":"/value",'
        '"body":{"n":1E+400}}]}'
    ),
    "嵌套对象中": (
        '{"routes":[{"method":"GET","path":"/value",'
        '"body":{"outer":{"inner":{"n":1e400}}}}]}'
    ),
    "嵌套数组中": (
        '{"routes":[{"method":"GET","path":"/value",'
        '"body":{"list":[1,[2,[1e400]]]}}]}'
    ),
    "body 直接为溢出数字": (
        '{"routes":[{"method":"GET","path":"/value","body":-1e400}]}'
    ),
    "被忽略的额外字段中": (
        '{"routes":[{"method":"GET","path":"/ok","body":{}}],'
        '"extra":{"note":1e400}}'
    ),
    "路由项中被忽略的额外字段": (
        '{"routes":[{"method":"GET","path":"/ok","body":{},'
        '"ignored":{"v":1e400}}]}'
    ),
    "存在合法路由时仍整份失败": (
        '{"routes":[{"method":"GET","path":"/ok",'
        '"body":{"fine":1}},{"method":"POST","path":"/bad",'
        '"body":{"n":1e400}}]}'
    ),
}


class OverflowNumberTests(unittest.TestCase):
    """1e400 等解析为 inf/-inf 的合法 JSON 数字：启动期整份规则拒绝。"""

    def _assert_non_finite_error(self, ctx):
        message = str(ctx.exception)
        self.assertIn(
            "non-finite number", message,
            f"错误消息应包含 'non-finite number'，实际消息={message!r}",
        )

    def test_load_rules_raises_rules_error(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for label, text in OVERFLOW_NUMBER_TEMPLATES.items():
                with self.subTest(位置=label):
                    rules_path = write_rules_text(
                        tmp, f"rules_ovf_{label}.json", text
                    )
                    with self.assertRaises(
                        RulesError,
                        msg=f"{label} 应使 load_rules 抛出 RulesError",
                    ) as ctx:
                        load_rules(rules_path)
                    self._assert_non_finite_error(ctx)

    def test_cli_rejects_overflow_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for label, text in OVERFLOW_NUMBER_TEMPLATES.items():
                with self.subTest(位置=label):
                    rules_path = write_rules_text(
                        tmp, f"rules_ovf_cli_{label}.json", text
                    )
                    returncode, stdout, stderr = start_and_wait_exit(
                        rules_path, free_port()
                    )
                    self.assertEqual(
                        returncode, 2,
                        f"{label}: 期望退出码 2，实际 {returncode}；"
                        f"stdout={stdout!r} stderr={stderr!r}",
                    )
                    self.assertIn(
                        "non-finite number", stderr,
                        f"{label}: 标准错误应包含 'non-finite number'，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertNotIn(
                        "Traceback", stderr,
                        f"{label}: 不应出现 Python 异常回溯，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertNotIn(
                        STARTUP_MARKER, stdout,
                        f"{label}: 标准输出不应出现监听提示，"
                        f"实际 stdout={stdout!r}",
                    )

    def test_port_reusable_after_overflow_failure(self):
        # 加载失败的进程不得占用端口：同一端口随后启动合法规则应成功
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            bad_path = write_rules_text(
                tmp, "rules_overflow_bad.json",
                '{"routes":[{"method":"GET","path":"/value",'
                '"body":{"n":1e400}}]}',
            )
            good_path = write_rules(
                tmp, "rules_overflow_good.json",
                [{"method": "GET", "path": "/value", "body": {"ok": True}}],
            )
            port = free_port()
            returncode, stdout, stderr = start_and_wait_exit(bad_path, port)
            self.assertEqual(returncode, 2)
            self.assertIn("non-finite number", stderr)

            server = ServerProcess(good_path, port)
            try:
                status, headers, raw = request(port, "GET", "/value")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8")), {"ok": True})
            finally:
                server.stop()


class FiniteValueRegressionTests(unittest.TestCase):
    """有限值对照：接近但未溢出/下溢为 0 的数字与字符串形态保持原语义。"""

    BODY = {"n": 1e308, "tiny": 1e-400, "text": "1e400", "empty": None}

    def test_finite_numbers_served_compatibly(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules_text(
                tmp, "rules_finite.json",
                '{"routes":[{"method":"GET","path":"/value",'
                '"body":{"n":1e308,"tiny":1e-400,'
                '"text":"1e400","empty":null}}]}',
            )
            # 入口一：直接调用 load_rules 应成功
            from mock_server import load_rules

            routes = load_rules(rules_path)
            self.assertIn(("GET", "/value"), routes)

            # 入口二：经命令行启动并核对实际响应
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/value")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                body = json.loads(raw.decode("utf-8"))
                self.assertIn("n", body)
                self.assertTrue(
                    math.isfinite(body["n"]),
                    f"n 应为有限数，实际 {body['n']!r}",
                )
                # 1e308 仍是最大量级附近的有限浮点数
                self.assertEqual(body["n"], 1e308)
                # 下溢按现有浮点语义变为 0.0
                self.assertEqual(body["tiny"], 0.0)
                self.assertEqual(body["text"], "1e400")
                self.assertIsNone(body["empty"])
                # 字节级紧凑 UTF-8 JSON 与 Content-Length 保持不变
                expected_raw = json.dumps(
                    self.BODY, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.assertEqual(raw, expected_raw)
                self.assertEqual(int(headers["Content-Length"]), len(raw))
            finally:
                server.stop()

    def test_infinity_string_and_nan_key_unchanged(self):
        body = {
            "text_inf": "Infinity",
            "text_neg_inf": "-Infinity",
            "NaN": "键名保持原样",
            "integer": 42,
        }
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp, "rules_strings.json",
                [{"method": "GET", "path": "/value", "body": body}],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/value")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(json.loads(raw.decode("utf-8")), body)
                expected_raw = json.dumps(
                    body, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.assertEqual(raw, expected_raw)
                self.assertEqual(int(headers["Content-Length"]), len(raw))
            finally:
                server.stop()


class LegalStringAndNumberTests(unittest.TestCase):
    """合法边界：字符串 "NaN" 等、普通数字与 null 不得被误判。"""

    BODY = {
        "s_nan": "NaN",
        "s_inf": "Infinity",
        "s_neg_inf": "-Infinity",
        "NaN": "作为键的字符串",
        "Infinity": 1,
        "-Infinity": -1,
        "int": 42,
        "neg_int": -7,
        "float": 3.14,
        "exp": 1e10,
        "neg_exp": -2.5e-3,
        "null": None,
    }

    def test_legal_body_served_verbatim(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp, "rules_legal.json",
                [{"method": "GET", "path": "/value", "body": self.BODY}],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/value")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                # 响应 JSON 原样返回，字符串 "NaN" 等不被特殊处理
                self.assertEqual(json.loads(raw.decode("utf-8")), self.BODY)
                # 字节级核对：紧凑 JSON 的 UTF-8 编码与 Content-Length 一致
                expected_raw = json.dumps(
                    self.BODY, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.assertEqual(raw, expected_raw)
                self.assertEqual(
                    int(headers["Content-Length"]), len(expected_raw)
                )
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
