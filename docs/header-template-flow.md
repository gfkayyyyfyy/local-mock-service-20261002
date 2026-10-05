# 请求头模板取值流程（`{{request.header.name}}`）

本文专门解释一件事：`bodyMode: "template"` 的响应体中，`{{request.header.name}}` 是如何从**本次请求的原始报文**一步步变成 **JSON 响应中的一个字符串值**的，以及同一连接上的下一次请求为什么不会沿用前一次的头值。

文中每条公开规则都标注了对应的源码文件与函数，并给出 `tests/test_mock_server.py` 中 `HeaderTemplateTests` 锁定该行为的现有用例。第 4 节主示例的请求与响应已按文档原样实测（命令与输出见该节）；其余小节的结论均标注源码依据与对应测试，未单独执行的推导不写成实测结果。

- 产品代码：`mock_server/__init__.py`
- 命令行入口：`mock_server/__main__.py`（`python -m mock_server`）
- 测试：`tests/test_mock_server.py`

下文行号均对应当前版本，函数名是长期稳定的核对锚点；即使行号有漂移，按函数名查找即可。

## 1. 完整规则文件

把下面内容原样保存为 `header-flow-rules.json`（UTF-8 编码，只配置一个 GET 路由，其余选项全部沿用默认值——不配置 `status` 即 200、不配置 `pathMode` 即 `exact`、不配置 `delayMs` 即不延迟）：

```json
{
  "routes": [
    {
      "method": "GET",
      "path": "/echo",
      "bodyMode": "template",
      "body": {"trace": "{{request.header.X-Trace-Id}}"}
    }
  ]
}
```

启动方式沿用 README：在项目根目录执行

```bash
python -m mock_server --rules header-flow-rules.json --port 8765
```

（环境中命令名为 `python3` 时用 `python3 -m mock_server ...`，完全等价；`--port 8765` 本身就是默认值，可省略。）看到 `mock_server listening on http://127.0.0.1:8765 (1 route(s))` 后即可核对下文示例；`Ctrl+C` 停止。

也可以先不启动服务，只用与启动完全一致的标准校验规则文件：

```bash
python -m mock_server --check-rules --rules header-flow-rules.json
# 输出：mock_server rules valid (1 route(s))
```

注意 `--check-rules` 只做规则加载校验：不模拟请求、**不渲染模板**、不绑定端口（`main`，`mock_server/__init__.py` 第 797–801 行及其注释）。占位符写法是否“合法”在校验阶段根本不被检查——`body` 只是普通 JSON 字符串，加载时不扫描其中的占位符（源码第 41–46 行注释明确说明“规则加载不检查 body 中的占位符写法”）。

## 2. 一句话结论

**每次请求命中模板路由后，服务用本次请求头现建一个“小写名称 → 第一个值”的映射，把响应体字符串值中的 `{{request.header.name}}` 替换为该映射中 `name` 小写后的对应值（缺失或为空则为空字符串），再序列化为 UTF-8 JSON 发出。** 取值只发生在最终选中路由的响应渲染阶段：请求头不参与选路、不参与 `requestBody` 比较，取值结果不跨请求保留。

## 3. 取值链路（公开规则 ↔ 源码）

一次 `GET /echo` 请求中，`{{request.header.X-Trace-Id}}` 经过的四个环节，全部在 `mock_server/__init__.py`：

### 3.1 取头：`_first_headers`（第 254–270 行）

- 服务**从不直接读取原始报文字节**。请求行与请求头由标准库 `http.server.BaseHTTPRequestHandler` 解析（`MockHandler` 继承自它，第 603 行），解析结果是 `self.headers`（一个 `email.message.Message`）。下文说的“头值”一律指**标准库解析后的文本**，不是原始字节：字段值前导的可选空白（OWS）已由解析器按协议折叠，其余内容原样保留。
- `_first_headers(self.headers)` 遍历 `headers.items()`：同名头按报文从上到下的顺序出现，**只保留第一项**——即使第一项的值为空字符串也不被后续同名头覆盖（第 265–270 行，`if key not in first`）；键统一 `name.lower()` 小写化，因此后续查找天然不区分大小写。
- 该函数**不额外去除首尾空白、不做 URL 解码、不按逗号分割、不做类型转换**，`+` 与百分号转义原样保留（函数 docstring，第 255–264 行）。
- 这个映射在 `MockHandler._respond` 的模板分支里**每次请求现取**（第 722 行 `_first_headers(self.headers)`），是局部变量，请求结束即丢弃——这是“不跨请求沿用”的直接原因（第 713–714 行注释：“头映射每次请求由本次请求头现取，均不跨请求沿用”）。

### 3.2 识别占位符：`_TEMPLATE_PLACEHOLDER_RE`（第 56–74 行）

- header 一族占位符的形态由 `HEADER_PREFIX = "{{request.header."`（第 49 行）与 `HEADER_NAME_RE = r"[A-Za-z][A-Za-z0-9-]*"`（第 50 行）界定：**前缀逐字符匹配、区分大小写**；名称首字符限 ASCII 英文字母，后续字符限 ASCII 字母、数字或连字符（`-`）。下划线、点号、空格、数字开头都不合名称规则。
- 六个占位符形态（四个固定占位符 + queryParam、header 两族）编成**一个正则**，对字符串**自左向右单次扫描**（第 51–55 行注释）：嵌入、重复、混合出现的占位符一次替换完，且**替换结果不再参与处理**——头值里即使恰含 `{{request.path}}` 之类的占位符形态，也不会被二次展开。
- 不匹配该正则的写法（带空格、前缀大小写不同、名称缺失或不合规则、未知占位符）**原样保留**，不执行任何表达式、不尝试取值，也不会导致规则加载失败（见第 6 节）。

### 3.3 渲染：`_render_template`（第 273–346 行）

- 递归遍历 `body` 的原始 JSON 值：字符串值交给上述正则替换（第 311–331 行）；**对象键不替换**（第 339–345 行只对 `value.items()` 的值递归）；数字、布尔、`null` 等**非字符串值原样返回**（第 346 行）；数组、嵌套对象中的字符串值同样替换（第 332–338 行）。
- header 分支在第 317–321 行：`headers.get(header_name.lower(), "")`——占位符中的名称小写化后查 3.1 的映射，**头缺失或值为空都得到空字符串**。
- 渲染的输入来自本次请求：路径、方法、查询串、路径剩余文本、查询参数映射与头映射一起作为参数传入（`_respond` 第 715–723 行），函数本身不保存任何跨请求状态。

### 3.4 响应发送：`MockHandler._respond` 模板分支（第 701–727 行）与 `_send`（第 729–734 行）

- 模板只作用于**最终选中的路由**（第 701 行 `if routes.body_modes.get(key) == "template":`，在 `_resolve` 选路与请求体校验之后）；`bodyMode` 缺省为 `"fixed"`（`_check_route_options` 第 474 行），fixed 路由的 `body` 在加载时已预序列化（第 532–535 行），占位符**原样返回**。
- 渲染结果用 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 序列化并编码为 UTF-8（第 724–726 行），与固定响应同一套紧凑格式。
- `_send` 发送状态行、`Content-Type: application/json; charset=utf-8`（常量第 16 行）与 **`Content-Length: str(len(body))`（第 732 行）——按渲染后最终 UTF-8 JSON 的字节数**，不是按模板原文。

流程图：

```text
本次请求报文
   │  标准库 http.server 解析（MockHandler 继承 BaseHTTPRequestHandler）
   ▼
self.headers（email.message.Message，已按协议折叠前导空白）
   │  _first_headers：{小写名称: 第一个值}，每次请求现建
   ▼
_render_template：_TEMPLATE_PLACEHOLDER_RE 单次扫描
   │  {{request.header.name}} → headers.get(name.lower(), "")
   │  （对象键、非字符串值不变；替换结果不再展开）
   ▼
json.dumps(ensure_ascii=False, 紧凑分隔符).encode("utf-8")
   │  _send：Content-Length = 最终字节数
   ▼
HTTP 响应（application/json; charset=utf-8）
```

## 4. 主示例：同一连接上的两次 GET /echo

本节命令与输出均已按文档原样实测（Python 3.14.4、curl 8.18.0；`Server`/`Date` 响应头随环境变化，下表从略）。

### 4.1 请求

用 curl 在同一连接上连续发送两个请求（`--next` 分隔的两段共用同一条 keep-alive 连接，可用 `-v` 观察到两段都走 `Connection #0`；`--next` 之后选项重置，因此第二段要重复 `-s -i`，且第一段的头不会带到第二段）：

```bash
curl -s -i \
  -H 'x-trace-id: A%2Fb+Z{{request.path}}' \
  -H 'X-Trace-Id: later' \
  http://127.0.0.1:8765/echo \
  --next -s -i http://127.0.0.1:8765/echo
```

第一个请求实际发出的报文（头顺序即报文顺序，`Host`/`User-Agent`/`Accept` 为 curl 自动添加，`User-Agent` 版本随环境不同）：

```http
GET /echo HTTP/1.1
Host: 127.0.0.1:8765
User-Agent: curl/8.18.0
Accept: */*
x-trace-id: A%2Fb+Z{{request.path}}
X-Trace-Id: later

```

第二个请求实际发出的报文（不携带任何 trace 头）：

```http
GET /echo HTTP/1.1
Host: 127.0.0.1:8765
User-Agent: curl/8.18.0
Accept: */*

```

### 4.2 确定响应

第一个响应：

```http
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 35

{"trace":"A%2Fb+Z{{request.path}}"}
```

第二个响应：

```http
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 12

{"trace":""}
```

### 4.3 逐步核对

**第一次请求，为什么是 `A%2Fb+Z{{request.path}}` 而不是 `later`、不是解码后的值、也没有再展开：**

1. **名称匹配不区分大小写**：占位符写作 `{{request.header.X-Trace-Id}}`，报文里第一项是 `x-trace-id`。`_first_headers` 把键统一小写（第 267 行 `key = name.lower()`），渲染时占位符名称也小写化后查找（第 321 行 `header_name.lower()`），所以 `x-trace-id` 与 `X-Trace-Id` 命中同一个键。
2. **重复头只取首项**：报文中 `x-trace-id`（第 5 行）与 `X-Trace-Id`（第 6 行）是同一个头的两次出现，`_first_headers` 的 `if key not in first`（第 268–269 行）只保留从上到下第一项 `A%2Fb+Z{{request.path}}`，第二项 `later` 被丢弃，不拼接、不覆盖。
3. **值不解码、不转换**：取到的是标准库解析后的文本本身，`%2F` 不还原为 `/`，`+` 不转为空格（`_first_headers` docstring 第 261–263 行；渲染分支直接返回映射值，第 321 行）。
4. **替换结果不再展开**：值里恰含 `{{request.path}}`，但正则对模板字符串只做**单次**自左向右扫描（第 51–55 行注释、第 331 行 `re.sub`），替换进去的文本不会再被扫描，所以响应里仍是字面的 `{{request.path}}`，不会被替换成 `/echo`。
5. **Content-Length 35**：`{"trace":"A%2Fb+Z{{request.path}}"}` 共 35 个 ASCII 字符、35 个 UTF-8 字节，`_send` 第 732 行按渲染后字节数给出。

**第二次请求，为什么是空字符串、而不是沿用第一次的值：**

6. 头映射是 `_respond` 模板分支里的局部变量，**每次请求调用时由本次 `self.headers` 现建**（第 722 行）；上一次请求的映射随第一次响应发出即被丢弃，路由对象 `routes` 中保存的只是 body 原始 JSON 值（`load_rules` 第 591–595 行写入 `template_bodies`），不含任何已渲染结果。本次报文没有 trace 头，`headers.get("x-trace-id", "")` 取到默认值 `""`（第 321 行），于是得到 `{"trace":""}`（12 字节）。keep-alive 只复用 TCP 连接，不复用任何请求级数据。

## 5. 头值：标准库解析后的文本，不是原始报文字节

取值链路（3.1）决定了以下边界，均有 `HeaderTemplateTests` 用例佐证（见第 9 节）：

- **不额外去除首尾空白**：字段值前导的可选空白由标准库解析器按协议折叠（这是 HTTP 解析本身的行为，发生在 mock_server 看到数据之前）；除此之外服务不再修剪——例如值 `trail  ` 的尾部两个空格原样保留在响应中。
- **不拆逗号**：`a,b,c` 是一个值，不会拆成三项，也不会与同名头的后续项合并。
- **不解码、不转换**：`A%2Fb+Z`、`%ZZ`、`123`、`true`、`null` 都按字面文本回显，`+` 不变空格，文本不转成数字或布尔。
- **首个同名头的值为空时结果仍为空字符串**：`X-Empty:`（空值）在前、`X-Empty: later` 在后时，首项空值**不被跳过、不被覆盖**，替换结果仍是 `""`（`_first_headers` 第 268–269 行只判断键是否已存在，不判断值是否为空）。

## 6. 占位符写法：哪些替换、哪些原样保留

由 3.2 的正则形态直接推出：

- **合法但头缺失**（如 `{{request.header.X-Missing}}` 而请求未携带）：替换为空字符串；嵌入写法 `x{{request.header.X-Missing}}y` 塌缩为 `xy`。
- **原样保留、不触发取值、也不触发加载错误**的写法：整体或名称内部带空格（`{{ request.header.X }}`、`{{request.header. X}}`、`{{request.header.x y}}`）、前缀大小写不符（`{{request.Header.X}}`、`{{request.headers.X}}`）、名称缺失或不合名称规则（`{{request.header.}}`、`{{request.header.1x}}`、`{{request.header.x_y}}`、`{{request.header.x.y}}`）、缺少 `}}`（`{{request.header.X`）以及其他未知占位符（`{{request.foo}}`）。它们只是普通字符串文本：规则加载与 `--check-rules` 都不检查占位符写法（第 41–46 行注释），渲染时正则不匹配即原样保留。
- 名称中允许连字符：`{{request.header.X--Y}}` 是合法占位符（`X--Y` 符合 `HEADER_NAME_RE`），该头未发送时替换为空字符串。

## 7. 渲染范围与 bodyMode 的边界

- **对象键不变**：`{"{{request.header.X-Trace-Id}}": "..."}` 的键原样保留，只有字符串值被替换（`_render_template` 第 339–345 行只对值递归）。
- **非字符串值不变**：数字、布尔、`null`、数组与对象结构原样保留（第 346 行）；数组与嵌套对象**内部**的字符串值照常替换（第 332–338 行）。
- **`fixed` 或省略 `bodyMode` 时占位符保持原文**：缺省 `body_mode` 为 `"fixed"`（第 474 行），渲染分支根本不执行（第 701 行），加载时预序列化的 body 原样返回，`{{request.header.X-Trace-Id}}` 按字面出现在响应中。

## 8. 请求头不参与的环节

- **不参与选路**：`_resolve`（第 646–670 行）只看请求方法（`self.command`）与去掉查询串的路径，从不读取 `self.headers`；带不带任何头，命中结果与 404 都一样。
- **不参与 `requestBody` 正文比较**：`_body_matches`（第 623–644 行）只比较请求正文字节与样例；POST 模板路由正文不匹配时仍返回固定的 `400 {"error":"request_body_mismatch"}`，头值不会泄漏到该响应中（模板分支在 400 返回之后，第 693–727 行的顺序）。
- **不参与 `--check-rules`**：校验只做与正常启动一致的规则加载（`main` 第 797–801 行），不模拟请求、不渲染模板，占位符写法不影响校验结果。
- **Content-Length 按最终字节数**：无论替换后内容长短，`_send` 第 732 行都按渲染后 UTF-8 JSON 的实际字节数给出（主示例中 35 与 12）。

## 9. 现有测试佐证

均可在项目根目录用 README 的方式运行：`python3 -m unittest discover -s tests`。以下用例全部位于 `tests/test_mock_server.py` 的 `HeaderTemplateTests`（第 6475 行）：

| 结论 | 测试位置 |
| --- | --- |
| 自带 `rules.json` 的 `GET /echo` 模板：`x-trace-id: A%2Fb+Z` 返回 `{"trace":"A%2Fb+Z"}`；同一连接下一次不带该头返回 `{"trace":""}`，不沿用上一次值；`Content-Length` 等于实际字节数 | `test_spec_acceptance_with_shipped_rules_on_same_connection`（第 6513 行） |
| 头值原样保留：不额外去首尾空白、不解码、不拆逗号、`+` 不转空格、`123`/`true`/`null` 不转换类型、`%ZZ` 原样 | `test_value_kept_verbatim_no_trim_no_decode_no_split_no_conversion`（第 6541 行） |
| 重复同名头只取第一项；不同大小写写法的同名头同样只取第一项；首项为空也不被后续非空项覆盖 | `test_duplicate_headers_first_wins_even_when_first_empty`（第 6583 行） |
| 头名称匹配不区分大小写；名称允许字母开头、后续字母/数字/连字符；占位符前缀区分大小写 | `test_header_name_case_insensitive_but_placeholder_prefix_case_sensitive`（第 6617 行） |
| 头缺失或值为空都替换为空字符串；嵌入形态同样塌缩 | `test_missing_or_empty_header_replaced_with_empty_string`（第 6649 行） |
| 带空格、前缀大小写不符、名称缺失/含下划线/点号/数字开头、缺少 `}}`、未知占位符均原样保留；`X--Y` 是合法头名 | `test_malformed_and_unknown_placeholders_kept_verbatim`（第 6682 行） |
| 顶层及嵌套对象、数组中的字符串值均替换；嵌入、重复及与其他占位符混用生效；对象键、非字符串值与 JSON 结构不变 | `test_nested_repeated_embedded_mixed_values_replaced`（第 6723 行） |
| 头值中即使含占位符形态（含 header 自身）也不再展开 | `test_replacement_text_is_not_reprocessed`（第 6767 行） |
| 同一连接连续请求：头值只来自本次请求头，不沿用前次结果 | `test_each_request_uses_its_own_headers_on_same_connection`（第 6796 行） |
| 请求头不改变路由选择与 404；不参与 `requestBody` 比较；正文不匹配返回固定 400 且头值不泄漏 | `test_headers_do_not_participate_in_routing_or_body_match`（第 6823 行） |
| 省略 `bodyMode` 或取 `fixed` 时 header 占位符原样返回 | `test_fixed_and_default_mode_keep_placeholder_verbatim`（第 6889 行） |
| 不合规则的占位符写法不影响规则加载与 `--check-rules` | `test_rules_load_and_check_rules_unaffected_by_placeholders`（第 6916 行） |

## 10. 响应汇总

| # | 请求（同一连接） | 关键请求头（按报文顺序） | 状态码 | 响应正文 | Content-Length |
| --- | --- | --- | --- | --- | --- |
| 4 | 第 1 个 `GET /echo` | `x-trace-id: A%2Fb+Z{{request.path}}`、`X-Trace-Id: later` | 200 | `{"trace":"A%2Fb+Z{{request.path}}"}` | 35 |
| 4 | 第 2 个 `GET /echo` | （无 trace 头） | 200 | `{"trace":""}` | 12 |

两个响应均带 `Content-Type: application/json; charset=utf-8`；`Content-Length` 为渲染后响应正文的 UTF-8 字节数。
