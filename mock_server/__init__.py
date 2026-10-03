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
REQUEST_BODY_MISMATCH_BODY = b'{"error":"request_body_mismatch"}'
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
    """{(method, path): (status, body)} 映射，附带每条路由的附加配置。

    delays 为 {(method, path): int}，缺省或为 0 的路由不增加人为等待；
    request_bodies 为 {(method, path): 样例值}，仅含显式配置 requestBody
    的 POST 路由，键不存在表示该路由忽略请求正文。
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


def _json_equal(expected, actual):
    """递归比较两个由 json 解析得到的值。

    对象须有完全相同的键集合（键序与排版空白不影响结果）；数组长度与
    逐元素顺序参与比较；字符串区分大小写；数字按数值比较（1 与 1.0 相等）；
    True/False 与 0/1 不等；None 只等于 None；类型不同即不相等。
    """
    stack = [(expected, actual)]
    while stack:
        want, got = stack.pop()
        if want is None or got is None:
            if want is not got:
                return False
            continue
        # bool 是 int 的子类，须在数字比较之前显式区分
        if isinstance(want, bool) or isinstance(got, bool):
            if type(want) is not type(got) or want != got:
                return False
            continue
        if isinstance(want, (int, float)):
            if not isinstance(got, (int, float)) or isinstance(got, bool):
                return False
            if want != got:
                return False
            continue
        if isinstance(want, str):
            if type(got) is not str or want != got:
                return False
            continue
        if isinstance(want, list):
            if type(got) is not list or len(want) != len(got):
                return False
            for index in range(len(want) - 1, -1, -1):
                stack.append((want[index], got[index]))
            continue
        if isinstance(want, dict):
            if type(got) is not dict or set(want) != set(got):
                return False
            for key in want:
                stack.append((want[key], got[key]))
            continue
        # json 解析结果只会是 None/bool/int/float/str/list/dict
        if want != got:
            return False
    return True


def load_rules(path):
    """加载并校验规则文件，返回 Routes：{(method, path): (状态码, 响应字节)}。

    返回值的 delays 属性为 {(method, path): 延迟毫秒数} 映射；
    request_bodies 属性为 {(method, path): requestBody 样例值} 映射，
    仅包含显式配置 requestBody 的 POST 路由（样例可以是 None，对应显式
    JSON null，故以键是否存在而非值是否为 None 区分）。
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
    routes.request_bodies = {}
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
        request_body_sample = None
        if "requestBody" in item:
            if method != "POST":
                raise RulesError(
                    f"{where}.requestBody is only allowed on POST routes, "
                    f"got method {method!r}"
                )
            try:
                # 与 body 相同的 UTF-8 可编码限制：样例中的字符串值与对象
                # 键都不得含未配对代理码点。样例仅用于按值比较，无需预序列化
                json.dumps(
                    item["requestBody"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except UnicodeEncodeError as exc:
                raise RulesError(
                    f"{where}.requestBody cannot be encoded as UTF-8: {exc}"
                )
            request_body_sample = item["requestBody"]
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
        if "requestBody" in item:
            routes.request_bodies[key] = request_body_sample
    return routes


def _make_handler(routes):
    class MockHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _read_body(self):
            # 按 Content-Length 读掉完整请求体以保持 keep-alive 连接可用，
            # 返回原始字节；无 Content-Length 时视为空正文。读取中断（连接
            # 提前关闭、长度非法）返回 None，交由调用方按请求体不匹配处理
            try:
                remaining = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            chunks = []
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    return None
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        @staticmethod
        def _body_matches(sample, raw):
            # 空正文、非法 UTF-8、JSON 语法错误、NaN/Infinity 等非标准
            # 字面量、溢出为 inf/-inf 的数字或递归比较不通过，均视为不匹配
            if raw is None:
                return False
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return False
            try:
                data = json.loads(text, parse_constant=_reject_constant)
            except ValueError:
                return False
            try:
                _ensure_finite_numbers(data)
            except ValueError:
                return False
            return _json_equal(sample, data)

        def _respond(self):
            raw_body = self._read_body()
            path = urlsplit(self.path).path
            key = (self.command, path)
            entry = routes.get(key)
            if entry is None:
                # 未命中即使正文非法也返回原有 404 正文，不做请求体校验
                self._send(404, NOT_FOUND_BODY)
                return
            if key in routes.request_bodies and not self._body_matches(
                routes.request_bodies[key], raw_body
            ):
                # 请求体不匹配：不应用配置的状态、正文或延迟
                self._send(400, REQUEST_BODY_MISMATCH_BODY)
                return
            # 请求体已读完且（若有样例）校验通过：每次命中（含错误状态码
            # 路由）都先等待配置的时长，再发送状态行、响应头与响应体
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
