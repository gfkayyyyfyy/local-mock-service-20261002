"""加载 JSON 规则并返回固定响应的本地 mock 服务（仅标准库）。"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

CONTENT_TYPE = "application/json; charset=utf-8"
NOT_FOUND_BODY = b'{"error":"route_not_found"}'
ALLOWED_METHODS = ("GET", "POST")


class RulesError(Exception):
    """规则文件无法加载或内容非法。"""


def _port(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"端口必须是整数: {value!r}")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"端口必须在 1 到 65535 之间: {port}")
    return port


def load_rules(path):
    """加载并校验规则文件，返回 {(method, path): 响应字节} 字典。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise RulesError(f"无法读取规则文件 {path}: {exc.strerror or exc}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesError(f"规则文件不是有效的 UTF-8: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RulesError(
            f"规则文件 JSON 语法错误: 第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}"
        )
    if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
        raise RulesError("规则文件顶层必须是包含 routes 数组的对象")

    routes = {}
    for index, item in enumerate(data["routes"]):
        where = f"routes[{index}]"
        if not isinstance(item, dict):
            raise RulesError(f"{where} 必须是对象")
        for field in ("method", "path", "body"):
            if field not in item:
                raise RulesError(f"{where} 缺少必填字段 {field}")
        method = item["method"]
        if method not in ALLOWED_METHODS:
            raise RulesError(f"{where} 的 method 非法: {method!r}（仅接受 GET、POST）")
        route_path = item["path"]
        if (
            not isinstance(route_path, str)
            or not route_path.startswith("/")
            or "?" in route_path
            or "#" in route_path
        ):
            raise RulesError(
                f"{where} 的 path 非法: {route_path!r}（必须以 / 开头且不含 ? 或 #）"
            )
        key = (method, route_path)
        if key in routes:
            raise RulesError(f"重复规则: {method} {route_path}")
        routes[key] = json.dumps(
            item["body"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    return routes


def make_handler(routes):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status, payload):
            self.send_response(status)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _route(self):
            # 读取并丢弃请求体，避免 keep-alive 连接上残留数据
            length = self.headers.get("Content-Length")
            if length is not None:
                try:
                    remaining = int(length)
                except ValueError:
                    remaining = 0
                if remaining > 0:
                    self.rfile.read(remaining)
            # 忽略查询字符串；路径大小写、尾部斜杠、百分号转义按原样比较
            path = self.path.split("?", 1)[0]
            payload = routes.get((self.command, path))
            if payload is None:
                self._respond(404, NOT_FOUND_BODY)
            else:
                self._respond(200, payload)

        do_GET = _route
        do_POST = _route

        def log_message(self, format, *args):
            pass

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="mock_server",
        description="加载 JSON 规则并返回固定响应的本地 mock 服务",
    )
    parser.add_argument("--rules", required=True, help="规则文件路径（UTF-8 JSON）")
    parser.add_argument(
        "--port", type=_port, default=8765, help="监听端口（1-65535，默认 8765）"
    )
    args = parser.parse_args(argv)

    try:
        routes = load_rules(args.rules)
    except RulesError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    try:
        server = HTTPServer(("127.0.0.1", args.port), make_handler(routes))
    except OSError as exc:
        print(
            f"错误: 无法绑定 127.0.0.1:{args.port}: {exc.strerror or exc}",
            file=sys.stderr,
        )
        return 2

    print(
        f"mock_server 正在监听 http://127.0.0.1:{args.port}（已加载 {len(routes)} 条规则）",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
