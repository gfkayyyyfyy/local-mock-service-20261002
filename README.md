# mock_server

仅使用 Python 3 标准库实现的本地 mock 服务：启动时加载 JSON 规则文件，之后对匹配的请求返回固定响应。服务始终绑定 `127.0.0.1`。

## 用法

```bash
python -m mock_server --rules rules.json --port 8765
```

- `--rules`：必填，JSON 规则文件路径。
- `--port`：可选，监听端口，1–65535 的整数，默认 `8765`。

启动成功后向标准输出打印监听地址，例如：

```
mock_server listening on http://127.0.0.1:8765 (1 route(s))
```

按 `Ctrl+C` 停止服务并释放端口。规则仅在启动时加载一次，之后修改文件不影响响应。

## 规则文件格式

UTF-8 编码的 JSON，顶层为对象，含 `routes` 数组；每项为含 `method`、`path`、`body` 三个字段的对象：

```json
{"routes":[{"method":"GET","path":"/hello","body":{"message":"你好"}}]}
```

- `method`：仅接受大写 `"GET"` 或 `"POST"`。
- `path`：以 `/` 开头、不含 `?` 和 `#` 的字符串。
- `pathMode`：可选，取值为区分大小写的字符串 `"exact"` 或 `"prefix"`。省略或取 `"exact"` 时保持完整路径相等匹配；取 `"prefix"` 时将 `path` 作为前缀：请求路径以该前缀开头且剩余部分非空（可含多级路径）即为候选。`prefix` 模式的 `path` 沿用上述限制，另外必须以 `/` 结尾（根路径 `/` 允许作为前缀）。其他取值（含 `"Prefix"`、`null`、数字等）或 `prefix` 路径未以 `/` 结尾都会使规则加载失败。
- `body`：任意 JSON 值（包括 `null`），命中时作为响应体返回。
- `requestBody`：可选，仅允许用于 `POST` 路由。值为任意 JSON 值（包括 `null`），作为完整请求正文的 JSON 样例：命中该 POST 路由时，请求正文必须按 UTF-8（与 `Content-Type` 无关）解析为与样例递归相等的 JSON 值才返回配置的响应。对象须有相同的键集合（键序与排版空白不影响），数组的长度和顺序参与比较，字符串区分大小写，数字按数值比较（`1` 与 `1.0` 相等），布尔值与数字互不相等；显式 `null` 样例只接受正文为 JSON `null`。样例中的字符串值与对象键沿用 `body` 的 UTF-8 可编码限制。缺省时仍忽略请求正文。
- `requestBodyMode`：可选，仅允许与 `POST` 路由的显式 `requestBody` 一起出现（`requestBody` 为 `null` 也算显式存在）。取值为区分大小写的字符串 `"exact"` 或 `"subset"`，省略或取 `"exact"` 时保持上述整体相等比较；取 `"subset"` 时启用对象子集匹配：样例对象的所有键都必须存在于请求正文的对应对象中，对应值递归按同一模式比较，请求对象允许额外键（空对象样例匹配任意对象，但也只匹配对象）；数组仍按相同长度和顺序比较（不接受前缀匹配），其中元素为对象时同样允许额外键；其余值沿用整体相等的比较语义。其他取值（含 `"Subset"`、`null`、数字等）、缺少 `requestBody` 或用于非 `POST` 路由都会使规则加载失败。
- `status`：可选，缺省为 `200`；仅接受整数 `200` 或 `400`–`599`（包含两端）。布尔值、`null`、字符串、浮点数（含 `503.0`）、数组、对象均非法。错误状态同样返回配置的 `body`，不替换为统一错误对象。
- `delayMs`：可选，缺省为 `0`；仅接受 `0`–`2000`（包含两端）的整数，单位为毫秒。布尔值、`null`、字符串、浮点数（含 `200.0`）、数组、对象、负数或大于 `2000` 的整数均非法。命中该路由时，请求体读取完毕后先等待至少配置的时长，再发送状态行、响应头与响应体；每次命中（含错误状态码路由、`requestBody` 校验通过的路由）都应用延迟，未命中的 404 不增加人为等待。
- `routes` 可以为空数组；路由项中除 `requestBody`、`requestBodyMode`、`pathMode`、`status` 与 `delayMs` 外的额外字段会被忽略。
- 不允许重复的 `method` + `path` 组合，不能靠不同的 `pathMode` 区分重复规则。

## 请求匹配与响应

- 匹配时忽略查询字符串；路径的大小写、尾部斜杠、百分号转义按原样比较，星号没有特殊含义。
- 先按请求方法筛选，再优先选择完整路径相等的 `exact` 规则；没有时在 `prefix` 候选中选择 `path` 最长的一条，规则排列顺序不影响结果。与前缀完整相等（剩余部分为空）不算命中。
- 未配置 `requestBody` 的路由忽略请求体（但仍会读完正文以复用连接）；配置了 `requestBody` 的 `POST` 路由先读完整正文再按样例校验，且仅对命中路由校验。
- `GET` / `POST` 命中且（对配置了 `requestBody` 的 POST）正文与样例相等：返回配置的 `status`（缺省 200）及配置的 `body`。
- 命中配置了 `requestBody` 的 POST 路由但正文为空、不是合法 UTF-8、JSON 语法错误、含非有限数字（如 `1e400`、`NaN`）或与样例不相等（`subset` 模式下缺键、被约束值不等或类型不符）：返回 HTTP 400 及 `{"error":"request_body_mismatch"}`，不采用配置的状态、正文或延迟。`subset` 模式下完整正文仍须通过全部 UTF-8、JSON 语法与非有限数字检查，额外字段不能绕过检查。
- 未命中：返回 HTTP 404 及 `{"error":"route_not_found"}`（即使正文非法也不校验、不返回 400；即使某条已命中路由自身配置了 404，也按命中处理并返回其 `body`）。
- 其他 HTTP 方法（PUT、DELETE、OPTIONS、HEAD 等）：返回 501 及 `{"error":"method_not_supported"}`，不读取路由配置（不使用该路径下规则的 `body`、`status` 或 `delayMs`）；响应携带 `Connection: close` 并在发送后关闭连接，携带请求体的请求其剩余字节不会被当作后续请求解释。HEAD 同样返回 501 与上述响应头，但不发送响应体，`Content-Length` 仍按该 JSON 正文的字节数给出。
- 所有响应均为 UTF-8 JSON，`Content-Type: application/json; charset=utf-8`，`Content-Length` 为实际字节数。

## 示例

```bash
$ python -m mock_server --rules rules.json &
$ curl -i "http://127.0.0.1:8765/hello?x=1"
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 25

{"message":"你好"}

$ curl -i "http://127.0.0.1:8765/missing"
HTTP/1.1 404 Not Found
Content-Type: application/json; charset=utf-8
Content-Length: 27

{"error":"route_not_found"}
```

## 测试

回归测试仅依赖 Python 3 标准库，会自行准备临时 UTF-8 规则文件与可用端口，通过 `python -m mock_server` 子进程在 `127.0.0.1` 上发起真实 HTTP 请求，结束后自动释放进程、连接与临时文件，不修改 `rules.json`。启动等待以标准输出的监听提示为准，不依赖 selector 监听子进程管道，Windows 与 Linux 上均可运行。在项目根目录执行：

```bash
python3 -m unittest discover -s tests
```

也可以直接运行：

```bash
python3 tests/test_mock_server.py
```

## 错误处理

以下情况均向标准错误输出原因并以退出码 2 结束，不接收请求：

- 参数缺失或无效（端口非整数或超出 1–65535）
- 规则文件不存在或不可读
- 文件不是合法 UTF-8
- JSON 语法错误
- 数字解析为非有限值（如 `1e400`、`-1e400`、`1E+400` 溢出为 `Infinity`；下溢为 `0` 的如 `1e-400` 不受影响）
- `body` 或 `requestBody` 中的字符串值或对象键含未配对的 Unicode 代理码点（如孤立的 `\ud800` 转义），无法编码为 UTF-8
- 规则结构非法（顶层非对象、缺少 `routes` 数组等）
- 路由项缺少必填字段、`method` 或 `path` 非法、`pathMode` 取值不是 `"exact"`/`"prefix"`、`prefix` 路径未以 `/` 结尾、`requestBody` 用于非 `POST` 路由、`requestBodyMode` 未与 `POST` 路由的显式 `requestBody` 一起出现或取值不是 `"exact"`/`"subset"`、`status` 或 `delayMs` 非法、规则重复
- 端口被占用
