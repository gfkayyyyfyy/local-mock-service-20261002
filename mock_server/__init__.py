"""mock_server：加载 JSON 规则并返回固定响应的本地服务（仅标准库）。"""

import argparse
import json
import math
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ALLOWED_METHODS = ("GET", "POST")
NOT_FOUND_BODY = b'{"error":"route_not_found"}'
METHOD_NOT_SUPPORTED_BODY = b'{"error":"method_not_supported"}'
CONTENT_TYPE = "application/json; charset=utf-8"


class RulesError(Exception):
    """规则文件无法加载或内容非法。"""


def _port(value):
    try:
        port = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid port {value!r}: must be an integer"
        )
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            f"invalid port {value!r}: must be between 1 and 65535"
        )
    return port


def _valid_status(value):
    # bool 是 int 的子类，需显式排除；503.0 等浮点数也不接受
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return value == 200 or 400 <= value <= 599


def _valid_delay_ms(value):
    # 与 status 同理排除 bool；200.0 等浮点数、null、字符串、数组、
    # 对象以及负数或大于 2000 的整数均不接受
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return 0 <= value <= 2000


class Routes(dict):
    """{(method, path): (status, body)} 映射，附带每条路由的延迟毫秒数。

    delays 为 {(method, path): int}，缺省或为 0 的路由不增加人为等待。
    """


def _reject_constant(value):
    # json.loads 默认接受 NaN/Infinity/-Infinity 三种非标准数字字面量，
    # 它们不是合法 JSON；无论在文档何处出现都视为格式错误
    raise ValueError(f"invalid JSON literal {value}")


def _ensure_finite_numbers(data):
    # 1e400 等溢出的合法 JSON 数字会被浮点解析为 inf/-inf（下溢为 0 不算），
    # 这些值无法序列化回合法 JSON。递归检查整份文档（含会被忽略的额外
    # 字段），发现第一个非有限数字即拒绝，使用显式栈避免深层嵌套递归。
    stack = [(data, "$")]
    while stack:
        value, location = stack.pop()
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"non-finite number at {location}: {value!r}")
        elif isinstance(value, dict):
            for key in sorted(value, reverse=True):
                stack.append((value[key], f"{location}.{key}"))
        elif isinstance(value, list):
            for index in range(len(value) - 1, -1, -1):
                stack.append((value[index], f"{location}[{index}]"))


def load_rules(path):
    """加载并校验规则文件，返回 Routes：{(method, path): (状态码, 响应字节)}。

    返回值的 delays 属性为 {(method, path): 延迟毫秒数} 映射。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise RulesError(f"cannot read rules file {path!r}: {exc.strerror or exc}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesError(f"rules file {path!r} is not valid UTF-8: {exc}")
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        # JSONDecodeError 与非标准字面量（ValueError）统一归为 JSON 格式错误
        raise RulesError(f"rules file {path!r} is not valid JSON: {exc}")
    try:
        _ensure_finite_numbers(data)
    except ValueError as exc:
        # 语法合法但解析为 inf/-inf 的数字（如 1e400）同样无法产出合法 JSON，
        # 整份规则拒绝加载
        raise RulesError(f"rules file {path!r} {exc}")

    if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
        raise RulesError("rules file must be a JSON object with a 'routes' array")

    routes = Routes()
    routes.delays = {}
    for index, item in enumerate(data["routes"]):
        where = f"routes[{index}]"
        if not isinstance(item, dict):
            raise RulesError(f"{where} must be an object")
        for field in ("method", "path", "body"):
            if field not in item:
                raise RulesError(f"{where} is missing required field {field!r}")
        method = item["method"]
        if method not in ALLOWED_METHODS:
            raise RulesError(
                f"{where}: method must be 'GET' or 'POST', got {method!r}"
            )
        route_path = item["path"]
        if (
            not isinstance(route_path, str)
            or not route_path.startswith("/")
            or "?" in route_path
            or "#" in route_path
        ):
            raise RulesError(
                f"{where}: path must be a string starting with '/' "
                f"and contain no '?' or '#', got {route_path!r}"
            )
        key = (method, route_path)
        if key in routes:
            raise RulesError(f"{where}: duplicate route {method} {route_path}")
        status = item.get("status", 200)
        if not _valid_status(status):
            raise RulesError(
                f"{where}: status must be the integer 200 or an integer "
                f"between 400 and 599, got {status!r}"
            )
        delay_ms = item.get("delayMs", 0)
        if not _valid_delay_ms(delay_ms):
            raise RulesError(
                f"{where}: delayMs must be an integer between 0 and 2000 "
                f"(inclusive), got {delay_ms!r}"
            )
        try:
            body = json.dumps(
                item["body"], ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except UnicodeEncodeError as exc:
            # body 中的字符串值或对象键含未配对的代理码点（如孤立 \ud800、
            # 方向颠倒的代理对）时无法编码为合法 UTF-8 响应；与既有规则
            # 错误一致，整份规则加载失败
            raise RulesError(
                f"{where}.body cannot be encoded as UTF-8: {exc}"
            )
        routes[key] = (status, body)
        routes.delays[key] = delay_ms
    return routes


def _make_handler(routes):
    class MockHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _discard_body(self):
            # 匹配忽略请求体，但需读掉以保持 keep-alive 连接可用
            try:
                remaining = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _respond(self):
            self._discard_body()
            path = urlsplit(self.path).path
            key = (self.command, path)
            entry = routes.get(key)
            if entry is None:
                self._send(404, NOT_FOUND_BODY)
            else:
                # 请求体已读完且路由已命中：每次命中（含错误状态码路由）
                # 都先等待配置的时长，再发送状态行、响应头与响应体
                delay_ms = routes.delays.get(key, 0)
                if delay_ms:
                    time.sleep(delay_ms / 1000)
                self._send(entry[0], entry[1])

        def _send(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _respond
        do_POST = _respond

        def send_error(self, code, message=None, explain=None):
            # 仅统一 501（方法不受支持）的公开格式；其余错误（如 400）
            # 仍沿用标准库默认处理
            if code == 501:
                self._send_method_not_supported()
            else:
                super().send_error(code, message, explain)

        def _send_method_not_supported(self):
            # 非 GET/POST 方法：不查路由、不应用延迟、不读取请求体，
            # 统一返回 JSON 拒绝并关闭连接（请求体剩余字节不会被当作
            # 后续请求解释）。HEAD 不发送响应体，但 Content-Length 仍
            # 按 JSON 正文的字节数给出。
            self.send_response(501)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header(
                "Content-Length", str(len(METHOD_NOT_SUPPORTED_BODY))
            )
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(METHOD_NOT_SUPPORTED_BODY)
            self.close_connection = True

        def log_message(self, format, *args):
            pass

    return MockHandler


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="mock_server",
        description="Serve fixed JSON responses from a rules file on 127.0.0.1.",
    )
    parser.add_argument("--rules", required=True, help="path to the JSON rules file")
    parser.add_argument(
        "--port",
        type=_port,
        default=8765,
        help="port to listen on, 1-65535 (default: 8765)",
    )
    args = parser.parse_args(argv)

    try:
        routes = load_rules(args.rules)
    except RulesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), _make_handler(routes))
    except OSError as exc:
        print(
            f"error: cannot bind 127.0.0.1:{args.port}: {exc.strerror or exc}",
            file=sys.stderr,
        )
        return 2

    host, port = server.server_address[:2]
    print(
        f"mock_server listening on http://{host}:{port} ({len(routes)} route(s))",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("mock_server stopped")
    return 0
