"""mock_server 从规则文件到 HTTP 响应的端到端回归测试。

仅依赖 Python 3 标准库（unittest / http.client / subprocess / tempfile 等）。
在项目根目录执行：

    python -m unittest discover -s tests -v

或直接运行：

    python tests/test_mock_server.py

测试自行准备独立的 UTF-8 规则文件与临时可用端口，只连接 127.0.0.1，
不依赖人工预启服务或固定端口，也不改写现有的 rules.json。
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_BODIES = {
    "/hello": {"message": "你好"},
    "/fail": {"error": "demo_failure"},
    "/custom404": {"error": "configured_missing"},
    "/missing": {"error": "route_not_found"},
}


def free_port():
    """向系统申请一个当前可用端口（关闭后由测试服务绑定）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port, timeout=10.0):
    """等待端口可连接，避免在服务就绪前发起请求。"""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError(
        f"server on 127.0.0.1:{port} did not become ready: {last_error}"
    )


def write_rules(content):
    """把 JSON 文本写为临时 UTF-8 规则文件，返回路径。"""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", delete=False
    )
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return tmp.name


def run_server(rules_path, port):
    """以 ``python -m mock_server`` 启动服务进程。"""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mock_server",
            "--rules",
            rules_path,
            "--port",
            str(port),
        ],
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def stop_server(proc):
    """终止服务进程并回收，确保端口与连接被释放。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def http_get(port, path, method="GET"):
    """发起一次本地 HTTP 请求，返回 (status, headers, 原始响应体字节)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


# 独立的 UTF-8 规则文件：显式写出中文（ensure_ascii=False），
# 与仓库自带的 rules.json 分离，互不影响。
RULES_TEXT = json.dumps(
    {
        "routes": [
            {"method": "GET", "path": "/hello", "body": {"message": "你好"}},
            {
                "method": "POST",
                "path": "/fail",
                "status": 503,
                "body": {"error": "demo_failure"},
            },
            {
                "method": "GET",
                "path": "/custom404",
                "status": 404,
                "body": {"error": "configured_missing"},
            },
        ]
    },
    ensure_ascii=False,
    indent=2,
)


class HttpResponseTests(unittest.TestCase):
    """通过真实本地 HTTP 请求核对状态码、响应体与响应头。"""

    @classmethod
    def setUpClass(cls):
        cls.rules_path = write_rules(RULES_TEXT)
        cls.port = free_port()
        cls.proc = run_server(cls.rules_path, cls.port)
        wait_for_port(cls.port)

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.proc)
        try:
            os.unlink(cls.rules_path)
        except OSError:
            pass

    def assertResponse(self, method, path, expected_status, expected_body):
        status, headers, raw = self.harness_request(method, path)

        # 状态码
        self.assertEqual(
            status,
            expected_status,
            f"{method} {path}: 期望状态码 {expected_status}，实际 {status}，"
            f"响应体 {raw!r}",
        )

        # Content-Type
        self.assertEqual(
            headers.get("Content-Type"),
            "application/json; charset=utf-8",
            f"{method} {path}: Content-Type 不正确："
            f"{headers.get('Content-Type')!r}",
        )

        # Content-Length 等于实际收到的响应体字节数
        content_length = headers.get("Content-Length")
        self.assertIsNotNone(
            content_length, f"{method} {path}: 缺少 Content-Length 响应头"
        )
        self.assertEqual(
            int(content_length),
            len(raw),
            f"{method} {path}: Content-Length={content_length} "
            f"与实际字节数 {len(raw)} 不一致",
        )

        # 响应体：UTF-8 解码后 JSON 与配置相等（中文可正确解析）
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.fail(f"{method} {path}: 响应体不是合法 UTF-8：{exc}; raw={raw!r}")
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError as exc:
            self.fail(
                f"{method} {path}: 响应体不是合法 JSON：{exc}; body={decoded!r}"
            )
        self.assertEqual(
            parsed,
            expected_body,
            f"{method} {path}: 响应 JSON {parsed!r} 与配置 "
            f"{expected_body!r} 不相等",
        )

    def harness_request(self, method, path):
        return http_get(self.port, path, method)

    def test_get_hello_defaults_to_200(self):
        # 未填写 status 时缺省 200，返回配置的中文 body
        self.assertResponse("GET", "/hello", 200, EXPECTED_BODIES["/hello"])

    def test_post_fail_returns_configured_503(self):
        self.assertResponse("POST", "/fail", 503, EXPECTED_BODIES["/fail"])

    def test_get_custom404_returns_configured_404(self):
        # 配置的 404：状态码 404，但 body 是规则里配置的内容
        self.assertResponse(
            "GET", "/custom404", 404, EXPECTED_BODIES["/custom404"]
        )

    def test_unknown_route_returns_generated_404(self):
        # 未命中的 404：body 为 route_not_found
        self.assertResponse("GET", "/missing", 404, EXPECTED_BODIES["/missing"])

    def test_configured_404_differs_from_missing_404(self):
        """同为 404，配置命中与未命中必须可通过响应体区分。"""
        _, _, configured_raw = http_get(self.port, "/custom404")
        _, _, missing_raw = http_get(self.port, "/missing")
        self.assertNotEqual(configured_raw, missing_raw)
        self.assertEqual(
            json.loads(configured_raw.decode("utf-8")),
            {"error": "configured_missing"},
        )
        self.assertEqual(
            json.loads(missing_raw.decode("utf-8")),
            {"error": "route_not_found"},
        )

    def test_utf8_chinese_byte_length(self):
        # 中文按 UTF-8 编码（每字 3 字节），响应体与 Content-Length 均为字节数
        _, headers, raw = http_get(self.port, "/hello")
        expected_raw = json.dumps(
            EXPECTED_BODIES["/hello"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(raw, expected_raw)
        self.assertEqual(int(headers["Content-Length"]), len(raw))
        self.assertIn("你好".encode("utf-8"), raw)


class StatusBoundaryTests(unittest.TestCase):
    """合法 status 边界 200 / 400 / 599 均可加载并原样返回。"""

    def assertBoundaryAccepted(self, status):
        rules_path = write_rules(
            json.dumps(
                {
                    "routes": [
                        {
                            "method": "GET",
                            "path": "/boundary",
                            "status": status,
                            "body": {"status": status},
                        }
                    ]
                }
            )
        )
        port = free_port()
        proc = run_server(rules_path, port)
        try:
            wait_for_port(port)
            code, headers, raw = http_get(port, "/boundary")
            self.assertEqual(code, status, f"status={status} 未原样返回：{code}")
            self.assertEqual(
                json.loads(raw.decode("utf-8")), {"status": status}
            )
            self.assertEqual(
                headers.get("Content-Type"), "application/json; charset=utf-8"
            )
        finally:
            stop_server(proc)
            try:
                os.unlink(rules_path)
            except OSError:
                pass

    def test_200(self):
        self.assertBoundaryAccepted(200)

    def test_400(self):
        self.assertBoundaryAccepted(400)

    def test_599(self):
        self.assertBoundaryAccepted(599)


# (用例标签, status 在 JSON 文本中的字面量)
INVALID_STATUSES = [
    ("199_below_range", "199"),
    ("201_in_gap", "201"),
    ("399_in_gap", "399"),
    ("600_above_range", "600"),
    ("true_bool", "true"),
    ("false_bool", "false"),
    ("null", "null"),
    ("string_503", '"503"'),
    ("float_503_0", "503.0"),
    ("empty_array", "[]"),
    ("empty_object", "{}"),
]


def invalid_rules_text(status_literal):
    """直接把 status 字面量拼进 JSON 文本，保留 true/null/503.0 等原始形态。"""
    return (
        '{"routes":[{"method":"GET","path":"/bad","status":'
        + status_literal
        + ',"body":{"x":1}}]}'
    )


class InvalidStatusTests(unittest.TestCase):
    """每份非法 status 配置都必须让命令行入口以退出码 2 报错且不监听。"""

    def assertInvalidStatusRejected(self, label, status_literal):
        rules_path = write_rules(invalid_rules_text(status_literal))
        port = free_port()
        proc = run_server(rules_path, port)
        try:
            stdout, stderr = proc.communicate(timeout=15)
            self.assertEqual(
                proc.returncode,
                2,
                f"[{label}] status={status_literal} 期望退出码 2，实际 "
                f"{proc.returncode}；stderr="
                f"{stderr.decode('utf-8', 'replace')!r}",
            )
            err_text = stderr.decode("utf-8", "replace")
            self.assertIn(
                "status",
                err_text,
                f"[{label}] stderr 未指出 status 配置错误：{err_text!r}",
            )
            out_text = stdout.decode("utf-8", "replace")
            self.assertNotIn(
                "listening",
                out_text,
                f"[{label}] 非法配置却输出了启动监听提示：{out_text!r}",
            )

            # 启动失败不应监听端口：连接必须被拒绝
            with self.assertRaises(
                (ConnectionRefusedError, OSError),
                msg=f"[{label}] 非法配置启动后端口 {port} 仍可连接",
            ):
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
        finally:
            try:
                os.unlink(rules_path)
            except OSError:
                pass

    def test_invalid_statuses(self):
        for label, literal in INVALID_STATUSES:
            with self.subTest(invalid_status=label, literal=literal):
                self.assertInvalidStatusRejected(label, literal)


if __name__ == "__main__":
    unittest.main(verbosity=2)
