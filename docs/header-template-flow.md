# {{request.header.name}} 取值流程：从本次请求头到 JSON 响应

本文专门解释一件事：`bodyMode: "template"` 路由的 body 字符串中的 `{{request.header.name}}`，是如何从**本次请求的请求头**一步步变成**响应 JSON 中的字符串**的。读完可以把每个中间结果与源码逐行核对。

文中每条公开规则都标注了对应的源码文件与函数，并给出 `tests/test_mock_server.py` 中 `HeaderTemplateTests` 锁定该行为的现有用例。主示例（第 4 节）已按文档原样实测；其余小节的行为结论均直接引用现有测试佐证，未额外执行的推导只标注源码依据，不写作实测结果。

- 产品代码：`mock_server/__init__.py`
- 命令行入口：`mock_server/__main__.py`（`python -m mock_server`）
- 测试：`tests/test_mock_server.py`（`HeaderTemplateTests`，第 6475 行起）

下文行号均对应当前版本，函数名是长期稳定的核对锚点；即使行号有漂移，按函数名查找即可。

## 1. 完整规则文件

把下面内容原样保存为 `header-flow-rules.json`——只配置一条 `GET /echo`，`bodyMode` 为 `template`，body 为 `{"trace":"{{request.header.X-Trace-Id}}"`，其余选项（`status`、`delayMs`、`pathMode` 等）全部沿用默认值：

```json
{
  "routes": [
    {
      "method": "GET",
      "path": "/echo",
      "bodyMode": "template",
      "body": {"trace": "{{request.header.X-Trace-Id}}" }
    }
  ]
}
```

仓库自带的 `rules.json` 中也有一条完全相同的 `/echo` 路由（另含其他演示路由），`HeaderTemplateTests.test_spec_acceptance_with_shipped_rules_on_same_connection`（第 6513 行）就是直接用它验收的；本文示例使用上面这份独立文件，行为一致。

启动方式沿用 README：在项目根目录执行

```bash
python -m mock_server --rules header-flow-rules.json --port 8765
```

（环境中命令名为 `python3` 时用 `python3 -m mock_server ...`，完全等价。）看到 `mock_server listening on http://127.0.0.1:8765 (1 route(s))` 后即可核对下文示例；`Ctrl+C` 停止。

也可以先不启动服务，只校验规则文件：

```bash
python -m mock_server --check-rules --rules header-flow-rules.json
# 输出：mock_server rules valid (1 route(s))
```

注意 `--check-rules` 只做与正常启动完全一致的规则加载校验：**不模拟请求、不渲染模板**、不绑定端口（`main`，第 797–801 行）。body 中的占位符写法是否合法在此阶段完全不被检查——占位符只是普通的 body 字符串文本。

## 2. 一句话结论

**每次请求命中 template 路由时，服务当场把本次请求头收集成「小写名称 → 第一个值」的映射，再用一次自左向右的正则扫描把 body 字符串值中的 `{{request.header.name}}` 替换为该映射中 `name` 小写后的值（缺失或为空则为空字符串），最后把渲染结果重新序列化为 UTF-8 JSON 发送。** 取值不缓存、不跨请求沿用；替换结果不再参与第二次扫描。

这条结论在 `mock_server/__init__.py` 的 `MockHandler._respond`（第 672–727 行）中：第 701 行判断选中路由的 `bodyMode` 为 `template` 后，第 715–723 行调用 `_render_template`，其中第 722 行 `_first_headers(self.headers)` 当场从本次请求头构建映射——这行代码在每次请求的 `_respond` 里都会重新执行，这就是「下一次请求不会沿用前一次的值」的直接原因。

## 3. 取值流水线（公开规则 ↔ 源码）

`{{request.header.name}}` 从请求到响应经过四个阶段，每阶段的判断都有明确的源码位置：

### 3.1 取头：标准库解析 → `_first_headers`

- 服务基于标准库 `http.server.BaseHTTPRequestHandler`（`mock_server/__init__.py` 第 9、603 行）。在处理任何请求之前，标准库已把请求头从原始报文字节解析成 `self.headers`（一个 `email.message.Message`）：字段值前导的可选空白（冒号后的 OWS）由解析器按协议折叠处理，本功能**从未接触原始报文字节**，拿到的就是解析后的字段名与字段值文本。
- `MockHandler._respond` 第 722 行把 `self.headers` 交给 `_first_headers`（第 254–270 行）：遍历 `headers.items()`，同名头按报文从上到下的顺序出现，**只保留第一项**——即使第一项的值为空字符串也不被后续同名头覆盖；键统一转为小写。
- 此阶段**不做**任何额外加工：不去除首尾空白、不做 URL 解码（`%2F`、`+` 原样保留）、不按逗号分割、不做类型转换。值里有什么就是什么。

### 3.2 识别占位符：`_TEMPLATE_PLACEHOLDER_RE`

- 占位符形态由常量定义：`HEADER_PREFIX = "{{request.header."`（第 49 行）与 `HEADER_NAME_RE = r"[A-Za-z][A-Za-z0-9-]*"`（第 50 行）——名称首字符限 ASCII 英文字母，后续字符限 ASCII 字母、数字或连字符（`-`）。下划线、点号等其他字符不合法。
- 六个占位符形态编译进同一个正则 `_TEMPLATE_PLACEHOLDER_RE`（第 56–74 行），header 一族是其中最后一个备选（第 70–73 行），捕获名称为分组 2。
- **前缀 `{{request.header.` 区分大小写**（`{{request.Header.X}}` 不识别），而**头名称匹配不区分大小写**（靠取值时的小写化，见 3.3）。
- 带空格的写法（`{{ request.header.X }}`、`{{request.header. X}}`）、名称缺失（`{{request.header.}}`）、名称不合规则（`{{request.header.1x}}`、`{{request.header.x_y}}`）都**不匹配正则**，因此原样保留；规则加载不检查 body 中的占位符写法，这些写法不会导致加载错误或 `--check-rules` 失败。

### 3.3 渲染：`_render_template`

- `_render_template`（第 273–346 行）递归遍历 body 的 JSON 值：字符串值用 `_TEMPLATE_PLACEHOLDER_RE.sub` 做**一次自左向右的扫描**（第 331 行）；列表、对象递归处理（第 332–345 行）；**对象键、非字符串值与 JSON 结构保持不变**。
- header 分支在第 317–321 行：`headers.get(header_name.lower(), "")`——占位符中的名称转小写后查 3.1 的小写映射，所以请求头写作 `x-trace-id`、`X-Trace-Id`、`X-TRACE-ID` 都能被 `{{request.header.X-Trace-Id}}` 取到；**头缺失或值为空都得到空字符串**。
- 正则单次扫描保证：**替换结果不再参与处理**。头值中即使含有 `{{request.path}}` 甚至 `{{request.header.X-Other}}` 这样的占位符形态字符，也只是普通文本，不会被二次展开。
- 渲染只发生在最终选中的路由上（`_respond` 第 701 行的 `body_modes.get(key) == "template"` 判断）；`bodyMode` 省略或取 `fixed` 时根本不走这条路，body 预序列化的字节原样返回，占位符保持原文。

### 3.4 响应发送：重新序列化 → `_send`

- 渲染结果在 `_respond` 第 724–726 行重新序列化：`json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))` 再 `.encode("utf-8")`——紧凑分隔符、非 ASCII 字符不转义、按 UTF-8 编码。
- `_send`（第 729–734 行）发送状态行与响应头：`Content-Type: application/json; charset=utf-8`（第 16 行常量），**`Content-Length` 按渲染后 UTF-8 JSON 的实际字节数给出**（第 732 行 `str(len(body))`），随后写入正文。
- 请求头只出现在 3.1–3.3 的取值链路中：**不参与路由选择**（`_resolve`，第 646–670 行，只看方法与路径）、**不参与 `requestBody` 正文比较**（`_body_matches`，第 623–644 行，只看正文字节与样例）。

流程图：

```text
标准库解析请求头（self.headers，原始字节 → 字段名/字段值）
        │
_first_headers：{小写名称: 第一个值}        ← 取头（每次请求现算）
        │
_TEMPLATE_PLACEHOLDER_RE 识别占位符形态      ← 识别（前缀区分大小写，
        │                                      名称限字母/数字/连字符）
_render_template：单次扫描替换                ← 渲染（缺失/空 → ""；
        │                                      替换结果不再展开；
        │                                      键与非字符串值不变）
json.dumps → UTF-8 字节 → _send             ← 发送（Content-Length
                                               按最终字节数）
```

## 4. 主示例：同一连接上的两次请求

服务以第 1 节的规则启动后，在**同一条 HTTP 连接**上连续发送两次 `GET /echo`。HTTP/1.1 长连接由 `protocol_version = "HTTP/1.1"`（第 604 行）保持，两次请求复用同一连接但完全独立处理。

### 4.1 第一次请求：重复头 + 占位符形态的值

完整请求报文（请求头按发送顺序逐行列出）：

```http
GET /echo HTTP/1.1
Host: 127.0.0.1:8765
x-trace-id: A%2Fb+Z{{request.path}}
X-Trace-Id: later
Accept: */*

```

确定响应（标准库自动生成的 `Server`、`Date` 响应头从略）：

```http
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 35

{"trace":"A%2Fb+Z{{request.path}}"}
```

逐步核对：

1. **取头**：`self.headers` 中有两个同名头（名称大小写不同，但匹配不区分大小写）：`x-trace-id: A%2Fb+Z{{request.path}}` 在前、`X-Trace-Id: later` 在后。`_first_headers`（第 265–269 行）按报文顺序只保留第一项，映射为 `{"x-trace-id": "A%2Fb+Z{{request.path}}", ...}`，`later` 被丢弃——**重复头只取首项**。
2. **识别**：body 字符串 `"{{request.header.X-Trace-Id}}"` 整体匹配 header 备选，捕获名称 `X-Trace-Id`。
3. **渲染**：`headers.get("x-trace-id")` 得到 `A%2Fb+Z{{request.path}}`。值中的 `%2F` 与 `+` **不做 URL 解码**原样保留；值尾部的 `{{request.path}}` 是替换结果的一部分，单次扫描**不会再次展开**——所以响应里是字面的 `{{request.path}}` 而不是 `/echo`。
4. **发送**：渲染结果 `{"trace":"A%2Fb+Z{{request.path}}"}` 序列化为 35 字节的 UTF-8 JSON，`Content-Length: 35`。

### 4.2 第二次请求：不带该头

完整请求报文：

```http
GET /echo HTTP/1.1
Host: 127.0.0.1:8765
Accept: */*

```

确定响应：

```http
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 12

{"trace":""}
```

为什么不是上一次的 `A%2Fb+Z{{request.path}}`：`_respond` 对每次请求完整执行一遍，第 722 行 `_first_headers(self.headers)` 用的是**本次请求**的 `self.headers`（标准库为连接上的每个请求重新解析请求头），上一次请求构建的映射没有任何地方被保存——`routes.template_bodies` 里存的是 body 原始 JSON 值（`load_rules` 第 591–595 行），渲染中间结果不落盘、不挂到路由或连接对象上。本次请求没有该头，`headers.get("x-trace-id", "")` 得到空字符串（第 321 行），于是 `{"trace":""}`，12 字节。

### 4.3 实测说明

第 4.1、4.2 两节的请求与响应已按文档原样实测（原始套接字发送上述报文，逐字节核对响应正文与 `Content-Length`）。与之对应的现有自动化验收是 `HeaderTemplateTests.test_spec_acceptance_with_shipped_rules_on_same_connection`（第 6513 行，用自带 `rules.json` 在同一条连接上验证「带头 → 回显、下一次不带 → 空字符串」）与 `test_each_request_uses_its_own_headers_on_same_connection`（第 6796 行，同一连接五种带头/不带组合各自独立取值）。

## 5. 逐条行为规则与源码依据

以下结论不再逐条实测，均给出源码依据与 `HeaderTemplateTests` 中的对应用例。

### 5.1 名称匹配不区分大小写，前缀区分大小写

- 占位符前缀 `{{request.header.` 必须与 `HEADER_PREFIX`（第 49 行）逐字符相等，`{{request.Header.X}}` 不识别、原样保留。
- 名称匹配不区分大小写靠两次小写化实现：收集时 `_first_headers` 第 267 行 `name.lower()`，取值时 `_render_template` 第 321 行 `header_name.lower()`。所以 `x-trace-id`、`X-Trace-Id`、`X-TRACE-ID` 互相等价。
- 佐证：`test_header_name_case_insensitive_but_placeholder_prefix_case_sensitive`（第 6617 行）；前缀大小写不符原样保留见 `test_malformed_and_unknown_placeholders_kept_verbatim`（第 6682 行，`lower_prefix` 键）。

### 5.2 重复头只取首项，首项为空仍为空

- `_first_headers` 第 268–269 行 `if key not in first`：第一项写入后，后续同名头（含大小写不同的写法）一律不覆盖，**即使第一项的值为空字符串**。不会拼接、不会跳过空项取下一项。
- 因此首个同名头值为空时，渲染结果就是空字符串，与「头缺失」不可区分。
- 佐证：`test_duplicate_headers_first_wins_even_when_first_empty`（第 6583 行，含 `[("X-Empty", ""), ("X-Empty", "later")]` 得 `""` 的子用例）。

### 5.3 值是标准库解析后的文本，不是原始报文字节

- 功能拿到的值来自 `self.headers`，原始报文字节到字段值的转换（含前导可选空白的协议折叠）全部由标准库完成，发生在本功能之外。
- 在此之上本功能**不再**做任何加工：不额外去除首尾空白（如值 `trail  ` 的尾部空格保留）、不按逗号拆分（`a,b,c` 是一个值）、不做 URL 解码（`A%2Fb+Z` 原样）、`+` 不转空格、`123`/`true`/`null` 等文本不转换类型。
- 佐证：`test_value_kept_verbatim_no_trim_no_decode_no_split_no_conversion`（第 6541 行，九组值逐组核对）。

### 5.4 替换结果不再展开

- `_TEMPLATE_PLACEHOLDER_RE.sub`（第 331 行）是对模板字符串的单次扫描；替换文本只是输出，不会重新进入正则。头值中含 `{{request.path}}`、`{{request.header.X-Other}}` 等形态都按字面文本进入响应（主示例 4.1 即此情形）。
- 佐证：`test_replacement_text_is_not_reprocessed`（第 6767 行：头 `X-Trace-Id` 的值为 `{{request.header.X-Path}}` 时，响应中该值原样保留，而 `X-Path` 自己的占位符正常取到 `LEAKED`，二者互不泄漏）。

### 5.5 非法形态的占位符原样保留，不触发加载错误

- 名称规则 `HEADER_NAME_RE`（第 50 行）：首字符 ASCII 字母，后续 ASCII 字母/数字/连字符。`{{request.header.x_y}}`（下划线）、`{{request.header.1x}}`（首字符数字）、`{{request.header.}}`（缺名称）、`{{request.header.x y}}`（名称含空格）、`{{ request.header.X }}`（整体带空格）都不匹配正则，原样保留，不尝试取值。
- 规则加载与 `--check-rules` 不检查 body 中的占位符写法：`_check_route_options`（第 451–553 行）只校验 `bodyMode` 取值本身（第 474–481 行）与 body 的 UTF-8 可编码性（第 532–542 行），占位符只是普通字符串文本。
- 佐证：`test_malformed_and_unknown_placeholders_kept_verbatim`（第 6682 行）、`test_rules_load_and_check_rules_unaffected_by_placeholders`（第 6916 行）。

### 5.6 缺失的合法头替换为空字符串

- 名称合法但本次请求没有该头（或字段值为空）时，`_render_template` 第 321 行的 `.get(..., "")` 得到空字符串；嵌入在文本中间的占位符同样塌缩（`x{{...}}y` → `xy`）。
- 佐证：`test_missing_or_empty_header_replaced_with_empty_string`（第 6649 行）。

### 5.7 对象键与非字符串值不变；fixed/省略 bodyMode 时占位符保持原文

- `_render_template` 只对字符串**值**做替换：第 339–345 行递归字典时键原样保留（键中的占位符文本不展开），数字、布尔、`null` 等非字符串值走到第 346 行原样返回，JSON 结构不变。
- `bodyMode` 省略或取 `fixed` 时，`_respond` 第 701 行的判断不成立，发送的是 `load_rules` 阶段预序列化的固定字节（第 532–535 行），占位符保持原文。
- 佐证：`test_nested_repeated_embedded_mixed_values_replaced`（第 6723 行，含键名保持原样、嵌套数组中的标量不变）、`test_fixed_and_default_mode_keep_placeholder_verbatim`（第 6889 行）。

### 5.8 请求头不参与选路与正文比较；--check-rules 不渲染模板

- 选路只看方法与路径：`_resolve`（第 646–670 行）的输入是 `self.command` 与去掉查询串的 `path`，完全不读请求头；带不带头、带什么头，命中结果与 404 都一样。
- `requestBody` 比较只看正文字节：`_body_matches`（第 623–644 行）不接触请求头；POST 模板路由正文校验失败时返回固定的 `400 {"error":"request_body_mismatch"}`，模板不渲染，头值不会泄漏到响应中。
- `--check-rules` 在 `main` 第 797–801 行：加载校验通过即打印退出，不启动服务、不模拟请求、不渲染模板。
- 佐证：`test_headers_do_not_participate_in_routing_or_body_match`（第 6823 行）、`test_rules_load_and_check_rules_unaffected_by_placeholders`（第 6916 行）。

### 5.9 Content-Length 按最终 UTF-8 JSON 字节数

- `Content-Length` 在 `_send` 第 732 行按**渲染并序列化之后**的正文字节数计算（`len(body)`，`body` 已是 UTF-8 字节串），不是模板原文的长度。主示例中两次响应分别为 35 与 12 字节。
- 佐证：上述各用例中的 `assertEqual(int(headers["Content-Length"]), len(raw))`（如第 6531、6535、6817 行）。

## 6. 响应汇总

| # | 请求（同一连接，按序） | 关键请求头（按报文顺序） | 状态码 | 响应正文 | Content-Length |
| --- | --- | --- | --- | --- | --- |
| 4.1 | `GET /echo` | `x-trace-id: A%2Fb+Z{{request.path}}`、`X-Trace-Id: later` | 200 | `{"trace":"A%2Fb+Z{{request.path}}"}` | 35 |
| 4.2 | `GET /echo` | （无该头） | 200 | `{"trace":""}` | 12 |

所有响应均带 `Content-Type: application/json; charset=utf-8`。

## 7. 现有测试佐证

均可在项目根目录用 README 的方式运行：`python3 -m unittest discover -s tests`。以下用例均位于 `tests/test_mock_server.py` 的 `HeaderTemplateTests`（第 6475 行起）。

| 结论 | 测试位置 |
| --- | --- |
| 主示例验收：同一连接上带头回显、下一次不带为空，不沿用前次的值 | `test_spec_acceptance_with_shipped_rules_on_same_connection`（第 6513 行）、`test_each_request_uses_its_own_headers_on_same_connection`（第 6796 行） |
| 值原样保留：不去首尾空白、不解码、不拆逗号、不转类型 | `test_value_kept_verbatim_no_trim_no_decode_no_split_no_conversion`（第 6541 行） |
| 重复头只取首项，首项为空也不被后续覆盖；大小写不同写法同样只取首项 | `test_duplicate_headers_first_wins_even_when_first_empty`（第 6583 行） |
| 头名称匹配不区分大小写；占位符前缀区分大小写；名称限字母开头 + 字母/数字/连字符 | `test_header_name_case_insensitive_but_placeholder_prefix_case_sensitive`（第 6617 行） |
| 头缺失或值为空都替换为空字符串，嵌入形态同样塌缩 | `test_missing_or_empty_header_replaced_with_empty_string`（第 6649 行） |
| 带空格、前缀大小写不符、名称含下划线/首字符数字/缺名称等写法原样保留 | `test_malformed_and_unknown_placeholders_kept_verbatim`（第 6682 行） |
| 嵌套、重复、混合占位符均替换；对象键与非字符串值不变 | `test_nested_repeated_embedded_mixed_values_replaced`（第 6723 行） |
| 替换结果（含占位符形态的头值）不再二次展开 | `test_replacement_text_is_not_reprocessed`（第 6767 行） |
| 请求头不参与路由选择与 `requestBody` 比较；校验失败的 400 不渲染模板 | `test_headers_do_not_participate_in_routing_or_body_match`（第 6823 行） |
| `fixed` 或省略 `bodyMode` 时占位符保持原文 | `test_fixed_and_default_mode_keep_placeholder_verbatim`（第 6889 行） |
| 非法占位符写法不影响规则加载与 `--check-rules`（不渲染模板） | `test_rules_load_and_check_rules_unaffected_by_placeholders`（第 6916 行） |
