"""mock_server：加载 JSON 规则并返回固定响应的本地服务（仅标准库）。"""

import argparse
import json
import math
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ALLOWED_METHODS = ("GET", "POST")
NOT_FOUND_BODY = b'{"error":"route_not_found"}'
METHOD_NOT_SUPPORTED_BODY = b'{"error":"method_not_supported"}'
REQUEST_BODY_MISMATCH_BODY = b'{"error":"request_body_mismatch"}'
CONTENT_TYPE = "application/json; charset=utf-8"
# requestBodyMode 的合法取值：省略或 "exact" 为原有的整体相等比较，
# "subset" 启用对象子集匹配；取值区分大小写
REQUEST_BODY_MODES = ("exact", "subset")
# pathMode 的合法取值：省略或 "exact" 为原有的完整路径相等匹配，
# "prefix" 将规则 path 作为前缀；取值区分大小写
PATH_MODES = ("exact", "prefix")
# bodyMode 的合法取值：省略或 "fixed" 为原有的固定响应（body 含占位符
# 也原样返回），"template" 将 body 字符串值中的 {{request.path}} 替换为
# 本次用于匹配的路径、{{request.method}} 替换为本次请求方法的大写形式
# （GET 或 POST）、{{request.query}} 替换为本次请求目标中的原始查询串
# （第一个问号之后、井号之前的文本，不解码、不校验）、
# {{request.pathSuffix}} 替换为最终选中前缀之后的路径剩余文本（exact
# 命中时为空字符串）、{{request.queryParam.name}} 替换为本次原始查询串
# 中参数 name 从左到右第一个匹配项的原始值（缺失时为空字符串）、
# {{request.header.name}} 替换为本次请求头 name 按从上到下第一项解析出
# 的文本（缺失时为空字符串，头名称匹配不区分大小写）；取值区分大小写
BODY_MODES = ("fixed", "template")
# template 模式下识别的四个固定占位符：带空格、大小写不同的写法及其他
# 占位符均保持原样
PATH_PLACEHOLDER = "{{request.path}}"
METHOD_PLACEHOLDER = "{{request.method}}"
QUERY_PLACEHOLDER = "{{request.query}}"
PATH_SUFFIX_PLACEHOLDER = "{{request.pathSuffix}}"
# 此外识别两族名称占位符：前缀后接名称并以 }} 收尾。queryParam 的参数名
# 首字符限 ASCII 英文字母，后续字符限 ASCII 字母、数字或下划线，区分
# 大小写；header 的头名称首字符限 ASCII 英文字母，后续字符限 ASCII 字母、
# 数字或连字符，占位符前缀区分大小写而头名称匹配不区分大小写。名称只在
# 对应正则内判定，规则加载不检查 body 中的占位符写法
QUERY_PARAM_PREFIX = "{{request.queryParam."
QUERY_PARAM_NAME_RE = r"[A-Za-z][A-Za-z0-9_]*"
HEADER_PREFIX = "{{request.header."
HEADER_NAME_RE = r"[A-Za-z][A-Za-z0-9-]*"
# 单次自左向右扫描同时处理六个占位符形态，保证嵌入、重复与混合出现的
# 占位符都被替换，且替换结果（如路径、参数值、头值、剩余文本或查询文本
# 中恰含占位符形态的字符）不再参与处理；queryParam 备选必须放在
# {{request.query}} 之后，二者前缀不同（queryParam.），互不干扰；header
# 与其余形态前缀均不同（header.），同样互不干扰。queryParam 名称为捕获
# 组 1，header 名称为捕获组 2
_TEMPLATE_PLACEHOLDER_RE = re.compile(
    re.escape(PATH_PLACEHOLDER)
    + "|"
    + re.escape(METHOD_PLACEHOLDER)
    + "|"
    + re.escape(QUERY_PLACEHOLDER)
    + "|"
    + re.escape(PATH_SUFFIX_PLACEHOLDER)
    + "|"
    + re.escape(QUERY_PARAM_PREFIX)
    + "("
    + QUERY_PARAM_NAME_RE
    + r")}}"
    + "|"
    + re.escape(HEADER_PREFIX)
    + "("
    + HEADER_NAME_RE
    + r")}}"
)


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
    # bool 是 int 的子类，需显式排除；503.0、201.0 等浮点数也不接受。
    # 合法取值仅 200、201 以及 400–599（包含两端）：201 是唯一额外允许
    # 的 2xx 整数（创建成功响应），不扩展为整个 2xx 范围，202、204 等
    # 其余 2xx 与 3xx 仍非法
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return value in (200, 201) or 400 <= value <= 599


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
    的 POST 路由，键不存在表示该路由忽略请求正文；
    request_body_modes 为 {(method, path): "exact"|"subset"}，键集合与
    request_bodies 一致，缺省 requestBodyMode 时记为 "exact"；
    path_modes 为 {(method, path): "exact"|"prefix"}，缺省 pathMode 时
    记为 "exact"，键集合与路由本身一致；
    body_modes 为 {(method, path): "fixed"|"template"}，缺省 bodyMode 时
    记为 "fixed"，键集合与路由本身一致；
    template_bodies 为 {(method, path): body 原始 JSON 值}，仅含
    bodyMode 为 "template" 的路由，供每次请求按当前方法、路径、查询串
    （含由其现算的单个查询参数原始值）、路径剩余文本与本次请求头渲染。
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


def _json_matches(expected, actual, allow_extra_keys):
    """exact 与 subset 共用的递归比较流程。

    标量、数组与嵌套值的语义两种模式一致：数组长度与逐元素顺序参与
    比较（不接受前缀匹配）；字符串区分大小写；数字按数值比较（1 与
    1.0 相等）；True/False 与 0/1 不等；None 只等于 None；类型不同
    即不匹配。唯一差异在对象的键集合：allow_extra_keys 为 False
    （exact）时要求键集合完全相同（键序与排版空白不影响结果），为
    True（subset）时只要求样例的键全部存在于实际对象中、对应值递归
    按同一流程比较，实际对象允许额外键（空对象样例因此匹配任意对象，
    但也只匹配对象）。
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
            if type(got) is not dict:
                return False
            # 两种模式唯一的差异：exact 要求键集合相同，subset 允许额外键
            if allow_extra_keys:
                if not set(want) <= set(got):
                    return False
            elif set(want) != set(got):
                return False
            for key in want:
                stack.append((want[key], got[key]))
            continue
        # json 解析结果只会是 None/bool/int/float/str/list/dict
        if want != got:
            return False
    return True


def _json_equal(expected, actual):
    """整体相等比较：对象须有完全相同的键集合，其余语义与 _json_matches
    一致（数组长度与顺序参与比较、字符串区分大小写、数字按数值比较、
    布尔与数字互不相等、None 只等于 None、类型不同即不相等）。
    """
    return _json_matches(expected, actual, allow_extra_keys=False)


def _json_subset(expected, actual):
    """对象子集匹配：样例对象的所有键都须存在于实际值的对应对象中，
    对应值递归按同一模式比较，实际对象允许出现额外键（空对象样例因此
    匹配任意对象，但也只匹配对象）。数组仍要求相同长度与顺序（不接受
    前缀匹配），其中元素为对象时同样允许额外键。其余值与 _json_equal
    语义一致：数字按数值比较（1 与 1.0 相等）、布尔与数字不等、字符串
    区分大小写、None 只匹配 None、类型不符即不匹配。
    """
    return _json_matches(expected, actual, allow_extra_keys=True)


def _query_params(query):
    """把原始查询串解析为 {名称: 第一个值} 映射，全程不解码、不转换。

    参数以 '&' 分隔，空片段（含结尾 '&' 产生的空串）忽略；每个片段中
    第一个 '=' 之前是名称，之后的全部文本是值（允许值中继续含 '='）；
    没有 '=' 时值为空字符串。名称与值都不做百分号解码，'+' 不转为空格，
    'true'、'null'、'123' 等文本不转换类型。重复名称保留从左到右第一个
    片段对应的值——即使第一个值为空也不被后续片段覆盖；空名称片段
    （如 '=x'）也照常记录，但合法占位符的名称不可能为空，故永远不会被
    取到。query 为空字符串时返回空映射。
    """
    params = {}
    for segment in query.split("&"):
        if not segment:
            continue
        name, has_equals, value = segment.partition("=")
        if name not in params:
            params[name] = value if has_equals else ""
    return params


def _header_values(headers):
    """把标准库解析后的请求头整理为 {小写名称: 第一个值} 映射。

    只读取 self.headers（http.client 经 email 解析后的 HTTPMessage）已
    解析出的文本：行首的冒号分隔空白由解析器去除，内部与尾部空白原样
    保留；本函数不额外去除首尾空白，不做 URL 解码、逗号分割或类型转换，
    加号与百分号转义原样保留。同名头重复出现时只取消息中从上到下第一个
    头的值——即使第一个值为空字符串也不被后续同名头覆盖；头名称在映射
    中统一为小写，而占位符查找时也把名称转小写，故匹配不区分大小写。
    """
    values = {}
    for name, value in headers.items():
        key = name.lower()
        if key not in values:
            values[key] = value
    return values


def _render_template(
    value, path, method, query, path_suffix, query_params, header_values
):
    """template 模式的响应渲染：把字符串值中的 {{request.path}} 替换为
    本次用于匹配的路径（已去除查询串，大小写、尾斜杠与百分号转义按
    原样保留，不额外解码或规范化）、{{request.method}} 替换为本次请求
    方法的大写形式（GET 或 POST，与查询参数和正文无关）、
    {{request.query}} 替换为本次请求目标中的原始查询串（第一个问号之后
    至井号或目标结尾的文本，不含问号；没有查询串或仅有结尾问号时为空
    字符串，保留参数顺序、重复参数、空值、没有等号的片段、加号与百分号
    转义，不做解码、排序或类型转换，%ZZ 等文本也原样保留）、
    {{request.pathSuffix}} 替换为最终选中前缀之后的路径剩余文本（prefix
    命中时为匹配路径去掉该路由完整 path 后的剩余部分，保留大小写、
    多级路径、连续斜杠、尾斜杠与百分号转义，不解码、不规范化，不含
    查询串与片段；根前缀 / 只去掉开头的一个斜杠；exact 命中时固定为空
    字符串）、{{request.queryParam.name}} 替换为本次原始查询串中名称为
    name 的参数从左到右第一个片段的原始值（name 首字符限 ASCII 字母，
    后续字符限 ASCII 字母、数字或下划线，区分大小写；不解码、'+' 不变
    空格、不做类型转换；没有等号时值为空；缺失参数、没有查询串或仅有
    结尾问号时为空字符串；第一个匹配项即使为空也不跳过）、
    {{request.header.name}} 替换为本次请求头 name 按消息从上到下第一个
    同名头经标准库解析后的文本（name 首字符限 ASCII 字母，后续字符限
    ASCII 字母、数字或连字符；占位符前缀区分大小写，头名称匹配不区分
    大小写；不额外去除解析后文本的首尾空白，不解码、不做逗号分割或类型
    转换；缺失该头或第一个同名头的值为空字符串时替换为空字符串，且空值
    不被后续同名头跳过；请求头不参与路由选择）。

    顶层及嵌套对象、数组中的字符串值均替换；对象键、非字符串值与
    JSON 结构保持不变。正则自左向右单次扫描，嵌入、重复或混合出现
    的占位符都被替换，且不会再次处理替换结果（路径、参数值、头值、剩余
    文本、方法或查询文本中即使含有占位符形态的字符也不会被二次替换）。
    参数名或头名称缺失或不合名称规则的写法（如
    {{request.queryParam.}}、{{request.queryParam.1x}}、
    {{request.header.}}、{{request.header.1x}}、
    {{request.header.x_y}}）、带空格、前缀大小写不同的写法及其他未知
    占位符保持原样，不执行任何表达式，也不尝试取值。
    """
    if isinstance(value, str):
        def _replace(match):
            query_name = match.group(1)
            if query_name is not None:
                # {{request.queryParam.<name>}}：缺失名称得到空字符串
                return query_params.get(query_name, "")
            header_name = match.group(2)
            if header_name is not None:
                # {{request.header.<name>}}：缺失头或第一个同名头为空均
                # 得到空字符串；头名称按小写不区分大小写匹配
                return header_values.get(header_name.lower(), "")
            token = match.group(0)
            if token == PATH_PLACEHOLDER:
                return path
            if token == METHOD_PLACEHOLDER:
                return method
            if token == QUERY_PLACEHOLDER:
                return query
            return path_suffix

        return _TEMPLATE_PLACEHOLDER_RE.sub(_replace, value)
    if isinstance(value, list):
        return [
            _render_template(
                item, path, method, query, path_suffix,
                query_params, header_values,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _render_template(
                item, path, method, query, path_suffix,
                query_params, header_values,
            )
            for key, item in value.items()
        }
    return value


def _read_route_items(path):
    """文件级检查：读取规则文件并返回其中的 routes 数组。

    依次检查文件可读、内容为合法 UTF-8、内容为合法 JSON（NaN/Infinity
    等非标准字面量与 1e400 等溢出为 inf/-inf 的数字都视为格式错误，
    含会被忽略的额外字段中的数字）、顶层是含 'routes' 数组的对象。
    任一不满足即抛 RulesError；此阶段不查看任何单条路由的内容。
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
    return data["routes"]


def _check_route_identity(item, where):
    """路由级检查（身份部分）：结构、必填字段、method 与 path。

    通过则返回 (method, path)：method 属于 ALLOWED_METHODS，path 是以
    '/' 开头且不含 '?'、'#' 的字符串。可选字段由 _check_route_options
    检查。
    """
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
    return method, route_path


class _RouteEntry:
    """一条路由通过全部校验后的规范化结果，供 load_rules 组装 Routes。

    body 为预序列化的紧凑 UTF-8 JSON 字节；template_body 仅当
    body_mode 为 "template" 时有意义（body 的原始 JSON 值，可以是
    None，对应显式 JSON null）；request_body_present 区分显式
    requestBody（含显式 null）与省略该字段，为 False 时
    request_body_sample 与 request_body_mode 不参与组装。
    """

    __slots__ = (
        "status",
        "body",
        "delay_ms",
        "path_mode",
        "body_mode",
        "template_body",
        "request_body_present",
        "request_body_sample",
        "request_body_mode",
    )

    def __init__(
        self, status, body, delay_ms, path_mode, body_mode, template_body,
        request_body_present, request_body_sample, request_body_mode,
    ):
        self.status = status
        self.body = body
        self.delay_ms = delay_ms
        self.path_mode = path_mode
        self.body_mode = body_mode
        self.template_body = template_body
        self.request_body_present = request_body_present
        self.request_body_sample = request_body_sample
        self.request_body_mode = request_body_mode


def _check_route_options(item, where, method, route_path):
    """路由级检查（选项部分）：校验可选字段并预序列化响应体。

    通过则返回 _RouteEntry。pathMode/bodyMode/requestBodyMode 的取值
    区分大小写；requestBody（含显式 null）与 requestBodyMode 只允许
    出现在 POST 路由上；status 与 delayMs 校验取值范围；body 与显式
    requestBody 中的字符串值和对象键必须可编码为 UTF-8。
    """
    path_mode = item.get("pathMode", "exact")
    if not isinstance(path_mode, str) or path_mode not in PATH_MODES:
        # 其他字符串（含 "Exact"、"PREFIX"）或非字符串值（null、布尔、
        # 数字、数组、对象）一律拒绝；取值区分大小写
        raise RulesError(
            f"{where}.pathMode must be 'exact' or 'prefix', "
            f"got {path_mode!r}"
        )
    if path_mode == "prefix" and not route_path.endswith("/"):
        # prefix 沿用 path 的原有限制，另外要求以 / 结尾（根路径 / 也
        # 允许）；exact 路径不做此要求
        raise RulesError(
            f"{where}.path for pathMode 'prefix' must end with '/', "
            f"got {route_path!r}"
        )
    body_mode = item.get("bodyMode", "fixed")
    if not isinstance(body_mode, str) or body_mode not in BODY_MODES:
        # 其他字符串（含 "Fixed"、"TEMPLATE"）或非字符串值（null、
        # 布尔、数字、数组、对象）一律拒绝；取值区分大小写
        raise RulesError(
            f"{where}.bodyMode must be 'fixed' or 'template', "
            f"got {body_mode!r}"
        )
    request_body_present = "requestBody" in item
    request_body_sample = None
    request_body_mode = "exact"
    if "requestBodyMode" in item:
        # 只允许与 POST 路由的显式 requestBody 一起出现（requestBody
        # 为 null 也算显式存在）；取值必须是区分大小写的 "exact" 或
        # "subset"，其他值一律拒绝
        if method != "POST" or not request_body_present:
            raise RulesError(
                f"{where}.requestBodyMode is only allowed together with "
                f"an explicit requestBody on a POST route"
            )
        mode = item["requestBodyMode"]
        if mode not in REQUEST_BODY_MODES:
            raise RulesError(
                f"{where}.requestBodyMode must be 'exact' or 'subset', "
                f"got {mode!r}"
            )
        request_body_mode = mode
    if request_body_present:
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
            f"{where}: status must be the integer 200, 201 or an integer "
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
    return _RouteEntry(
        status=status,
        body=body,
        delay_ms=delay_ms,
        path_mode=path_mode,
        body_mode=body_mode,
        template_body=item["body"] if body_mode == "template" else None,
        request_body_present=request_body_present,
        request_body_sample=request_body_sample,
        request_body_mode=request_body_mode,
    )


def load_rules(path):
    """加载并校验规则文件，返回 Routes：{(method, path): (状态码, 响应字节)}。

    返回值的 delays 属性为 {(method, path): 延迟毫秒数} 映射；
    request_bodies 属性为 {(method, path): requestBody 样例值} 映射，
    仅包含显式配置 requestBody 的 POST 路由（样例可以是 None，对应显式
    JSON null，故以键是否存在而非值是否为 None 区分）；
    request_body_modes 属性为 {(method, path): "exact"|"subset"} 映射，
    键集合与 request_bodies 一致；
    path_modes 属性为 {(method, path): "exact"|"prefix"} 映射，每条路由
    都有键，缺省 pathMode 时记为 "exact"；
    body_modes 属性为 {(method, path): "fixed"|"template"} 映射，每条
    路由都有键，缺省 bodyMode 时记为 "fixed"；
    template_bodies 属性为 {(method, path): body 原始 JSON 值} 映射，
    仅包含 bodyMode 为 "template" 的路由，供每次命中按当前方法、路径、
    查询串、路径剩余文本与本次请求头渲染。
    """
    routes = Routes()
    routes.delays = {}
    routes.request_bodies = {}
    routes.request_body_modes = {}
    routes.path_modes = {}
    routes.body_modes = {}
    routes.template_bodies = {}
    for index, item in enumerate(_read_route_items(path)):
        where = f"routes[{index}]"
        method, route_path = _check_route_identity(item, where)
        key = (method, route_path)
        if key in routes:
            raise RulesError(f"{where}: duplicate route {method} {route_path}")
        entry = _check_route_options(item, where, method, route_path)
        routes[key] = (entry.status, entry.body)
        routes.delays[key] = entry.delay_ms
        routes.path_modes[key] = entry.path_mode
        routes.body_modes[key] = entry.body_mode
        if entry.body_mode == "template":
            # 模板路由保留 body 的原始 JSON 值，每次命中按当前方法、路径、
            # 查询串、路径剩余文本与本次请求头渲染；选项校验中的序列化已
            # 保证其中字符串值与对象键可编码为 UTF-8
            routes.template_bodies[key] = entry.template_body
        if entry.request_body_present:
            routes.request_bodies[key] = entry.request_body_sample
            routes.request_body_modes[key] = entry.request_body_mode
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
        def _body_matches(sample, raw, mode):
            # 空正文、非法 UTF-8、JSON 语法错误、NaN/Infinity 等非标准
            # 字面量、溢出为 inf/-inf 的数字或递归比较不通过，均视为不匹配；
            # 完整正文先通过全部解析检查，再按模式比较（额外字段不能绕过检查）
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
            if mode == "subset":
                return _json_subset(sample, data)
            return _json_equal(sample, data)

        def _resolve(self, path):
            # 先按请求方法筛选，再优先选择完整路径相等的 exact 规则
            # （同路径若配成 prefix 不算 exact 命中）；没有 exact 时选择
            # path 最长的 prefix 候选：请求路径以前缀开头且前缀之后的
            # 剩余部分非空（剩余部分可含多级路径）。规则排列顺序不影响
            # 结果。两种模式都忽略查询字符串（path 已由 urlsplit 去掉），
            # 路径大小写、尾部斜杠与百分号转义均按原样比较。
            # 返回 (路由键, 路径剩余文本)：exact 命中时剩余文本固定为空
            # 字符串；prefix 命中时为匹配路径去掉该路由完整 path 后的
            # 剩余部分（根前缀 / 只去掉开头的一个斜杠），保留大小写、
            # 连续斜杠、尾斜杠与百分号转义，不解码、不规范化。
            exact_key = (self.command, path)
            if routes.path_modes.get(exact_key) == "exact":
                return exact_key, ""
            best_key = None
            for key, mode in routes.path_modes.items():
                if key[0] != self.command or mode != "prefix":
                    continue
                route_path = key[1]
                if path.startswith(route_path) and len(path) > len(route_path):
                    if best_key is None or len(route_path) > len(best_key[1]):
                        best_key = key
            if best_key is None:
                return None, ""
            return best_key, path[len(best_key[1]):]

        def _respond(self):
            raw_body = self._read_body()
            # 查询串取请求目标中第一个问号之后至井号或目标结尾的文本
            # （不含问号）；urlsplit 不做解码或校验，%ZZ 等文本原样保留，
            # 没有问号或仅有结尾问号时得到空字符串。查询串不参与路由选择
            target = urlsplit(self.path)
            path = target.path
            query = target.query
            key, path_suffix = self._resolve(path)
            if key is None:
                # 未命中即使正文非法也返回原有 404 正文，不做请求体校验
                self._send(404, NOT_FOUND_BODY)
                return
            entry = routes[key]
            if key in routes.request_bodies and not self._body_matches(
                routes.request_bodies[key],
                raw_body,
                routes.request_body_modes.get(key, "exact"),
            ):
                # 请求体不匹配：只校验最终选中的路由，不尝试其他候选，
                # 也不应用配置的状态、正文或延迟
                self._send(400, REQUEST_BODY_MISMATCH_BODY)
                return
            # 请求体已读完且（若有样例）校验通过：每次命中（含错误状态码
            # 路由）都先等待配置的时长，再发送状态行、响应头与响应体
            delay_ms = routes.delays.get(key, 0)
            if delay_ms:
                time.sleep(delay_ms / 1000)
            status, body = entry
            if routes.body_modes.get(key) == "template":
                # 模板只作用于最终选中的路由：把 body 字符串值中的
                # {{request.method}} 替换为本次请求方法（大写 GET 或
                # POST）、{{request.path}} 替换为本次用于匹配的路径、
                # {{request.query}} 替换为本次请求目标中的原始查询串、
                # {{request.pathSuffix}} 替换为最终选中前缀之后的路径
                # 剩余文本（exact 命中时为空字符串）、
                # {{request.queryParam.name}} 替换为参数 name 的第一个
                # 原始值（缺失时为空字符串）、{{request.header.name}}
                # 替换为请求头 name 的第一个同名头解析后文本（缺失或为空
                # 时为空字符串，头名称不区分大小写）后再序列化，
                # Content-Length 按替换后的 UTF-8 JSON 字节数给出。
                # 参数映射每次请求由本次查询串现算，头值每次请求由本次
                # 请求头现算，均不跨请求沿用；请求头不参与路由选择或
                # requestBody 比较
                rendered = _render_template(
                    routes.template_bodies[key],
                    path,
                    self.command,
                    query,
                    path_suffix,
                    _query_params(query),
                    _header_values(self.headers),
                )
                body = json.dumps(
                    rendered, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            self._send(status, body)

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
    parser.add_argument(
        "--check-rules",
        action="store_true",
        help=(
            "validate the rules file and exit without starting the server "
            "(no port is bound or probed)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        routes = load_rules(args.rules)
    except RulesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check_rules:
        # 仅做与正常启动完全一致的规则加载校验：不模拟请求、不渲染模板、
        # 不按 delayMs 等待，也不绑定或探测端口；校验通过即结束
        print(f"mock_server rules valid ({len(routes)} route(s))")
        return 0

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
