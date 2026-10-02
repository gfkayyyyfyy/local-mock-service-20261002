"""mock_server：加载 JSON 规则并返回固定响应的本地服务（仅标准库）。"""

import argparse
import json
import math
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ALLOWED_METHODS = ("GET", "POST")
NOT_FOUND_BODY = b'{"error":"route_not_found"}'
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


def _reject_constant(value):
    # json.loads 默认接受 NaN/Infinity/-Infinity 三种非标准数字字面量，
    # 它们不是合法 JSON；无论在文档何处出现都视为格式错误
    raise ValueError(f"invalid JSON literal {value}")


def _ensure_finite_numbers(value, where="$"):
    # parse_constant 拦不住 1e400 这类语法合法但溢出的数字：它们会被默认
    # 解析成 inf/-inf，随后 json.dumps 又会产出非标准的 Infinity 字面量。
    # 因此解析后递归遍历整份文档（含会被忽略的额外字段），拒绝一切非有限浮点。
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RulesError(f"non-finite number at {where}")
    elif isinstance(value, dict):
        for key, item in value.items():
            _ensure_finite_numbers(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_finite_numbers(item, f"{where}[{index}]")


def load_rules(path):
    """加载并校验规则文件，返回 {(method, path): (状态码, 响应字节)} 字典。"""
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

    # 1e400 等溢出数字语法合法，parse_constant 不会被调用；需在结构校验前
    # 遍历整份文档（含额外字段），发现 inf/-inf/NaN 即拒绝整份规则
    _ensure_finite_numbers(data)

    if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
        raise RulesError("rules file must be a JSON object with a 'routes' array")

    routes = {}
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
        body = json.dumps(
            item["body"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        routes[key] = (status, body)
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
            entry = routes.get((self.command, path))
            if entry is None:
                self._send(404, NOT_FOUND_BODY)
            else:
                self._send(entry[0], entry[1])

        def _send(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _respond
        do_POST = _respond

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
