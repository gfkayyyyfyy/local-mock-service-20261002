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
    """合法 status：200、201、400、599 均可加载并原样返回。"""

    def test_boundary_statuses_load_and_return(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            routes = [
                {"method": "GET", "path": f"/s{status}",
                 "status": status, "body": {"status": status}}
                for status in (200, 201, 400, 599)
            ]
            rules_path = write_rules(tmp, "rules_boundary.json", routes)
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                for status in (200, 201, 400, 599):
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


# 201 创建成功响应的专用规则：POST /items 要求正文 {"ok": true}，
# 校验通过返回 201 与 {"id": 1}；另一条 GET 路由证明 GET 同样可配置 201
CREATED_ROUTES = [
    {"method": "POST", "path": "/items",
     "requestBody": {"ok": True},
     "status": 201, "body": {"id": 1}},
    {"method": "GET", "path": "/created",
     "status": 201, "body": {"id": 2}},
]


class Status201BehaviorTests(unittest.TestCase):
    """status=201：GET 与 POST 均可配置；POST 仅在正文样例校验通过后
    返回 201 与配置正文，校验失败仍返回 400 且不采用配置的状态、正文
    或延迟。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rules_path = write_rules(
            cls._tmp.name, "rules_status_201.json", CREATED_ROUTES
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def test_post_matching_body_returns_201_created(self):
        # 任务约定场景：POST /items 正文 {"ok":true} -> 201 {"id":1}
        status, headers, raw = request(
            self.port, "POST", "/items", body=b'{"ok":true}'
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
        self.assertEqual(raw, b'{"id":1}')
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_get_route_can_also_be_configured_with_201(self):
        status, headers, raw = request(self.port, "GET", "/created")
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
        self.assertEqual(raw, b'{"id":2}')
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_post_mismatching_body_still_returns_400(self):
        # {"ok":false} 与样例不相等：400 固定错误正文，不采用配置的
        # 201 与 {"id":1}
        for label, raw_body in [
            ("ok 为 false", b'{"ok":false}'),
            ("缺少 ok 键", b'{}'),
            ("ok 为数字 1", b'{"ok":1}'),
            ("非法 JSON（孤立左花括号）", b"{"),
            ("空正文", b""),
            ("NaN 字面量", b'{"ok":NaN}'),
            ("非法 UTF-8", b"\xff"),
        ]:
            with self.subTest(正文=label):
                code, headers, resp_raw = request(
                    self.port, "POST", "/items", body=raw_body
                )
                self.assertEqual(code, 400)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(resp_raw, REQUEST_BODY_MISMATCH)
                self.assertEqual(
                    int(headers["Content-Length"]), len(resp_raw)
                )

    def test_omitted_status_still_defaults_to_200(self):
        # 省略 status 的行为不变：另起一份只有默认状态的规则快速核对
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            path = write_rules(
                tmp,
                "rules_status_default.json",
                [{"method": "GET", "path": "/d", "body": {"ok": True}}],
            )
            port = free_port()
            server = ServerProcess(path, port)
            try:
                status, _, raw = request(port, "GET", "/d")
                self.assertEqual(status, 200)
                self.assertEqual(raw, b'{"ok":true}')
            finally:
                server.stop()

    def test_201_route_uses_template_and_delay_semantics(self):
        # 201 路由沿用既有模板与延迟语义：模板只渲染最终选中的路由，
        # 延迟只在正文读取完毕且样例校验通过后应用；响应不附加任何字段
        delay_ms = 200
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            path = write_rules(
                tmp,
                "rules_status_201_delay.json",
                [
                    {"method": "POST", "path": "/items",
                     "requestBody": {"ok": True},
                     "status": 201, "delayMs": delay_ms,
                     "bodyMode": "template",
                     "body": {"id": 1, "made": "{{request.method}}"}},
                ],
            )
            port = free_port()
            server = ServerProcess(path, port)
            try:
                # 校验通过：等满延迟后返回 201 与渲染后的模板正文
                status, headers, raw, elapsed = timed_request(
                    port, "POST", "/items?x=1", body=b'{"ok":true}'
                )
                self.assertGreaterEqual(
                    elapsed, delay_ms / 1000 - 0.02,
                    f"201 路由校验通过后仍应等待 {delay_ms}ms，实际 "
                    f"{elapsed * 1000:.1f}ms",
                )
                self.assertEqual(status, 201)
                self.assertEqual(raw, b'{"id":1,"made":"POST"}')
                self.assertEqual(int(headers["Content-Length"]), len(raw))
                # 校验失败：立即 400，不等待、不使用 201 或模板正文
                start = time.monotonic()
                status, headers, raw = request(
                    port, "POST", "/items", body=b'{"ok":false}'
                )
                failed_elapsed = time.monotonic() - start
                self.assertLess(
                    failed_elapsed, delay_ms / 1000 - 0.05,
                    "正文校验失败时不应应用延迟，实际 "
                    f"{failed_elapsed * 1000:.1f}ms",
                )
                self.assertEqual(status, 400)
                self.assertEqual(raw, REQUEST_BODY_MISMATCH)
            finally:
                server.stop()

    def test_check_rules_accepts_single_201_route(self):
        # 现有 --check-rules 入口检查任务中的 create.json 形态：
        # 只输出一行 mock_server rules valid (1 route(s))，退出码 0，
        # 不监听或探测端口
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            path = write_rules(
                tmp, "create.json", [CREATED_ROUTES[0]]
            )
            with occupied_local_port() as occupied:
                returncode, stdout, stderr, elapsed = run_check_rules(
                    path, port=occupied.port
                )
            self.assertEqual(
                returncode, 0,
                f"含 201 路由的规则应通过检查，实际退出码 {returncode}；"
                f"stdout={stdout!r} stderr={stderr!r}",
            )
            self.assertEqual(stdout, "mock_server rules valid (1 route(s))\n")
            self.assertEqual(stderr, "")
            self.assertLess(elapsed, CHECK_RULES_NO_WAIT_MAX_SECONDS)


INVALID_STATUSES = [
    ("整数 199（低于下限）", 199),
    ("整数 202（2xx 仅接受 200 与 201）", 202),
    ("整数 204（2xx 仅接受 200 与 201）", 204),
    ("整数 300（3xx 不接受）", 300),
    ("整数 399（3xx 不接受）", 399),
    ("整数 600（高于上限）", 600),
    ("布尔 true", True),
    ("布尔 false", False),
    ("null", None),
    ('字符串 "503"', "503"),
    ('字符串 "201"', "201"),
    ("浮点数 503.0", 503.0),
    ("浮点数 201.0", 201.0),
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


# ---------------------------------------------------------------------------
# requestBodyMode 子集匹配回归
#
# 路由项可选的 requestBodyMode 仅允许与 POST 路由的显式 requestBody 一起
# 出现（null 样例也算存在），取值为区分大小写的 "exact" 或 "subset"：
# 省略或 "exact" 保持整体相等比较；"subset" 要求样例对象的所有键存在于
# 请求对应对象中、值递归按同一模式比较，请求对象允许额外键；数组仍按
# 相同长度与顺序比较（元素对象同样允许额外键），不接受前缀匹配。缺键、
# 被约束值不等或类型不符一律 400 request_body_mismatch，不采用配置的
# 状态、正文或延迟；完整正文仍须通过 UTF-8、JSON 语法与非有限数字检查。
# ---------------------------------------------------------------------------

# 行为测试使用的规则（对应任务验收场景）：
#   POST /check   subset 样例 {"user":{"id":1},"tags":["a"]}，503 + 配置正文
#   POST /exact   同样样例但不配置模式（省略即 exact），用于对照
#   POST /anyobj  subset 样例 {"meta":{}}，空对象只匹配对象
#   POST /guarded subset 样例 {"v":1}，配置 503 + 200ms 延迟（失败时不得使用）
SUBSET_MODE_RULES = [
    {"method": "POST", "path": "/check", "requestBodyMode": "subset",
     "requestBody": {"user": {"id": 1}, "tags": ["a"]},
     "status": 503, "body": {"accepted": True}},
    {"method": "POST", "path": "/exact",
     "requestBody": {"user": {"id": 1}, "tags": ["a"]},
     "status": 503, "body": {"accepted": True}},
    {"method": "POST", "path": "/anyobj", "requestBodyMode": "subset",
     "requestBody": {"meta": {}}, "body": {"ok": True}},
    {"method": "POST", "path": "/guarded", "requestBodyMode": "subset",
     "delayMs": DELAY_MS, "status": 503,
     "requestBody": {"v": 1}, "body": {"error": "demo_failure"}},
]


class SubsetRequestBodyTests(unittest.TestCase):
    """subset 模式：额外键放行，缺键/值不等/类型不符一律 400。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rules_path = write_rules(
            cls._tmp.name, "rules_subset_mode.json", SUBSET_MODE_RULES
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def _assert_status_and_body(self, label, target, raw_body,
                                expected_status, expected_raw):
        status, headers, raw = request(
            self.port, "POST", target, body=raw_body
        )
        self.assertEqual(
            status, expected_status,
            f"样例 {label}: 状态码应为 {expected_status}，实际 {status}；"
            f"响应={raw!r}",
        )
        self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
        self.assertEqual(raw, expected_raw, f"样例 {label}: 响应体不符")
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def _assert_400_mismatch(self, label, raw_body, target="/check"):
        self._assert_status_and_body(
            label, target, raw_body, 400, REQUEST_BODY_MISMATCH
        )

    def test_task_acceptance_body_with_extra_keys_returns_configured_503(self):
        # 任务验收正文：id 写为 1.0、user 与顶层均有额外键、含中文
        self._assert_status_and_body(
            "验收正文",
            "/check",
            '{"user":{"id":1.0,"name":"甲"},"tags":["a"],'
            '"trace":"demo"}'.encode("utf-8"),
            503, b'{"accepted":true}',
        )

    def test_task_acceptance_rejections(self):
        # 任务验收：id 改为 true（布尔不等于数字）或 tags 多一个元素 -> 400
        self._assert_400_mismatch(
            "id 为布尔 true", b'{"user":{"id":true},"tags":["a"]}'
        )
        self._assert_400_mismatch(
            "tags 多一个元素", b'{"user":{"id":1},"tags":["a","b"]}'
        )

    def test_subset_matching_and_mismatching_bodies(self):
        matching = [
            ("完全一致", b'{"user":{"id":1},"tags":["a"]}'),
            ("键序不同", b'{"tags":["a"],"user":{"id":1}}'),
            ("仅顶层额外键", b'{"user":{"id":1},"tags":["a"],"x":null}'),
            ("仅嵌套额外键", b'{"user":{"id":1,"extra":[1,2]},"tags":["a"]}'),
            ("数组元素对象允许额外键",
             b'{"user":{"id":1},"tags":["a"],"unused":1}'),
        ]
        for label, raw_body in matching:
            with self.subTest(正文=label):
                self._assert_status_and_body(
                    label, "/check", raw_body, 503, b'{"accepted":true}'
                )
        mismatching = [
            ("缺少 user 键", b'{"tags":["a"]}'),
            ("缺少嵌套 id 键", b'{"user":{},"tags":["a"]}'),
            ("缺少 tags 键", b'{"user":{"id":1}}'),
            ("id 数值不同", b'{"user":{"id":2},"tags":["a"]}'),
            ("id 为字符串", b'{"user":{"id":"1"},"tags":["a"]}'),
            ("tags 少元素（数组不接受前缀匹配）",
             b'{"user":{"id":1},"tags":[]}'),
            ("tags 元素不同", b'{"user":{"id":1},"tags":["A"]}'),
            ("tags 不是数组", b'{"user":{"id":1},"tags":"a"}'),
            ("user 不是对象", b'{"user":1,"tags":["a"]}'),
            ("整体不是对象", b'[{"user":{"id":1},"tags":["a"]}]'),
            ("整体为 null", b'null'),
        ]
        for label, raw_body in mismatching:
            with self.subTest(正文=label):
                self._assert_400_mismatch(label, raw_body)

    def test_empty_object_sample_matches_any_object_only(self):
        for label, raw_body, expected in [
            ("空对象", b'{"meta":{}}', 200),
            ("meta 为非空对象", b'{"meta":{"a":1,"b":[2]}}', 200),
            ("顶层额外键", b'{"meta":{},"extra":true}', 200),
            ("meta 为 null", b'{"meta":null}', 400),
            ("meta 为数组", b'{"meta":[]}', 400),
            ("meta 为字符串", b'{"meta":"{}"}', 400),
            ("缺少 meta 键", b'{}', 400),
        ]:
            with self.subTest(正文=label):
                status, headers, raw = request(
                    self.port, "POST", "/anyobj", body=raw_body
                )
                self.assertEqual(status, expected, f"样例 {label}")
                if expected == 200:
                    self.assertEqual(raw, b'{"ok":true}')
                else:
                    self.assertEqual(raw, REQUEST_BODY_MISMATCH)

    def test_array_element_objects_allow_extra_keys_but_no_prefix(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_subset_array.json",
                [
                    {"method": "POST", "path": "/arr",
                     "requestBodyMode": "subset",
                     "requestBody": [{"id": 1}, {"id": 2}],
                     "body": {"ok": True}},
                ],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                for label, raw_body, expected in [
                    ("元素对象带额外键",
                     b'[{"id":1,"x":1},{"id":2,"y":2}]', 200),
                    ("完全一致", b'[{"id":1},{"id":2}]', 200),
                    ("长度不同（不接受前缀）", b'[{"id":1}]', 400),
                    ("顺序不同", b'[{"id":2},{"id":1}]', 400),
                    ("元素缺键", b'[{"id":1},{"x":2}]', 400),
                ]:
                    with self.subTest(正文=label):
                        status, headers, raw = request(
                            port, "POST", "/arr", body=raw_body
                        )
                        self.assertEqual(status, expected, f"样例 {label}")
            finally:
                server.stop()

    def test_full_body_checks_still_apply_to_extra_fields(self):
        # 额外字段中的非法内容同样令整体 400：额外键不能绕过检查
        for label, raw_body in [
            ("额外字段含 NaN 字面量",
             b'{"user":{"id":1},"tags":["a"],"extra":NaN}'),
            ("额外字段含溢出数字 1e400",
             b'{"user":{"id":1},"tags":["a"],"extra":1e400}'),
            ("非法 UTF-8", b"\xff\xfe"),
            ("JSON 语法错误", b'{"user":{"id":1},"tags":["a"],'),
            ("空正文", b""),
        ]:
            with self.subTest(正文=label):
                self._assert_400_mismatch(label, raw_body)

    def test_mismatch_skips_configured_status_body_and_delay(self):
        # subset 校验失败：立即 400，不等待 200ms、不使用配置的 503 与正文
        start = time.monotonic()
        status, headers, raw = request(
            self.port, "POST", "/guarded", body=b'{"v":1,"extra":1,"bad":'
        )
        elapsed = time.monotonic() - start
        self.assertEqual(status, 400)
        self.assertEqual(raw, REQUEST_BODY_MISMATCH)
        self.assertLess(
            elapsed, NO_DELAY_MAX_SECONDS,
            f"不匹配时不应应用 {DELAY_MS}ms 延迟，实际 {elapsed * 1000:.1f}ms",
        )
        # 校验通过（含额外键）：仍按原规则应用延迟并返回配置响应
        status, headers, raw, elapsed = timed_request(
            self.port, "POST", "/guarded", body=b'{"v":1.0,"extra":"ok"}'
        )
        self.assertGreaterEqual(elapsed, DELAY_MIN_SECONDS)
        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(raw.decode("utf-8")), {"error": "demo_failure"}
        )

    def test_exact_mode_unchanged_when_mode_omitted(self):
        # 删除模式字段后（/exact 未配置 requestBodyMode）：带额外键 -> 400
        self._assert_400_mismatch(
            "exact 模式带额外键",
            b'{"user":{"id":1},"tags":["a"],"trace":"demo"}',
            target="/exact",
        )
        # 完全相等仍放行
        self._assert_status_and_body(
            "exact 模式完全相等",
            "/exact",
            b'{"user":{"id":1.0},"tags":["a"]}',
            503, b'{"accepted":true}',
        )


class JsonSubsetHelperTests(unittest.TestCase):
    """直接锁定子集匹配语义（无需启动服务）。"""

    def test_subset_semantics(self):
        from mock_server import _json_subset

        matching_pairs = [
            ({}, {}),
            ({}, {"a": 1}),
            ({"a": 1}, {"a": 1.0, "b": 2}),
            ({"a": {"b": 1}}, {"a": {"b": 1, "c": 2}, "d": 3}),
            ([{"a": 1}], [{"a": 1, "b": 2}]),
            ([1, "x"], [1.0, "x"]),
            ({"t": [1, {"k": None}]}, {"t": [1, {"k": None, "j": 0}]}),
            ({"s": "Case"}, {"s": "Case"}),
            ({"n": None}, {"n": None}),
            ({"b": True}, {"b": True}),
            ([], []),
        ]
        for expected, actual in matching_pairs:
            with self.subTest(pair=(expected, actual)):
                self.assertTrue(
                    _json_subset(expected, actual),
                    f"{expected!r} 应是 {actual!r} 的子集",
                )

        non_matching_pairs = [
            ({"a": 1}, {}),                    # 缺键
            ({"a": 1}, {"a": 2}),              # 被约束值不等
            ({"a": 1}, {"a": True}),           # 布尔不等于数字
            ({"a": True}, {"a": 1}),
            ({"a": 1}, {"a": "1"}),            # 类型不符
            ({"a": None}, {"a": 0}),           # null 只匹配 null
            ({"a": None}, {"a": False}),
            ({"s": "Case"}, {"s": "case"}),    # 字符串区分大小写
            ({}, []),                          # 空对象只匹配对象
            ({}, None),
            ([1], [1, 2]),                     # 数组不接受前缀匹配
            ([1, 2], [1]),
            ([1, 2], [2, 1]),                  # 数组顺序参与比较
            ([{"a": 1}], [{"b": 1}]),          # 数组元素对象缺键
            ({"a": {"b": 1}}, {"a": {"c": 1}}),
            ({"a": {}}, {"a": []}),            # 嵌套空对象只匹配对象
            (1, True),
            ("1", 1),
        ]
        for expected, actual in non_matching_pairs:
            with self.subTest(pair=(expected, actual)):
                self.assertFalse(
                    _json_subset(expected, actual),
                    f"{expected!r} 不应是 {actual!r} 的子集",
                )


# requestBodyMode 规则加载校验：未与 POST 路由的显式 requestBody 一起
# 出现，或取值不是区分大小写的 "exact"/"subset"，load_rules 抛
# RulesError，CLI 退出码 2 且不监听
INVALID_REQUEST_BODY_MODE_CASES = [
    # (说明, 路由项, 非法项下标)
    (
        "POST 路由缺少 requestBody（位于 routes[0]）",
        {"method": "POST", "path": "/p",
         "requestBodyMode": "subset", "body": {}},
        0,
    ),
    (
        "POST 路由缺少 requestBody（合法路由之后的 routes[1]）",
        {"method": "POST", "path": "/p",
         "requestBodyMode": "exact", "body": {}},
        1,
    ),
    (
        "GET 路由携带 requestBodyMode（无 requestBody）",
        {"method": "GET", "path": "/g",
         "requestBodyMode": "subset", "body": {}},
        0,
    ),
    (
        "GET 路由同时携带 requestBody 与 requestBodyMode",
        {"method": "GET", "path": "/g", "requestBody": {"v": 1},
         "requestBodyMode": "subset", "body": {}},
        0,
    ),
    (
        "取值为小写开头的其他字符串",
        {"method": "POST", "path": "/p", "requestBody": {"v": 1},
         "requestBodyMode": "sub", "body": {}},
        0,
    ),
    (
        "取值大小写不符（Subset）",
        {"method": "POST", "path": "/p", "requestBody": {"v": 1},
         "requestBodyMode": "Subset", "body": {}},
        0,
    ),
    (
        "取值大小写不符（EXACT）",
        {"method": "POST", "path": "/p", "requestBody": {"v": 1},
         "requestBodyMode": "EXACT", "body": {}},
        0,
    ),
    (
        "取值为 null",
        {"method": "POST", "path": "/p", "requestBody": {"v": 1},
         "requestBodyMode": None, "body": {}},
        0,
    ),
    (
        "取值为布尔",
        {"method": "POST", "path": "/p", "requestBody": {"v": 1},
         "requestBodyMode": True, "body": {}},
        0,
    ),
    (
        "取值为数字",
        {"method": "POST", "path": "/p", "requestBody": {"v": 1},
         "requestBodyMode": 1, "body": {}},
        0,
    ),
    (
        "取值为数组",
        {"method": "POST", "path": "/p", "requestBody": {"v": 1},
         "requestBodyMode": ["subset"], "body": {}},
        0,
    ),
]


class RequestBodyModeRulesValidationTests(unittest.TestCase):
    """requestBodyMode 只能与 POST 路由的显式 requestBody 一起出现。"""

    def _items_for(self, bad_item, bad_index):
        if bad_index == 0:
            return [bad_item]
        return [
            {"method": "GET", "path": "/ok", "body": {"fine": 1}},
            bad_item,
        ]

    def _write_invalid_rules(self, tmp, name, bad_item, bad_index):
        return write_rules(
            tmp, name, self._items_for(bad_item, bad_index)
        )

    def test_load_rules_rejects_invalid_request_body_mode(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, bad_item, bad_index) in enumerate(
                INVALID_REQUEST_BODY_MODE_CASES
            ):
                with self.subTest(样例=label):
                    rules_path = self._write_invalid_rules(
                        tmp, f"rules_bad_mode_{index}.json",
                        bad_item, bad_index,
                    )
                    with self.assertRaises(
                        RulesError,
                        msg=f"样例 {label}: load_rules 应抛出 RulesError",
                    ) as ctx:
                        load_rules(rules_path)
                    message = str(ctx.exception)
                    self.assertIn(
                        f"routes[{bad_index}].requestBodyMode", message,
                        f"样例 {label}: 错误应标明 routes[{bad_index}]"
                        f".requestBodyMode，实际 {message!r}",
                    )

    def test_cli_rejects_with_exit_code_2_and_location(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, bad_item, bad_index) in enumerate(
                INVALID_REQUEST_BODY_MODE_CASES
            ):
                with self.subTest(样例=label):
                    rules_path = self._write_invalid_rules(
                        tmp, f"rules_bad_mode_cli_{index}.json",
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
                        f"routes[{bad_index}].requestBodyMode", stderr,
                        f"样例 {label}: 标准错误应包含路由位置与字段名，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertNotIn("Traceback", stderr)
                    self.assertNotIn(STARTUP_MARKER, stdout)

    def test_valid_modes_load(self):
        # 省略、显式 "exact"、"subset" 均可加载；null 样例也算显式存在
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            routes = [
                {"method": "POST", "path": "/omit",
                 "requestBody": {"a": 1}, "body": {}},
                {"method": "POST", "path": "/exact",
                 "requestBody": {"a": 1}, "requestBodyMode": "exact",
                 "body": {}},
                {"method": "POST", "path": "/subset",
                 "requestBody": {"a": 1}, "requestBodyMode": "subset",
                 "body": {}},
                {"method": "POST", "path": "/nullish",
                 "requestBody": None, "requestBodyMode": "subset",
                 "body": {}},
            ]
            rules_path = write_rules(tmp, "rules_modes_ok.json", routes)
            loaded = load_rules(rules_path)
            self.assertEqual(
                loaded.request_body_modes[("POST", "/omit")], "exact"
            )
            self.assertEqual(
                loaded.request_body_modes[("POST", "/exact")], "exact"
            )
            self.assertEqual(
                loaded.request_body_modes[("POST", "/subset")], "subset"
            )
            self.assertEqual(
                loaded.request_body_modes[("POST", "/nullish")], "subset"
            )
            self.assertIn(None, [loaded.request_bodies[("POST", "/nullish")]])


class RequestBodyModeRemovalTests(unittest.TestCase):
    """删除模式字段后，带额外键的正文应回到整体相等语义（400）。"""

    def test_removing_mode_restores_exact_semantics(self):
        sample = {"user": {"id": 1}, "tags": ["a"]}
        route_with_mode = {
            "method": "POST", "path": "/check",
            "requestBody": sample, "requestBodyMode": "subset",
            "body": {"accepted": True},
        }
        route_without_mode = {
            "method": "POST", "path": "/check",
            "requestBody": sample,
            "body": {"accepted": True},
        }
        body_with_extra = (
            b'{"user":{"id":1.0,"name":"\xe7\x94\xb2"},'
            b'"tags":["a"],"trace":"demo"}'
        )
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            with_mode = write_rules(
                tmp, "rules_with_mode.json", [route_with_mode]
            )
            without_mode = write_rules(
                tmp, "rules_without_mode.json", [route_without_mode]
            )
            port_a = free_port()
            port_b = free_port()
            subset_server = ServerProcess(with_mode, port_a)
            exact_server = ServerProcess(without_mode, port_b)
            try:
                expectations = {port_a: 200, port_b: 400}
                for port, expected in expectations.items():
                    with self.subTest(port=port):
                        status, headers, raw = request(
                            port, "POST", "/check", body=body_with_extra
                        )
                        self.assertEqual(
                            status, expected,
                            f"端口 {port}: 期望 {expected}，实际 {status}",
                        )
                        if expected == 200:
                            self.assertEqual(raw, b'{"accepted":true}')
                        else:
                            self.assertEqual(raw, REQUEST_BODY_MISMATCH)
            finally:
                subset_server.stop()
                exact_server.stop()


# ---------------------------------------------------------------------------
# 请求体比较流程重构回归
#
# exact 与 subset 的比较流程已合并为共享实现（标量、数组与嵌套值只维护
# 一份），_json_equal 与 _json_subset 保留原有的两参数调用方式与布尔
# 返回值。下列用例按任务验收场景锁定两种模式的差异与共同语义：
#   POST /exact  样例 {"user":{"id":1}}，省略模式字段（等同 exact），
#                503 + {"accepted":true}，不设置延迟
#   POST /subset 同样样例与响应，requestBodyMode 为 "subset"
#   POST /exact_list / /subset_list  样例 [1,{"k":"v"}]，锁定数组语义
#   POST /exact_slow / /subset_slow  样例 {"v":1}，503 + 200ms 延迟，
#                锁定匹配失败不采用配置状态、正文或延迟
# ---------------------------------------------------------------------------
COMPARISON_REFACTOR_RULES = [
    {"method": "POST", "path": "/exact",
     "requestBody": {"user": {"id": 1}},
     "status": 503, "body": {"accepted": True}},
    {"method": "POST", "path": "/subset", "requestBodyMode": "subset",
     "requestBody": {"user": {"id": 1}},
     "status": 503, "body": {"accepted": True}},
    {"method": "POST", "path": "/exact_list",
     "requestBody": [1, {"k": "v"}],
     "status": 503, "body": {"accepted": True}},
    {"method": "POST", "path": "/subset_list", "requestBodyMode": "subset",
     "requestBody": [1, {"k": "v"}],
     "status": 503, "body": {"accepted": True}},
    {"method": "POST", "path": "/exact_slow",
     "requestBody": {"v": 1}, "delayMs": DELAY_MS,
     "status": 503, "body": {"error": "demo_failure"}},
    {"method": "POST", "path": "/subset_slow", "requestBodyMode": "subset",
     "requestBody": {"v": 1}, "delayMs": DELAY_MS,
     "status": 503, "body": {"error": "demo_failure"}},
]

ACCEPTED_BODY = b'{"accepted":true}'


class RequestBodyComparisonRefactorTests(unittest.TestCase):
    """验收场景：/exact 与 /subset 同样样例 {"user":{"id":1}}、503、无延迟。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rules_path = write_rules(
            cls._tmp.name, "rules_comparison_refactor.json",
            COMPARISON_REFACTOR_RULES,
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def _assert_response(self, label, target, raw_body, expected_status,
                         expected_raw):
        status, headers, raw = request(
            self.port, "POST", target, body=raw_body
        )
        self.assertEqual(
            status, expected_status,
            f"样例 {label}: 状态码应为 {expected_status}，实际 {status}；"
            f"响应={raw!r}",
        )
        # UTF-8 JSON 响应类型与按字节计算的 Content-Length 保持原义
        self.assertEqual(
            headers.get("Content-Type"), CONTENT_TYPE,
            f"样例 {label}: Content-Type 应为 {CONTENT_TYPE!r}",
        )
        self.assertEqual(
            raw, expected_raw,
            f"样例 {label}: 响应体应为 {expected_raw!r}，实际 {raw!r}",
        )
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def _assert_400_mismatch(self, label, raw_body, target):
        self._assert_response(
            label, target, raw_body, 400, REQUEST_BODY_MISMATCH
        )

    def test_acceptance_extra_key_distinguishes_modes(self):
        # 验收正文：id 写为 1.0（数值相等）、user 含额外键 name（中文）
        body = '{"user":{"id":1.0,"name":"甲"}}'.encode("utf-8")
        self._assert_400_mismatch(
            "exact 拒绝额外键", body, "/exact"
        )
        self._assert_response(
            "subset 允许额外键", "/subset", body, 503, ACCEPTED_BODY
        )
        # 删除 name 后两者均返回配置响应（1.0 与 1 数值相等）
        body_without_name = b'{"user":{"id":1.0}}'
        for target in ("/exact", "/subset"):
            with self.subTest(路由=target):
                self._assert_response(
                    "删除额外键后放行", target, body_without_name,
                    503, ACCEPTED_BODY,
                )

    def test_missing_sample_keys_rejected_in_both_modes(self):
        for label, raw_body in [
            ("缺少 user 键", b"{}"),
            ("缺少嵌套 id 键", b'{"user":{}}'),
            ("整体为 null", b"null"),
            ("整体不是对象", b'[{"user":{"id":1}}]'),
        ]:
            for target in ("/exact", "/subset"):
                with self.subTest(正文=label, 路由=target):
                    self._assert_400_mismatch(label, raw_body, target)

    def test_constrained_value_and_type_mismatch_rejected_in_both_modes(self):
        for label, raw_body in [
            ("被约束值不同", b'{"user":{"id":2}}'),
            ("被约束值为字符串", b'{"user":{"id":"1"}}'),
            ("布尔不等于数字", b'{"user":{"id":true}}'),
            ("嵌套值类型不符", b'{"user":1}'),
            ("嵌套值为 null", b'{"user":null}'),
        ]:
            for target in ("/exact", "/subset"):
                with self.subTest(正文=label, 路由=target):
                    self._assert_400_mismatch(label, raw_body, target)

    def test_array_length_and_order_rejected_in_both_modes(self):
        for target in ("/exact_list", "/subset_list"):
            with self.subTest(路由=target, 正文="完全一致"):
                self._assert_response(
                    "完全一致", target, b'[1,{"k":"v"}]', 503, ACCEPTED_BODY
                )
            for label, raw_body in [
                ("数组长度变短", b"[1]"),
                ("数组长度变长", b'[1,{"k":"v"},2]'),
                ("数组顺序改变", b'[{"k":"v"},1]'),
                ("元素被约束值不同", b'[1,{"k":"V"}]'),
                ("元素类型不符", b'[1,"k"]'),
            ]:
                with self.subTest(路由=target, 正文=label):
                    self._assert_400_mismatch(label, raw_body, target)

    def test_array_element_object_extra_keys_only_in_subset(self):
        # 数组中的对象同样适用：subset 允许元素对象带额外键，exact 不允许
        body = b'[1,{"k":"v","x":1}]'
        self._assert_400_mismatch(
            "exact 拒绝元素对象额外键", body, "/exact_list"
        )
        self._assert_response(
            "subset 允许元素对象额外键", "/subset_list", body,
            503, ACCEPTED_BODY,
        )

    def test_malformed_bodies_rejected_in_both_modes(self):
        for label, raw_body in [
            ("空正文", b""),
            ("非法 UTF-8", b"\xff\xfe"),
            ("JSON 语法错误", b'{"user":{"id":1}'),
            ("非有限数字 1e400", b'{"user":{"id":1e400}}'),
            ("非标准字面量 NaN", b"NaN"),
        ]:
            for target in ("/exact", "/subset"):
                with self.subTest(正文=label, 路由=target):
                    self._assert_400_mismatch(label, raw_body, target)

    def test_subset_extra_fields_cannot_bypass_full_body_checks(self):
        # subset 的额外字段仍须通过完整的 UTF-8、JSON 语法与非有限数字检查
        for label, raw_body in [
            ("额外字段含 NaN 字面量", b'{"user":{"id":1},"extra":NaN}'),
            ("额外字段含溢出数字", b'{"user":{"id":1},"extra":1e400}'),
            ("额外字段含非法 UTF-8", b'{"user":{"id":1},"extra":"\xff"}'),
            ("合法前缀后语法错误", b'{"user":{"id":1},"extra":'),
        ]:
            with self.subTest(正文=label):
                self._assert_400_mismatch(label, raw_body, "/subset")

    def test_mismatch_ignores_configured_status_body_and_delay(self):
        # 两种模式的慢路由：校验失败立即 400，不等待、不使用 503 与配置正文
        for target in ("/exact_slow", "/subset_slow"):
            with self.subTest(路由=target, 分支="不匹配"):
                start = time.monotonic()
                status, headers, raw = request(
                    self.port, "POST", target, body=b'{"v":2}'
                )
                elapsed = time.monotonic() - start
                self.assertEqual(status, 400)
                self.assertEqual(raw, REQUEST_BODY_MISMATCH)
                self.assertLess(
                    elapsed, NO_DELAY_MAX_SECONDS,
                    f"{target} 不匹配时不应应用 {DELAY_MS}ms 延迟，"
                    f"实际 {elapsed * 1000:.1f}ms",
                )
            with self.subTest(路由=target, 分支="匹配"):
                # 校验通过才应用既有延迟并返回配置的状态与正文
                status, headers, raw, elapsed = timed_request(
                    self.port, "POST", target, body=b'{"v":1.0}'
                )
                self.assertGreaterEqual(
                    elapsed, DELAY_MIN_SECONDS,
                    f"{target} 匹配后应等待 {DELAY_MS}ms，"
                    f"实际 {elapsed * 1000:.1f}ms",
                )
                self.assertEqual(status, 503)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")), {"error": "demo_failure"}
                )


class JsonComparisonHelperRefactorTests(unittest.TestCase):
    """重构后 _json_equal/_json_subset 的调用方式与布尔返回值保持不变。"""

    def test_helpers_keep_two_arg_call_and_bool_return(self):
        from mock_server import _json_equal, _json_subset

        sample = {"user": {"id": 1}}
        cases = [
            # (expected, actual, exact 结果, subset 结果)
            (sample, {"user": {"id": 1.0}}, True, True),
            (sample, {"user": {"id": 1.0, "name": "甲"}}, False, True),
            (sample, {"user": {"id": 2}}, False, False),
            (sample, {"user": {}}, False, False),
            (sample, {"user": {"id": True}}, False, False),
            ({"meta": {}}, {"meta": {"a": 1}}, False, True),
            ({"meta": {}}, {"meta": []}, False, False),
        ]
        for expected, actual, exact_result, subset_result in cases:
            with self.subTest(pair=(expected, actual)):
                exact = _json_equal(expected, actual)
                subset = _json_subset(expected, actual)
                # 返回值必须是布尔，而非真值/假值的其他类型
                self.assertIs(type(exact), bool)
                self.assertIs(type(subset), bool)
                self.assertEqual(exact, exact_result)
                self.assertEqual(subset, subset_result)

    def test_shared_engine_matches_both_modes(self):
        # 共享比较流程：allow_extra_keys 分别对应 exact 与 subset 语义
        from mock_server import _json_equal, _json_matches, _json_subset

        pairs = [
            ({"a": 1, "b": [True, "x"]}, {"b": [True, "x"], "a": 1.0}),
            ({"a": 1}, {"a": 1, "b": 2}),
            ([{"k": None}], [{"k": None, "j": 0}]),
            ("Case", "case"),
            (None, None),
            (None, 0),
        ]
        for expected, actual in pairs:
            with self.subTest(pair=(expected, actual)):
                self.assertEqual(
                    _json_matches(expected, actual, allow_extra_keys=False),
                    _json_equal(expected, actual),
                )
                self.assertEqual(
                    _json_matches(expected, actual, allow_extra_keys=True),
                    _json_subset(expected, actual),
                )


# ---------------------------------------------------------------------------
# pathMode 前缀匹配回归
#
# 路由项可选的 pathMode 取值为区分大小写的 "exact" 或 "prefix"：省略或
# "exact" 保持完整路径相等；"prefix" 将 path 作为前缀（path 必须以 /
# 结尾，根路径 / 也允许），请求路径以前缀开头且前缀之后的剩余部分非空
# 才算候选（剩余部分可含多级路径）。解析顺序：先按请求方法筛选，再优先
# exact 完整路径相等，其次选 path 最长的 prefix 候选，规则排列顺序不
# 影响结果。两种模式都忽略查询字符串，路径大小写、尾部斜杠、百分号转义
# 按原样比较，星号没有特殊含义。
# ---------------------------------------------------------------------------

PREFIX_RULES = [
    {"method": "GET", "path": "/api/", "pathMode": "prefix",
     "body": {"v": 1}},
    {"method": "GET", "path": "/api/v1/", "pathMode": "prefix",
     "body": {"v": 2}},
    {"method": "GET", "path": "/api/v1/ping", "body": {"v": 3}},
    # 前缀路由同样沿用配置的状态码与延迟
    {"method": "GET", "path": "/slow/", "pathMode": "prefix",
     "delayMs": DELAY_MS, "status": 503,
     "body": {"error": "demo_failure"}},
]


class PrefixMatchingBehaviorTests(unittest.TestCase):
    """验收场景：exact 优先、最长前缀、剩余非空、方法筛选与字面比较。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rules_path = write_rules(
            cls._tmp.name, "rules_prefix.json", PREFIX_RULES
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def _check(self, label, method, target, expected_status, expected_body,
               request_body=None):
        status, headers, raw = request(
            self.port, method, target, body=request_body
        )
        self.assertEqual(
            status, expected_status,
            f"样例 {label}: 状态码应为 {expected_status}，实际 {status}；"
            f"响应={raw!r}",
        )
        self.assertEqual(
            headers.get("Content-Type"), CONTENT_TYPE,
            f"样例 {label}: Content-Type 不符，实际 {headers}",
        )
        self.assertEqual(int(headers["Content-Length"]), len(raw))
        self.assertEqual(
            json.loads(raw.decode("utf-8")), expected_body,
            f"样例 {label}: 响应体应为 {expected_body}，实际 {raw!r}",
        )

    def test_task_acceptance_matrix(self):
        self._check("GET /api/x", "GET", "/api/x", 200, {"v": 1})
        self._check("GET /api/v1/x", "GET", "/api/v1/x", 200, {"v": 2})
        # 查询字符串被忽略；exact 完整相等优先于两个前缀候选
        self._check(
            "GET /api/v1/ping?x=1", "GET", "/api/v1/ping?x=1",
            200, {"v": 3},
        )
        self._check("GET /api/v1/ping", "GET", "/api/v1/ping", 200, {"v": 3})
        # /api 不以 /api/ 开头；/api/ 与前缀完全相等（剩余为空）
        self._check(
            "GET /api（未命中）", "GET", "/api",
            404, {"error": "route_not_found"},
        )
        self._check(
            "GET /api/（剩余为空）", "GET", "/api/",
            404, {"error": "route_not_found"},
        )
        # 方法先筛选：GET 前缀不服务 POST
        self._check(
            "POST /api/x（方法不同）", "POST", "/api/x",
            404, {"error": "route_not_found"},
        )

    def test_exact_is_literal_and_prefixes_cover_neighbors(self):
        # 尾斜杠与多/少字符都不与 exact 相等，回退到最长前缀
        self._check(
            "GET /api/v1/ping/（exact 不等，前缀剩余为 ping/）",
            "GET", "/api/v1/ping/", 200, {"v": 2},
        )
        self._check(
            "GET /api/v1/pingx（exact 不等，前缀剩余为 pingx）",
            "GET", "/api/v1/pingx", 200, {"v": 2},
        )

    def test_remainder_may_contain_multiple_path_segments(self):
        self._check(
            "GET /api/a/b/c（单级前缀覆盖多级剩余）",
            "GET", "/api/a/b/c", 200, {"v": 1},
        )
        self._check(
            "GET /api/v1/a/b/c（最长前缀覆盖多级剩余）",
            "GET", "/api/v1/a/b/c", 200, {"v": 2},
        )

    def test_declaration_order_does_not_affect_selection(self):
        # 逆序声明同样的规则，解析结果必须一致：只按 exact 优先、前缀长度
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp, "rules_prefix_reversed.json", list(reversed(PREFIX_RULES))
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                for target, expected_body in [
                    ("/api/x", {"v": 1}),
                    ("/api/v1/x", {"v": 2}),
                    ("/api/v1/ping?x=1", {"v": 3}),
                    ("/api/v1/a/b/c", {"v": 2}),
                ]:
                    with self.subTest(target=target):
                        status, headers, raw = request(port, "GET", target)
                        self.assertEqual(status, 200)
                        self.assertEqual(
                            headers.get("Content-Type"), CONTENT_TYPE
                        )
                        self.assertEqual(
                            json.loads(raw.decode("utf-8")), expected_body
                        )
                # 逆序不改变剩余为空即未命中的结论
                status, _, raw = request(port, "GET", "/api/")
                self.assertEqual(status, 404)
                self.assertEqual(raw, NOT_FOUND_BODY)
            finally:
                server.stop()

    def test_root_prefix_and_root_exact(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_root_prefix.json",
                [
                    # 根前缀覆盖任意非空剩余
                    {"method": "GET", "path": "/", "pathMode": "prefix",
                     "body": {"root_prefix": 1}},
                    {"method": "GET", "path": "/api/", "pathMode": "prefix",
                     "body": {"v": 1}},
                    # exact 规则即使存在根前缀候选也优先
                    {"method": "GET", "path": "/exact",
                     "body": {"exact": 1}},
                ],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                def check(label, target, status, body=None):
                    code, headers, raw = request(port, "GET", target)
                    self.assertEqual(
                        code, status,
                        f"样例 {label} GET {target}: 期望 {status}，"
                        f"实际 {code}；{raw!r}",
                    )
                    self.assertEqual(
                        headers.get("Content-Type"), CONTENT_TYPE
                    )
                    if body is not None:
                        self.assertEqual(json.loads(raw.decode("utf-8")), body)

                # 根路径本身剩余为空，根前缀不覆盖
                check("GET / 剩余为空", "/", 404)
                # 根前缀覆盖任意多级剩余（查询串忽略）
                check("GET /anything", "/anything", 200, {"root_prefix": 1})
                check("GET /a/b/c?x=1", "/a/b/c?x=1", 200,
                      {"root_prefix": 1})
                # 更长的前缀优先于根前缀
                check("GET /api/x 最长前缀", "/api/x", 200, {"v": 1})
                # /api/ 对 /api/ 前缀剩余为空，但对根前缀剩余 "api/" 非空
                check("GET /api/ 回退根前缀", "/api/", 200,
                      {"root_prefix": 1})
                # exact 完整相等优先于根前缀
                check("GET /exact exact 优先", "/exact", 200, {"exact": 1})
            finally:
                server.stop()

    def test_method_not_supported_does_not_use_prefix_route(self):
        # PUT 等不支持方法：即使前缀能覆盖该路径也走统一 501，
        # 不使用前缀路由的 body/status/delayMs
        status, headers, raw, elapsed = timed_request(
            self.port, "PUT", "/slow/x?y=1"
        )
        self.assertEqual(status, 501)
        self.assertEqual(headers.get("Connection"), "close")
        self.assertEqual(raw, METHOD_REJECT_BODY)
        self.assertNotIn(b"demo_failure", raw)
        self.assertLess(
            elapsed, NO_DELAY_MAX_SECONDS,
            f"PUT /slow/x: 501 不应应用前缀路由的 {DELAY_MS}ms 延迟，"
            f"实际 {elapsed * 1000:.1f}ms",
        )

    def test_literal_path_comparison(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_prefix_literal.json",
                [
                    {"method": "GET", "path": "/Static/", "pathMode": "prefix",
                     "body": {"v": "capital"}},
                    {"method": "GET", "path": "/static/", "pathMode": "prefix",
                     "body": {"v": "lower"}},
                    {"method": "GET", "path": "/enc/", "pathMode": "prefix",
                     "body": {"v": "enc"}},
                    # 星号只是普通字符，不是通配符
                    {"method": "GET", "path": "/wild/*/", "pathMode": "prefix",
                     "body": {"v": "star"}},
                ],
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                def check(label, target, status, body=None):
                    code, headers, raw = request(port, "GET", target)
                    self.assertEqual(
                        code, status,
                        f"样例 {label} GET {target}: 期望 {status}，"
                        f"实际 {code}；{raw!r}",
                    )
                    self.assertEqual(
                        headers.get("Content-Type"), CONTENT_TYPE
                    )
                    if body is not None:
                        self.assertEqual(json.loads(raw.decode("utf-8")), body)

                # 大小写敏感
                check("大写前缀", "/Static/x", 200, {"v": "capital"})
                check("小写前缀", "/static/x", 200, {"v": "lower"})
                check("大小写不符", "/STATIC/x", 404)
                # 尾部斜杠不合并：目录本身剩余为空
                check("/Static/ 剩余为空", "/Static/", 404)
                check("/Static 缺少尾斜杠", "/Static", 404)
                # 百分号转义不解码：%2F 是字面三个字符，不能跨过分段
                check("/enc/%61 字面匹配", "/enc/%61", 200, {"v": "enc"})
                check("/enc%2Fa 不解码为 /enc/a", "/enc%2Fa", 404)
                # 星号没有特殊含义
                check("字面星号请求", "/wild/*/x", 200, {"v": "star"})
                check("星号不当通配符", "/wild/a/x", 404)
            finally:
                server.stop()

    def test_prefix_route_uses_configured_status_and_delay(self):
        # 命中前缀路由后：沿用配置的 503 与 200ms 延迟
        status, headers, raw, elapsed = timed_request(
            self.port, "GET", "/slow/x?z=1"
        )
        self.assertGreaterEqual(
            elapsed, DELAY_MIN_SECONDS,
            f"前缀路由命中后应等待 {DELAY_MS}ms，实际 "
            f"{elapsed * 1000:.1f}ms",
        )
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
        self.assertEqual(int(headers["Content-Length"]), len(raw))
        self.assertEqual(
            json.loads(raw.decode("utf-8")), {"error": "demo_failure"}
        )
        # 剩余为空不算命中：不等待、返回统一 404
        status, _, raw, elapsed = timed_request(self.port, "GET", "/slow/")
        self.assertEqual(status, 404)
        self.assertEqual(raw, NOT_FOUND_BODY)
        self.assertLess(
            elapsed, NO_DELAY_MAX_SECONDS,
            f"未命中不应应用延迟，实际 {elapsed * 1000:.1f}ms",
        )


# POST 前缀路由的 requestBody 校验回归：
#   POST prefix /api/    样例 {"v":1}，配置正文 {"posted":1}
#   GET  prefix /api/    同路径不同方法，配置正文 {"get":1}
#   POST prefix /        无样例，配置正文 {"root":1}（用于验证校验失败
#                        不会回退到其他候选）
#   POST exact  /api/exact  样例 {"v":9}，503 + {"exact":1}
PREFIX_BODY_RULES = [
    {"method": "POST", "path": "/api/", "pathMode": "prefix",
     "requestBody": {"v": 1}, "body": {"posted": 1}},
    {"method": "GET", "path": "/api/", "pathMode": "prefix",
     "body": {"get": 1}},
    {"method": "POST", "path": "/", "pathMode": "prefix",
     "body": {"root": 1}},
    {"method": "POST", "path": "/api/exact", "requestBody": {"v": 9},
     "status": 503, "body": {"exact": 1}},
]


class PrefixRequestBodyTests(unittest.TestCase):
    """路由选定后才校验 requestBody：失败返回 400，不尝试其他候选。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.rules_path = write_rules(
            cls._tmp.name, "rules_prefix_body.json", PREFIX_BODY_RULES
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def test_matching_body_returns_prefix_response(self):
        for label, raw_body in [
            ("与样例相等", b'{"v":1}'),
            ("1 与 1.0 数值相等", b'{"v":1.0}'),
        ]:
            with self.subTest(正文=label):
                status, headers, raw = request(
                    self.port, "POST", "/api/x", body=raw_body
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                self.assertEqual(raw, b'{"posted":1}')
                self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_mismatching_body_returns_400_without_fallback(self):
        # /api/x 同时可被 POST 前缀 /api/ 与根前缀 / 覆盖：选中更长的
        # /api/ 后正文不匹配，必须 400，不得回退到根前缀返回 {"root":1}
        for label, raw_body in [
            ("被约束值不等", b'{"v":2}'),
            ("空正文", b""),
            ("JSON 语法错误", b"{"),
            ("非法 UTF-8", b"\xff"),
        ]:
            with self.subTest(正文=label):
                status, headers, raw = request(
                    self.port, "POST", "/api/x", body=raw_body
                )
                self.assertEqual(status, 400, f"样例 {label}: {raw!r}")
                self.assertEqual(
                    headers.get("Content-Type"), CONTENT_TYPE
                )
                self.assertEqual(raw, REQUEST_BODY_MISMATCH)

    def test_empty_remainder_falls_through_to_root_prefix(self):
        # POST /api/ 对前缀 /api/ 剩余为空（非候选），但对根前缀剩余非空，
        # 根前缀无样例：忽略正文并返回其配置响应
        status, headers, raw = request(
            self.port, "POST", "/api/", body=b"not json"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
        self.assertEqual(raw, b'{"root":1}')

    def test_exact_route_validated_with_its_own_sample(self):
        # exact /api/exact 优先于前缀 /api/：
        # 正文满足 exact 样例 {"v":9} 时返回 exact 的 503（前缀样例不会拦截）
        status, headers, raw = request(
            self.port, "POST", "/api/exact", body=b'{"v":9}'
        )
        self.assertEqual(status, 503)
        self.assertEqual(raw, b'{"exact":1}')
        # 正文只满足前缀样例 {"v":1}：exact 先被选中并按它自己的样例校验，
        # 失败即 400，不得以前缀命中放行
        status, headers, raw = request(
            self.port, "POST", "/api/exact", body=b'{"v":1}'
        )
        self.assertEqual(status, 400)
        self.assertEqual(raw, REQUEST_BODY_MISMATCH)

    def test_same_prefix_path_distinct_by_method(self):
        status, headers, raw = request(self.port, "GET", "/api/x")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
        self.assertEqual(raw, b'{"get":1}')


# ---------------------------------------------------------------------------
# 多条路由竞争时只校验最终选中路由的真实 HTTP 回归
#
# 同一份包含三条 POST 路由的临时规则分别按文件原顺序与逆序各启动一个
# 服务，锁定“先按 exact 优先、最长前缀其次选出唯一路由，再只按该路由的
# requestBody 校验；校验失败返回 400，绝不回退尝试其他候选”：
#   POST prefix /        不配置 requestBody，200 + JSON 字符串 "root"
#   POST prefix /api/    requestBodyMode=subset、样例 {"v":1}，503 + "prefix"
#   POST exact  /api/item 默认整体比较、样例 {"v":9}，200 + "exact"
# 其中 POST /api/ 对前缀 /api/ 剩余为空（不参与竞争），仅对根前缀剩余
# 非空：以非 JSON 正文请求时应得到根前缀响应。
# ---------------------------------------------------------------------------

ROUTE_COMPETITION_RULES = [
    {"method": "POST", "path": "/", "pathMode": "prefix",
     "body": "root"},
    {"method": "POST", "path": "/api/", "pathMode": "prefix",
     "requestBodyMode": "subset", "requestBody": {"v": 1},
     "status": 503, "body": "prefix"},
    {"method": "POST", "path": "/api/item",
     "requestBody": {"v": 9}, "body": "exact"},
]

# (说明, 请求目标（含查询串）, 请求正文, 预期状态码, 预期响应 JSON 值)
ROUTE_COMPETITION_CASES = [
    # exact /api/item 优先于前缀 /api/：正文满足 exact 样例 {"v":9}，
    # 返回精确响应，查询串不影响匹配
    ("精确路径命中：{\"v\":9} 返回 200 \"exact\"",
     "/api/item?x=1", b'{"v":9}', 200, "exact"),
    # 同一正文 {"v":1} 能通过前缀的 subset 样例，但 exact 已被选中：
    # 必须按 exact 自己的样例校验并返回 400，不能改用前缀的 503
    ("精确路径按自身样例校验失败：{\"v\":1} 返回 400，不回退前缀",
     "/api/item?x=1", b'{"v":1}', 400,
     {"error": "request_body_mismatch"}),
    # 其他 /api/* 路径由最长前缀 /api/ 选中：subset 允许额外键
    ("前缀命中：subset 允许额外键，返回 503 \"prefix\"",
     "/api/other", b'{"v":1,"extra":true}', 503, "prefix"),
    # 以下正文均通不过前缀的 subset 校验：必须 400，不能回退到无样例的
    # 根前缀返回 200 "root"
    ("前缀校验失败：正文缺少 v 键，返回 400 而非根前缀响应",
     "/api/other", b'{}', 400, {"error": "request_body_mismatch"}),
    ("前缀校验失败：v 改为布尔 true（布尔不等于数字），返回 400",
     "/api/other", b'{"v":true}', 400,
     {"error": "request_body_mismatch"}),
    ("前缀校验失败：额外字段放入非有限数字 1e400，返回 400",
     "/api/other", b'{"v":1,"extra":1e400}', 400,
     {"error": "request_body_mismatch"}),
    # /api/ 对前缀 /api/ 剩余为空，该前缀不参与竞争；根前缀剩余 "api/"
    # 非空且未配置 requestBody，忽略非 JSON 正文返回 200 "root"
    ("剩余路径为空的前缀不参与竞争：非 JSON 正文回退根前缀",
     "/api/", b"not json", 200, "root"),
]


class SelectedRouteOnlyValidationTests(unittest.TestCase):
    """多路由竞争：只校验最终选中的路由；规则原顺序与逆序预期完全一致。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        # 临时目录（含两份仅顺序不同的规则文件）在类结束时无条件清理，
        # 即使后续断言失败也会执行
        cls.addClassCleanup(cls._tmp.cleanup)
        # 同一份三条路由规则：按文件原顺序与逆序分别启动独立服务；
        # addClassCleanup 保证任一断言失败后服务进程仍被回收
        cls.servers = []
        for index, (order_label, routes) in enumerate((
            ("规则原顺序", ROUTE_COMPETITION_RULES),
            ("规则逆序", list(reversed(ROUTE_COMPETITION_RULES))),
        )):
            rules_path = write_rules(
                cls._tmp.name,
                f"rules_route_competition_{index}.json",
                routes,
            )
            port = free_port()
            server = ServerProcess(rules_path, port)
            cls.servers.append((order_label, port, server))
            cls.addClassCleanup(server.stop)

    def _assert_exchange(self, order_label, port, label, target, raw_body,
                         expected_status, expected_json):
        """发起一次 POST 并核对状态、JSON 内容、Content-Type、Content-Length。

        失败信息统一附带规则顺序、请求路径与请求正文，并对照给出预期
        响应与实际响应的差异，便于直接定位是哪条路由被错误选中。
        """
        expected_raw = json.dumps(
            expected_json, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        context = (
            f"[{order_label}] 样例 {label}：POST {target}，"
            f"请求正文 {raw_body!r}"
        )
        # request() 在 finally 中关闭连接，断言失败也不泄漏连接
        status, headers, raw = request(port, "POST", target, body=raw_body)
        self.assertEqual(
            status, expected_status,
            f"{context}：预期状态码 {expected_status} 与响应 "
            f"{expected_raw!r}，实际状态码 {status} 与响应 {raw!r}",
        )
        content_type = headers.get("Content-Type")
        self.assertEqual(
            content_type, CONTENT_TYPE,
            f"{context}：预期 Content-Type {CONTENT_TYPE!r}，"
            f"实际 {content_type!r}；实际响应={raw!r}",
        )
        content_length = headers.get("Content-Length")
        self.assertIsNotNone(
            content_length,
            f"{context}：响应缺少 Content-Length；实际响应={raw!r}",
        )
        self.assertEqual(
            int(content_length), len(raw),
            f"{context}：预期 Content-Length 等于实际响应字节数 "
            f"{len(raw)}，实际 Content-Length={content_length!r}；"
            f"实际响应={raw!r}",
        )
        self.assertEqual(
            raw, expected_raw,
            f"{context}：预期响应字节 {expected_raw!r}（JSON 值 "
            f"{expected_json!r}），实际响应字节 {raw!r}",
        )
        actual_json = json.loads(raw.decode("utf-8"))
        self.assertEqual(
            actual_json, expected_json,
            f"{context}：预期响应 JSON {expected_json!r}，"
            f"实际解析为 {actual_json!r}",
        )

    def test_same_expectations_under_original_and_reversed_rule_order(self):
        # 逐顺序、逐用例核对：subTest 使单个用例失败不影响其余用例执行，
        # 服务进程与临时文件的回收由 addClassCleanup 统一保证
        for order_label, port, _server in self.servers:
            for label, target, raw_body, expected_status, expected_json in (
                ROUTE_COMPETITION_CASES
            ):
                with self.subTest(
                    规则顺序=order_label, 样例=label, request=f"POST {target}"
                ):
                    self._assert_exchange(
                        order_label, port, label, target, raw_body,
                        expected_status, expected_json,
                    )


# pathMode 规则加载校验：取值必须是区分大小写的 "exact"/"prefix"，且
# prefix 的 path 必须以 / 结尾；否则 load_rules 抛 RulesError，CLI 退出
# 码 2 且不监听
INVALID_PATH_MODE_CASES = [
    # (说明, pathMode 的 JSON 值)
    ("大写 PREFIX", "PREFIX"),
    ("首字母大写 Prefix", "Prefix"),
    ("首字母大写 Exact", "Exact"),
    ("带尾随空白", "prefix "),
    ("空字符串", ""),
    ("null", None),
    ("布尔 true", True),
    ("整数 1", 1),
    ("浮点数 1.0", 1.0),
    ("数组", ["prefix"]),
    ("对象", {"mode": "prefix"}),
]

# prefix 模式下未以 / 结尾的 path
PREFIX_MISSING_SLASH_CASES = [
    # (说明, method, path)
    ("缺少尾斜杠的目录", "GET", "/api"),
    ("多段路径缺少尾斜杠", "GET", "/api/v1"),
    ("单段路径缺少尾斜杠", "POST", "/x"),
]


class PathModeRulesValidationTests(unittest.TestCase):
    """pathMode 取值与 prefix 尾斜杠校验：load_rules 与 CLI 两条入口。"""

    def _items(self, bad_item, bad_index):
        if bad_index == 0:
            return [bad_item]
        return [
            {"method": "GET", "path": "/ok", "body": {"fine": 1}},
            bad_item,
        ]

    def test_load_rules_rejects_invalid_path_mode(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            seq = 0
            for label, bad_mode in INVALID_PATH_MODE_CASES:
                for bad_index in (0, 1):
                    with self.subTest(样例=label, 位置=f"routes[{bad_index}]"):
                        bad_item = {
                            "method": "GET", "path": "/api/",
                            "pathMode": bad_mode, "body": {},
                        }
                        rules_path = write_rules(
                            tmp, f"rules_bad_pathmode_{seq}.json",
                            self._items(bad_item, bad_index),
                        )
                        seq += 1
                        with self.assertRaises(RulesError) as ctx:
                            load_rules(rules_path)
                        message = str(ctx.exception)
                        self.assertIn(
                            f"routes[{bad_index}].pathMode", message,
                            f"样例 {label}: 错误应标明 "
                            f"routes[{bad_index}].pathMode，实际 {message!r}",
                        )

    def test_cli_rejects_invalid_path_mode_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            seq = 0
            for label, bad_mode in INVALID_PATH_MODE_CASES:
                for bad_index in (0, 1):
                    with self.subTest(样例=label, 位置=f"routes[{bad_index}]"):
                        bad_item = {
                            "method": "GET", "path": "/api/",
                            "pathMode": bad_mode, "body": {},
                        }
                        rules_path = write_rules(
                            tmp, f"rules_bad_pathmode_cli_{seq}.json",
                            self._items(bad_item, bad_index),
                        )
                        seq += 1
                        returncode, stdout, stderr = start_and_wait_exit(
                            rules_path, free_port()
                        )
                        self.assertEqual(
                            returncode, 2,
                            f"样例 {label}: 期望退出码 2，实际 {returncode}；"
                            f"stdout={stdout!r} stderr={stderr!r}",
                        )
                        self.assertIn(
                            f"routes[{bad_index}].pathMode", stderr,
                            f"样例 {label}: 标准错误应包含路由位置与出错字段"
                            f" pathMode，实际 stderr={stderr!r}",
                        )
                        self.assertNotIn("Traceback", stderr)
                        self.assertNotIn(STARTUP_MARKER, stdout)

    def test_load_rules_rejects_prefix_path_without_trailing_slash(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, method, bad_path) in enumerate(
                PREFIX_MISSING_SLASH_CASES
            ):
                for bad_index in (0, 1):
                    with self.subTest(
                        样例=label, 位置=f"routes[{bad_index}]"
                    ):
                        bad_item = {
                            "method": method, "path": bad_path,
                            "pathMode": "prefix", "body": {},
                        }
                        rules_path = write_rules(
                            tmp, f"rules_prefix_slash_{index}_{bad_index}.json",
                            self._items(bad_item, bad_index),
                        )
                        with self.assertRaises(RulesError) as ctx:
                            load_rules(rules_path)
                        message = str(ctx.exception)
                        self.assertIn(
                            f"routes[{bad_index}]", message,
                            f"样例 {label}: 错误应标明 routes[{bad_index}]，"
                            f"实际 {message!r}",
                        )
                        self.assertIn(
                            "pathMode", message,
                            f"样例 {label}: 错误应包含出错字段 pathMode，"
                            f"实际 {message!r}",
                        )
                        self.assertIn(
                            bad_path, message,
                            f"样例 {label}: 错误应标明实际 path {bad_path!r}，"
                            f"实际 {message!r}",
                        )

    def test_cli_rejects_prefix_without_trailing_slash_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, method, bad_path) in enumerate(
                PREFIX_MISSING_SLASH_CASES
            ):
                bad_item = {
                    "method": method, "path": bad_path,
                    "pathMode": "prefix", "body": {},
                }
                rules_path = write_rules(
                    tmp, f"rules_prefix_slash_cli_{index}.json", [bad_item]
                )
                returncode, stdout, stderr = start_and_wait_exit(
                    rules_path, free_port()
                )
                self.assertEqual(
                    returncode, 2,
                    f"样例 {label}: 期望退出码 2，实际 {returncode}；"
                    f"stdout={stdout!r} stderr={stderr!r}",
                )
                self.assertIn("routes[0]", stderr)
                self.assertIn("pathMode", stderr)
                self.assertIn(bad_path, stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertNotIn(STARTUP_MARKER, stdout)


class PathModeValidLoadingTests(unittest.TestCase):
    """合法对照：省略/显式 exact、prefix 尾斜杠与根路径均正常加载。"""

    def test_path_modes_recorded_for_every_route(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_pathmodes_ok.json",
                [
                    {"method": "GET", "path": "/omit", "body": {}},
                    {"method": "GET", "path": "/exact",
                     "pathMode": "exact", "body": {}},
                    {"method": "GET", "path": "/api/", "pathMode": "prefix",
                     "body": {}},
                    {"method": "GET", "path": "/", "pathMode": "prefix",
                     "body": {}},
                    # exact 路径不需要以 / 结尾
                    {"method": "POST", "path": "/api", "pathMode": "exact",
                     "body": {}},
                ],
            )
            routes = load_rules(rules_path)
            self.assertEqual(
                routes.path_modes[("GET", "/omit")], "exact"
            )
            self.assertEqual(
                routes.path_modes[("GET", "/exact")], "exact"
            )
            self.assertEqual(
                routes.path_modes[("GET", "/api/")], "prefix"
            )
            self.assertEqual(
                routes.path_modes[("GET", "/")], "prefix"
            )
            self.assertEqual(
                routes.path_modes[("POST", "/api")], "exact"
            )
            # path_modes 的键集合与路由本身一致
            self.assertEqual(set(routes.path_modes), set(routes))


class PathModeDuplicateTests(unittest.TestCase):
    """同一 method+path 仍禁止重复，不能靠 pathMode 区分。"""

    def test_duplicate_same_method_and_path_rejected(self):
        from mock_server import RulesError, load_rules

        cases = [
            (
                "先 prefix 后 exact",
                [
                    {"method": "GET", "path": "/api/", "pathMode": "prefix",
                     "body": 1},
                    {"method": "GET", "path": "/api/", "body": 2},
                ],
            ),
            (
                "两条都是 prefix",
                [
                    {"method": "GET", "path": "/api/", "pathMode": "prefix",
                     "body": 1},
                    {"method": "GET", "path": "/api/", "pathMode": "prefix",
                     "body": 2},
                ],
            ),
            (
                "先 exact 后 prefix",
                [
                    {"method": "GET", "path": "/api/", "body": 1},
                    {"method": "GET", "path": "/api/", "pathMode": "prefix",
                     "body": 2},
                ],
            ),
        ]
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, items) in enumerate(cases):
                with self.subTest(样例=label):
                    rules_path = write_rules(
                        tmp, f"rules_pathmode_dup_{index}.json", items
                    )
                    with self.assertRaises(RulesError) as ctx:
                        load_rules(rules_path)
                    message = str(ctx.exception)
                    self.assertIn("duplicate route", message)
                    self.assertIn("routes[1]", message)
                    self.assertIn("GET /api/", message)

    def test_same_path_different_methods_or_paths_still_load(self):
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(
                tmp,
                "rules_pathmode_distinct.json",
                [
                    # 同路径不同方法（且模式不同）是两条路由
                    {"method": "GET", "path": "/api/", "pathMode": "prefix",
                     "body": {"m": "GET"}},
                    {"method": "POST", "path": "/api/", "body": {"m": "POST"}},
                    # /api（exact）与 /api/（prefix）路径不同，可以共存
                    {"method": "GET", "path": "/api", "body": {"m": "dir"}},
                ],
            )
            routes = load_rules(rules_path)
            self.assertEqual(len(routes), 3)
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                status, _, raw = request(port, "GET", "/api/x")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8")),
                                 {"m": "GET"})
                status, _, raw = request(port, "GET", "/api")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8")),
                                 {"m": "dir"})
                # GET /api/ 对 GET 前缀剩余为空，未命中
                status, _, raw = request(port, "GET", "/api/")
                self.assertEqual(status, 404)
                status, _, raw = request(port, "POST", "/api/")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8")),
                                 {"m": "POST"})
            finally:
                server.stop()


class PathModeBackwardCompatibilityTests(unittest.TestCase):
    """旧规则无需补字段：自带 rules.json 全部按 exact 加载并正常服务。"""

    def test_shipped_rules_load_as_exact_and_serve(self):
        from mock_server import load_rules

        rules_path = PROJECT_ROOT / "rules.json"
        routes = load_rules(rules_path)
        self.assertTrue(routes)
        self.assertTrue(
            all(mode == "exact" for mode in routes.path_modes.values())
        )
        port = free_port()
        server = ServerProcess(rules_path, port)
        try:
            status, headers, raw = request(port, "GET", "/hello?x=1")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
            self.assertEqual(
                json.loads(raw.decode("utf-8")), {"message": "你好"}
            )
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# bodyMode 回归
#
# bodyMode 为可选的区分大小写字段：省略或 "fixed" 保持固定响应（body 含
# 占位符也原样返回）；"template" 把 body 字符串值中的 {{request.path}}
# 替换为本次用于匹配的路径、{{request.method}} 替换为本次请求方法的大写
# 形式、{{request.query}} 替换为本次请求目标中的原始查询串（第一个问号
# 之后至井号或结尾的文本，不做解码或校验）。其他取值使 load_rules 抛出
# RulesError，命令行以退出码 2 结束且不监听。
# ---------------------------------------------------------------------------


class BodyModeTemplateTests(unittest.TestCase):
    """bodyMode "template"：按本次请求方法、路径与查询串渲染 body 字符串值。"""

    def _start(self, tmp, name, routes):
        rules_path = write_rules(tmp, name, routes)
        port = free_port()
        server = ServerProcess(rules_path, port)
        self.addCleanup(server.stop)
        return port

    def test_spec_example_prefix_echo_percent_escaped(self):
        # 规格样例：prefix /echo/ + 模板 "{{request.path}}"，
        # GET /echo/a%20b?x=1 返回 200 与 JSON 字符串 "/echo/a%20b"
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_echo.json", [
                {"method": "GET", "path": "/echo/", "pathMode": "prefix",
                 "bodyMode": "template", "body": "{{request.path}}"},
            ])
            status, headers, raw = request(port, "GET", "/echo/a%20b?x=1")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
            expected = json.dumps(
                "/echo/a%20b", ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.assertEqual(raw, expected)
            self.assertEqual(json.loads(raw.decode("utf-8")), "/echo/a%20b")
            # Content-Length 按替换后的 UTF-8 JSON 字节数给出
            self.assertEqual(int(headers["Content-Length"]), len(expected))

    def test_each_request_echoes_its_own_path_verbatim(self):
        # 每次请求回显当前路径：大小写、尾斜杠与百分号转义按原样保留，
        # 查询串被去除，不额外解码或规范化
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_echo_each.json", [
                {"method": "GET", "path": "/echo/", "pathMode": "prefix",
                 "bodyMode": "template", "body": {"path": "{{request.path}}"}},
            ])
            cases = [
                ("/echo/a%20b?x=1", "/echo/a%20b"),
                ("/echo/Mixed/Case/", "/echo/Mixed/Case/"),
                ("/echo/%2F%41", "/echo/%2F%41"),
                ("/echo/%E4%BD%A0%E5%A5%BD", "/echo/%E4%BD%A0%E5%A5%BD"),
            ]
            for target, expected_path in cases:
                with self.subTest(target=target):
                    status, headers, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"path": expected_path},
                    )
                    self.assertEqual(
                        int(headers["Content-Length"]), len(raw)
                    )

    def test_nested_values_replaced_keys_and_scalars_untouched(self):
        # 顶层及嵌套对象、数组中的字符串值均替换；对象键（即使形如占位符）、
        # 非字符串值与 JSON 结构不变；嵌入、重复或混合出现的三个占位符均替换
        body = {
            "{{request.path}}": "键名保持原样 {{request.path}}",
            "plain": "prefix-{{request.path}}-{{request.path}}-suffix",
            "nested": {"list": ["{{request.path}}", 1, 1.5, True, None,
                                ["{{request.method}}"]]},
            "spaced": "{{ request.path }}",
            "case": "{{Request.Path}}",
            "method_spaced": "{{ request.method }}",
            "method_case": "{{Request.Method}}",
            "mix": "{{request.method}} {{request.path}} {{request.method}}",
            "query_plain": "{{request.query}}",
            "query_mix": "q={{request.query}}|{{request.path}}",
            "query_case": "{{Request.Query}}",
            "other": "{{request.other}}",
            "empty": "",
        }
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_nested.json", [
                {"method": "GET", "path": "/t", "bodyMode": "template",
                 "body": body},
            ])
            status, headers, raw = request(port, "GET", "/t?y=2&b")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {
                    "{{request.path}}": "键名保持原样 /t",
                    "plain": "prefix-/t-/t-suffix",
                    "nested": {"list": ["/t", 1, 1.5, True, None, ["GET"]]},
                    "spaced": "{{ request.path }}",
                    "case": "{{Request.Path}}",
                    "method_spaced": "{{ request.method }}",
                    "method_case": "{{Request.Method}}",
                    "mix": "GET /t GET",
                    "query_plain": "y=2&b",
                    "query_mix": "q=y=2&b|/t",
                    "query_case": "{{Request.Query}}",
                    "other": "{{request.other}}",
                    "empty": "",
                },
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_top_level_scalar_bodies_render(self):
        # body 为任意 JSON 值：顶层字符串、数字、null 均合法；
        # 非字符串值不含可替换内容，原样返回
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_scalar.json", [
                {"method": "GET", "path": "/s", "bodyMode": "template",
                 "body": "{{request.path}}"},
                {"method": "GET", "path": "/n", "bodyMode": "template",
                 "body": 42},
                {"method": "GET", "path": "/nil", "bodyMode": "template",
                 "body": None},
            ])
            status, _, raw = request(port, "GET", "/s")
            self.assertEqual(status, 200)
            self.assertEqual(raw, b'"/s"')
            status, _, raw = request(port, "GET", "/n")
            self.assertEqual(status, 200)
            self.assertEqual(raw, b"42")
            status, _, raw = request(port, "GET", "/nil")
            self.assertEqual(status, 200)
            self.assertEqual(raw, b"null")

    def test_fixed_mode_returns_placeholder_verbatim(self):
        # 省略 bodyMode 或显式 "fixed"：两个占位符都原样返回
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_fixed.json", [
                {"method": "GET", "path": "/default",
                 "body": {"text": "{{request.method}} {{request.path}}"}},
                {"method": "GET", "path": "/fixed", "bodyMode": "fixed",
                 "body": {"text": "{{request.method}} {{request.path}}"}},
            ])
            for target in ("/default", "/fixed"):
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"text": "{{request.method}} {{request.path}}"},
                    )

    def test_method_echo_spec_example_post_and_get(self):
        # 规格样例：prefix /echo/ + requestBody {"ok":true} + 模板
        # {"text":"{{request.method}} {{request.path}}"}
        # POST /echo/a%20b?x=1 正文 {"ok":true} -> 200 与
        # {"text":"POST /echo/a%20b"}；改为 GET 并移除 requestBody 后，
        # 同一路径回显 GET，路径文本不变
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            post_rules = write_rules(tmp, "rules_echo_method_post.json", [
                {"method": "POST", "path": "/echo/", "pathMode": "prefix",
                 "bodyMode": "template", "requestBody": {"ok": True},
                 "body": {"text": "{{request.method}} {{request.path}}"}},
            ])
            port = free_port()
            post_server = ServerProcess(post_rules, port)
            try:
                status, headers, raw = request(
                    port, "POST", "/echo/a%20b?x=1",
                    body=b'{"ok":true}',
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
                expected = json.dumps(
                    {"text": "POST /echo/a%20b"},
                    ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(raw, expected)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"text": "POST /echo/a%20b"},
                )
                self.assertEqual(int(headers["Content-Length"]), len(expected))
                # 方法取自本次请求，不受查询参数或正文影响：
                # 正文 {"ok":1}（布尔与数字不等）-> 400 request_body_mismatch
                status, _, raw = request(
                    port, "POST", "/echo/a%20b?x=1",
                    body=b'{"ok":1}',
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"error": "request_body_mismatch"},
                )
                # GET 不命中仅 POST 注册的路由
                status, _, raw = request(port, "GET", "/echo/a%20b?x=1")
                self.assertEqual(status, 404)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"error": "route_not_found"},
                )
            finally:
                post_server.stop()

            # 改为 GET 并移除 requestBody 后重启：同一路径回显 GET，
            # 路径文本不变
            get_rules = write_rules(tmp, "rules_echo_method_get.json", [
                {"method": "GET", "path": "/echo/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"text": "{{request.method}} {{request.path}}"}},
            ])
            get_server = ServerProcess(get_rules, port)
            try:
                status, headers, raw = request(
                    port, "GET", "/echo/a%20b?x=1"
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"text": "GET /echo/a%20b"},
                )
                self.assertEqual(int(headers["Content-Length"]), len(raw))
            finally:
                get_server.stop()

    def test_replacement_text_is_not_reprocessed(self):
        # 替换结果不再参与模板处理：路径文本本身含占位符形态的字符时，
        # 替换进 {{request.path}} 后不会被当作占位符二次替换
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_reprocess.json", [
                {"method": "GET", "path": "/p/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"text": "{{request.method}}:{{request.path}}"}},
            ])
            # 路径中的 {{request.method}} 与 %7B%7B 等文本均为普通字面量
            target = "/p/x/{{request.method}}"
            status, headers, raw = request(port, "GET", target)
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"text": f"GET:{target}"},
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_each_request_echoes_its_own_method_and_path(self):        # 同一路径分别以 GET 与 POST 注册模板路由：每次命中回显当前方法，
        # 互不影响，也不改变已加载规则或后续响应；方法不受查询参数影响
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_echo_each_method.json", [
                {"method": "GET", "path": "/echo/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"text": "{{request.method}} {{request.path}}"}},
                {"method": "POST", "path": "/echo/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"text": "{{request.method}} {{request.path}}"}},
            ])
            for call, expected in (
                (("GET", "/echo/a%20b?x=1", None),
                 {"text": "GET /echo/a%20b"}),
                (("POST", "/echo/a%20b?x=1", b""),
                 {"text": "POST /echo/a%20b"}),
                (("GET", "/echo/Trail/?", None),
                 {"text": "GET /echo/Trail/"}),
                (("POST", "/echo/Trail/?", b""),
                 {"text": "POST /echo/Trail/"}),
                (("GET", "/echo/a%20b?x=1", None),
                 {"text": "GET /echo/a%20b"}),
            ):
                with self.subTest(request=call):
                    status, headers, raw = request(port, *call)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")), expected
                    )
                    self.assertEqual(
                        int(headers["Content-Length"]), len(raw)
                    )

    def test_query_echo_spec_example_and_no_carryover_on_same_connection(self):
        # 规格样例：规则 {"routes":[{"method":"GET","path":"/echo",
        # "bodyMode":"template","body":{"q":"{{request.query}}"}}]}
        # GET /echo?tag=a+z&tag=&flag&x=%2f 返回 200 与
        # {"q":"tag=a+z&tag=&flag&x=%2f"}；同一连接随后无查询串请求
        # 返回 {"q":""}，不沿用前次查询内容
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_query_spec.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"q": "{{request.query}}"}},
            ])
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                conn.request(
                    "GET", "/echo?tag=a+z&tag=&flag&x=%2f"
                )
                resp = conn.getresponse()
                raw = resp.read()
                self.assertEqual(resp.status, 200)
                expected_query = "tag=a+z&tag=&flag&x=%2f"
                expected = json.dumps(
                    {"q": expected_query},
                    ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(raw, expected)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"q": expected_query},
                )
                self.assertEqual(
                    int(resp.getheader("Content-Length")), len(expected)
                )
                # 同一连接、无查询串：回显空字符串
                conn.request("GET", "/echo")
                resp = conn.getresponse()
                raw = resp.read()
                self.assertEqual(resp.status, 200)
                self.assertEqual(raw, b'{"q":""}')
                self.assertEqual(
                    int(resp.getheader("Content-Length")), len(raw)
                )
                # 仅结尾问号同样回显空字符串
                conn.request("GET", "/echo?")
                resp = conn.getresponse()
                raw = resp.read()
                self.assertEqual(resp.status, 200)
                self.assertEqual(raw, b'{"q":""}')
            finally:
                conn.close()

    def test_query_text_kept_verbatim_without_decode_or_validation(self):
        # 查询文本保留参数顺序、重复参数、空值、没有等号的片段、加号与
        # 百分号转义；不做解码、排序或类型转换，%ZZ 也原样返回，不返回 400
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_query_verbatim.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"q": "{{request.query}}"}},
            ])
            cases = [
                "b=2&a=1&a=1&a=0",
                "empty=&flag&=nokey&z=",
                "plus=a+b+c&enc=%2f%2F%41%25",
                "bad=%ZZ&also=%zz&good=%2f",
                "x=1&",
                "q=%E4%BD%A0%E5%A5%BD&p=a%20b",
                "a={{request.path}}&b={{request.method}}",
                "x=1#fragment-ignored",
            ]
            for raw_target in cases:
                with self.subTest(target=raw_target):
                    status, headers, raw = request(
                        port, "GET", "/echo?" + raw_target
                    )
                    self.assertEqual(status, 200)
                    expected_query = raw_target.split("#", 1)[0]
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"q": expected_query},
                    )
                    self.assertEqual(
                        int(headers["Content-Length"]), len(raw)
                    )

    def test_query_placeholder_in_nested_and_top_level_strings(self):
        # 顶层字符串及嵌套对象、数组中的字符串值均可替换；对象键不变
        body = {
            "top": "{{request.query}}",
            "nested": {"list": ["{{request.query}}", {"q": "x={{request.query}}"}]},
            "{{request.query}}": "key untouched",
            "num": 1,
            "nil": None,
        }
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_query_nested.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": body},
            ])
            status, headers, raw = request(port, "GET", "/echo?a=1&b=2")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {
                    "top": "a=1&b=2",
                    "nested": {
                        "list": ["a=1&b=2", {"q": "x=a=1&b=2"}],
                    },
                    "{{request.query}}": "key untouched",
                    "num": 1,
                    "nil": None,
                },
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_query_replacement_text_is_not_reprocessed(self):
        # 替换得到的查询文本即使含 {{request.path}} 等占位符形态也不再展开
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_query_reprocess.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"q": "{{request.query}}"}},
            ])
            target = "/echo?t={{request.path}}&m={{request.method}}&q={{request.query}}"
            status, _, raw = request(port, "GET", target)
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"q": target.split("?", 1)[1]},
            )

    def test_query_echo_works_for_post_with_and_without_body_check(self):
        # GET 与 POST 均适用：POST 模板路由同样回显原始查询串；
        # requestBody 校验失败时仍返回 400 request_body_mismatch，
        # 不渲染模板
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_query_post.json", [
                {"method": "POST", "path": "/echo", "bodyMode": "template",
                 "body": {"q": "{{request.query}}"}},
                {"method": "POST", "path": "/checked", "bodyMode": "template",
                 "requestBody": {"ok": True},
                 "body": {"q": "{{request.query}}"}},
            ])
            status, headers, raw = request(
                port, "POST", "/echo?a=1&flag&b=", body=b'{"anything":1}'
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"q": "a=1&flag&b="},
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))
            # 无查询串的 POST 回显空字符串
            status, _, raw = request(port, "POST", "/echo", body=b"")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8")), {"q": ""})
            # 正文不匹配：不渲染模板、不用配置正文，返回固定 400
            status, _, raw = request(
                port, "POST", "/checked?should=not_appear",
                body=b'{"ok":false}',
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"error": "request_body_mismatch"},
            )

    def test_fixed_and_default_mode_keep_query_placeholder_verbatim(self):
        # fixed 或默认模式下 {{request.query}} 与另外两个占位符均原样返回
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_query_fixed.json", [
                {"method": "GET", "path": "/default",
                 "body": {"q": "{{request.query}}",
                          "all": "{{request.method}} {{request.path}} "
                                 "{{request.query}}"}},
                {"method": "GET", "path": "/fixed", "bodyMode": "fixed",
                 "body": {"q": "{{request.query}}"}},
            ])
            status_default, _, raw_default = request(
                port, "GET", "/default?a=1"
            )
            self.assertEqual(status_default, 200)
            self.assertEqual(
                json.loads(raw_default.decode("utf-8")),
                {"q": "{{request.query}}",
                 "all": "{{request.method}} {{request.path}} "
                        "{{request.query}}"},
            )
            status_fixed, _, raw_fixed = request(
                port, "GET", "/fixed?a=1"
            )
            self.assertEqual(status_fixed, 200)
            self.assertEqual(
                json.loads(raw_fixed.decode("utf-8")),
                {"q": "{{request.query}}"},
            )

    def test_query_does_not_participate_in_routing(self):
        # 查询串不参与路由选择：prefix 模板路由按路径命中并回显各自查询串
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_query_routing.json", [
                {"method": "GET", "path": "/echo/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"path": "{{request.path}}",
                          "query": "{{request.query}}"}},
            ])
            for target, expected_path, expected_query in (
                ("/echo/a?x=1&x=2", "/echo/a", "x=1&x=2"),
                ("/echo/a/b?flag", "/echo/a/b", "flag"),
                ("/echo/a", "/echo/a", ""),
            ):
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"path": expected_path, "query": expected_query},
                    )

    def test_template_applies_only_to_selected_route(self):
        # 精确优先、最长前缀匹配保持不变；模板只作用于最终选中的路由
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_select.json", [
                {"method": "GET", "path": "/api/", "pathMode": "prefix",
                 "bodyMode": "template", "body": {"v": "{{request.path}}"}},
                {"method": "GET", "path": "/api/v1/", "pathMode": "prefix",
                 "bodyMode": "template", "body": {"v": "{{request.path}}"}},
                {"method": "GET", "path": "/api/v1/ping",
                 "body": {"v": "fixed"}},
            ])
            cases = [
                ("/api/x", {"v": "/api/x"}),
                ("/api/v1/x", {"v": "/api/v1/x"}),
                ("/api/v1/ping?x=1", {"v": "fixed"}),
            ]
            for target, expected in cases:
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(raw.decode("utf-8")), expected)
            # 前缀剩余部分为空仍不命中
            status, _, raw = request(port, "GET", "/api/")
            self.assertEqual(status, 404)
            self.assertEqual(
                json.loads(raw.decode("utf-8")), {"error": "route_not_found"}
            )

    def test_template_route_keeps_status_and_mismatch_behaviour(self):
        # 命中且校验通过时沿用配置状态；POST 正文校验失败仍返回 400 与
        # request_body_mismatch，不使用模板与配置状态，不回退其他候选
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_post.json", [
                {"method": "POST", "path": "/submit",
                 "bodyMode": "template", "status": 503,
                 "requestBody": {"token": "abc"},
                 "body": {"echo": "{{request.path}}"}},
                {"method": "POST", "path": "/submit/", "pathMode": "prefix",
                 "body": {"v": "fallback"}},
            ])
            # 校验通过：配置状态 503 + 模板渲染后的 body
            status, headers, raw = request(
                port, "POST", "/submit", body=b'{"token":"abc"}'
            )
            self.assertEqual(status, 503)
            self.assertEqual(
                json.loads(raw.decode("utf-8")), {"echo": "/submit"}
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))
            # 校验失败：400 与固定错误体，不回退到 /submit/ 前缀候选
            status, _, raw = request(
                port, "POST", "/submit", body=b'{"token":"wrong"}'
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"error": "request_body_mismatch"},
            )

    def test_load_rules_return_shape_and_defaults(self):
        # load_rules 返回格式保持兼容：body_modes 每条路由都有键，
        # 缺省记为 "fixed"；template_bodies 仅含 template 路由的原始值
        from mock_server import load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(tmp, "rules_shape.json", [
                {"method": "GET", "path": "/old", "body": {"ok": True}},
                {"method": "GET", "path": "/t", "bodyMode": "template",
                 "body": {"p": "{{request.path}}"}},
            ])
            routes = load_rules(rules_path)
            self.assertEqual(
                routes[("GET", "/old")], (200, b'{"ok":true}')
            )
            self.assertEqual(routes.body_modes[("GET", "/old")], "fixed")
            self.assertEqual(routes.body_modes[("GET", "/t")], "template")
            self.assertEqual(
                routes.template_bodies[("GET", "/t")],
                {"p": "{{request.path}}"},
            )
            self.assertNotIn(("GET", "/old"), routes.template_bodies)


SPEC_SUFFIX_ROUTES = [
    {"method": "GET", "path": "/files/", "pathMode": "prefix",
     "bodyMode": "template",
     "body": {"suffix": "{{request.pathSuffix}}"}},
    {"method": "GET", "path": "/files/images/", "pathMode": "prefix",
     "bodyMode": "template",
     "body": {"suffix": "{{request.pathSuffix}}"}},
    {"method": "GET", "path": "/files/ping", "bodyMode": "template",
     "body": {"suffix": "{{request.pathSuffix}}"}},
]


class PathSuffixTemplateTests(unittest.TestCase):
    """{{request.pathSuffix}}：prefix 命中回显选中前缀之后的剩余文本，
    exact 命中固定为空字符串；其余模板规则与既有占位符一致。"""

    def _start(self, tmp, name, routes):
        rules_path = write_rules(tmp, name, routes)
        port = free_port()
        server = ServerProcess(rules_path, port)
        self.addCleanup(server.stop)
        return port

    def test_spec_acceptance(self):
        # 规格样例：GET /files/images/A%2Fb/detail/?x=1 -> 200
        # {"suffix":"A%2Fb/detail/"}；GET /files/ping?x=1 -> 200
        # {"suffix":""}
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_suffix_spec.json", SPEC_SUFFIX_ROUTES)
            status, headers, raw = request(
                port, "GET", "/files/images/A%2Fb/detail/?x=1"
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Type"), CONTENT_TYPE)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"suffix": "A%2Fb/detail/"},
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))
            status, headers, raw = request(port, "GET", "/files/ping?x=1")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")), {"suffix": ""}
            )
            self.assertEqual(raw, b'{"suffix":""}')
            self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_rule_order_does_not_change_suffix(self):
        # 调换规则顺序：最长前缀选择与 suffix 文本均不变
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(
                tmp,
                "rules_suffix_reversed.json",
                list(reversed(SPEC_SUFFIX_ROUTES)),
            )
            for target, expected in (
                ("/files/images/A%2Fb/detail/?x=1", "A%2Fb/detail/"),
                ("/files/images/x", "x"),
                ("/files/other/x", "other/x"),
                ("/files/ping?x=1", ""),
            ):
                with self.subTest(target=target):
                    status, headers, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"suffix": expected},
                    )
                    self.assertEqual(
                        int(headers["Content-Length"]), len(raw)
                    )

    def test_consecutive_requests_echo_their_own_suffixes(self):
        # 连续请求不同路径只回显各自的剩余文本，互不沿用（同一连接）
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(
                tmp, "rules_suffix_sequence.json", SPEC_SUFFIX_ROUTES
            )
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                sequence = [
                    ("/files/images/A%2Fb/detail/?x=1", "A%2Fb/detail/"),
                    ("/files/plain", "plain"),
                    ("/files/ping?y=2", ""),
                    ("/files/images//Tail/", "/Tail/"),
                    ("/files/images/A%2Fb/detail/?x=1", "A%2Fb/detail/"),
                ]
                for target, expected in sequence:
                    with self.subTest(target=target):
                        conn.request("GET", target)
                        resp = conn.getresponse()
                        raw = resp.read()
                        self.assertEqual(resp.status, 200)
                        self.assertEqual(
                            json.loads(raw.decode("utf-8")),
                            {"suffix": expected},
                        )
                        self.assertEqual(
                            int(resp.getheader("Content-Length")), len(raw)
                        )
            finally:
                conn.close()

    def test_remainder_kept_verbatim_without_decode_or_normalization(self):
        # 剩余文本保留大小写、多级路径、连续斜杠、尾斜杠与百分号转义，
        # 不解码、不规范化；查询串与片段不计入。原始 UTF-8 段按 UTF-8
        # 回显，Content-Length 按最终字节数计算
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_suffix_verbatim.json", [
                {"method": "GET", "path": "/files/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
            ])
            cases = [
                ("/files/A%2Fb/detail/?x=1", "A%2Fb/detail/"),
                ("/files/Mixed//Double///", "Mixed//Double///"),
                ("/files/a/b/c", "a/b/c"),
                ("/files/%ZZ%zz%2f", "%ZZ%zz%2f"),
                ("/files/UPPER", "UPPER"),
                ("/files/a#frag-ment", "a"),
                ("/files/a?x=1#frag", "a"),
                ("/files/%E4%BD%A0", "%E4%BD%A0"),
            ]
            for target, expected in cases:
                with self.subTest(target=target):
                    status, headers, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    expected_raw = json.dumps(
                        {"suffix": expected},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.assertEqual(raw, expected_raw)
                    self.assertEqual(
                        int(headers["Content-Length"]), len(expected_raw)
                    )

    def test_exact_hit_always_has_empty_suffix(self):
        # 精确路由（根路径、普通路径、尾斜杠路径）命中时 suffix 固定为空，
        # 与同路径是否存在 prefix 配置无关
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_suffix_exact.json", [
                {"method": "GET", "path": "/e", "bodyMode": "template",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
                {"method": "GET", "path": "/e/", "bodyMode": "template",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
                {"method": "GET", "path": "/", "bodyMode": "template",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
            ])
            for target in ("/e", "/e?x=1", "/e/", "/"):
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(raw, b'{"suffix":""}')

    def test_root_prefix_strips_only_one_leading_slash(self):
        # 根前缀 / 只去掉开头的一个斜杠：suffix 是路径中紧跟首斜杠之后的
        # 全部文本，内部的连续斜杠与尾斜杠原样保留。注：标准库 HTTP
        # 处理器会把以多个斜杠开头的请求目标（// 形式，客户端视作网络
        # 路径）收敛为单斜杠，因此经 HTTP 到达根前缀的 suffix 不会以
        # 斜杠开头，这是与 {{request.path}} 一致的既有传输层行为
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_suffix_root.json", [
                {"method": "GET", "path": "/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
            ])
            cases = [
                ("/a", "a"),
                ("/a//b", "a//b"),
                ("/a///b/", "a///b/"),
                ("/a/b/", "a/b/"),
                ("/files/x?z=9", "files/x"),
            ]
            for target, expected in cases:
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"suffix": expected},
                    )
            # 路径与前缀相等（剩余为空）仍不命中根前缀规则
            status, _, raw = request(port, "GET", "/")
            self.assertEqual(status, 404)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"error": "route_not_found"},
            )

    def test_empty_remainder_does_not_match_prefix(self):
        # 请求路径与某个前缀相等时不命中该前缀规则（即使模板引用 suffix）。
        # 只剩自身这一条前缀时返回 404；若还存在更短前缀，则按更短前缀的
        # 非空剩余文本命中（/files/images/ -> /files/ -> "images/"）
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port_a = self._start(tmp, "rules_suffix_empty_a.json", [
                {"method": "GET", "path": "/files/images/",
                 "pathMode": "prefix", "bodyMode": "template",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
            ])
            status, _, raw = request(port_a, "GET", "/files/images/")
            self.assertEqual(status, 404)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"error": "route_not_found"},
            )

            port_b = self._start(tmp, "rules_suffix_empty_b.json", [
                {"method": "GET", "path": "/files/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
            ])
            status, _, raw = request(port_b, "GET", "/files/")
            self.assertEqual(status, 404)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"error": "route_not_found"},
            )

            # 完整规则集：/files/images/ 与最长前缀相等，但仍是更短前缀
            # /files/ 的非空剩余部分，故按更短前缀命中
            port_c = self._start(
                tmp, "rules_suffix_empty_c.json", SPEC_SUFFIX_ROUTES
            )
            status, _, raw = request(port_c, "GET", "/files/images/")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")), {"suffix": "images/"}
            )

    def test_longest_prefix_owns_the_suffix(self):
        # 多个前缀候选时选最长者：suffix 只去掉最长前缀
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(
                tmp, "rules_suffix_longest.json", SPEC_SUFFIX_ROUTES
            )
            for target, expected in (
                ("/files/images/a/b", "a/b"),
                ("/files/imagesx", "imagesx"),
                ("/files/x", "x"),
            ):
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"suffix": expected},
                    )

    def test_nested_repeated_mixed_values_replaced(self):
        # 沿用既有模板规则：顶层与嵌套对象、数组中的字符串值均可替换，
        # 支持嵌入、重复及与已有占位符混用；对象键、非字符串值与 JSON
        # 结构不变；带空格或大小写不同的写法原样保留
        body = {
            "{{request.pathSuffix}}": "键名保持原样",
            "s": "{{request.pathSuffix}}",
            "repeat": "<{{request.pathSuffix}}><{{request.pathSuffix}}>",
            "embed": "x{{request.pathSuffix}}y",
            "mix": "{{request.path}}|[{{request.pathSuffix}}]|"
                   "{{request.method}}|{{request.query}}",
            "nested": {"list": ["{{request.pathSuffix}}", 1, 1.5, True,
                                None, ["x{{request.pathSuffix}}y"]]},
            "spaced": "{{ request.pathSuffix }}",
            "case": "{{Request.PathSuffix}}",
            "other": "{{request.suffix}}",
        }
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_suffix_nested.json", [
                {"method": "GET", "path": "/p/", "pathMode": "prefix",
                 "bodyMode": "template", "body": body},
                {"method": "GET", "path": "/e", "bodyMode": "template",
                 "body": body},
            ])
            status, headers, raw = request(port, "GET", "/p/a/b?z=9")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {
                    "{{request.pathSuffix}}": "键名保持原样",
                    "s": "a/b",
                    "repeat": "<a/b><a/b>",
                    "embed": "xa/by",
                    "mix": "/p/a/b|[a/b]|GET|z=9",
                    "nested": {"list": ["a/b", 1, 1.5, True, None,
                                        ["xa/by"]]},
                    "spaced": "{{ request.pathSuffix }}",
                    "case": "{{Request.PathSuffix}}",
                    "other": "{{request.suffix}}",
                },
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))
            # exact 命中：suffix 固定为空，其余占位符照常渲染
            status, _, raw = request(port, "GET", "/e")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {
                    "{{request.pathSuffix}}": "键名保持原样",
                    "s": "",
                    "repeat": "<><>",
                    "embed": "xy",
                    "mix": "/e|[]|GET|",
                    "nested": {"list": ["", 1, 1.5, True, None, ["xy"]]},
                    "spaced": "{{ request.pathSuffix }}",
                    "case": "{{Request.PathSuffix}}",
                    "other": "{{request.suffix}}",
                },
            )

    def test_suffix_replacement_text_is_not_reprocessed(self):
        # 替换产生的文本不再次展开：剩余路径中的占位符形态与百分号转义
        # 都按普通字面量保留
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_suffix_reprocess.json", [
                {"method": "GET", "path": "/p/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"t": "{{request.pathSuffix}}"}},
            ])
            status, _, raw = request(
                port, "GET", "/p/x/{{request.pathSuffix}}"
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"t": "x/{{request.pathSuffix}}"},
            )
            status, _, raw = request(
                port, "GET",
                "/p/" + "%7B%7Brequest.pathSuffix%7D%7D",
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"t": "%7B%7Brequest.pathSuffix%7D%7D"},
            )

    def test_fixed_and_default_mode_keep_suffix_placeholder_verbatim(self):
        # fixed 及省略 bodyMode 时 {{request.pathSuffix}} 原样返回
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_suffix_fixed.json", [
                {"method": "GET", "path": "/default/", "pathMode": "prefix",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
                {"method": "GET", "path": "/fixed/", "pathMode": "prefix",
                 "bodyMode": "fixed",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
            ])
            for target in ("/default/a", "/fixed/a"):
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"suffix": "{{request.pathSuffix}}"},
                    )

    def test_suffix_echo_works_for_post(self):
        # GET 与 POST 均可使用：POST 前缀模板路由同样回显剩余文本
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_suffix_post.json", [
                {"method": "POST", "path": "/files/", "pathMode": "prefix",
                 "bodyMode": "template",
                 "body": {"suffix": "{{request.pathSuffix}}"}},
                {"method": "POST", "path": "/submit",
                 "bodyMode": "template", "requestBody": {"ok": True},
                 "body": {"suffix": "{{request.pathSuffix}}"}},
                {"method": "POST", "path": "/files/alt/",
                 "pathMode": "prefix", "body": {"v": "fallback"}},
            ])
            status, headers, raw = request(
                port, "POST", "/files/upload?a=1", body=b'{"anything":1}'
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")), {"suffix": "upload"}
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))
            # exact POST 命中 suffix 为空
            status, _, raw = request(
                port, "POST", "/submit?z=1", body=b'{"ok":true}'
            )
            self.assertEqual(status, 200)
            self.assertEqual(raw, b'{"suffix":""}')
            # 正文样例校验失败：400 request_body_mismatch，不采用配置响应
            # 或延迟，不尝试其他路由，模板不渲染
            status, _, raw = request(
                port, "POST", "/submit?z=1", body=b'{"ok":false}'
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"error": "request_body_mismatch"},
            )


class QueryParamTemplateTests(unittest.TestCase):
    """{{request.queryParam.name}}：按本次原始查询串读取单个参数的第一个
    原始值，不解码、不转换、区分大小写；其余模板规则与既有占位符一致。"""

    def _start(self, tmp, name, routes):
        rules_path = write_rules(tmp, name, routes)
        port = free_port()
        server = ServerProcess(rules_path, port)
        self.addCleanup(server.stop)
        return port

    def test_spec_acceptance_examples(self):
        # 规格样例：模板 body 为 {"value":"{{request.queryParam.name}"}，
        # 省略 status；?name=A%2Fb+Z&name=other 取第一个原始值；
        # ?name=&name=later 第一个值为空也不跳过
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_spec.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"value": "{{request.queryParam.name}}"}},
            ])
            for target, expected in (
                ("/echo?name=A%2Fb+Z&name=other",
                 b'{"value":"A%2Fb+Z"}'),
                ("/echo?name=&name=later", b'{"value":""}'),
            ):
                with self.subTest(target=target):
                    status, headers, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(raw, expected)
                    self.assertEqual(
                        int(headers["Content-Length"]), len(raw)
                    )

    def test_first_match_wins_even_when_first_is_empty(self):
        # 重复参数始终取从左到右第一个片段：第一个为空（无论有无等号）
        # 都不被后续非空片段覆盖
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_first.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"a": "{{request.queryParam.a}}",
                          "b": "{{request.queryParam.b}}"}},
            ])
            cases = [
                ("/echo?a=1&a=2&a=3", {"a": "1", "b": ""}),
                ("/echo?a=&a=later", {"a": "", "b": ""}),
                ("/echo?a&a=later", {"a": "", "b": ""}),
                ("/echo?b=first&other=1&b=second", {"a": "", "b": "first"}),
            ]
            for target, expected in cases:
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")), expected
                    )

    def test_missing_param_no_query_and_trailing_question_mark_empty(self):
        # 缺失参数、没有查询串或仅有结尾问号均替换为空字符串；
        # 空片段（含结尾 &）忽略，不影响取值
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_missing.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"v": "x{{request.queryParam.name}}y"}},
            ])
            for target in (
                "/echo?other=1",
                "/echo",
                "/echo?",
                "/echo?&",
                "/echo?=name&other=1",
            ):
                with self.subTest(target=target):
                    status, headers, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(raw, b'{"v":"xy"}')
                    self.assertEqual(
                        int(headers["Content-Length"]), len(raw)
                    )

    def test_value_kept_verbatim_no_decode_no_plus_no_type_conversion(self):
        # 值为第一个 = 之后的全部文本（值内可继续含 =）；不解码百分号、
        # + 不变空格、数字/布尔/null 文本不转换类型，%ZZ 原样返回不产生
        # 400；多字节 UTF-8 百分号文本同样原样回显
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_verbatim.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"v": "{{request.queryParam.x}}"}},
            ])
            cases = [
                ("x=A%2Fb+Z", "A%2Fb+Z"),
                ("x=a+b+c", "a+b+c"),
                ("x=a=b=c", "a=b=c"),
                ("x=123", "123"),
                ("x=true", "true"),
                ("x=null", "null"),
                ("x=%ZZ&x=%2f", "%ZZ"),
                ("x=%E4%BD%A0%E5%A5%BD", "%E4%BD%A0%E5%A5%BD"),
                ("x=1&", "1"),
                ("x=1&&x=2", "1"),
            ]
            for raw_target, expected_value in cases:
                with self.subTest(target=raw_target):
                    status, headers, raw = request(
                        port, "GET", "/echo?" + raw_target
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"v": expected_value},
                    )
                    self.assertEqual(
                        int(headers["Content-Length"]), len(raw)
                    )

    def test_name_rules_and_case_sensitivity(self):
        # 名称首字符限 ASCII 字母，后续限字母、数字、下划线，区分大小写
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_names.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {
                     "a": "{{request.queryParam.a}}",
                     "A": "{{request.queryParam.A}}",
                     "name": "{{request.queryParam.name}}",
                     "n2": "{{request.queryParam.n2}}",
                     "x_y": "{{request.queryParam.x_y}}",
                     "Z9_": "{{request.queryParam.Z9_}}",
                 }},
            ])
            target = "/echo?a=lower&A=upper&name=n&n2=2&x_y=u&Z9_=z&a=skip"
            status, _, raw = request(port, "GET", target)
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"a": "lower", "A": "upper", "name": "n", "n2": "2",
                 "x_y": "u", "Z9_": "z"},
            )
            # 查询串中名称可以是任意文本；占位符名称规则之外的名称无法
            # 通过占位符引用，例如以数字开头的参数名没有对应合法占位符
            status, _, raw = request(port, "GET", "/echo?2x=9")
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"a": "", "A": "", "name": "", "n2": "", "x_y": "",
                 "Z9_": ""},
            )

    def test_malformed_and_unknown_placeholders_kept_verbatim(self):
        # 前缀大小写不符、整体或参数名内部带空格、参数名缺失或不合名称
        # 规则、缺少 }}、未知占位符均保持原样，不尝试取值
        body = {
            "spaced_outer": "{{ request.queryParam.x }}",
            "spaced_name": "{{request.queryParam. x}}",
            "lower_prefix": "{{request.queryparam.x}}",
            "upper_request": "{{Request.QueryParam.x}}",
            "missing_name": "{{request.queryParam.}}",
            "leading_digit": "{{request.queryParam.1x}}",
            "hyphen": "{{request.queryParam.x-y}}",
            "dot": "{{request.queryParam.x.y}}",
            "unclosed": "{{request.queryParam.x",
            "unknown": "{{request.queryParameter.x}}",
            "other": "{{request.foo}}",
            "good": "[{{request.queryParam.x}}]",
        }
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_malformed.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": body},
            ])
            status, _, raw = request(port, "GET", "/echo?x=9")
            self.assertEqual(status, 200)
            expected = dict(body)
            expected["good"] = "[9]"
            self.assertEqual(json.loads(raw.decode("utf-8")), expected)

    def test_nested_repeated_embedded_mixed_values_replaced(self):
        # 顶层及嵌套对象、数组中的字符串值均替换；嵌入、重复及与已有四个
        # 占位符混用都生效；对象键、非字符串值与 JSON 结构不变
        body = {
            "{{request.queryParam.a}}": "键名保持原样",
            "embed": "x{{request.queryParam.a}}y",
            "repeat": "<{{request.queryParam.a}}><{{request.queryParam.a}}>",
            "mix": "{{request.path}}|{{request.method}}|{{request.query}}|"
                   "{{request.pathSuffix}}|{{request.queryParam.a}}",
            "nested": {"list": ["{{request.queryParam.a}}", 1, 1.5, True,
                                None, ["z={{request.queryParam.b}}"]]},
        }
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_nested.json", [
                {"method": "GET", "path": "/p/", "pathMode": "prefix",
                 "bodyMode": "template", "body": body},
            ])
            status, headers, raw = request(
                port, "GET", "/p/sub?a=1&b=2&a=3"
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {
                    "{{request.queryParam.a}}": "键名保持原样",
                    "embed": "x1y",
                    "repeat": "<1><1>",
                    "mix": "/p/sub|GET|a=1&b=2&a=3|sub|1",
                    "nested": {"list": ["1", 1, 1.5, True, None,
                                        ["z=2"]]},
                },
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_replacement_text_is_not_reprocessed(self):
        # 参数值中即使含占位符形态（含 queryParam 自身）也不再展开
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_reprocess.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {
                     "a": "{{request.queryParam.a}}",
                     "b": "{{request.queryParam.b}}",
                 }},
            ])
            target = (
                "/echo?a=%7B%7Brequest.queryParam.b%7D%7D"
                "&b=LEAKED"
            )
            status, _, raw = request(port, "GET", target)
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"a": "%7B%7Brequest.queryParam.b%7D%7D",
                 "b": "LEAKED"},
            )
            # 未编码的原始占位符文本作为值时同样不再扫描
            status, _, raw = request(
                port, "GET",
                "/echo?a={{request.queryParam.b}}&b=LEAKED",
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"a": "{{request.queryParam.b}}", "b": "LEAKED"},
            )

    def test_each_request_uses_its_own_query_on_same_connection(self):
        # 同一连接连续请求：参数取值只来自本次查询串，不沿用前次结果
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_conn.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"v": "{{request.queryParam.name}}"}},
            ])
            conn = HTTPConnection("127.0.0.1", port, timeout=REQUEST_TIMEOUT)
            try:
                for target, expected in (
                    ("/echo?name=first", b'{"v":"first"}'),
                    ("/echo?name=second", b'{"v":"second"}'),
                    ("/echo", b'{"v":""}'),
                    ("/echo?other=1", b'{"v":""}'),
                    ("/echo?name=again", b'{"v":"again"}'),
                ):
                    conn.request("GET", target)
                    resp = conn.getresponse()
                    raw = resp.read()
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(raw, expected)
            finally:
                conn.close()

    def test_fragment_after_hash_excluded(self):
        # # 之后的片段不属于查询串：其中的同名参数不能被取到
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_frag.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"a": "{{request.queryParam.a}}",
                          "b": "{{request.queryParam.b}}"}},
            ])
            status, _, raw = request(
                port, "GET", "/echo?a=1#b=2&a=99"
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")), {"a": "1", "b": ""}
            )

    def test_fixed_and_default_mode_keep_placeholder_verbatim(self):
        # 省略 bodyMode 或取 fixed 时 queryParam 占位符原样返回
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_fixed.json", [
                {"method": "GET", "path": "/default",
                 "body": {"v": "{{request.queryParam.name}}"}},
                {"method": "GET", "path": "/fixed", "bodyMode": "fixed",
                 "body": {"v": "{{request.queryParam.name}}"}},
            ])
            for target in ("/default?name=x", "/fixed?name=x"):
                with self.subTest(target=target):
                    status, _, raw = request(port, "GET", target)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"v": "{{request.queryParam.name}}"},
                    )

    def test_post_renders_but_body_mismatch_returns_400_without_render(self):
        # POST 模板路由同样按本次查询串渲染；requestBody 校验失败仍返回
        # 400 request_body_mismatch，不渲染模板
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_qp_post.json", [
                {"method": "POST", "path": "/echo", "bodyMode": "template",
                 "body": {"v": "{{request.queryParam.name}}"}},
                {"method": "POST", "path": "/checked", "bodyMode": "template",
                 "requestBody": {"ok": True},
                 "body": {"v": "{{request.queryParam.name}}"}},
            ])
            status, headers, raw = request(
                port, "POST", "/echo?name=A%2Fb+Z", body=b'{"anything":1}'
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8")),
                             {"v": "A%2Fb+Z"})
            self.assertEqual(int(headers["Content-Length"]), len(raw))
            # 校验通过
            status, _, raw = request(
                port, "POST", "/checked?name=ok", body=b'{"ok":true}'
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw.decode("utf-8")),
                             {"v": "ok"})
            # 校验失败：固定 400 正文，模板不渲染、配置状态不采用
            status, _, raw = request(
                port, "POST", "/checked?name=should_not_appear",
                body=b'{"ok":false}',
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"error": "request_body_mismatch"},
            )

    def test_rules_load_and_check_rules_unaffected_by_placeholders(self):
        # 占位符只是 body 字符串文本：不合规则的写法（缺名称、首字符数字
        # 等）不影响规则加载与 --check-rules
        from mock_server import load_rules

        weird = "{{request.queryParam.}} {{request.queryParam.1x}} " \
                "{{ request.queryParam.x }} {{request.queryparam.x}}"
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(tmp, "rules_qp_load.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"v": weird}},
            ])
            routes = load_rules(rules_path)
            self.assertEqual(routes.body_modes[("GET", "/echo")],
                             "template")
        # CLI --check-rules 同样通过
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(tmp, "rules_qp_check.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"v": weird}},
            ])
            proc = subprocess.run(
                [sys.executable, "-m", "mock_server",
                 "--rules", str(rules_path), "--check-rules"],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(proc.returncode, 0,
                             proc.stderr.decode("utf-8", "replace"))
            self.assertIn(b"rules valid", proc.stdout)


class HeaderTemplateTests(unittest.TestCase):
    """{{request.header.name}}：按本次请求头取标准库解析后的第一个值，
    头名称匹配不区分大小写、缺失或空值为空字符串、同名头只取第一项；
    其余模板规则与既有占位符一致。"""

    def _start(self, tmp, name, routes):
        rules_path = write_rules(tmp, name, routes)
        port = free_port()
        server = ServerProcess(rules_path, port)
        self.addCleanup(server.stop)
        return port

    @staticmethod
    def _exchange(conn, method, target, headers=(), body=None):
        # headers 允许为 (名称, 值) 元组序列，同名元组按顺序各发一行，
        # 可构造重复请求头（HTTPConnection.request 只接受映射、无法发送
        # 同名头，故直接用 putrequest/putheader）；返回 (状态码, 响应头,
        # 原始响应体)
        conn.putrequest(method, target)
        if isinstance(body, str):
            body = body.encode("utf-8")
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders()
        if body is not None:
            conn.send(body)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k: v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw

    def _echo_route(self):
        return {"method": "GET", "path": "/echo",
                "bodyMode": "template",
                "body": {"trace": "{{request.header.X-Trace-Id}}"}}

    def test_spec_acceptance_with_shipped_rules_on_same_connection(self):
        # 验收：以启动入口加载自带 rules.json 的 GET /echo 模板，
        # x-trace-id: A%2Fb+Z 返回 {"trace":"A%2Fb+Z"}；同一连接下一次
        # 不带该头返回 {"trace":""}，不沿用上一次值
        rules_path = PROJECT_ROOT / "rules.json"
        port = free_port()
        server = ServerProcess(rules_path, port)
        try:
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                status, headers, raw = self._exchange(
                    conn, "GET", "/echo",
                    headers=[("x-trace-id", "A%2Fb+Z")],
                )
                self.assertEqual(status, 200)
                self.assertEqual(raw, b'{"trace":"A%2Fb+Z"}')
                self.assertEqual(int(headers["Content-Length"]), len(raw))
                status, headers, raw = self._exchange(conn, "GET", "/echo")
                self.assertEqual(status, 200)
                self.assertEqual(raw, b'{"trace":""}')
                self.assertEqual(int(headers["Content-Length"]), len(raw))
            finally:
                conn.close()
        finally:
            server.stop()

    def test_value_kept_verbatim_no_trim_no_decode_no_split_no_conversion(
        self,
    ):
        # 头值取标准库 HTTP 解析后的文本：不额外去除首尾空白（字段值前导
        # OWS 由标准库解析器按协议处理，尾部空白保留），不做 URL 解码、
        # 不按逗号分割、'+' 不转空格、文本不转换类型
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_verbatim.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"v": "{{request.header.X-V}}"}},
            ])
            cases = [
                ("A%2Fb+Z", "A%2Fb+Z"),
                ("a+b+c", "a+b+c"),
                ("a,b,c", "a,b,c"),
                ("123", "123"),
                ("true", "true"),
                ("null", "null"),
                ("%ZZ", "%ZZ"),
                ("trail  ", "trail  "),
                ('a"b\\c', 'a"b\\c'),
            ]
            for value, expected in cases:
                with self.subTest(value=value):
                    conn = HTTPConnection(
                        "127.0.0.1", port, timeout=REQUEST_TIMEOUT
                    )
                    try:
                        status, resp_headers, raw = self._exchange(
                            conn, "GET", "/echo",
                            headers=[("X-V", value)],
                        )
                    finally:
                        conn.close()
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")), {"v": expected}
                    )
                    self.assertEqual(
                        int(resp_headers["Content-Length"]), len(raw)
                    )

    def test_duplicate_headers_first_wins_even_when_first_empty(self):
        # 同名头按报文从上到下只取第一项：首项为空也不跳过、不拼接
        # 后续项；不同大小写写法的同名头同样只取第一项
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_first.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"t": "{{request.header.X-Trace-Id}}",
                          "e": "{{request.header.X-Empty}}"}},
            ])
            cases = [
                ([("X-Trace-Id", "first"), ("X-Trace-Id", "second")],
                 {"t": "first", "e": ""}),
                ([("x-trace-id", "low"), ("X-TRACE-ID", "up")],
                 {"t": "low", "e": ""}),
                # 第一项为空：不被后续非空项覆盖
                ([("X-Empty", ""), ("X-Empty", "later")],
                 {"t": "", "e": ""}),
            ]
            for req_headers, expected in cases:
                with self.subTest(headers=req_headers):
                    conn = HTTPConnection(
                        "127.0.0.1", port, timeout=REQUEST_TIMEOUT
                    )
                    try:
                        status, _, raw = self._exchange(
                            conn, "GET", "/echo", headers=req_headers
                        )
                    finally:
                        conn.close()
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")), expected
                    )

    def test_header_name_case_insensitive_but_placeholder_prefix_case_sensitive(
        self,
    ):
        # 头名称匹配不区分大小写；名称允许字母开头、后续为字母数字连字符；
        # 占位符前缀 request.header 区分大小写
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_names.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {
                     "trace": "{{request.header.X-Trace-Id}}",
                     "a": "{{request.header.a}}",
                     "b1": "{{request.header.B1}}",
                     "z_9": "{{request.header.Z-9}}",
                 }},
            ])
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                status, _, raw = self._exchange(
                    conn, "GET", "/echo",
                    headers=[("x-TRACE-id", "T"), ("A", "aa"),
                             ("b1", "bb"), ("z-9", "cc")],
                )
            finally:
                conn.close()
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"trace": "T", "a": "aa", "b1": "bb", "z_9": "cc"},
            )

    def test_missing_or_empty_header_replaced_with_empty_string(self):
        # 头缺失或字段值为空都替换为空字符串；嵌入形态同样塌缩
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_missing.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"plain": "{{request.header.X-Missing}}",
                          "embed": "x{{request.header.X-Missing}}y",
                          "empty": "[{{request.header.X-Empty}}]"}},
            ])
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                # 不带任何相关头
                status, headers, raw = self._exchange(conn, "GET", "/echo")
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"plain": "", "embed": "xy", "empty": "[]"},
                )
                self.assertEqual(int(headers["Content-Length"]), len(raw))
                # 显式空值头
                status, _, raw = self._exchange(
                    conn, "GET", "/echo", headers=[("X-Empty", "")]
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"plain": "", "embed": "xy", "empty": "[]"},
                )
            finally:
                conn.close()

    def test_malformed_and_unknown_placeholders_kept_verbatim(self):
        # 前缀大小写不符、整体或头名称内部带空格、名称缺失或不合名称
        # 规则、缺少 }}、未知占位符均保持原样，不尝试取值
        body = {
            "spaced_outer": "{{ request.header.X }}",
            "spaced_name": "{{request.header. X}}",
            "space_in_name": "{{request.header.x y}}",
            "lower_prefix": "{{request.Header.X}}",
            "plural": "{{request.headers.X}}",
            "missing_name": "{{request.header.}}",
            "leading_digit": "{{request.header.1x}}",
            "underscore": "{{request.header.x_y}}",
            "dot": "{{request.header.x.y}}",
            "double_hyphen_ok": "{{request.header.X--Y}}",
            "unclosed": "{{request.header.X",
            "unknown": "{{request.foo}}",
            "good": "[{{request.header.X-Good}}]",
        }
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_malformed.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": body},
            ])
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                status, _, raw = self._exchange(
                    conn, "GET", "/echo",
                    headers=[("X-Good", "9"), ("X", "skip"),
                             ("x_y", "skip"), ("1x", "skip")],
                )
            finally:
                conn.close()
            self.assertEqual(status, 200)
            expected = dict(body)
            # 名称中允许连字符，X--Y 是合法头名；该头未发送故为空
            expected["double_hyphen_ok"] = ""
            expected["good"] = "[9]"
            self.assertEqual(json.loads(raw.decode("utf-8")), expected)

    def test_nested_repeated_embedded_mixed_values_replaced(self):
        # 顶层及嵌套对象、数组中的字符串值均替换；嵌入、重复及与全部已有
        # 占位符混用都生效；对象键、非字符串值与 JSON 结构不变
        body = {
            "{{request.header.X-Trace-Id}}": "键名保持原样",
            "embed": "x{{request.header.X-Trace-Id}}y",
            "repeat": "<{{request.header.X-Trace-Id}}>"
                      "<{{request.header.X-Trace-Id}}>",
            "mix": "{{request.path}}|{{request.method}}|{{request.query}}|"
                   "{{request.pathSuffix}}|{{request.queryParam.a}}|"
                   "{{request.header.X-Trace-Id}}",
            "nested": {"list": ["{{request.header.X-Trace-Id}}", 1, 1.5,
                                True, None,
                                ["z={{request.header.X-Other}}"]]},
        }
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_nested.json", [
                {"method": "GET", "path": "/p/", "pathMode": "prefix",
                 "bodyMode": "template", "body": body},
            ])
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                status, headers, raw = self._exchange(
                    conn, "GET", "/p/sub?a=1&a=3",
                    headers=[("X-Trace-Id", "H1"), ("X-Other", "H2")],
                )
            finally:
                conn.close()
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {
                    "{{request.header.X-Trace-Id}}": "键名保持原样",
                    "embed": "xH1y",
                    "repeat": "<H1><H1>",
                    "mix": "/p/sub|GET|a=1&a=3|sub|1|H1",
                    "nested": {"list": ["H1", 1, 1.5, True, None,
                                        ["z=H2"]]},
                },
            )
            self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_replacement_text_is_not_reprocessed(self):
        # 头值中即使含占位符形态（含 header 自身）也不再展开
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_reprocess.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {
                     "t": "{{request.header.X-Trace-Id}}",
                     "p": "{{request.header.X-Path}}",
                 }},
            ])
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                status, _, raw = self._exchange(
                    conn, "GET", "/echo",
                    headers=[
                        ("X-Trace-Id", "{{request.header.X-Path}}"),
                        ("X-Path", "LEAKED"),
                    ],
                )
            finally:
                conn.close()
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"t": "{{request.header.X-Path}}", "p": "LEAKED"},
            )

    def test_each_request_uses_its_own_headers_on_same_connection(self):
        # 同一连接连续请求：头值只来自本次请求头，不沿用前次结果
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_conn.json",
                               [self._echo_route()])
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                for req_headers, expected in (
                    ([("X-Trace-Id", "first")], b'{"trace":"first"}'),
                    ([("X-Trace-Id", "second")], b'{"trace":"second"}'),
                    ((), b'{"trace":""}'),
                    ([("x-trace-id", "again")], b'{"trace":"again"}'),
                    ([("Other", "x")], b'{"trace":""}'),
                ):
                    status, resp_headers, raw = self._exchange(
                        conn, "GET", "/echo", headers=req_headers
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(raw, expected)
                    self.assertEqual(
                        int(resp_headers["Content-Length"]), len(raw)
                    )
            finally:
                conn.close()

    def test_headers_do_not_participate_in_routing_or_body_match(self):
        # 任意请求头不改变路由选择结果；头不参与 requestBody 比较：
        # 同样的正文带不带头都通过校验，且命中后模板才渲染头
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_route.json", [
                self._echo_route(),
                {"method": "GET", "path": "/api/", "pathMode": "prefix",
                 "body": {"v": 1}},
                {"method": "POST", "path": "/checked",
                 "bodyMode": "template",
                 "requestBody": {"ok": True},
                 "body": {"t": "{{request.header.X-Trace-Id}}"}},
            ])
            # 头不影响 exact/prefix 选择与 404
            for headers in ((), [("X-Trace-Id", "x")]):
                with self.subTest(headers=headers):
                    conn = HTTPConnection(
                        "127.0.0.1", port, timeout=REQUEST_TIMEOUT
                    )
                    try:
                        status, _, raw = self._exchange(
                            conn, "GET", "/api/x", headers=headers
                        )
                        self.assertEqual(status, 200)
                        self.assertEqual(
                            json.loads(raw.decode("utf-8")), {"v": 1}
                        )
                        status, _, raw = self._exchange(
                            conn, "GET", "/nope", headers=headers
                        )
                        self.assertEqual(status, 404)
                    finally:
                        conn.close()
            # 头不参与正文比较：正文相同即通过，通过后渲染头
            conn = HTTPConnection(
                "127.0.0.1", port, timeout=REQUEST_TIMEOUT
            )
            try:
                status, _, raw = self._exchange(
                    conn, "POST", "/checked",
                    headers=[("X-Trace-Id", "T")],
                    body=b'{"ok":true}',
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8")),
                                 {"t": "T"})
                status, _, raw = self._exchange(
                    conn, "POST", "/checked", body=b'{"ok":true}'
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw.decode("utf-8")),
                                 {"t": ""})
                # 正文不匹配仍返回固定 400，头值不泄漏到响应中
                status, _, raw = self._exchange(
                    conn, "POST", "/checked",
                    headers=[("X-Trace-Id", "should_not_appear")],
                    body=b'{"ok":false}',
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    json.loads(raw.decode("utf-8")),
                    {"error": "request_body_mismatch"},
                )
            finally:
                conn.close()

    def test_fixed_and_default_mode_keep_placeholder_verbatim(self):
        # 省略 bodyMode 或取 fixed 时 header 占位符原样返回
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            port = self._start(tmp, "rules_hdr_fixed.json", [
                {"method": "GET", "path": "/default",
                 "body": {"v": "{{request.header.X-Trace-Id}}"}},
                {"method": "GET", "path": "/fixed", "bodyMode": "fixed",
                 "body": {"v": "{{request.header.X-Trace-Id}}"}},
            ])
            for target in ("/default", "/fixed"):
                with self.subTest(target=target):
                    conn = HTTPConnection(
                        "127.0.0.1", port, timeout=REQUEST_TIMEOUT
                    )
                    try:
                        status, _, raw = self._exchange(
                            conn, "GET", target,
                            headers=[("X-Trace-Id", "x")],
                        )
                    finally:
                        conn.close()
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        json.loads(raw.decode("utf-8")),
                        {"v": "{{request.header.X-Trace-Id}}"},
                    )

    def test_rules_load_and_check_rules_unaffected_by_placeholders(self):
        # 占位符只是 body 字符串文本：不合规则的写法（缺名称、首字符
        # 数字、下划线等）不影响规则加载与 --check-rules
        weird = "{{request.header.}} {{request.header.1x}} " \
                "{{request.header.x_y}} {{ request.header.X }} " \
                "{{request.Header.X}}"
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(tmp, "rules_hdr_load.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"v": weird}},
            ])
            from mock_server import load_rules
            routes = load_rules(rules_path)
            self.assertEqual(routes.body_modes[("GET", "/echo")],
                             "template")
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(tmp, "rules_hdr_check.json", [
                {"method": "GET", "path": "/echo", "bodyMode": "template",
                 "body": {"v": weird}},
            ])
            proc = subprocess.run(
                [sys.executable, "-m", "mock_server",
                 "--rules", str(rules_path), "--check-rules"],
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(proc.returncode, 0,
                             proc.stderr.decode("utf-8", "replace"))
            self.assertIn(b"rules valid", proc.stdout)


INVALID_BODY_MODES = [
    ('大小写不同 "Fixed"', "Fixed"),
    ('大小写不同 "TEMPLATE"', "TEMPLATE"),
    ("null", None),
    ("布尔 true", True),
    ("数字 1", 1),
    ("空数组 []", []),
    ("空对象 {}", {}),
]


class InvalidBodyModeTests(unittest.TestCase):
    """非法 bodyMode：load_rules 抛 RulesError，CLI 退出码 2 且不监听。"""

    def test_load_rules_raises_rules_error(self):
        from mock_server import RulesError, load_rules

        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, bad_mode) in enumerate(INVALID_BODY_MODES):
                with self.subTest(样例=label):
                    rules_path = write_rules(
                        tmp, f"rules_bm_{index}.json",
                        [{"method": "GET", "path": "/x",
                          "bodyMode": bad_mode, "body": {}}],
                    )
                    with self.assertRaises(
                        RulesError,
                        msg=f"样例 {label}: load_rules 应抛出 RulesError",
                    ) as ctx:
                        load_rules(rules_path)
                    message = str(ctx.exception)
                    # 错误消息应指出路由位置及 bodyMode
                    self.assertIn("routes[0].bodyMode", message)
                    self.assertIn(repr(bad_mode), message)

    def test_cli_rejects_invalid_body_mode_with_exit_code_2(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            for index, (label, bad_mode) in enumerate(INVALID_BODY_MODES):
                with self.subTest(样例=label):
                    # 非法项位于合法路由之后：错误应标明实际下标
                    rules_path = write_rules(
                        tmp, f"rules_bm_cli_{index}.json",
                        [
                            {"method": "GET", "path": "/ok",
                             "body": {"fine": 1}},
                            {"method": "GET", "path": "/x",
                             "bodyMode": bad_mode, "body": {}},
                        ],
                    )
                    returncode, stdout, stderr = start_and_wait_exit(
                        rules_path, free_port()
                    )
                    self.assertEqual(
                        returncode, 2,
                        f"样例 {label}: 期望退出码 2，实际 {returncode}；"
                        f"stdout={stdout!r} stderr={stderr!r}",
                    )
                    self.assertIn("routes[1].bodyMode", stderr)
                    self.assertNotIn(
                        "Traceback", stderr,
                        f"样例 {label}: 不应出现 Python 异常回溯，"
                        f"实际 stderr={stderr!r}",
                    )
                    self.assertNotIn(
                        STARTUP_MARKER, stdout,
                        f"样例 {label}: 校验失败时不应监听，"
                        f"实际 stdout={stdout!r}",
                    )


# ---------------------------------------------------------------------------
# 正文校验 + 模板渲染 + 路由延迟组合回归
#
# 一条带 requestBody 的延迟模板 POST 路由：正文校验通过才进入既有流程
# （读完正文后先等待 delayMs，再按本次请求渲染模板并返回配置状态）；
# 校验失败立即返回 400 与 request_body_mismatch，不采用配置的状态、
# 模板正文或延迟。计时口径与 DelayBehaviorTests 一致：从发送请求前量到
# 响应状态行与响应头接收完毕。
# ---------------------------------------------------------------------------


class DelayedTemplateCheckedRouteTests(unittest.TestCase):
    """同一连接上先后验证：校验通过走延迟模板响应，校验失败立即 400。"""

    ROUTE = {
        "method": "POST",
        "path": "/checked",
        "requestBody": {"ok": True},
        "bodyMode": "template",
        "status": 503,
        "delayMs": DELAY_MS,
        "body": {
            "text": "你好 {{request.method}} {{request.path}}?{{request.query}}"
        },
    }

    @staticmethod
    def _timed_post(conn, target, body):
        """在既有连接上发 POST，返回 (状态码, 响应头, 响应体字节, 到响应头的耗时)。"""
        start = time.monotonic()
        conn.request("POST", target, body=body)
        resp = conn.getresponse()
        elapsed = time.monotonic() - start
        raw = resp.read()
        headers = {k: v for k, v in resp.getheaders()}
        return resp.status, headers, raw, elapsed

    def _assert_json_response(self, label, headers, raw, expected_body):
        content_type = headers.get("Content-Type")
        self.assertEqual(
            content_type, CONTENT_TYPE,
            f"{label}: Content-Type 应为 {CONTENT_TYPE!r}，实际 {content_type!r}",
        )
        content_length = headers.get("Content-Length")
        self.assertIsNotNone(content_length, f"{label}: 缺少 Content-Length")
        self.assertEqual(
            int(content_length), len(raw),
            f"{label}: Content-Length={content_length} "
            f"与实际响应体字节数 {len(raw)} 不符",
        )
        # 中文内容必须能按 UTF-8 正确解析，且 JSON 值符合预期
        actual_body = json.loads(raw.decode("utf-8"))
        self.assertEqual(
            actual_body, expected_body,
            f"{label}: 响应 JSON 应为 {expected_body}，实际 {actual_body}",
        )

    def test_body_validation_gates_delay_and_template_response(self):
        with tempfile.TemporaryDirectory(prefix="mock_server_test_") as tmp:
            rules_path = write_rules(tmp, "rules_checked.json", [self.ROUTE])
            port = free_port()
            server = ServerProcess(rules_path, port)
            try:
                conn = HTTPConnection(
                    "127.0.0.1", port, timeout=REQUEST_TIMEOUT
                )
                try:
                    # 第一次：正文与样例一致；查询串含加号、空值、无等号
                    # 片段与百分号转义，应原样进入模板渲染
                    ok = self._timed_post(
                        conn, "/checked?tag=a+z&tag=&flag&x=%2f",
                        b'{"ok":true}',
                    )
                    # 读完响应后在同一连接上再发：正文不匹配样例
                    bad = self._timed_post(
                        conn, "/checked?x=other", b'{"ok":false}',
                    )
                finally:
                    conn.close()

                # 校验通过：先等待 delayMs 再开始响应，随后返回配置的
                # 503 与按本次请求渲染的模板正文（查询串保持原样）
                status, headers, raw, elapsed = ok
                self.assertGreaterEqual(
                    elapsed, DELAY_MIN_SECONDS,
                    f"校验通过：响应开始应不早于 {DELAY_MS}ms，"
                    f"实际 {elapsed * 1000:.1f}ms",
                )
                self.assertEqual(status, 503)
                self._assert_json_response(
                    "校验通过", headers, raw,
                    {"text": "你好 POST /checked?tag=a+z&tag=&flag&x=%2f"},
                )

                # 校验失败：立即返回 400，不采用配置的 503、模板正文或延迟
                status, headers, raw, elapsed = bad
                self.assertLess(
                    elapsed, NO_DELAY_MAX_SECONDS,
                    f"校验失败：不应等待配置的 {DELAY_MS}ms，"
                    f"实际 {elapsed * 1000:.1f}ms",
                )
                self.assertEqual(status, 400)
                self._assert_json_response(
                    "校验失败", headers, raw,
                    {"error": "request_body_mismatch"},
                )
            finally:
                server.stop()


# ---------------------------------------------------------------------------
# --check-rules 规则检查入口回归
#
# 下列用例只针对规则检查入口：用临时规则文件经
# `python -m mock_server --check-rules` 的真实命令行启动子进程，核对
# 退出码与标准输出/标准错误两条流（不调用内部函数代替公开行为）。检查
# 进程校验完规则后自行退出，不启动服务、不绑定或探测端口，因此用例不
# 发送请求、不等待监听提示，也不需要手动中断。
# ---------------------------------------------------------------------------

CHECK_RULES_TIMEOUT = 10.0
# 成功检查只做与正常启动一致的规则加载：不展开模板、不按 delayMs 等待。
# 样例中模板路由配置了 2000ms 延迟，检查若误执行等待，耗时必然远超该上界
CHECK_RULES_NO_WAIT_MAX_SECONDS = 1.0

# 规则检查成功样例：两条合法路由。GET /hello 固定中文正文；POST /echo
# 为带 2000ms 延迟的模板路由（检查只校验配置，绝不渲染或等待）
CHECK_RULES_VALID_ROUTES = [
    {"method": "GET", "path": "/hello",
     "body": {"message": "你好"}},
    {"method": "POST", "path": "/echo",
     "bodyMode": "template",
     "body": "{{request.path}}",
     "delayMs": 2000},
]
CHECK_RULES_VALID_LINE = f"mock_server rules valid (2 route(s))\n"
CHECK_RULES_EMPTY_LINE = "mock_server rules valid (0 route(s))\n"


def run_check_rules(rules_path, port=None, timeout=CHECK_RULES_TIMEOUT):
    """经公开命令行入口执行 `python -m mock_server --check-rules`。

    等待检查进程【自行退出】，返回 (returncode, stdout, stderr, elapsed)。
    进程在 timeout 秒内未结束即判失败：杀掉并回收进程与管道后抛
    AssertionError，绝不靠等待用户中断完成测试。即使调用方随后断言失败，
    进程也已退出、管道已关闭，无资源残留。
    """
    command = [
        sys.executable, "-m", "mock_server",
        "--rules", str(rules_path),
        "--check-rules",
    ]
    if port is not None:
        command.extend(["--port", str(port)])
    proc = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    start = time.monotonic()
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 超时判失败并回收进程，不通过等待用户中断完成测试
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise AssertionError(
                f"mock_server --check-rules 在 {timeout}s 内未自行退出"
                f"（pid={proc.pid}）"
            )
        elapsed = time.monotonic() - start
        return proc.returncode, stdout, stderr, elapsed
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


class occupied_local_port:
    """上下文期间占用一个 127.0.0.1 端口并保持可接受连接的本地监听器。

    退出上下文即关闭监听套接字；断言异常时同样释放占用的端口。
    """

    def __enter__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        return self

    def __exit__(self, exc_type, exc, tb):
        self.sock.close()
        return False

    def assert_still_usable(self, test_case):
        """检查子进程结束后，原监听器仍可接受新连接。"""
        probe_timeout = 2.0
        self.sock.settimeout(probe_timeout)
        client = socket.create_connection(
            ("127.0.0.1", self.port), timeout=probe_timeout
        )
        try:
            server_conn, _ = self.sock.accept()
            server_conn.close()
        finally:
            client.close()
        test_case.assertTrue(
            True, "被占用端口上的原监听器在规则检查后仍可接受连接"
        )


class CheckRulesEntryTests(unittest.TestCase):
    """--check-rules：校验配置后自行退出；不启动服务、不触碰端口。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_check_")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.valid_path = write_rules(
            cls._tmp.name, "rules_check_valid.json", CHECK_RULES_VALID_ROUTES
        )
        cls.empty_path = write_rules(
            cls._tmp.name, "rules_check_empty.json", []
        )
        # JSON 语法错误：json.dumps 无法产出，直接写原始文本
        cls.syntax_error_path = write_rules_text(
            cls._tmp.name,
            "rules_check_syntax_error.json",
            '{"routes": [\n'
            '  {"method": "GET", "path": "/oops", "body": }\n'
            ']}\n',
        )
        # bodyMode 取值区分大小写："Template" 非法（合法值为 "template"）
        cls.bad_body_mode_path = write_rules(
            cls._tmp.name,
            "rules_check_bad_body_mode.json",
            [{"method": "GET", "path": "/x",
              "bodyMode": "Template", "body": "ignored"}],
        )
        # 溢出数字 1e400 位于会被忽略的额外字段中，仍须拒绝整份规则
        cls.overflow_path = write_rules_text(
            cls._tmp.name,
            "rules_check_overflow_extra.json",
            '{"routes":[],"ignored":{"huge":1e400}}\n',
        )

    def assert_check_success(self, returncode, stdout, stderr, expected_line,
                            elapsed=None):
        self.assertEqual(
            returncode, 0,
            f"规则检查应成功（退出码 0），实际 {returncode}；"
            f"stdout={stdout!r} stderr={stderr!r}",
        )
        # 标准输出只有这一行（含换行），标准错误为空
        self.assertEqual(
            stdout, expected_line,
            f"标准输出应只有 {expected_line!r} 一行，实际 {stdout!r}",
        )
        self.assertEqual(
            stderr, "", f"成功时标准错误应为空，实际 {stderr!r}"
        )
        # 检查进程不输出监听或停止提示（精确等值已保证，这里显式声明意图）
        self.assertNotIn(STARTUP_MARKER, stdout + stderr)
        self.assertNotIn("mock_server stopped", stdout + stderr)
        if elapsed is not None:
            self.assertLess(
                elapsed, CHECK_RULES_NO_WAIT_MAX_SECONDS,
                f"规则检查不应按 delayMs 等待或渲染模板，实际耗时 "
                f"{elapsed * 1000:.1f}ms",
            )

    def assert_check_rules_error(self, returncode, stdout, stderr, fragment):
        """规则加载错误：退出码 2、stdout 为空、stderr 以 error: 给原因。"""
        self.assertEqual(
            returncode, 2,
            f"规则检查失败应返回退出码 2，实际 {returncode}；"
            f"stdout={stdout!r} stderr={stderr!r}",
        )
        self.assertEqual(
            stdout, "", f"失败时标准输出应为空，实际 {stdout!r}"
        )
        self.assertTrue(
            stderr.startswith("error: "),
            f"规则加载错误应以 'error: ' 开头，实际 stderr={stderr!r}",
        )
        self.assertIn(
            fragment, stderr,
            f"标准错误应包含原因片段 {fragment!r}，实际 stderr={stderr!r}",
        )
        self.assertNotIn("Traceback", stderr)

    def assert_check_argparse_error(self, returncode, stdout, stderr,
                                    *fragments):
        """参数错误：退出码 2、stdout 为空，stderr 保留 argparse 表达。"""
        self.assertEqual(
            returncode, 2,
            f"参数错误应返回退出码 2，实际 {returncode}；"
            f"stdout={stdout!r} stderr={stderr!r}",
        )
        self.assertEqual(
            stdout, "", f"参数错误时标准输出应为空，实际 {stdout!r}"
        )
        # argparse 的标准形式 "prog: error: argument ..."，区别于规则加载
        # 错误的 "error: ..."
        self.assertIn(
            "mock_server: error: argument --port:", stderr,
            f"应保留 argparse 的参数错误表达，实际 stderr={stderr!r}",
        )
        for fragment in fragments:
            self.assertIn(
                fragment, stderr,
                f"标准错误应包含 {fragment!r}，实际 stderr={stderr!r}",
            )
        self.assertNotIn("Traceback", stderr)

    def test_valid_rules_without_port(self):
        # 省略 --port：使用默认端口，但检查不绑定或探测它
        returncode, stdout, stderr, elapsed = run_check_rules(self.valid_path)
        self.assert_check_success(
            returncode, stdout, stderr, CHECK_RULES_VALID_LINE, elapsed
        )

    def test_valid_rules_on_occupied_port(self):
        # 传入一个被本地监听器占用的合法端口：检查仍应成功，且不触碰端口，
        # 原监听器在检查结束后仍可使用
        with occupied_local_port() as occupied:
            returncode, stdout, stderr, elapsed = run_check_rules(
                self.valid_path, port=occupied.port
            )
            self.assert_check_success(
                returncode, stdout, stderr, CHECK_RULES_VALID_LINE, elapsed
            )
            occupied.assert_still_usable(self)

    def test_empty_routes_reports_zero(self):
        returncode, stdout, stderr, _ = run_check_rules(self.empty_path)
        self.assert_check_success(
            returncode, stdout, stderr, CHECK_RULES_EMPTY_LINE
        )

    def test_json_syntax_error_exits_2(self):
        returncode, stdout, stderr, _ = run_check_rules(
            self.syntax_error_path
        )
        self.assert_check_rules_error(
            returncode, stdout, stderr, "is not valid JSON"
        )

    def test_invalid_body_mode_value_exits_2(self):
        returncode, stdout, stderr, _ = run_check_rules(
            self.bad_body_mode_path
        )
        self.assert_check_rules_error(
            returncode, stdout, stderr,
            "bodyMode must be 'fixed' or 'template', got 'Template'",
        )

    def test_overflow_number_in_ignored_extra_field_exits_2(self):
        returncode, stdout, stderr, _ = run_check_rules(self.overflow_path)
        self.assert_check_rules_error(
            returncode, stdout, stderr, "non-finite number"
        )

    def test_port_non_integer_exits_2(self):
        returncode, stdout, stderr, _ = run_check_rules(
            self.valid_path, port="abc"
        )
        self.assert_check_argparse_error(
            returncode, stdout, stderr,
            "invalid port 'abc'", "must be an integer",
        )

    def test_port_zero_exits_2(self):
        returncode, stdout, stderr, _ = run_check_rules(
            self.valid_path, port=0
        )
        self.assert_check_argparse_error(
            returncode, stdout, stderr,
            "invalid port '0'", "must be between 1 and 65535",
        )


# ---------------------------------------------------------------------------
# 规则文件读取失败回归：正常启动与 --check-rules 对同一坏文件的确定结果
#
# 只覆盖加载路径上的文件读取、UTF-8 解码与 JSON 语法检查：从未创建的
# 文件路径、仅含原始字节 0xff 0xfe 的文件、UTF-8 内容为 {"routes":[ 的
# 截断文件。每种输入都经两条公开命令行入口验证——正常启动
# （python -m mock_server --rules ... --port ...）与附加 --check-rules
# 的规则检查——两者均应以退出码 2 自行结束、标准输出为空、标准错误以
# error: 开头并包含样例文件名与相同原因，不出现 Traceback、监听提示或
# 校验成功提示。另以一份合法中文规则作对照，核对检查入口的成功输出与
# 正常启动后的真实 GET 响应。全部用例只依赖标准库、临时文件与回环地址。
# ---------------------------------------------------------------------------


class RulesFileReadErrorTests(unittest.TestCase):
    """规则文件读取/解码/语法失败：两条入口一致的退出码与错误输出。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_read_")
        cls.addClassCleanup(cls._tmp.cleanup)
        # 临时目录内从未创建的文件路径
        cls.missing_path = Path(cls._tmp.name) / "rules_missing.json"
        # 仅含原始字节 0xff 0xfe：不是合法 UTF-8
        cls.invalid_utf8_path = Path(cls._tmp.name) / "rules_not_utf8.json"
        cls.invalid_utf8_path.write_bytes(b"\xff\xfe")
        # UTF-8 合法但 JSON 被截断
        cls.truncated_path = write_rules_text(
            cls._tmp.name, "rules_truncated.json", '{"routes":['
        )
        # 对照用合法中文规则：routes 只含一条 GET /hello
        cls.valid_chinese_path = write_rules(
            cls._tmp.name,
            "rules_valid_chinese.json",
            [{"method": "GET", "path": "/hello",
              "body": {"message": "你好"}}],
        )

    def assert_load_failure(self, returncode, stdout, stderr, path, fragment,
                            entry_label):
        """单条入口对坏文件的共同期望：退出码 2、stdout 为空、stderr 以
        error: 开头、包含样例文件名与原因片段，且无 Traceback、监听提示
        或校验成功提示。"""
        self.assertEqual(
            returncode, 2,
            f"{entry_label}：规则加载失败应返回退出码 2，实际 "
            f"{returncode}；stdout={stdout!r} stderr={stderr!r}",
        )
        self.assertEqual(
            stdout, "",
            f"{entry_label}：失败时标准输出应为空，实际 {stdout!r}",
        )
        self.assertTrue(
            stderr.startswith("error: "),
            f"{entry_label}：标准错误应以 'error: ' 开头，实际 "
            f"stderr={stderr!r}",
        )
        self.assertIn(
            fragment, stderr,
            f"{entry_label}：标准错误应包含原因片段 {fragment!r}，实际 "
            f"stderr={stderr!r}",
        )
        self.assertIn(
            path.name, stderr,
            f"{entry_label}：标准错误应包含样例文件名 {path.name!r}，实际 "
            f"stderr={stderr!r}",
        )
        self.assertNotIn(
            "Traceback", stderr,
            f"{entry_label}：不应出现 Python 异常回溯，实际 "
            f"stderr={stderr!r}",
        )
        self.assertNotIn(
            STARTUP_MARKER, stdout + stderr,
            f"{entry_label}：不应出现监听提示，实际 stdout={stdout!r} "
            f"stderr={stderr!r}",
        )
        self.assertNotIn(
            "rules valid", stdout + stderr,
            f"{entry_label}：不应出现校验成功提示，实际 stdout={stdout!r} "
            f"stderr={stderr!r}",
        )

    def assert_both_entries_fail(self, path, fragment):
        """同一坏文件经正常启动与 --check-rules 两条入口验证：结果一致。

        两个进程都应在各自超时上界内自行退出（辅助函数在超时后杀掉进程
        并判失败），返回前进程与管道均已回收。
        """
        port = free_port()
        start_rc, start_out, start_err = start_and_wait_exit(path, port)
        check_rc, check_out, check_err, _ = run_check_rules(path, port=port)
        self.assert_load_failure(
            start_rc, start_out, start_err, path, fragment, "正常启动"
        )
        self.assert_load_failure(
            check_rc, check_out, check_err, path, fragment, "--check-rules"
        )
        # 两条入口走同一加载路径，对同一坏文件给出逐字相同的错误输出
        self.assertEqual(
            start_err, check_err,
            f"正常启动与 --check-rules 对同一坏文件的标准错误应一致："
            f"启动 stderr={start_err!r}，检查 stderr={check_err!r}",
        )

    def test_missing_file_fails_in_both_entries(self):
        self.assert_both_entries_fail(
            self.missing_path, "cannot read rules file"
        )

    def test_invalid_utf8_fails_in_both_entries(self):
        self.assert_both_entries_fail(
            self.invalid_utf8_path, "is not valid UTF-8"
        )

    def test_truncated_json_fails_in_both_entries(self):
        self.assert_both_entries_fail(
            self.truncated_path, "is not valid JSON"
        )

    def test_valid_chinese_rules_check_entry(self):
        # 对照：合法中文规则经检查入口，退出码 0、stderr 为空、stdout
        # 只有一行成功提示
        returncode, stdout, stderr, _ = run_check_rules(
            self.valid_chinese_path, port=free_port()
        )
        self.assertEqual(
            returncode, 0,
            f"合法规则检查应成功（退出码 0），实际 {returncode}；"
            f"stdout={stdout!r} stderr={stderr!r}",
        )
        self.assertEqual(
            stdout, "mock_server rules valid (1 route(s))\n",
            f"标准输出应只有一行成功提示，实际 {stdout!r}",
        )
        self.assertEqual(
            stderr, "", f"成功时标准错误应为空，实际 {stderr!r}"
        )

    def test_valid_chinese_rules_serves_hello(self):
        # 对照：正常启动出现监听提示后，GET /hello 返回 200 与中文正文
        port = free_port()
        server = ServerProcess(self.valid_chinese_path, port)
        try:
            status, headers, raw = request(port, "GET", "/hello")
            self.assertEqual(status, 200, f"GET /hello 应返回 200，实际 {status}")
            self.assertEqual(
                raw, '{"message":"你好"}'.encode("utf-8"),
                f"响应体应为紧凑 UTF-8 JSON，实际 {raw!r}",
            )
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 长 UTF-8 正文回归
#
# 针对 UTF-8 编码后总长度为 65536 与 65537 字节的 POST 正文（长度按完整
# JSON 文本的字节数计，不按字符数）：正文为仅含 text 键的对象，值由直接
# 编码的汉字、ASCII 填充字符与末尾 END 组成；规则的 requestBody 与该对象
# 相等（省略 requestBodyMode），status 为 201、body 为 {"accepted":true}。
# 相符正文应得 201 与配置响应；仅把末尾 END 改为 BAD（正文仍合法、字节
# 长度不变）应得 400 与 {"error":"request_body_mismatch"}。每次读完 POST
# 响应后，都在同一条未重新建立的 TCP 连接上请求 GET /hello（body 为
# {"message":"你好"}、省略 status），预期 200 与该中文正文；连接被关闭、
# 重新建连或后续请求超时均判为失败。
# ---------------------------------------------------------------------------

LONG_BODY_TIMEOUT = 5.0
LONG_BODY_TARGETS = (65536, 65537)
LONG_BODY_CHINESE = "汉字" * 1000  # 6000 个 UTF-8 字节，直接编码进正文
LONG_BODY_TAIL = "END"
LONG_BODY_MODIFIED_TAIL = "BAD"  # 与 END 同为 3 个 ASCII 字节，总长不变
LONG_BODY_ACCEPTED = b'{"accepted":true}'
LONG_BODY_HELLO = '{"message":"你好"}'.encode("utf-8")


def long_body_value(total_bytes, tail=LONG_BODY_TAIL):
    """构造 text 值：汉字 + ASCII 填充 + 末尾标记。

    返回值与 '{"text":"' 前缀、'"}' 后缀拼接后的完整 JSON 文本，其 UTF-8
    编码长度恰好为 total_bytes 字节。
    """
    overhead = len('{"text":""}'.encode("utf-8"))
    chinese_bytes = len(LONG_BODY_CHINESE.encode("utf-8"))
    tail_bytes = len(tail.encode("utf-8"))
    padding = total_bytes - overhead - chinese_bytes - tail_bytes
    if padding <= 0:
        raise ValueError(f"目标长度 {total_bytes} 不足以容纳固定部分")
    return LONG_BODY_CHINESE + "x" * padding + tail


def long_request_body(total_bytes, modified):
    """生成完整请求正文字节；modified 时仅把末尾 END 换成 BAD。"""
    tail = LONG_BODY_MODIFIED_TAIL if modified else LONG_BODY_TAIL
    raw = ('{"text":"' + long_body_value(total_bytes, tail) + '"}').encode(
        "utf-8"
    )
    assert len(raw) == total_bytes, (len(raw), total_bytes)
    return raw


class LongUtf8RequestBodyTests(unittest.TestCase):
    """65536/65537 字节 UTF-8 正文：完整正文参与 requestBody 校验，
    校验（无论通过与否）结束后同一条 TCP 连接仍可复用。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock_server_test_")
        cls.addClassCleanup(cls._tmp.cleanup)
        routes = [
            {
                "method": "POST",
                "path": f"/check-{total}",
                "requestBody": {"text": long_body_value(total)},
                "status": 201,
                "body": {"accepted": True},
            }
            for total in LONG_BODY_TARGETS
        ]
        routes.append(
            {"method": "GET", "path": "/hello", "body": {"message": "你好"}}
        )
        cls.rules_path = write_rules(
            cls._tmp.name, "rules_long_utf8_body.json", routes
        )
        cls.port = free_port()
        cls.server = ServerProcess(cls.rules_path, cls.port)
        cls.addClassCleanup(cls.server.stop)

    def _assert_response(self, case, phase, resp, expected_status,
                         expected_body):
        label = f"{case}/{phase}"
        raw = resp.read()
        self.assertEqual(
            resp.status, expected_status,
            f"{label}: 状态码应为 {expected_status}，实际 {resp.status}；"
            f"响应={raw!r}",
        )
        self.assertEqual(
            resp.getheader("Content-Type"), CONTENT_TYPE,
            f"{label}: Content-Type 应为 {CONTENT_TYPE!r}，实际 "
            f"{resp.getheader('Content-Type')!r}",
        )
        self.assertEqual(
            int(resp.getheader("Content-Length")), len(raw),
            f"{label}: Content-Length 应等于实际响应正文字节数 "
            f"{len(raw)}，实际 {resp.getheader('Content-Length')!r}",
        )
        self.assertEqual(
            raw, expected_body,
            f"{label}: 响应体应为 {expected_body!r}，实际 {raw!r}",
        )

    def _assert_keep_alive(self, label, conn, sock, resp):
        self.assertFalse(
            resp.will_close,
            f"{label}: 服务不应在响应后关闭连接（will_close 为真）",
        )
        self.assertIs(
            conn.sock, sock,
            f"{label}: 连接被关闭或重新建立，未复用原 TCP 连接",
        )

    def _run_post_then_hello(self, total, modified):
        case = (f"正文{total}字节/"
                f"{'末尾END改BAD' if modified else '原样'}")
        raw_body = long_request_body(total, modified)
        expected_status = 400 if modified else 201
        expected_body = REQUEST_BODY_MISMATCH if modified else LONG_BODY_ACCEPTED

        conn = HTTPConnection(
            "127.0.0.1", self.port, timeout=LONG_BODY_TIMEOUT
        )
        try:
            conn.request("POST", f"/check-{total}", body=raw_body)
            sock = conn.sock
            self.assertIsNotNone(
                sock, f"{case}/POST: 请求发出后连接不存在"
            )
            resp = conn.getresponse()
            self._assert_response(
                case, "POST", resp, expected_status, expected_body
            )
            self._assert_keep_alive(f"{case}/POST 之后", conn, sock, resp)

            # 同一条未重新建立的 TCP 连接上请求 GET /hello
            conn.request("GET", "/hello")
            resp = conn.getresponse()
            self._assert_response(
                case, "GET /hello", resp, 200, LONG_BODY_HELLO
            )
            self._assert_keep_alive(
                f"{case}/GET /hello 之后", conn, sock, resp
            )
        finally:
            conn.close()

    def test_request_body_byte_lengths_are_exact(self):
        # 长度按完整 JSON 文本的 UTF-8 字节数计，不以字符数代替
        for total in LONG_BODY_TARGETS:
            for modified in (False, True):
                with self.subTest(字节数=total, 改末尾=modified):
                    raw = long_request_body(total, modified)
                    self.assertEqual(
                        len(raw), total,
                        f"正文 UTF-8 字节数应为 {total}，实际 {len(raw)}",
                    )
                    self.assertNotEqual(len(raw), len(raw.decode("utf-8")))
                    tail = LONG_BODY_MODIFIED_TAIL if modified else LONG_BODY_TAIL
                    self.assertTrue(raw.endswith((tail + '"}').encode("ascii")))
                    parsed = json.loads(raw.decode("utf-8"))
                    self.assertEqual(set(parsed), {"text"})
        # 原样正文解析后与规则 requestBody 样例递归相等
        for total in LONG_BODY_TARGETS:
            self.assertEqual(
                json.loads(long_request_body(total, False).decode("utf-8")),
                {"text": long_body_value(total)},
            )

    def test_matching_body_65536_returns_201_then_hello_same_connection(self):
        self._run_post_then_hello(65536, modified=False)

    def test_mismatching_tail_65536_returns_400_then_hello_same_connection(self):
        self._run_post_then_hello(65536, modified=True)

    def test_matching_body_65537_returns_201_then_hello_same_connection(self):
        self._run_post_then_hello(65537, modified=False)

    def test_mismatching_tail_65537_returns_400_then_hello_same_connection(self):
        self._run_post_then_hello(65537, modified=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
