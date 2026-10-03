"""mock_server 回归测试：规则文件 -> 命令行入口 -> 本地 HTTP 响应。

仅依赖 Python 3 标准库，可重复执行：

    python -m unittest discover -s tests
    python tests/test_mock_server.py

测试自行准备 UTF-8 规则文件与可用端口，通过 `python -m mock_server`
子进程启动服务，只连接 127.0.0.1；结束后释放进程、连接与临时文件，
不修改项目自带的 rules.json。

启动等待以标准输出的监听提示为准，不依赖 selector 监听子进程管道
（Windows 的 DefaultSelector 不支持普通管道），因此在 Windows 与
Linux 上都能可靠完成。
"""

import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
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
    """以现有命令行入口启动的 mock_server 子进程。

    就绪等待以标准输出出现 STARTUP_MARKER 为准。等待实现不依赖 selector
    监听管道（Windows 的 DefaultSelector 不支持普通管道）：后台线程持续
    读取标准输出/标准错误到内存，主循环按期限轮询已收集的内容，因此在
    Windows 与 Linux 上行为一致，且管道上的阻塞读不会拖住启动等待。
    """

    def __init__(self, rules_path, port, startup_timeout=STARTUP_TIMEOUT,
                 command=None, wait=True):
        if command is None:
            command = [
                sys.executable,
                "-m",
                "mock_server",
                "--rules",
                str(rules_path),
                "--port",
                str(port),
            ]
        self.proc = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.port = port
        self._stdout_chunks = []
        self._stderr_chunks = []
        self._readers = [
            threading.Thread(
                target=self._drain,
                args=(self.proc.stdout, self._stdout_chunks),
                daemon=True,
                name="mock-server-stdout-reader",
            ),
            threading.Thread(
                target=self._drain,
                args=(self.proc.stderr, self._stderr_chunks),
                daemon=True,
                name="mock-server-stderr-reader",
            ),
        ]
        for reader in self._readers:
            reader.start()
        if wait:
            self.wait_until_listening(startup_timeout)

    @staticmethod
    def _drain(stream, chunks):
        # read1 拿到多少算多少，不等待凑满缓冲区，因此未换行的片段也能
        # 及时进入 chunks；进程结束后管道到达 EOF，线程随之自然退出
        try:
            while True:
                data = stream.read1(65536)
                if not data:
                    return
                chunks.append(data)
        except (OSError, ValueError):
            # stop() 关闭管道时读取线程可能被打断，属正常收尾
            return

    def _collected_output(self):
        stdout = b"".join(self._stdout_chunks).decode("utf-8", "replace")
        stderr = b"".join(self._stderr_chunks).decode("utf-8", "replace")
        return stdout, stderr

    def wait_until_listening(self, timeout=STARTUP_TIMEOUT):
        """等待标准输出出现监听提示；失败抛 AssertionError 并先回收子进程。"""
        deadline = time.monotonic() + timeout
        try:
            while True:
                stdout, _ = self._collected_output()
                if STARTUP_MARKER in stdout:
                    return
                returncode = self.proc.poll()
                if returncode is not None:
                    # 进程已退出：等读取线程排空管道，保证输出收集完整
                    for reader in self._readers:
                        reader.join(timeout=5)
                    stdout, stderr = self._collected_output()
                    if STARTUP_MARKER in stdout:
                        return
                    raise AssertionError(
                        f"mock_server 在输出监听提示前退出"
                        f"（退出码 {returncode}）；"
                        f"stdout={stdout!r} stderr={stderr!r}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr = self._collected_output()
                    raise AssertionError(
                        f"mock_server 启动等待超时：{timeout}s 内标准输出"
                        f"未出现监听提示 {STARTUP_MARKER!r}；"
                        f"stdout={stdout!r} stderr={stderr!r}"
                    )
                time.sleep(min(0.02, remaining))
        except BaseException:
            # 失败报错前回收子进程、关闭输出管道，监听端口可立即重新使用
            self.stop()
            raise

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        # 进程结束后管道到达 EOF，读取线程随之退出；回收线程并关闭管道，
        # 避免文件描述符与线程泄漏
        for reader in self._readers:
            reader.join(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


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


# ---------------------------------------------------------------------------
# 启动等待流程回归
#
# 下列用例直接验证 ServerProcess 的就绪等待：成功、提示前的普通输出、
# 提前退出与超时（完全静默 / 仅输出未换行片段）四条路径。等待实现只用
# 后台线程加轮询，不依赖 selector 监听管道，因此在 Windows 与 Linux 上
# 都实际执行，不做平台跳过。
# ---------------------------------------------------------------------------


class StartupWaitFlowTests(unittest.TestCase):
    """启动等待：以监听提示为准，失败可诊断，进程与管道必被回收。"""

    # 超时用例使用较短期限，避免拖慢测试；默认期限仍保持 STARTUP_TIMEOUT
    SHORT_TIMEOUT = 0.5

    def assert_port_reusable(self, port):
        """端口已可重新绑定：证明先前进程与监听套接字确被回收。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    def assert_process_reaped(self, server):
        """子进程已回收、输出管道已关闭。"""
        self.assertIsNotNone(
            server.proc.poll(), "子进程应已被回收（退出或被杀掉并 wait）"
        )
        self.assertTrue(server.proc.stdout.closed, "标准输出管道应已关闭")
        self.assertTrue(server.proc.stderr.closed, "标准错误管道应已关闭")

    def test_ready_marker_then_serves_hello_with_query_string(self):
        # 临时规则：GET /hello -> {"message":"你好"}；等待就绪后带查询串请求
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_startup_hello.json",
                [{"method": "GET", "path": "/hello",
                  "body": {"message": "你好"}}],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, headers, raw = request(port, "GET", "/hello?x=1")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                # 中文按 UTF-8 编码，Content-Length 为实际字节数
                expected = '{"message":"你好"}'.encode("utf-8")
                self.assertEqual(raw, expected)
                self.assertEqual(
                    int(headers["Content-Length"]), len(expected)
                )
            finally:
                server.stop()
            # 正常停止后进程回收、管道关闭、端口可重新使用
            self.assert_process_reaped(server)
            self.assert_port_reusable(port)

    def test_ordinary_lines_before_marker_do_not_stop_waiting(self):
        # 提示前出现普通输出行不影响继续等待真实监听提示
        port = free_port()
        command = [
            sys.executable, "-c",
            "import sys, time\n"
            "print('noise: still starting', flush=True)\n"
            "print('another ordinary line', flush=True)\n"
            f"print('{STARTUP_MARKER} on http://127.0.0.1:{port} "
            "(0 route(s))', flush=True)\n"
            "time.sleep(30)\n",
        ]
        server = ServerProcess(None, port, command=command, wait=False)
        try:
            # 不抛 AssertionError 即通过：普通行之后仍等到了监听提示
            server.wait_until_listening(5.0)
        finally:
            server.stop()
        self.assert_process_reaped(server)

    def test_exit_before_marker_raises_with_exit_code_and_output(self):
        # 重复路由：子进程在监听提示前以退出码 2 结束
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_startup_dup.json",
                [
                    {"method": "GET", "path": "/same", "body": 1},
                    {"method": "GET", "path": "/same", "body": 2},
                ],
            )
            port = free_port()
            server = ServerProcess(rules_path, port, wait=False)
            with self.assertRaises(AssertionError) as ctx:
                server.wait_until_listening()
            message = str(ctx.exception)
            # 报错包含实际退出码与已收集的 stdout/stderr
            self.assertIn("退出码 2", message)
            self.assertIn("duplicate route", message)
            # 即使某项输出为空，消息中也能辨认 stdout 与 stderr 两项
            self.assertIn("stdout=", message)
            self.assertIn("stderr=", message)
            # 报错前已回收子进程并关闭输出管道
            self.assert_process_reaped(server)
            # 端口可重新使用：同一端口启动合法规则并取得响应
            good_path = write_rules(
                tmp,
                "rules_startup_good.json",
                [{"method": "GET", "path": "/ok", "body": {"ok": True}}],
            )
            good = ServerProcess(good_path, port)
            try:
                status, headers, raw = request(port, "GET", "/ok")
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), {"ok": True}
                )
            finally:
                good.stop()

    def test_timeout_when_child_stays_silent(self):
        # 子进程持续存活但完全静默：期限届满抛 AssertionError
        # 默认等待期限保持十秒，本用例仅用较短期限验证超时路径
        self.assertEqual(STARTUP_TIMEOUT, 10.0)
        port = free_port()
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        server = ServerProcess(None, port, command=command, wait=False)
        started = time.monotonic()
        with self.assertRaises(AssertionError) as ctx:
            server.wait_until_listening(self.SHORT_TIMEOUT)
        elapsed = time.monotonic() - started
        # 确实等到了期限届满，且读取输出没有让等待无限阻塞
        self.assertGreaterEqual(elapsed, self.SHORT_TIMEOUT)
        self.assertLess(elapsed, STARTUP_TIMEOUT)
        message = str(ctx.exception)
        self.assertIn("超时", message)
        self.assertIn(f"{self.SHORT_TIMEOUT}s", message)
        # 输出为空时仍能辨认 stdout 与 stderr 两项
        self.assertIn("stdout=''", message)
        self.assertIn("stderr=''", message)
        # 报错前已回收子进程、关闭管道，端口可重新使用
        self.assert_process_reaped(server)
        self.assert_port_reusable(port)

    def test_timeout_when_output_fragment_without_newline(self):
        # 子进程持续存活，仅输出不带换行的片段且始终不给监听提示
        fragment = "mock_server listenin"  # 缺少结尾字符，且不换行
        port = free_port()
        command = [
            sys.executable, "-c",
            "import sys, time\n"
            f"sys.stdout.write({fragment!r})\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n",
        ]
        server = ServerProcess(None, port, command=command, wait=False)
        started = time.monotonic()
        with self.assertRaises(AssertionError) as ctx:
            server.wait_until_listening(self.SHORT_TIMEOUT)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, self.SHORT_TIMEOUT)
        self.assertLess(elapsed, STARTUP_TIMEOUT)
        message = str(ctx.exception)
        self.assertIn("超时", message)
        self.assertIn(f"{self.SHORT_TIMEOUT}s", message)
        # 未换行的片段也被收集并保留在报错中
        self.assertIn(fragment, message)
        self.assertIn("stderr=''", message)
        self.assert_process_reaped(server)
        self.assert_port_reusable(port)


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
# 路由级固定响应延迟（delayMs）回归
# ---------------------------------------------------------------------------

# 行为测试使用的延迟配置：200ms 足以与本地回环的正常往返（毫秒级）区分
DELAY_MS = 200
DELAY_SECONDS = DELAY_MS / 1000
# 下限留少量时钟粒度余量；上限要求未配置延迟的路由远快于 DELAY_MS
DELAY_MIN_SECONDS = DELAY_SECONDS - 0.02
NO_DELAY_MAX_SECONDS = DELAY_SECONDS - 0.05


def timed_request(port, method, target, body=None):
    """发起请求并返回 (状态码, 响应头, 响应体字节, 到收到响应头的耗时秒数)。

    耗时从发送请求前量到响应状态行与响应头接收完毕，即“响应开始”的时刻。
    """
    conn = HTTPConnection("127.0.0.1", port, timeout=REQUEST_TIMEOUT)
    try:
        start = time.monotonic()
        conn.request(method, target, body=body)
        resp = conn.getresponse()
        elapsed = time.monotonic() - start
        raw = resp.read()
        headers = {k: v for k, v in resp.getheaders()}
        return resp.status, headers, raw, elapsed
    finally:
        conn.close()


class DelayBehaviorTests(unittest.TestCase):
    """delayMs 命中即等待：每次请求、错误状态码与配置的 404 都先等后答。"""

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
            ],
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def test_delayed_route_waits_then_returns_configured_error(self):
        # GET /slow?x=1：完整发送请求后至少 DELAY_MS 才收到响应开始，
        # 最终得到配置的 503 与 body；查询串不影响匹配
        for attempt in (1, 2):
            with self.subTest(第几次请求=attempt):
                status, headers, raw, elapsed = timed_request(
                    self.port, "GET", "/slow?x=1"
                )
                self.assertGreaterEqual(
                    elapsed, DELAY_MIN_SECONDS,
                    f"第 {attempt} 次请求：响应开始应不早于 {DELAY_MS}ms，"
                    f"实际 {elapsed * 1000:.1f}ms（每次命中都应应用延迟）",
                )
                self.assertEqual(status, 503)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(int(headers["Content-Length"]), len(raw))
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), {"error": "demo_failure"}
                )

    def test_routes_without_delay_respond_promptly(self):
        # /hello 与未命中的 /missing 不增加人为等待
        for label, target, expected_status, expected_body in [
            ("GET /hello", "/hello", 200, {"message": "你好"}),
            ("GET /missing（未命中）", "/missing", 404,
             {"error": "route_not_found"}),
        ]:
            with self.subTest(样例=label):
                status, headers, raw, elapsed = timed_request(
                    self.port, "GET", target
                )
                self.assertLess(
                    elapsed, NO_DELAY_MAX_SECONDS,
                    f"{label}: 不应增加人为等待，实际耗时 "
                    f"{elapsed * 1000:.1f}ms",
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), expected_body
                )

    def test_configured_404_with_delay_waits_then_returns_own_body(self):
        # 命中配置了 404 的路由：先等待，再返回它自己的 body
        status, headers, raw, elapsed = timed_request(
            self.port, "GET", "/slow404"
        )
        self.assertGreaterEqual(
            elapsed, DELAY_MIN_SECONDS,
            f"配置的 404 也应先等待 {DELAY_MS}ms，实际 {elapsed * 1000:.1f}ms",
        )
        self.assertEqual(status, 404)
        self.assertEqual(
            json.loads(raw.decode("utf-8")), {"error": "configured_missing"}
        )

    def test_delay_applied_on_keep_alive_connection(self):
        # 同一 HTTP/1.1 连接上后续请求命中延迟路由时仍逐次等待
        conn = HTTPConnection("127.0.0.1", self.port, timeout=REQUEST_TIMEOUT)
        try:
            for attempt in (1, 2):
                with self.subTest(第几次请求=attempt):
                    start = time.monotonic()
                    conn.request("GET", "/slow")
                    resp = conn.getresponse()
                    elapsed = time.monotonic() - start
                    raw = resp.read()
                    self.assertGreaterEqual(
                        elapsed, DELAY_MIN_SECONDS,
                        f"keep-alive 第 {attempt} 次请求仍应等待 "
                        f"{DELAY_MS}ms，实际 {elapsed * 1000:.1f}ms",
                    )
                    self.assertEqual(resp.status, 503)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"error": "demo_failure"},
                    )
        finally:
            conn.close()


INVALID_DELAY_CASES = [
    # (说明, 非法 delayMs 的 JSON 值)
    ("null", None),
    ("布尔 true", True),
    ("布尔 false", False),
    ('字符串 "200"', "200"),
    ("浮点数 200.0", 200.0),
    ("浮点数 0.5", 0.5),
    ("空数组 []", []),
    ("空对象 {}", {}),
    ("负数 -1", -1),
    ("大于上限 2001", 2001),
]


class InvalidDelayMsTests(unittest.TestCase):
    """非法 delayMs：load_rules 抛 RulesError，CLI 退出码 2 且不监听。"""

    def _write_case(self, tmp, name, bad_delay, bad_index):
        # 非法项分别位于 routes[0] 与一条合法路由之后的 routes[1]
        bad_item = {
            "method": "GET",
            "path": "/slow",
            "delayMs": bad_delay,
            "status": 503,
            "body": {"error": "demo_failure"},
        }
        items = [bad_item] if bad_index == 0 else [
            {"method": "GET", "path": "/ok", "body": {"fine": 1}},
            bad_item,
        ]
        return write_rules(tmp, name, items)

    def test_load_rules_raises_rules_error(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            seq = 0
            for label, bad_delay in INVALID_DELAY_CASES:
                for bad_index in (0, 1):
                    with self.subTest(样例=label, 位置=f"routes[{bad_index}]"):
                        rules_path = self._write_case(
                            tmp, f"rules_delay_{seq}.json", bad_delay, bad_index
                        )
                        seq += 1
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
                            f"样例 {label!r}: 错误应包含 delayMs 的原因，"
                            f"实际消息={message!r}",
                        )

    def test_cli_rejects_invalid_delay_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            seq = 0
            for label, bad_delay in INVALID_DELAY_CASES:
                for bad_index in (0, 1):
                    with self.subTest(样例=label, 位置=f"routes[{bad_index}]"):
                        rules_path = self._write_case(
                            tmp, f"rules_delay_cli_{seq}.json",
                            bad_delay, bad_index,
                        )
                        seq += 1
                        returncode, stdout, stderr = start_and_wait_exit(
                            rules_path, free_port()
                        )
                        self.assertEqual(
                            returncode, 2,
                            f"样例 {label!r} @ routes[{bad_index}]: 期望退出码 "
                            f"2，实际 {returncode}；stdout={stdout!r} "
                            f"stderr={stderr!r}",
                        )
                        self.assertIn(
                            f"routes[{bad_index}]", stderr,
                            f"样例 {label!r}: 标准错误应标明实际下标 "
                            f"routes[{bad_index}]，实际 stderr={stderr!r}",
                        )
                        self.assertIn(
                            "delayMs", stderr,
                            f"样例 {label!r}: 标准错误应包含 delayMs 的原因，"
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


class DelayBoundaryTests(unittest.TestCase):
    """合法边界：delayMs 为 0 或 2000 均可加载；缺省与 0 不增加等待。"""

    def test_boundary_values_load(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_delay_boundary.json",
                [
                    {"method": "GET", "path": "/zero", "delayMs": 0,
                     "body": {"d": 0}},
                    {"method": "GET", "path": "/max", "delayMs": 2000,
                     "body": {"d": 2000}},
                    {"method": "GET", "path": "/absent",
                     "body": {"d": None}},
                ],
            )
            routes = load_rules(rules_path)
            self.assertEqual(
                set(routes),
                {("GET", "/zero"), ("GET", "/max"), ("GET", "/absent")},
            )
            self.assertEqual(routes.delays[("GET", "/zero")], 0)
            self.assertEqual(routes.delays[("GET", "/max")], 2000)
            self.assertEqual(routes.delays[("GET", "/absent")], 0)

    def test_zero_and_absent_delay_add_no_wait(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_delay_zero.json",
                [
                    {"method": "GET", "path": "/zero", "delayMs": 0,
                     "body": {"d": 0}},
                    {"method": "GET", "path": "/absent", "body": {"d": None}},
                ],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                for target in ("/zero", "/absent"):
                    with self.subTest(path=target):
                        status, headers, raw, elapsed = timed_request(
                            port, "GET", target
                        )
                        self.assertEqual(status, 200)
                        self.assertLess(
                            elapsed, NO_DELAY_MAX_SECONDS,
                            f"GET {target}: delayMs 缺省或为 0 不应增加等待，"
                            f"实际 {elapsed * 1000:.1f}ms",
                        )
            finally:
                server.stop()


# ---------------------------------------------------------------------------
# 分段发送请求体时的延迟回归
#
# 规则约定请求体读取完毕后才开始延迟。下列用例用裸 socket 把 Content-Length
# 合法的请求体拆成两段发送，分别核对“补齐前保持静默”与“补齐后重新计满延迟”
# 两个观察结果，而不是只看整个请求的总耗时。
# ---------------------------------------------------------------------------

PARTIAL_DELAY_MS = 200
PARTIAL_DELAY_SECONDS = PARTIAL_DELAY_MS / 1000
# 补齐请求体后允许的计时误差上限（20ms）
PARTIAL_TIMING_TOLERANCE = 0.02
# 补齐前静默观察时长：大于 200ms 延迟，提前响应或拿接收耗时抵扣都会在此窗口暴露
PARTIAL_PRE_WAIT_SECONDS = 0.30
# 补齐后取得完整响应的硬上限：超时判失败，不得跳过或视为成功
PARTIAL_RESPONSE_TIMEOUT = 5.0
PARTIAL_RULES = [
    {
        "method": "POST",
        "path": "/slow",
        "delayMs": PARTIAL_DELAY_MS,
        "status": 503,
        "body": {"error": "demo_failure"},
    }
]


class PartialRequestBodyDelayTests(unittest.TestCase):
    """请求体分段到达时：读完之前不响应，读完之后延迟重新计满 200ms。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rules_path = write_rules(
            cls._tmp.name, "rules_delay_partial.json", PARTIAL_RULES
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def _wait_until_readable(self, selector, deadline):
        """轮询等到 socket 可读返回 True，到达 deadline 仍不可读返回 False。"""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if selector.select(timeout=min(0.05, remaining)):
                return True

    def _recv_or_fail(self, sock, selector, deadline, phase):
        """在 deadline 前读取一段响应字节；超时或连接关闭均判失败。"""
        if not self._wait_until_readable(selector, deadline):
            self.fail(
                f"{phase}：补齐请求体后超过 "
                f"{PARTIAL_RESPONSE_TIMEOUT:.0f} 秒仍未取得完整响应"
            )
        chunk = sock.recv(4096)
        if not chunk:
            self.fail(
                f"{phase}:服务端关闭了连接，未取得完整的 HTTP 响应"
            )
        return chunk

    def test_delay_starts_only_after_entire_request_body_read(self):
        # POST /slow?x=1，Content-Length: 6，请求体 abcdef 分两段发送：
        # 先发请求头与 "abc"，连接保持打开；300ms 静默后再补发 "def"。
        # 查询字符串与请求体内容都不改变该路由的配置响应。
        request_head = (
            "POST /slow?x=1 HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Content-Length: 6\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")

        sock = socket.create_connection(
            ("127.0.0.1", self.port), timeout=REQUEST_TIMEOUT
        )
        # 禁用 Nagle，保证两段请求体各自立即发出
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        selector = DefaultSelector()
        selector.register(sock, EVENT_READ)
        try:
            # ---- 阶段一（补齐前）：只发送请求头与前 3 字节请求体 ----
            sock.sendall(request_head + b"abc")
            silence_start = time.monotonic()
            silence_deadline = silence_start + PARTIAL_PRE_WAIT_SECONDS
            while time.monotonic() < silence_deadline:
                if self._wait_until_readable(selector, silence_deadline):
                    chunk = sock.recv(4096)
                    waited_ms = (time.monotonic() - silence_start) * 1000
                    if not chunk:
                        self.fail(
                            "补齐请求体之前（阶段一）：仅发送 3/6 字节请求体，"
                            f"服务端在 {waited_ms:.1f}ms 后关闭了连接；"
                            "请求体尚未读完时既不应返回，也不应结束请求"
                        )
                    self.fail(
                        "补齐请求体之前（阶段一）：仅发送 3/6 字节请求体并保持"
                        f"连接 {waited_ms:.1f}ms 后就收到 {len(chunk)} 个响应"
                        f"字节：{chunk[:120]!r}；请求体读完前不得返回响应，"
                        "接收请求体的耗时也不得抵扣配置的延迟"
                    )

            # ---- 阶段二（补齐后）：补发剩余 3 字节，延迟从此刻起算 ----
            completion = time.monotonic()
            sock.sendall(b"def")
            response_deadline = completion + PARTIAL_RESPONSE_TIMEOUT

            if not self._wait_until_readable(selector, response_deadline):
                self.fail(
                    "补齐请求体之后（阶段二）：发送剩余请求体后超过 "
                    f"{PARTIAL_RESPONSE_TIMEOUT:.0f} 秒仍未收到任何响应字节"
                )
            first_byte_at = time.monotonic()
            first_chunk = sock.recv(4096)
            wait_before_first = first_byte_at - completion
            if not first_chunk:
                self.fail(
                    "补齐请求体之后（阶段二）：服务端在补齐请求体后关闭了"
                    f"连接（等待 {wait_before_first * 1000:.1f}ms），"
                    "未返回任何响应"
                )
            self.assertGreaterEqual(
                wait_before_first,
                PARTIAL_DELAY_SECONDS - PARTIAL_TIMING_TOLERANCE,
                "补齐请求体之后（阶段二）：从补齐请求体到首个响应字节仅 "
                f"{wait_before_first * 1000:.1f}ms，早于配置的 "
                f"{PARTIAL_DELAY_MS}ms（计时误差容忍 "
                f"{PARTIAL_TIMING_TOLERANCE * 1000:.0f}ms）；"
                "延迟必须在请求体读完后重新计满，不得与接收请求体的耗时重叠",
            )

            # 在同一 5 秒上限内收齐响应头与响应体，逐段解析
            buffer = first_chunk
            while b"\r\n\r\n" not in buffer:
                buffer += self._recv_or_fail(
                    sock, selector, response_deadline,
                    "补齐请求体之后（阶段二：读取响应头）",
                )
            head_bytes, _, body_bytes = buffer.partition(b"\r\n\r\n")
            head_lines = head_bytes.split(b"\r\n")
            status_parts = head_lines[0].split(b" ", 2)
            self.assertEqual(
                len(status_parts), 3,
                f"补齐请求体之后（阶段二）：状态行格式非法，实际 "
                f"{head_lines[0]!r}",
            )
            self.assertEqual(
                status_parts[0], b"HTTP/1.1",
                f"补齐请求体之后（阶段二）：应返回 HTTP/1.1 响应，实际状态行 "
                f"{head_lines[0]!r}",
            )
            self.assertEqual(
                status_parts[1], b"503",
                f"补齐请求体之后（阶段二）：状态码应为 503，实际状态行 "
                f"{head_lines[0]!r}",
            )
            headers = {}
            for line in head_lines[1:]:
                name, sep, value = line.partition(b":")
                if sep:
                    headers[name.strip().decode("ascii").lower()] = (
                        value.strip().decode("ascii")
                    )

            content_type = headers.get("content-type")
            self.assertEqual(
                content_type, CONTENT_TYPE,
                "补齐请求体之后（阶段二）：Content-Type 应为 "
                f"{CONTENT_TYPE!r}，实际 {content_type!r}",
            )
            self.assertIn(
                "content-length", headers,
                "补齐请求体之后（阶段二）：响应缺少 Content-Length",
            )
            content_length = int(headers["content-length"])

            while len(body_bytes) < content_length:
                body_bytes += self._recv_or_fail(
                    sock, selector, response_deadline,
                    "补齐请求体之后（阶段二：读取响应体）",
                )
            self.assertEqual(
                len(body_bytes), content_length,
                "补齐请求体之后（阶段二）：响应体长度应恰好等于 "
                f"Content-Length={content_length}，实际收到 "
                f"{len(body_bytes)} 字节",
            )
            self.assertEqual(
                json.loads(body_bytes.decode("utf-8")),
                {"error": "demo_failure"},
                "补齐请求体之后（阶段二）：查询字符串与请求体内容不应改变"
                f"路由的配置响应，实际响应体 {body_bytes!r}",
            )
        finally:
            selector.close()
            sock.close()


# ---------------------------------------------------------------------------
# 不支持方法的 501 JSON 拒绝响应回归
#
# 非 GET/POST 方法统一返回 501 与 {"error":"method_not_supported"}，
# 不读取路由配置（body/status/delayMs 均不适用），响应携带
# Connection: close 并在发送后关闭连接；HEAD 不发送响应体。
# ---------------------------------------------------------------------------

METHOD_REJECT_BODY = b'{"error":"method_not_supported"}'


class MethodNotSupportedTests(unittest.TestCase):
    """501 拒绝响应：统一 JSON 格式、关闭连接，与既有路由行为互不干扰。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        # GET /hello 配置 503 + 延迟 + 中文 body：501 不得使用其中任何一项
        cls.rules_path = write_rules(
            cls._tmp.name,
            "rules_method_501.json",
            [
                {"method": "GET", "path": "/hello", "status": 503,
                 "delayMs": DELAY_MS, "body": {"message": "你好"}},
            ],
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def _assert_501_headers(self, label, headers):
        content_type = headers.get("Content-Type")
        self.assertEqual(
            content_type, CONTENT_TYPE,
            f"样例 {label}: Content-Type 应为 {CONTENT_TYPE!r}，"
            f"实际 {content_type!r}",
        )
        content_length = headers.get("Content-Length")
        self.assertIsNotNone(content_length, f"样例 {label}: 缺少 Content-Length")
        self.assertEqual(
            int(content_length), len(METHOD_REJECT_BODY),
            f"样例 {label}: Content-Length 应为拒绝正文的字节数 "
            f"{len(METHOD_REJECT_BODY)}，实际 {content_length!r}",
        )
        connection = headers.get("Connection")
        self.assertEqual(
            connection, "close",
            f"样例 {label}: 拒绝响应应携带 Connection: close，"
            f"实际 {connection!r}",
        )

    def test_put_on_configured_route_returns_json_501(self):
        # PUT /hello?x=1：命中路径存在且配置了 503/延迟/中文 body，
        # 但 501 拒绝不得使用其中任何一项，也不应等待
        status, headers, raw, elapsed = timed_request(
            self.port, "PUT", "/hello?x=1"
        )
        self.assertEqual(status, 501)
        self._assert_501_headers("PUT /hello?x=1", headers)
        self.assertEqual(raw, METHOD_REJECT_BODY)
        self.assertNotIn("你好".encode("utf-8"), raw)
        self.assertLess(
            elapsed, NO_DELAY_MAX_SECONDS,
            f"PUT /hello?x=1: 501 拒绝不应应用路由延迟 {DELAY_MS}ms，"
            f"实际耗时 {elapsed * 1000:.1f}ms",
        )

    def test_unsupported_methods_on_missing_path(self):
        # 路径不存在时同样返回统一的 501（而非 404）
        for method in ("PUT", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, raw = request(self.port, method, "/missing")
                self.assertEqual(status, 501)
                self._assert_501_headers(f"{method} /missing", headers)
                self.assertEqual(raw, METHOD_REJECT_BODY)

    def test_head_returns_501_headers_without_body(self):
        # HEAD：同样的 501 与响应头约定，Content-Length 仍按 JSON 正文
        # 字节数给出，但不发送响应体
        status, headers, raw = request(self.port, "HEAD", "/hello")
        self.assertEqual(status, 501)
        self._assert_501_headers("HEAD /hello", headers)
        self.assertEqual(raw, b"")

    def test_get_route_unaffected_after_rejections(self):
        # 新连接上的 GET 仍按路由配置：等待延迟后返回 503 与中文 JSON
        status, headers, raw, elapsed = timed_request(
            self.port, "GET", "/hello"
        )
        self.assertGreaterEqual(
            elapsed, DELAY_MIN_SECONDS,
            f"GET /hello: 仍应等待配置的 {DELAY_MS}ms，"
            f"实际 {elapsed * 1000:.1f}ms",
        )
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
        self.assertEqual(raw, '{"message":"你好"}'.encode("utf-8"))
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_body_request_rejected_once_and_connection_closed(self):
        # 携带请求体的 PUT 之后紧跟一个流水线 GET：只应得到一个 501，
        # 请求体与其后的字节都不得被当作后续请求解释
        request_bytes = (
            "PUT /hello HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Content-Length: 5\r\n"
            "\r\n"
        ).encode("ascii") + b"hello" + (
            "GET /hello HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "\r\n"
        ).encode("ascii")
        sock = socket.create_connection(
            ("127.0.0.1", self.port), timeout=REQUEST_TIMEOUT
        )
        try:
            sock.sendall(request_bytes)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    # 服务端在响应后关闭了连接
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        data = b"".join(chunks)
        head, _, body = data.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        self.assertEqual(
            status_line, b"HTTP/1.1 501 Not Implemented",
            f"状态行应为 501，实际 {status_line!r}",
        )
        self.assertEqual(
            data.count(b"HTTP/1.1"), 1,
            f"流水线中的第二个请求不得被处理，实际响应 {data!r}",
        )
        self.assertEqual(body, METHOD_REJECT_BODY)
        self.assertIn(b"Connection: close", head)
        self.assertNotIn("你好".encode("utf-8"), data)


# ---------------------------------------------------------------------------
# 规则文件与已启动服务的生命周期回归
#
# README 约定：规则仅在启动时加载一次，之后修改文件不影响响应。下列用例
# 固定这一约定：同一文件在运行期间被改写为合法新规则、改写为非法 JSON、
# 删除，原服务都继续返回启动时加载的响应且不退岀；只有重新启动的进程才
# 读取当前文件内容。文件变化与请求按固定顺序发生，不依赖文件时间戳精度。
# ---------------------------------------------------------------------------


class RulesFileLifecycleTests(unittest.TestCase):
    """运行期间改写/删除规则文件不影响已启动服务；重启后才读取当前文件。"""

    INITIAL_BODY = {"version": "初始"}
    UPDATED_BODY = {"version": "更新"}

    def _assert_snapshot(self, stage, port, expected_status, expected_body):
        """核对 GET /snapshot 的状态码、完整 JSON 正文与响应头。

        失败信息指明发生变化的阶段与实际响应，便于定位是哪一步打破了
        生命周期约定。
        """
        expected_raw = json.dumps(
            expected_body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        status, headers, raw = request(port, "GET", "/snapshot")
        self.assertEqual(
            status, expected_status,
            f"阶段[{stage}]: 状态码应为 {expected_status}，实际 {status}；"
            f"响应体={raw!r}",
        )
        content_type = headers.get("Content-Type")
        self.assertEqual(
            content_type, CONTENT_TYPE,
            f"阶段[{stage}]: Content-Type 应为 {CONTENT_TYPE!r}，"
            f"实际 {content_type!r}；响应体={raw!r}",
        )
        content_length = headers.get("Content-Length")
        self.assertIsNotNone(
            content_length, f"阶段[{stage}]: 缺少 Content-Length"
        )
        self.assertEqual(
            int(content_length), len(raw),
            f"阶段[{stage}]: Content-Length={content_length} 与实际响应体"
            f"字节数 {len(raw)} 不符；响应体={raw!r}",
        )
        # 中文正文必须保持 UTF-8：字节级与紧凑 JSON 序列化完全一致
        self.assertEqual(
            raw, expected_raw,
            f"阶段[{stage}]: 响应体应为 {expected_raw!r}，实际 {raw!r}",
        )
        self.assertEqual(
            json.loads(raw.decode("utf-8")), expected_body,
            f"阶段[{stage}]: 响应 JSON 应为 {expected_body}，"
            f"实际 {json.loads(raw.decode('utf-8'))}",
        )

    def _assert_server_alive(self, stage, server):
        self.assertIsNone(
            server.proc.poll(),
            f"阶段[{stage}]: 服务不应因规则文件变化而退出，"
            f"实际退出码={server.proc.poll()}",
        )

    def test_running_server_ignores_file_changes_restart_reloads(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_snapshot.json",
                [{"method": "GET", "path": "/snapshot",
                  "body": self.INITIAL_BODY}],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                # 阶段 1：启动后返回初始规则
                self._assert_snapshot(
                    "启动后初始请求", port, 200, self.INITIAL_BODY
                )

                # 阶段 2：同一文件改写为合法新规则（status 503 + 更新正文），
                # 原服务仍返回启动时加载的 200 与初始正文
                write_rules(
                    tmp,
                    "rules_snapshot.json",
                    [{"method": "GET", "path": "/snapshot", "status": 503,
                      "body": self.UPDATED_BODY}],
                )
                self._assert_server_alive("改写为合法新规则后", server)
                self._assert_snapshot(
                    "改写为合法新规则后", port, 200, self.INITIAL_BODY
                )

                # 阶段 3：文件变成非法 JSON（只有一个左花括号），
                # 原服务不受影响，不能退出或返回加载错误
                write_rules_text(tmp, "rules_snapshot.json", "{")
                self._assert_server_alive("改写为非法 JSON 后", server)
                self._assert_snapshot(
                    "改写为非法 JSON 后", port, 200, self.INITIAL_BODY
                )

                # 阶段 4：删除规则文件，原服务仍返回初始响应
                rules_path.unlink()
                self._assert_server_alive("删除规则文件后", server)
                self._assert_snapshot(
                    "删除规则文件后", port, 200, self.INITIAL_BODY
                )

                # 阶段 5：同一路径恢复合法的更新规则，停止原服务并在
                # 同一端口重新启动；新进程读取当前文件，返回 503 与更新正文
                write_rules(
                    tmp,
                    "rules_snapshot.json",
                    [{"method": "GET", "path": "/snapshot", "status": 503,
                      "body": self.UPDATED_BODY}],
                )
            finally:
                server.stop()

            restarted = ServerProcess(rules_path, port)
            try:
                self._assert_snapshot(
                    "恢复原规则并重启后", port, 503, self.UPDATED_BODY
                )
            finally:
                restarted.stop()


# ---------------------------------------------------------------------------
# requestBody 请求正文样例校验回归
#
# POST 路由可选的 requestBody 以 JSON 样例约束完整请求正文：命中后按 UTF-8
# 解析正文并与样例递归比较，不通过（含空正文、非法 UTF-8、JSON 语法错误、
# 非有限数字）一律返回 400 request_body_mismatch，不采用配置的状态、正文
# 或延迟；缺省 requestBody 时仍忽略正文。
# ---------------------------------------------------------------------------

REQUEST_BODY_MISMATCH = b'{"error":"request_body_mismatch"}'
NOT_FOUND_BODY = b'{"error":"route_not_found"}'

# 行为测试使用的规则：
#   POST /check   样例 {"amount":1,"ok":true}，缺省 200
#   POST /guarded 样例 {"v":1}，配置 503 + 200ms 延迟（校验失败时均不得使用）
#   POST /loose   无 requestBody，任何正文都被忽略
#   POST /nullish 样例为显式 null，只接受 JSON null
#   GET  /hello   不受影响
REQUEST_BODY_RULES = [
    {"method": "POST", "path": "/check",
     "requestBody": {"amount": 1, "ok": True},
     "body": {"accepted": True}},
    {"method": "POST", "path": "/guarded", "delayMs": DELAY_MS, "status": 503,
     "requestBody": {"v": 1}, "body": {"error": "demo_failure"}},
    {"method": "POST", "path": "/loose", "body": {"ignored": True}},
    {"method": "POST", "path": "/nullish", "requestBody": None,
     "body": {"was": "null"}},
    {"method": "GET", "path": "/hello", "body": {"message": "你好"}},
]

# 与样例相等的各种正文写法：键序、排版空白、1 与 1.0 数值相等
MATCHING_BODIES = [
    ("紧凑原样", b'{"amount":1,"ok":true}'),
    ("键序不同", b'{"ok":true,"amount":1.0}'),
    ("多余空白", b'  {  "amount" : 1 ,  "ok" : true }  '),
    ("整数写为浮点", b'{"amount":1.0,"ok":true}'),
    ("指数数字", b'{"amount":1e0,"ok":true}'),
]

# 与样例不相等但本身是合法 JSON 的正文
MISMATCHING_VALID_BODIES = [
    ("ok 为数字 1（布尔不等于数字）", b'{"ok":1,"amount":1.0}'),
    ("ok 为 0", b'{"ok":0,"amount":1}'),
    ("ok 为字符串", b'{"ok":"true","amount":1}'),
    ("amount 为布尔", b'{"amount":true,"ok":true}'),
    ("amount 数值不同", b'{"amount":2,"ok":true}'),
    ("缺少 amount 键", b'{"ok":true}'),
    ("缺少 ok 键", b'{"amount":1}'),
    ("多出额外键", b'{"amount":1,"ok":true,"extra":null}'),
    ("空对象", b'{}'),
    ("对象写成数组", b'[{"amount":1,"ok":true}]'),
    ("整体为 null", b'null'),
]

# 根本无法解析为合法有限 JSON 的正文
MALFORMED_BODIES = [
    ("空正文", b""),
    ("非法 UTF-8", b"\xff\xfe"),
    ("孤立左花括号", b"{"),
    ("被截断的对象", b'{"amount":1,"ok":true'),
    ("裸文本", b"not json"),
    ("NaN 字面量", b'{"amount":NaN,"ok":true}'),
    ("Infinity 字面量", b'{"amount":Infinity,"ok":true}'),
    ("溢出数字 1e400", b'{"amount":1e400,"ok":true}'),
]


def raw_request(port, raw_body, target="/check", method="POST",
                content_type=None, send_content_length=True,
                wait_response=True):
    """用裸 socket 发送请求，返回 (status, headers, body_bytes, sock)。

    可自定义 Content-Type 与是否发送 Content-Length；sock 交由调用方关闭，
    以便复用连接或在响应到达前观察服务行为。
    """
    sock = socket.create_connection(
        ("127.0.0.1", port), timeout=REQUEST_TIMEOUT
    )
    head_lines = [f"{method} {target} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    if send_content_length:
        head_lines.append(f"Content-Length: {len(raw_body)}")
    if content_type is not None:
        head_lines.append(f"Content-Type: {content_type}")
    # 要求服务端响应后关闭连接，裸 socket 读到 EOF 即可拿到完整响应
    head_lines.append("Connection: close")
    sock.sendall(("\r\n".join(head_lines) + "\r\n\r\n").encode("ascii"))
    if raw_body:
        sock.sendall(raw_body)
    if not wait_response:
        return None, None, None, sock
    chunks = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
    data = b"".join(chunks)
    head, _, body_bytes = data.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    status = int(status_line.split(b" ", 2)[1])
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        name, sep, value = line.partition(b":")
        if sep:
            headers[name.strip().decode("ascii").lower()] = (
                value.strip().decode("ascii")
            )
    return status, headers, body_bytes, sock


class RequestBodyBehaviorTests(unittest.TestCase):
    """requestBody 命中校验：匹配放行、不匹配一律 400 且不用配置响应。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rules_path = write_rules(
            cls._tmp.name, "rules_request_body.json", REQUEST_BODY_RULES
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def _assert_400_mismatch(self, label, raw_body, target="/check"):
        status, headers, raw = request(self.port, "POST", target, body=raw_body)
        self.assertEqual(
            status, 400,
            f"样例 {label}: 应返回 400，实际 {status}；响应={raw!r}",
        )
        self.assertEqual(
            headers.get("Content-Type"), CONTENT_TYPE,
            f"样例 {label}: 400 响应 Content-Type 应为 {CONTENT_TYPE!r}",
        )
        self.assertEqual(raw, REQUEST_BODY_MISMATCH)
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_matching_bodies_return_configured_response(self):
        for label, raw_body in MATCHING_BODIES:
            with self.subTest(正文=label):
                status, headers, raw = request(
                    self.port, "POST", "/check", body=raw_body
                )
                self.assertEqual(status, 200, f"样例 {label}: 应返回 200")
                self.assertEqual(
                    headers.get("Content-Type"), CONTENT_TYPE
                )
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), {"accepted": True}
                )
                self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_task_example_ok_true_amount_float_accepted(self):
        # 任务明确约定的请求：{"ok":true,"amount":1.0} -> 200
        status, headers, raw = request(
            self.port, "POST", "/check", body=b'{"ok":true,"amount":1.0}'
        )
        self.assertEqual(status, 200)
        self.assertEqual(raw, b'{"accepted":true}')
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_task_example_ok_one_rejected(self):
        # 任务明确约定：ok 改为 1（数字不等于布尔）-> 400
        self._assert_400_mismatch(
            "ok 为数字 1", b'{"ok":1,"amount":1.0}'
        )

    def test_valid_json_but_mismatching_bodies_return_400(self):
        for label, raw_body in MISMATCHING_VALID_BODIES:
            with self.subTest(正文=label):
                self._assert_400_mismatch(label, raw_body)

    def test_malformed_bodies_return_400(self):
        for label, raw_body in MALFORMED_BODIES:
            with self.subTest(正文=label):
                self._assert_400_mismatch(label, raw_body)

    def test_explicit_null_sample_only_accepts_json_null(self):
        for label, raw_body, expected in [
            ("JSON null", b"null", 200),
            ("空正文", b"", 400),
            ("数字 0", b"0", 400),
            ("布尔 false", b"false", 400),
            ("字符串 null", b'"null"', 400),
            ("空对象", b"{}", 400),
        ]:
            with self.subTest(正文=label):
                status, headers, raw = request(
                    self.port, "POST", "/nullish", body=raw_body
                )
                self.assertEqual(status, expected)
                if expected == 200:
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")), {"was": "null"}
                    )
                else:
                    self.assertEqual(raw, REQUEST_BODY_MISMATCH)

    def test_mismatch_does_not_apply_configured_status_body_or_delay(self):
        # /guarded 配置了 503 + 200ms 延迟：校验失败时 400 立即返回，
        # 不等待、不使用配置的状态与正文
        start = time.monotonic()
        status, headers, raw = request(
            self.port, "POST", "/guarded", body=b'{"v":2}'
        )
        elapsed = time.monotonic() - start
        self.assertEqual(status, 400)
        self.assertEqual(raw, REQUEST_BODY_MISMATCH)
        self.assertLess(
            elapsed, NO_DELAY_MAX_SECONDS,
            f"不匹配时不应应用 200ms 延迟，实际 {elapsed * 1000:.1f}ms",
        )
        # 语法错误同样立即 400
        status, _, raw = request(self.port, "POST", "/guarded", body=b"{")
        self.assertEqual(status, 400)
        self.assertEqual(raw, REQUEST_BODY_MISMATCH)

    def test_match_still_applies_delay_and_configured_response(self):
        # 校验通过后：读完正文 -> 等满 200ms -> 返回配置的 503 与正文
        status, headers, raw, elapsed = timed_request(
            self.port, "POST", "/guarded", body=b'{"v":1.0}'
        )
        self.assertGreaterEqual(
            elapsed, DELAY_MIN_SECONDS,
            f"校验通过后仍应等待 {DELAY_MS}ms，实际 {elapsed * 1000:.1f}ms",
        )
        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(raw.decode("utf-8")), {"error": "demo_failure"}
        )

    def test_route_without_request_body_ignores_any_body(self):
        for label, raw_body in [
            ("空正文", b""),
            ("非法 UTF-8", b"\xff"),
            ("JSON 语法错误", b"{"),
            ("合法但任意的 JSON", b'{"anything":false}'),
            ("裸文本", b"plain text"),
        ]:
            with self.subTest(正文=label):
                status, headers, raw = request(
                    self.port, "POST", "/loose", body=raw_body
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), {"ignored": True}
                )

    def test_unmatched_route_with_invalid_body_returns_404(self):
        # 未命中即使正文非法也返回原有 404 正文，不做请求体校验
        for label, raw_body in [
            ("非法 UTF-8", b"\xff"),
            ("JSON 语法错误", b"{"),
            ("合法 JSON", b'{"amount":1,"ok":true}'),
        ]:
            with self.subTest(正文=label):
                status, headers, raw = request(
                    self.port, "POST", "/missing", body=raw_body
                )
                self.assertEqual(status, 404)
                self.assertEqual(raw, NOT_FOUND_BODY)

    def test_query_string_still_ignored_in_matching(self):
        status, headers, raw = request(
            self.port, "POST", "/check?x=1&y=2",
            body=b'{"ok":true,"amount":1}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw.decode("utf-8")), {"accepted": True})

    def test_content_type_does_not_affect_validation(self):
        # 正文一律按 UTF-8 JSON 解析，与 Content-Type 无关
        for label, content_type in [
            ("text/plain", "text/plain"),
            ("application/xml", "application/xml"),
            ("application/json; charset=latin-1",
             "application/json; charset=latin-1"),
        ]:
            with self.subTest(Content_Type=label):
                try:
                    status, headers, raw, sock = raw_request(
                        self.port, b'{"amount":1,"ok":true}',
                        content_type=content_type,
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(raw, b'{"accepted":true}')
                finally:
                    sock.close()

    def test_no_content_length_with_empty_body_is_mismatch(self):
        # 不发送 Content-Length 且无正文字节：按空正文处理 -> 400
        try:
            status, headers, raw, sock = raw_request(
                self.port, b"", send_content_length=False
            )
            self.assertEqual(status, 400)
            self.assertEqual(raw, REQUEST_BODY_MISMATCH)
        finally:
            sock.close()

    def test_keep_alive_reused_after_400_and_200(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=REQUEST_TIMEOUT)
        try:
            sequence = [
                (b'{"ok":1,"amount":1}', 400),
                (b'{"ok":true,"amount":1}', 200),
                (b"{", 400),
                (b'garbage', 400),
                (b'{"amount":1.0,"ok":true}', 200),
            ]
            for raw_body, expected in sequence:
                with self.subTest(body=raw_body):
                    conn.request("POST", "/check", body=raw_body)
                    resp = conn.getresponse()
                    raw = resp.read()
                    self.assertEqual(resp.status, expected)
                    self.assertEqual(
                        resp.getheader("Content-Type"), CONTENT_TYPE
                    )
                    self.assertEqual(
                        int(resp.getheader("Content-Length")), len(raw)
                    )
        finally:
            conn.close()

    def test_response_validated_only_after_entire_body_read(self):
        # 只发一半正文时服务必须保持静默；补齐后（此处整体为 JSON 语法
        # 错误）才返回 400
        partial_body = b'{"ok":1'  # 合法前缀但不完整
        sock = socket.create_connection(
            ("127.0.0.1", self.port), timeout=REQUEST_TIMEOUT
        )
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        selector = DefaultSelector()
        selector.register(sock, EVENT_READ)
        try:
            sock.sendall(
                (f"POST /check HTTP/1.1\r\n"
                 f"Host: 127.0.0.1:{self.port}\r\n"
                 f"Content-Length: {len(partial_body) + 3}\r\n"
                 f"\r\n").encode("ascii") + partial_body
            )
            ready = selector.select(timeout=PARTIAL_PRE_WAIT_SECONDS)
            if ready:
                self.fail(
                    f"正文未读完时不应响应，实际收到 {sock.recv(4096)!r}"
                )
            # 补齐 3 字节，整体 '{"ok":1???' 为 JSON 语法错误 -> 400
            sock.sendall(b"???")
            chunks = []
            sock.settimeout(PARTIAL_RESPONSE_TIMEOUT)
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                data = b"".join(chunks)
                head, _, body_bytes = data.partition(b"\r\n\r\n")
                if head and len(body_bytes) >= len(REQUEST_BODY_MISMATCH):
                    break
            data = b"".join(chunks)
            self.assertIn(b"HTTP/1.1 400", data)
            self.assertTrue(
                data.endswith(REQUEST_BODY_MISMATCH),
                f"应返回 request_body_mismatch，实际 {data!r}",
            )
        finally:
            selector.close()
            sock.close()

    def test_other_methods_keep_501_behavior_on_guarded_route(self):
        # PUT/HEAD 在配置了 requestBody 的路径上仍走既有 501 流程
        status, headers, raw = request(
            self.port, "PUT", "/check", body=b'{"amount":1,"ok":true}'
        )
        self.assertEqual(status, 501)
        self.assertEqual(headers.get("Connection"), "close")
        self.assertEqual(raw, METHOD_REJECT_BODY)

        status, headers, raw = request(self.port, "HEAD", "/guarded")
        self.assertEqual(status, 501)
        self.assertEqual(raw, b"")
        self.assertEqual(
            int(headers["Content-Length"]), len(METHOD_REJECT_BODY)
        )

    def test_get_route_unaffected(self):
        status, headers, raw = request(self.port, "GET", "/hello")
        self.assertEqual(status, 200)
        self.assertEqual(raw, '{"message":"你好"}'.encode("utf-8"))


class ScalarAndNestedRequestBodyTests(unittest.TestCase):
    """标量与嵌套样例：字符串大小写、数组顺序、嵌套结构逐值比较。"""

    def test_scalar_string_number_and_array_samples(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_request_body_scalars.json",
                [
                    {"method": "POST", "path": "/s", "requestBody": "HeLLo",
                     "body": {"ok": "string"}},
                    {"method": "POST", "path": "/n", "requestBody": 42,
                     "body": {"ok": "number"}},
                    {"method": "POST", "path": "/a",
                     "requestBody": [1, True, "x", {"k": [2, 3]}],
                     "body": {"ok": "array"}},
                ],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                cases = [
                    ("/s", b'"HeLLo"', 200),
                    ("/s", b'"hello"', 400),     # 字符串区分大小写
                    ("/s", b'"HeLLo "', 400),    # 多余字符
                    ("/n", b"42", 200),
                    ("/n", b"42.0", 200),        # 数值相等
                    ("/n", b"true", 400),        # 布尔不等于数字
                    ("/n", b'"42"', 400),
                    ("/a", b'[1,true,"x",{"k":[2,3]}]', 200),
                    ("/a", b'[1,true,"x",{"k":[3,2]}]', 400),  # 数组顺序
                    ("/a", b'[1,true,"x"]', 400),              # 长度不同
                    ("/a", b'[1,true,"x",{"k":[2,3]},9]', 400),
                    ("/a", b'{"0":1}', 400),
                ]
                for target, raw_body, expected in cases:
                    with self.subTest(target=target, body=raw_body):
                        status, headers, raw = request(
                            port, "POST", target, body=raw_body
                        )
                        self.assertEqual(
                            status, expected,
                            f"POST {target} 正文 {raw_body!r}: 期望 "
                            f"{expected}，实际 {status}；{raw!r}",
                        )
            finally:
                server.stop()


class JsonEqualityHelperTests(unittest.TestCase):
    """直接锁定递归比较语义（无需启动服务）。"""

    def test_comparison_semantics(self):
        from mock_server import _json_equal

        equal_pairs = [
            ({"a": 1, "b": True}, {"b": True, "a": 1.0}),
            ({"a": {"b": [1, 2, 3]}}, {"a": {"b": [1.0, 2, 3]}}),
            ([1, "x", None, False], [1, "x", None, False]),
            ("Case", "Case"),
            (1, 1.0),
            (0, 0.0),
            (-0.0, 0),
            (None, None),
            (True, True),
            ([], []),
            ({}, {}),
        ]
        for expected, actual in equal_pairs:
            with self.subTest(pair=(expected, actual)):
                self.assertTrue(
                    _json_equal(expected, actual),
                    f"{expected!r} 应等于 {actual!r}",
                )

        unequal_pairs = [
            ({"a": 1}, {"a": 1, "b": 2}),
            ({"a": 1, "b": 2}, {"a": 1}),
            ([1, 2], [2, 1]),
            ([1], [1, 0]),
            ("Case", "case"),
            (1, True),
            (0, False),
            (True, 1),
            (False, 0.0),
            (None, False),
            (None, 0),
            ("1", 1),
            ([1], {"0": 1}),
            ({"a": None}, {"a": False}),
            (1.5, 1),
        ]
        for expected, actual in unequal_pairs:
            with self.subTest(pair=(expected, actual)):
                self.assertFalse(
                    _json_equal(expected, actual),
                    f"{expected!r} 不应等于 {actual!r}",
                )


# requestBody 规则加载校验：GET 路由携带 requestBody，或样例字符串/键无法
# 编码为 UTF-8 时，load_rules 抛 RulesError，CLI 退出码 2 且不监听
INVALID_REQUEST_BODY_RULE_CASES = [
    # (说明, 路由项, 非法项下标)
    (
        "GET 路由携带 requestBody（位于 routes[0]）",
        {"method": "GET", "path": "/g", "requestBody": {"v": 1}, "body": {}},
        0,
    ),
    (
        "GET 路由携带 requestBody（合法路由之后的 routes[1]）",
        {"method": "GET", "path": "/g", "requestBody": None, "body": {}},
        1,
    ),
    (
        "样例字符串含孤立高代理",
        {"method": "POST", "path": "/p",
         "requestBody": "a\ud800b", "body": {}},
        0,
    ),
    (
        "嵌套样例字符串含孤立低代理",
        {"method": "POST", "path": "/p",
         "requestBody": {"outer": ["\udc00"]}, "body": {}},
        0,
    ),
    (
        "样例对象键含孤立代理",
        {"method": "POST", "path": "/p",
         "requestBody": {"\ud800": 1}, "body": {}},
        0,
    ),
    (
        "数组样例中的对象键含孤立代理",
        {"method": "POST", "path": "/p",
         "requestBody": [{"\ud800": 1}], "body": {}},
        0,
    ),
]


class RequestBodyRulesValidationTests(unittest.TestCase):
    """requestBody 只能用于 POST；样例字符串与对象键须可编码为 UTF-8。"""

    def _items_for(self, bad_item, bad_index):
        if bad_index == 0:
            return [bad_item]
        return [
            {"method": "GET", "path": "/ok", "body": {"fine": 1}},
            bad_item,
        ]

    def _write_invalid_rules(self, tmp, name, bad_item, bad_index):
        # ensure_ascii 默认输出 \ud800 形式的 ASCII 转义：测试文件本身可
        # 编码为 UTF-8，而服务端解析后仍得到含孤立代理码点的字符串
        return write_rules_text(
            tmp,
            name,
            json.dumps({"routes": self._items_for(bad_item, bad_index)}),
        )

    def test_load_rules_rejects_invalid_request_body(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, bad_item, bad_index) in enumerate(
                INVALID_REQUEST_BODY_RULE_CASES
            ):
                with self.subTest(样例=label):
                    rules_path = self._write_invalid_rules(
                        tmp, f"rules_bad_request_body_{index}.json",
                        bad_item, bad_index,
                    )
                    with self.assertRaises(RulesError) as ctx:
                        load_rules(rules_path)
                    message = str(ctx.exception)
                    self.assertIn(
                        f"routes[{bad_index}].requestBody", message,
                        f"样例 {label}: 错误应标明 routes[{bad_index}]"
                        f".requestBody，实际 {message!r}",
                    )

    def test_surrogate_cases_mention_utf8(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, bad_item, bad_index) in enumerate(
                INVALID_REQUEST_BODY_RULE_CASES
            ):
                if "代理" not in label:
                    continue
                with self.subTest(样例=label):
                    rules_path = self._write_invalid_rules(
                        tmp, f"rules_bad_request_body_sur_{index}.json",
                        bad_item, bad_index,
                    )
                    with self.assertRaises(RulesError) as ctx:
                        load_rules(rules_path)
                    self.assertIn("UTF-8", str(ctx.exception))

    def test_cli_rejects_with_exit_code_2_and_location(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, bad_item, bad_index) in enumerate(
                INVALID_REQUEST_BODY_RULE_CASES
            ):
                with self.subTest(样例=label):
                    rules_path = self._write_invalid_rules(
                        tmp, f"rules_bad_request_body_cli_{index}.json",
                        bad_item, bad_index,
                    )
                    returncode, stdout, stderr = start_and_wait_exit(
                        rules_path, free_port()
                    )
                    self.assertEqual(
                        returncode, 2,
                        f"样例 {label}: 期望退出码 2，实际 {returncode}；"
                        f"stdout={stdout!r} stderr={stderr!r}",
                    )
                    self.assertIn(
                        f"routes[{bad_index}].requestBody", stderr,
                        f"样例 {label}: 标准错误应包含路由位置与字段名，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertNotIn("Traceback", stderr)
                    self.assertNotIn(STARTUP_MARKER, stdout)

    def test_request_body_accepts_any_json_value_type(self):
        # 显式 null、数字、字符串、布尔、数组、对象样例均可加载
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            samples = [None, 1, "x", True, False, [1, 2], {"k": "v"}, []]
            routes = [
                {"method": "POST", "path": f"/s{i}",
                 "requestBody": sample, "body": {"i": i}}
                for i, sample in enumerate(samples)
            ]
            rules_path = write_rules(tmp, "rules_rb_types.json", routes)
            loaded = load_rules(rules_path)
            for i, sample in enumerate(samples):
                self.assertEqual(
                    loaded.request_bodies[("POST", f"/s{i}")], sample
                )

    def test_duplicate_routes_with_request_body_still_rejected(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_rb_dup.json",
                [
                    {"method": "POST", "path": "/same",
                     "requestBody": {"a": 1}, "body": {}},
                    {"method": "POST", "path": "/same",
                     "requestBody": {"a": 2}, "body": {"x": 1}},
                ],
            )
            with self.assertRaises(RulesError) as ctx:
                load_rules(rules_path)
            self.assertIn("duplicate route", str(ctx.exception))
            self.assertIn("routes[1]", str(ctx.exception))

    def test_paired_surrogate_and_chinese_sample_load_and_match(self):
        # 与 body 相同：正确配对的代理与中文样例合法，且可端到端匹配
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules_text(
                tmp,
                "rules_rb_unicode.json",
                '{"routes":[{"method":"POST","path":"/u",'
                '"requestBody":{"face":"\\ud83d\\ude00","text":"你好"},'
                '"body":{"ok":true}}]}',
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                raw_body = (
                    '{"text":"你好","face":"😀"}'
                ).encode("utf-8")
                status, headers, raw = request(
                    port, "POST", "/u", body=raw_body
                )
                self.assertEqual(status, 200)
                self.assertEqual(raw, b'{"ok":true}')
                # 表情符号不匹配时同样 400
                status, _, raw = request(
                    port, "POST", "/u",
                    body='{"text":"你好","face":"x"}'.encode("utf-8"),
                )
                self.assertEqual(status, 400)
                self.assertEqual(raw, REQUEST_BODY_MISMATCH)
            finally:
                server.stop()


class RequestBodyRemovalTests(unittest.TestCase):
    """移除样例后，原本匹配与不匹配的两种正文都应返回固定响应。"""

    def test_removing_request_body_makes_both_bodies_pass(self):
        route_with_sample = {
            "method": "POST", "path": "/check",
            "requestBody": {"amount": 1, "ok": True},
            "body": {"accepted": True},
        }
        route_without_sample = {
            "method": "POST", "path": "/check",
            "body": {"accepted": True},
        }
        bodies = [
            ("数值/布尔匹配", b'{"ok":true,"amount":1.0}'),
            ("ok 为数字（不匹配）", b'{"ok":1,"amount":1.0}'),
        ]
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            with_sample = write_rules(
                tmp, "rules_with_sample.json", [route_with_sample]
            )
            without_sample = write_rules(
                tmp, "rules_without_sample.json", [route_without_sample]
            )
            port_a = free_port()
            port_b = free_port()
            guarded = ServerProcess(with_sample, port_a)
            loose = ServerProcess(without_sample, port_b)
            try:
                expectations = {
                    port_a: [200, 400],
                    port_b: [200, 200],
                }
                for port, expected_statuses in expectations.items():
                    for (label, raw_body), expected in zip(
                        bodies, expected_statuses
                    ):
                        with self.subTest(port=port, 正文=label):
                            status, headers, raw = request(
                                port, "POST", "/check", body=raw_body
                            )
                            self.assertEqual(
                                status, expected,
                                f"端口 {port} 正文 {label}: 期望 "
                                f"{expected}，实际 {status}",
                            )
                            if expected == 200:
                                self.assertEqual(raw, b'{"accepted":true}')
                            else:
                                self.assertEqual(raw, REQUEST_BODY_MISMATCH)
            finally:
                guarded.stop()
                loose.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
