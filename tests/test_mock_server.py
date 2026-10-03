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


UNPAIRED_SURROGATE_TEMPLATES = {
    # (说明, 规则文本, 出错路由下标)：body 的字符串值或对象键含未配对代理
    # 码点，无法编码为 UTF-8 响应，整份规则应加载失败
    "孤立高代理位于 body 字符串": (
        '{"routes":[{"method":"GET","path":"/bad","body":"\\ud800"}]}', 0,
    ),
    "孤立低代理位于 body 字符串": (
        '{"routes":[{"method":"GET","path":"/bad","body":"\\udc00"}]}', 0,
    ),
    "低代理在前高代理在后（未组成合法字符）": (
        '{"routes":[{"method":"GET","path":"/bad","body":"\\udc00\\ud800"}]}',
        0,
    ),
    "两个高代理相连": (
        '{"routes":[{"method":"GET","path":"/bad","body":"\\ud800\\ud800"}]}',
        0,
    ),
    "嵌套数组中的字符串": (
        '{"routes":[{"method":"GET","path":"/bad",'
        '"body":{"list":["ok",["\\ud800"]]}}]}',
        0,
    ),
    "嵌套对象中的字符串": (
        '{"routes":[{"method":"GET","path":"/bad",'
        '"body":{"outer":{"inner":"\\udc00"}}}]}',
        0,
    ),
    "body 直接为孤立代理字符串": (
        '{"routes":[{"method":"GET","path":"/bad","body":"\\ud800"}]}', 0,
    ),
    "对象键含孤立代理": (
        '{"routes":[{"method":"GET","path":"/bad","body":{"\\ud800":1}}]}', 0,
    ),
    "存在合法路由时仍整份失败": (
        '{"routes":[{"method":"GET","path":"/ok","body":{"fine":1}},'
        '{"method":"POST","path":"/bad","body":"\\ud800"}]}',
        1,
    ),
}


class UnpairedSurrogateTests(unittest.TestCase):
    """body 字符串值/对象键含未配对代理码点：启动期整份规则拒绝。"""

    def test_load_rules_raises_rules_error(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for label, (text, index) in UNPAIRED_SURROGATE_TEMPLATES.items():
                with self.subTest(位置=label):
                    rules_path = write_rules_text(
                        tmp, f"rules_sur_{label}.json", text
                    )
                    with self.assertRaises(
                        RulesError,
                        msg=f"{label} 应使 load_rules 抛出 RulesError",
                    ) as ctx:
                        load_rules(rules_path)
                    message = str(ctx.exception)
                    self.assertIn(
                        f"routes[{index}].body", message,
                        f"{label}: 错误消息应标明 routes[{index}].body，"
                        f"实际消息={message!r}",
                    )
                    self.assertIn(
                        "UTF-8", message,
                        f"{label}: 错误消息应包含 'UTF-8'，实际消息={message!r}",
                    )

    def test_cli_rejects_unpaired_surrogates_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for label, (text, index) in UNPAIRED_SURROGATE_TEMPLATES.items():
                with self.subTest(位置=label):
                    rules_path = write_rules_text(
                        tmp, f"rules_sur_cli_{label}.json", text
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
                        f"routes[{index}].body", stderr,
                        f"{label}: 标准错误应标明 routes[{index}].body，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertIn(
                        "UTF-8", stderr,
                        f"{label}: 标准错误应包含 'UTF-8'，"
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

    def test_port_reusable_after_surrogate_failure(self):
        # 加载失败的进程不得占用端口：同一端口随后启动合法规则应成功
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            bad_path = write_rules_text(
                tmp, "rules_sur_bad.json",
                '{"routes":[{"method":"GET","path":"/bad","body":"\\ud800"}]}',
            )
            good_path = write_rules(
                tmp, "rules_sur_good.json",
                [{"method": "GET", "path": "/ok", "body": {"ok": True}}],
            )
            port = free_port()
            returncode, stdout, stderr = start_and_wait_exit(bad_path, port)
            self.assertEqual(returncode, 2)
            self.assertIn("UTF-8", stderr)

            server = ServerProcess(good_path, port)
            try:
                status, headers, raw = request(port, "GET", "/ok")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8")), {"ok": True})
            finally:
                server.stop()

    def test_surrogate_in_ignored_extra_fields_still_loads(self):
        # 仅被忽略的额外字段含代理码点：不参与响应编码，按原有语义忽略
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules_text(
                tmp, "rules_sur_extra.json",
                '{"routes":[{"method":"GET","path":"/ok","body":{"fine":1},'
                '"ignored":"\\ud800"}],"extra":{"note":"\\udc00"}}',
            )
            routes = load_rules(rules_path)
            self.assertIn(("GET", "/ok"), routes)


class LegalUnicodeBodyTests(unittest.TestCase):
    """合法 Unicode 对照：配对代理、中文与普通文本保持紧凑 UTF-8 响应。"""

    # 规则文本中 \\ud83d\\ude00 是正确配对的代理转义（😀）；
    # \\\\ud800 解码后是六个普通字符 \ud800（反斜杠加字母 u 等），不应误判
    RULES_TEXT = (
        '{"routes":[{"method":"GET","path":"/emoji","body":'
        '{"face":"\\ud83d\\ude00","text":"你好",'
        '"literal":"\\\\ud800","num":1,"nil":null}}]}'
    )
    EXPECTED_BODY = {
        "face": "\U0001F600",
        "text": "你好",
        "literal": "\\ud800",
        "num": 1,
        "nil": None,
    }

    def test_paired_surrogate_loads_and_serves_utf8(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules_text(
                tmp, "rules_paired.json", self.RULES_TEXT
            )
            expected_raw = json.dumps(
                self.EXPECTED_BODY, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")

            # 入口一：直接调用 load_rules 应成功并产出相同的 UTF-8 字节
            from mock_server import load_rules

            routes = load_rules(rules_path)
            self.assertIn(("GET", "/emoji"), routes)
            status, body_bytes = routes[("GET", "/emoji")]
            self.assertEqual(status, 200)
            self.assertEqual(body_bytes, expected_raw)

            # 入口二：经命令行启动并核对实际 HTTP 响应
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                code, headers, raw = request(port, "GET", "/emoji")
                self.assertEqual(code, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(raw, expected_raw)
                self.assertEqual(int(headers["Content-Length"]), len(raw))
                decoded = json.loads(raw.decode("utf-8"))
                self.assertEqual(decoded, self.EXPECTED_BODY)
                # 正确配对的转义应解码为 😀，而非替换或转义文本
                self.assertEqual(decoded["face"], "😀")
                # 普通文本反斜杠加字母 u 不应被当作代理转义
                self.assertEqual(decoded["literal"], "\\ud800")
            finally:
                server.stop()


# ---------------------------------------------------------------------------
# 规则结构校验回归
#
# 下列样例全部由 json.dumps 产出，是语法合法的 UTF-8 JSON，因此失败只能
# 归因于结构校验，而不是 UTF-8 编码或 JSON 语法问题。
# ---------------------------------------------------------------------------

TOP_LEVEL_STRUCTURE_CASES = [
    # (说明, 规则文件的顶层 JSON 值)
    ("顶层为数组", []),
    ("顶层为 null", None),
    ("对象缺少 routes", {}),
    ("routes 为对象", {"routes": {}}),
    ("routes 为 null", {"routes": None}),
]

NON_OBJECT_ITEM_CASES = [
    # (说明, routes 数组内容, 非法项的实际下标)
    ("routes[0] 为 null", [None], 0),
    ("routes[0] 为字符串", ["GET /ok"], 0),
    ("routes[0] 为数组", [["GET", "/ok"]], 0),
    (
        "合法路由之后的项为 null（整份加载失败，不返回部分路由）",
        [{"method": "GET", "path": "/ok", "body": {"fine": 1}}, None],
        1,
    ),
    (
        "合法路由之后的项为数组（整份加载失败，不返回部分路由）",
        [
            {"method": "GET", "path": "/ok", "body": {"fine": 1}},
            ["POST", "/bad"],
        ],
        1,
    ),
]

MISSING_FIELD_CASES = [
    # (说明, routes 数组内容, 非法项下标, 缺失字段名)
    (
        "routes[0] 缺少 method",
        [{"path": "/x", "body": 1}],
        0,
        "method",
    ),
    (
        "routes[0] 缺少 path",
        [{"method": "GET", "body": 1}],
        0,
        "path",
    ),
    (
        "routes[0] 缺少 body",
        [{"method": "GET", "path": "/x"}],
        0,
        "body",
    ),
    (
        "合法路由之后的 routes[1] 缺少 body（整份加载失败，不返回部分路由）",
        [
            {"method": "GET", "path": "/ok", "body": {"fine": 1}},
            {"method": "POST", "path": "/bad"},
        ],
        1,
        "body",
    ),
]


class RulesStructureValidationTests(unittest.TestCase):
    """结构非法但 JSON 语法合法：load_rules 抛 RulesError，CLI 退出码 2。"""

    def test_load_rules_rejects_invalid_top_level(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, value) in enumerate(TOP_LEVEL_STRUCTURE_CASES):
                with self.subTest(样例=label):
                    rules_path = write_rules_text(
                        tmp,
                        f"rules_struct_top_{index}.json",
                        json.dumps(value, ensure_ascii=False),
                    )
                    with self.assertRaises(
                        RulesError,
                        msg=f"样例 {label!r}: load_rules 应抛出 RulesError",
                    ) as ctx:
                        load_rules(rules_path)
                    self.assertIn(
                        "routes",
                        str(ctx.exception),
                        f"样例 {label!r}: 错误消息应指向 routes，"
                        f"实际消息={ctx.exception!r}",
                    )

    def test_load_rules_rejects_non_object_route_items(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, items, bad_index) in enumerate(
                NON_OBJECT_ITEM_CASES
            ):
                with self.subTest(样例=label):
                    rules_path = write_rules(
                        tmp, f"rules_struct_item_{index}.json", items
                    )
                    with self.assertRaises(
                        RulesError,
                        msg=f"样例 {label!r}: load_rules 应抛出 RulesError，"
                            f"不得返回部分路由",
                    ) as ctx:
                        load_rules(rules_path)
                    self.assertIn(
                        f"routes[{bad_index}]",
                        str(ctx.exception),
                        f"样例 {label!r}: 错误消息应标明实际下标 "
                        f"routes[{bad_index}]，实际消息={ctx.exception!r}",
                    )

    def test_load_rules_rejects_missing_required_fields(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, items, bad_index, field) in enumerate(
                MISSING_FIELD_CASES
            ):
                with self.subTest(样例=label):
                    rules_path = write_rules(
                        tmp, f"rules_struct_missing_{index}.json", items
                    )
                    with self.assertRaises(
                        RulesError,
                        msg=f"样例 {label!r}: load_rules 应抛出 RulesError，"
                            f"不得返回部分路由",
                    ) as ctx:
                        load_rules(rules_path)
                    message = str(ctx.exception)
                    self.assertIn(
                        f"routes[{bad_index}]",
                        message,
                        f"样例 {label!r}: 错误消息应标明实际下标 "
                        f"routes[{bad_index}]，实际消息={message!r}",
                    )
                    self.assertIn(
                        field,
                        message,
                        f"样例 {label!r}: 错误消息应标明缺失字段 {field!r}，"
                        f"实际消息={message!r}",
                    )

    def _assert_cli_rejects(self, label, rules_path, expected_fragments):
        # start_and_wait_exit 保证 10 秒未退出即判失败并回收进程与管道
        returncode, stdout, stderr = start_and_wait_exit(
            rules_path, free_port()
        )
        self.assertEqual(
            returncode, 2,
            f"样例 {label!r}: 期望退出码 2，实际 {returncode}；"
            f"stdout={stdout!r} stderr={stderr!r}",
        )
        for fragment in expected_fragments:
            self.assertIn(
                fragment, stderr,
                f"样例 {label!r}: 标准错误应包含定位片段 {fragment!r}，"
                f"实际 stderr={stderr!r}",
            )
        self.assertNotIn(
            "Traceback", stderr,
            f"样例 {label!r}: 不应出现 Python 异常回溯，"
            f"实际 stderr={stderr!r}",
        )
        self.assertNotIn(
            STARTUP_MARKER, stdout,
            f"样例 {label!r}: 校验失败时标准输出不应出现监听提示，"
            f"实际 stdout={stdout!r}",
        )

    def test_cli_rejects_invalid_structure_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, value) in enumerate(TOP_LEVEL_STRUCTURE_CASES):
                with self.subTest(样例=label):
                    rules_path = write_rules_text(
                        tmp,
                        f"rules_cli_top_{index}.json",
                        json.dumps(value, ensure_ascii=False),
                    )
                    self._assert_cli_rejects(label, rules_path, ["routes"])

            for index, (label, items, bad_index) in enumerate(
                NON_OBJECT_ITEM_CASES
            ):
                with self.subTest(样例=label):
                    rules_path = write_rules(
                        tmp, f"rules_cli_item_{index}.json", items
                    )
                    self._assert_cli_rejects(
                        label, rules_path, [f"routes[{bad_index}]"]
                    )

            for index, (label, items, bad_index, field) in enumerate(
                MISSING_FIELD_CASES
            ):
                with self.subTest(样例=label):
                    rules_path = write_rules(
                        tmp, f"rules_cli_missing_{index}.json", items
                    )
                    self._assert_cli_rejects(
                        label,
                        rules_path,
                        [f"routes[{bad_index}]", field],
                    )


class ValidStructureControlTests(unittest.TestCase):
    """合法结构对照：空 routes、body 显式 null、额外字段被忽略。"""

    def test_empty_routes_loads_as_empty_mapping(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules_text(
                tmp, "rules_empty.json", json.dumps({"routes": []})
            )
            # 直接加载：得到空映射，而非报错
            self.assertEqual(load_rules(rules_path), {})

            # 端到端：空规则服务可以启动，任意请求均得到 route_not_found
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/anything")
                self.assertEqual(status, 404)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"error": "route_not_found"},
                )
            finally:
                server.stop()

    def test_explicit_null_body_keeps_default_200_and_null_bytes(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_null_body.json",
                [{"method": "GET", "path": "/nil", "body": None}],
            )
            # body 显式为 null：缺省 status 仍为 200，响应字节即 b"null"
            self.assertEqual(
                load_rules(rules_path),
                {("GET", "/nil"): (200, b"null")},
            )

            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/nil")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(raw, b"null")
                self.assertEqual(int(headers["Content-Length"]), len(raw))
                self.assertIsNone(json.loads(raw.decode("utf-8")))
            finally:
                server.stop()

    def test_extra_fields_ignored(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            # 顶层与路由项中的普通额外字段均不影响加载结果与响应
            document = {
                "version": 1,
                "note": "顶层额外字段被忽略",
                "routes": [
                    {
                        "method": "GET",
                        "path": "/x",
                        "body": {"ok": True},
                        "description": "路由项额外字段被忽略",
                        "weight": 7,
                        "extra": {"n": 1},
                    }
                ],
            }
            rules_path = write_rules_text(
                tmp,
                "rules_extra.json",
                json.dumps(document, ensure_ascii=False),
            )
            self.assertEqual(
                load_rules(rules_path),
                {("GET", "/x"): (200, b'{"ok":true}')},
            )

            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/x")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), {"ok": True}
                )
                self.assertEqual(raw, b'{"ok":true}')
                self.assertEqual(int(headers["Content-Length"]), len(raw))
            finally:
                server.stop()


# ---------------------------------------------------------------------------
# 启动期 path 校验回归
#
# 下列样例全部由 json.dumps 产出为 UTF-8 JSON，除 path 外的规则内容均合法
# （method 合法、body 可编码、status 缺省），因此失败只能归因于 path 校验。
# 每个非法 path 分别放在 routes[0] 与一条合法路由之后的 routes[1]，每份
# 文件只放一个非法项；整份规则必须加载失败，绝不返回部分路由。
# ---------------------------------------------------------------------------

INVALID_PATH_CASES = [
    # (说明, 非法 path 的 JSON 值)
    ("null", None),
    ("布尔 true", True),
    ("整数 123", 123),
    ("浮点数 1.5", 1.5),
    ("空数组 []", []),
    ("空对象 {}", {}),
    ("空字符串", ""),
    ("hello（不以 / 开头）", "hello"),
    ("hello/x（不以 / 开头）", "hello/x"),
    ("/hello?x=1（规则 path 中含问号）", "/hello?x=1"),
    ("/hello#part（规则 path 中含井号）", "/hello#part"),
]

# routes[1] 样例中位于非法项之前的合法路由
VALID_LEADING_ROUTE = {"method": "GET", "path": "/ok", "body": {"fine": 1}}

# 合法对照：根路径路由与配置对象的紧凑 UTF-8 响应字节
VALID_ROOT_ROUTES = [
    {"method": "GET", "path": "/", "body": {"message": "你好"}}
]
VALID_ROOT_BODY_BYTES = '{"message":"你好"}'.encode("utf-8")


def invalid_path_samples():
    """展开为 (序号, 说明, 非法path, 非法项下标, routes 内容) 的全部样例。"""
    seq = 0
    for label, bad_path in INVALID_PATH_CASES:
        yield seq, label, bad_path, 0, [
            {"method": "GET", "path": bad_path, "body": {"v": 1}}
        ]
        seq += 1
        yield seq, label, bad_path, 1, [
            VALID_LEADING_ROUTE,
            {"method": "POST", "path": bad_path, "body": {"v": 2}},
        ]
        seq += 1


def _path_error_fragment(bad_path):
    """错误文本中用于定位实际 path 的片段：非字符串与空串取 repr。"""
    if not isinstance(bad_path, str) or bad_path == "":
        return repr(bad_path)
    return bad_path


class InvalidPathTests(unittest.TestCase):
    """非法 path：直接加载抛 RulesError，命令行入口退出码 2。"""

    def test_load_rules_raises_rules_error_for_invalid_path(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for seq, label, bad_path, bad_index, items in invalid_path_samples():
                with self.subTest(样例=label, 位置=f"routes[{bad_index}]"):
                    rules_path = write_rules(
                        tmp, f"rules_badpath_{seq}.json", items
                    )
                    # 抛异常即证明没有返回（含部分）路由字典
                    with self.assertRaises(
                        RulesError,
                        msg=f"入口 load_rules，样例 {label!r} @ "
                            f"routes[{bad_index}]: 应抛出 RulesError，"
                            f"不得返回部分路由",
                    ) as ctx:
                        load_rules(rules_path)
                    message = str(ctx.exception)
                    self.assertIn(
                        f"routes[{bad_index}]",
                        message,
                        f"入口 load_rules，样例 {label!r}: 错误应标明实际下标 "
                        f"routes[{bad_index}]，实际消息={message!r}",
                    )
                    fragment = _path_error_fragment(bad_path)
                    self.assertIn(
                        fragment,
                        message,
                        f"入口 load_rules，样例 {label!r}: 错误应标明实际 path "
                        f"{fragment!r}，实际消息={message!r}",
                    )

    def test_cli_rejects_invalid_path_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for seq, label, bad_path, bad_index, items in invalid_path_samples():
                with self.subTest(样例=label, 位置=f"routes[{bad_index}]"):
                    rules_path = write_rules(
                        tmp, f"rules_badpath_cli_{seq}.json", items
                    )
                    # start_and_wait_exit 保证超时也会杀掉并回收子进程
                    returncode, stdout, stderr = start_and_wait_exit(
                        rules_path, free_port()
                    )
                    self.assertEqual(
                        returncode, 2,
                        f"入口 python -m mock_server，样例 {label!r} @ "
                        f"routes[{bad_index}]: 期望退出码 2，实际 "
                        f"{returncode}；stdout={stdout!r} stderr={stderr!r}",
                    )
                    self.assertIn(
                        f"routes[{bad_index}]",
                        stderr,
                        f"入口 python -m mock_server，样例 {label!r}: "
                        f"标准错误应标明实际下标 routes[{bad_index}]，"
                        f"实际 stderr={stderr!r}",
                    )
                    fragment = _path_error_fragment(bad_path)
                    self.assertIn(
                        fragment,
                        stderr,
                        f"入口 python -m mock_server，样例 {label!r}: "
                        f"标准错误应标明实际 path {fragment!r}，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertNotIn(
                        "Traceback",
                        stderr,
                        f"入口 python -m mock_server，样例 {label!r}: "
                        f"不应出现 Python 异常回溯，实际 stderr={stderr!r}",
                    )
                    self.assertNotIn(
                        STARTUP_MARKER,
                        stdout,
                        f"入口 python -m mock_server，样例 {label!r}: "
                        f"校验失败时标准输出不应出现监听提示，"
                        f"实际 stdout={stdout!r}",
                    )

    def test_port_reusable_after_invalid_path_failure(self):
        # 选取规则 path 含问号的非法样例：失败进程退出后，同一端口必须能
        # 启动合法规则并取得响应，证明拒绝加载没有留下监听服务
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            bad_path = write_rules(
                tmp,
                "rules_badpath_port.json",
                [{"method": "GET", "path": "/hello?x=1", "body": {}}],
            )
            good_path = write_rules(
                tmp, "rules_root_port.json", VALID_ROOT_ROUTES
            )
            port = free_port()
            returncode, stdout, stderr = start_and_wait_exit(bad_path, port)
            self.assertEqual(
                returncode, 2,
                f"非法 path 样例期望退出码 2，实际 {returncode}；"
                f"stdout={stdout!r} stderr={stderr!r}",
            )
            self.assertIn("routes[0]", stderr)
            self.assertIn("/hello?x=1", stderr)

            # 同一端口启动合法根路径规则，并携带查询串请求以取得响应
            server = ServerProcess(good_path, port)
            try:
                status, headers, raw = request(port, "GET", "/?x=1")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(raw, VALID_ROOT_BODY_BYTES)
                self.assertEqual(int(headers["Content-Length"]), len(raw))
            finally:
                server.stop()


class ValidRootPathControlTests(unittest.TestCase):
    """合法对照：根路径路由加载为默认 200 的 UTF-8 响应；请求 /?x=1 命中。

    规则 path 中的问号被拒绝（见 InvalidPathTests）与请求中的查询字符串
    在匹配时被忽略，是两种各自既有的行为，本测试固定后者。
    """

    def test_load_rules_root_route_defaults_to_200_utf8_bytes(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(tmp, "rules_root.json", VALID_ROOT_ROUTES)
            routes = load_rules(rules_path)
            # 只有根路径这一条路由
            self.assertEqual(set(routes), {("GET", "/")})
            # status 缺省为 200；body 为配置对象的紧凑 UTF-8 JSON 字节
            self.assertEqual(
                routes[("GET", "/")], (200, VALID_ROOT_BODY_BYTES)
            )

    def test_cli_serves_root_route_when_request_has_query_string(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(tmp, "rules_root_cli.json", VALID_ROOT_ROUTES)
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/?x=1")
                self.assertEqual(
                    status, 200,
                    f"GET /?x=1 应命中根路径路由并返回 200，实际 {status}",
                )
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), {"message": "你好"}
                )
                self.assertEqual(raw, VALID_ROOT_BODY_BYTES)
                # Content-Length 等于实际响应体字节数（中文按 UTF-8 计 3 字节）
                self.assertEqual(int(headers["Content-Length"]), len(raw))
            finally:
                server.stop()


# ---------------------------------------------------------------------------
# 路由级固定延迟 delayMs
# ---------------------------------------------------------------------------

DELAY_MS = 200
DELAY_SEC = DELAY_MS / 1000
# 本地回环请求正常只需数毫秒；留足计时与调度误差后仍远低于 200ms
DELAY_EPSILON = 0.05


def timed_request(port, method, target, body=None):
    """发起一次请求，返回 (耗时秒, 状态码, 响应头, 原始响应体字节)。"""
    start = time.monotonic()
    status, headers, raw = request(port, method, target, body=body)
    return time.monotonic() - start, status, headers, raw


class RouteDelayTests(unittest.TestCase):
    """命中配置 delayMs 的路由：读完请求体后至少等待配置时长再响应。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rules_path = write_rules(
            cls._tmp.name,
            "rules_delay.json",
            [
                {"method": "GET", "path": "/hello",
                 "body": {"message": "你好"}},
                {"method": "GET", "path": "/slow", "delayMs": DELAY_MS,
                 "status": 503, "body": {"error": "demo_failure"}},
                {"method": "GET", "path": "/slow404", "delayMs": DELAY_MS,
                 "status": 404, "body": {"error": "configured_missing"}},
                {"method": "GET", "path": "/zero", "delayMs": 0,
                 "body": {"ok": True}},
            ],
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def test_delayed_route_waits_then_returns_configured_error(self):
        # GET /slow?x=1：完整发送请求后至少 200ms 才收到响应，最终为 503
        elapsed, status, headers, raw = timed_request(
            self.port, "GET", "/slow?x=1"
        )
        self.assertGreaterEqual(
            elapsed, DELAY_SEC - DELAY_EPSILON,
            f"GET /slow?x=1 应至少等待 {DELAY_MS}ms，实际耗时 {elapsed:.3f}s",
        )
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
        self.assertEqual(int(headers["Content-Length"]), len(raw))
        self.assertEqual(
            json.loads(raw.decode("utf-8")), {"error": "demo_failure"}
        )

    def test_delay_applied_on_every_hit(self):
        # 再次请求同一路由仍应用延迟
        for attempt in (1, 2):
            with self.subTest(第几次请求=attempt):
                elapsed, status, _, raw = timed_request(
                    self.port, "GET", "/slow"
                )
                self.assertGreaterEqual(elapsed, DELAY_SEC - DELAY_EPSILON)
                self.assertEqual(status, 503)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), {"error": "demo_failure"}
                )

    def test_configured_404_waits_then_returns_own_body(self):
        # 命中配置了 404 的路由：先等待，再返回它自己的 body
        elapsed, status, _, raw = timed_request(self.port, "GET", "/slow404")
        self.assertGreaterEqual(elapsed, DELAY_SEC - DELAY_EPSILON)
        self.assertEqual(status, 404)
        self.assertEqual(
            json.loads(raw.decode("utf-8")), {"error": "configured_missing"}
        )

    def test_routes_without_delay_do_not_wait(self):
        # 未配置 delayMs、delayMs 为 0 与未命中路由：均不增加人为等待
        for label, target, expected_status, expected_body in [
            ("GET /hello（未配置 delayMs）", "/hello", 200,
             {"message": "你好"}),
            ("GET /zero（delayMs 为 0）", "/zero", 200, {"ok": True}),
            ("GET /missing（未命中）", "/missing", 404,
             {"error": "route_not_found"}),
        ]:
            with self.subTest(样例=label):
                elapsed, status, _, raw = timed_request(
                    self.port, "GET", target
                )
                self.assertLess(
                    elapsed, DELAY_SEC - DELAY_EPSILON,
                    f"{label}: 不应增加人为等待，实际耗时 {elapsed:.3f}s",
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(json.loads(raw.decode("utf-8")), expected_body)

    def test_keep_alive_connection_reused_across_delayed_hit(self):
        # 同一 HTTP/1.1 连接：延迟命中之后的后续请求仍正常
        conn = HTTPConnection("127.0.0.1", self.port, timeout=REQUEST_TIMEOUT)
        try:
            start = time.monotonic()
            conn.request("GET", "/slow")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 503)
            resp.read()
            self.assertGreaterEqual(
                time.monotonic() - start, DELAY_SEC - DELAY_EPSILON
            )
            conn.request("GET", "/hello")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(
                json.loads(resp.read().decode("utf-8")), {"message": "你好"}
            )
        finally:
            conn.close()


class DelayBoundaryTests(unittest.TestCase):
    """合法边界：delayMs 缺省、0 与 2000 均可加载；旧规则结果不变。"""

    def test_load_rules_boundaries_and_default(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_delay_bounds.json",
                [
                    {"method": "GET", "path": "/default", "body": 1},
                    {"method": "GET", "path": "/zero", "delayMs": 0,
                     "body": 2},
                    {"method": "GET", "path": "/max", "delayMs": 2000,
                     "body": 3},
                ],
            )
            routes = load_rules(rules_path)
            # 路由表内容仍是 (status, body) 二元组，与普通 dict 相等
            self.assertEqual(
                routes,
                {
                    ("GET", "/default"): (200, b"1"),
                    ("GET", "/zero"): (200, b"2"),
                    ("GET", "/max"): (200, b"3"),
                },
            )
            # 缺省与 0 不记录延迟；2000 原样记录
            self.assertEqual(routes.delays, {("GET", "/max"): 2000})

    def test_old_rules_without_delay_field_unchanged(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_delay_legacy.json",
                [{"method": "GET", "path": "/nil", "body": None}],
            )
            self.assertEqual(
                load_rules(rules_path), {("GET", "/nil"): (200, b"null")}
            )


INVALID_DELAY_CASES = [
    ("null", None),
    ("布尔 true", True),
    ("布尔 false", False),
    ('字符串 "200"', "200"),
    ("浮点数 200.0", 200.0),
    ("浮点数 0.5", 0.5),
    ("空数组 []", []),
    ("空对象 {}", {}),
    ("负整数 -1", -1),
    ("超过上限 2001", 2001),
]


class InvalidDelayTests(unittest.TestCase):
    """非法 delayMs：load_rules 抛 RulesError，CLI 退出码 2 且定位下标。"""

    def _write_case(self, tmp, name, bad_delay, bad_index):
        items = [{"method": "GET", "path": "/ok", "body": {"fine": 1}}]
        bad = {"method": "GET", "path": "/bad",
               "delayMs": bad_delay, "body": {}}
        items.insert(bad_index, bad)
        return write_rules(tmp, name, items)

    def test_load_rules_raises_rules_error(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for bad_index in (0, 1):
                for index, (label, bad_delay) in enumerate(
                    INVALID_DELAY_CASES
                ):
                    with self.subTest(样例=label, 位置=f"routes[{bad_index}]"):
                        rules_path = self._write_case(
                            tmp,
                            f"rules_baddelay_{bad_index}_{index}.json",
                            bad_delay,
                            bad_index,
                        )
                        with self.assertRaises(
                            RulesError,
                            msg=f"样例 {label!r} @ routes[{bad_index}]: "
                                f"load_rules 应抛出 RulesError",
                        ) as ctx:
                            load_rules(rules_path)
                        message = str(ctx.exception)
                        self.assertIn(
                            f"routes[{bad_index}]", message,
                            f"样例 {label!r}: 错误应标明实际下标 "
                            f"routes[{bad_index}]，实际消息={message!r}",
                        )
                        self.assertIn(
                            "delayMs", message,
                            f"样例 {label!r}: 错误应包含 'delayMs'，"
                            f"实际消息={message!r}",
                        )

    def test_cli_rejects_invalid_delay_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for bad_index in (0, 1):
                for index, (label, bad_delay) in enumerate(
                    INVALID_DELAY_CASES
                ):
                    with self.subTest(样例=label, 位置=f"routes[{bad_index}]"):
                        rules_path = self._write_case(
                            tmp,
                            f"rules_baddelay_cli_{bad_index}_{index}.json",
                            bad_delay,
                            bad_index,
                        )
                        returncode, stdout, stderr = start_and_wait_exit(
                            rules_path, free_port()
                        )
                        self.assertEqual(
                            returncode, 2,
                            f"样例 {label!r}: 期望退出码 2，实际 "
                            f"{returncode}；stdout={stdout!r} "
                            f"stderr={stderr!r}",
                        )
                        self.assertIn(
                            f"routes[{bad_index}]", stderr,
                            f"样例 {label!r}: 标准错误应标明实际下标 "
                            f"routes[{bad_index}]，实际 stderr={stderr!r}",
                        )
                        self.assertIn(
                            "delayMs", stderr,
                            f"样例 {label!r}: 标准错误应包含 'delayMs'，"
                            f"实际 stderr={stderr!r}",
                        )
                        self.assertNotIn(
                            "Traceback", stderr,
                            f"样例 {label!r}: 不应出现 Python 异常回溯，"
                            f"实际 stderr={stderr!r}",
                        )
                        self.assertNotIn(
                            STARTUP_MARKER, stdout,
                            f"样例 {label!r}: 校验失败时标准输出不应出现监听"
                            f"提示，实际 stdout={stdout!r}",
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
