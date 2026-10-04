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
- `pathMode`：可选，取值为区分大小写的字符串 `"exact"` 或 `"prefix"`，省略或取 `"exact"` 时保持完整路径相等匹配；取 `"prefix"` 时把 `path` 作为前缀，且 `path` 除满足上述限制外还必须以 `/` 结尾（根路径 `/` 也允许）。请求路径以前缀开头、且前缀之后的剩余部分非空才算前缀命中，剩余部分可以包含多级路径。其他取值（含 `"Exact"`、`"PREFIX"`、`null`、数字等），或 `prefix` 路径未以 `/` 结尾都会使规则加载失败。
- `body`：任意 JSON 值（包括 `null`），命中时作为响应体返回。
- `bodyMode`：可选，取值为区分大小写的字符串 `"fixed"` 或 `"template"`，省略或取 `"fixed"` 时保持固定响应：即使 `body` 的字符串值含 `{{request.path}}` 也原样返回；取 `"template"` 时启用请求路径回显：`body` 仍为必填的任意 JSON 值（包括 `null`），命中后只把字符串值中的 `{{request.path}}` 替换为本次用于匹配的请求路径，再序列化为响应。顶层及嵌套对象、数组中的字符串值都会替换；对象键、非字符串值（`null`、布尔、数字）与 JSON 结构保持不变。同一字符串中的多个、相邻或被其他文本包围的占位符都会替换；带空格（`{{ request.path }}`）、大小写不同（`{{Request.Path}}`）的写法以及其他占位符（`{{x}}`）保持原样，不执行表达式，也不会再次处理替换结果。其他取值（含 `"Fixed"`、`"Template"`、`null`、数字等）都会使规则加载失败。用于替换的路径已去除查询串，保留大小写、尾部斜杠与百分号转义（不额外解码或规范化），例如前缀规则 `{"method":"GET","path":"/echo/","pathMode":"prefix","bodyMode":"template","body":"{{request.path}}"}` 下，`GET /echo/a%20b?x=1` 返回 JSON 字符串 `"/echo/a%20b"`；每次请求都回显当前路径，`Content-Length` 按替换后的 UTF-8 JSON 字节数给出。模板只作用于选中路由，不影响 exact 优先、最长前缀的匹配规则。
- `requestBody`：可选，仅允许用于 `POST` 路由。值为任意 JSON 值（包括 `null`），作为完整请求正文的 JSON 样例：命中该 POST 路由时，请求正文必须按 UTF-8（与 `Content-Type` 无关）解析为与样例递归相等的 JSON 值才返回配置的响应。对象须有相同的键集合（键序与排版空白不影响），数组的长度和顺序参与比较，字符串区分大小写，数字按数值比较（`1` 与 `1.0` 相等），布尔值与数字互不相等；显式 `null` 样例只接受正文为 JSON `null`。样例中的字符串值与对象键沿用 `body` 的 UTF-8 可编码限制。缺省时仍忽略请求正文。
- `requestBodyMode`：可选，仅允许与 `POST` 路由的显式 `requestBody` 一起出现（`requestBody` 为 `null` 也算显式存在）。取值为区分大小写的字符串 `"exact"` 或 `"subset"`，省略或取 `"exact"` 时保持上述整体相等比较；取 `"subset"` 时启用对象子集匹配：样例对象的所有键都必须存在于请求正文的对应对象中，对应值递归按同一模式比较，请求对象允许额外键（空对象样例匹配任意对象，但也只匹配对象）；数组仍按相同长度和顺序比较（不接受前缀匹配），其中元素为对象时同样允许额外键；其余值沿用整体相等的比较语义。其他取值（含 `"Subset"`、`null`、数字等）、缺少 `requestBody` 或用于非 `POST` 路由都会使规则加载失败。
- `status`：可选，缺省为 `200`；仅接受整数 `200` 或 `400`–`599`（包含两端）。布尔值、`null`、字符串、浮点数（含 `503.0`）、数组、对象均非法。错误状态同样返回配置的 `body`，不替换为统一错误对象。
- `delayMs`：可选，缺省为 `0`；仅接受 `0`–`2000`（包含两端）的整数，单位为毫秒。布尔值、`null`、字符串、浮点数（含 `200.0`）、数组、对象、负数或大于 `2000` 的整数均非法。命中该路由时，请求体读取完毕后先等待至少配置的时长，再发送状态行、响应头与响应体；每次命中（含错误状态码路由、`requestBody` 校验通过的路由）都应用延迟，未命中的 404 不增加人为等待。
- `routes` 可以为空数组；路由项中除 `pathMode`、`bodyMode`、`requestBody`、`requestBodyMode`、`status` 与 `delayMs` 外的额外字段会被忽略。
- 不允许重复的 `method` + `path` 组合，即使两条规则的 `pathMode` 不同也视为重复。

## 请求匹配与响应

- 先按请求方法筛选（`GET`/`POST` 之外的方法直接走 501，不查路由），再在该方法的规则中优先选择完整路径相等的 `exact` 规则；没有 `exact` 命中时，选择 `path` 最长的 `prefix` 候选（请求路径以前缀开头且剩余部分非空），规则在文件中的排列顺序不影响结果。
- 匹配时忽略查询字符串；路径的大小写、尾部斜杠、百分号转义按原样比较，`path` 中的星号没有特殊含义。
- 未配置 `requestBody` 的路由忽略请求体（但仍会读完正文以复用连接）；配置了 `requestBody` 的 `POST` 路由在路由选定后先读完整正文再按样例校验，仅对最终选中的路由校验——校验失败时返回 400，不会再尝试其他 `exact`/`prefix` 候选。
- `GET` / `POST` 命中且（对配置了 `requestBody` 的 POST）正文与样例相等：返回配置的 `status`（缺省 200）及配置的 `body`；`bodyMode` 为 `"template"` 的路由在发送前用本次匹配路径渲染 `body`（仅替换字符串值中的 `{{request.path}}`，查询串已去除，路径保持原样不解码），`bodyMode` 省略或为 `"fixed"` 时返回与规则文件中 `body` 一致的固定 JSON。模板只作用于最终选中的路由：POST 正文校验失败、未命中或方法不受支持时都不会渲染。
- 命中配置了 `requestBody` 的 POST 路由但正文为空、不是合法 UTF-8、JSON 语法错误、含非有限数字（如 `1e400`、`NaN`）或与样例不相等（`subset` 模式下缺键、被约束值不等或类型不符）：返回 HTTP 400 及 `{"error":"request_body_mismatch"}`，不采用配置的状态、正文或延迟。`subset` 模式下完整正文仍须通过全部 UTF-8、JSON 语法与非有限数字检查，额外字段不能绕过检查。
- 未命中：返回 HTTP 404 及 `{"error":"route_not_found"}`（即使正文非法也不校验、不返回 400；即使某条已命中路由自身配置了 404，也按命中处理并返回其 `body`）。仅有前缀规则时，路径与前缀完全相等（剩余部分为空）不算命中，例如前缀 `/api/` 不覆盖请求 `/api/` 或 `/api`。
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

前缀规则（`"pathMode":"prefix"`、`path` 以 `/` 结尾）覆盖同一目录下的接口，且始终让完整路径相等的 `exact` 规则优先。对规则

```json
{"routes":[
  {"method":"GET","path":"/api/","pathMode":"prefix","body":{"v":1}},
  {"method":"GET","path":"/api/v1/","pathMode":"prefix","body":{"v":2}},
  {"method":"GET","path":"/api/v1/ping","body":{"v":3}}
]}
```

`GET /api/x` 返回 `{"v":1}`，`GET /api/v1/x` 返回 `{"v":2}`（多个前缀候选时选 `path` 最长的），`GET /api/v1/ping?x=1` 返回 `{"v":3}`（查询串被忽略，exact 优先于前缀）；`GET /api`、`GET /api/`（剩余部分为空）与 `POST /api/x`（方法不同）均返回 404。

路径回显规则（`"bodyMode":"template"`）在每次命中时把字符串值中的 `{{request.path}}` 替换为本次匹配路径。对规则

```json
{"routes":[
  {"method":"GET","path":"/echo/","pathMode":"prefix","bodyMode":"template",
   "body":{"path":"{{request.path}}","who":["{{request.path}}",1]}}
]}
```

`GET /echo/a%20b?x=1` 返回 `{"path":"/echo/a%20b","who":["/echo/a%20b",1]}`：查询串被去除，`%20` 保持转义不解码；再请求 `GET /echo/x/y/` 则回显 `{"path":"/echo/x/y/","who":["/echo/x/y/",1]}`。对象键与非字符串值不变，`Content-Length` 按替换后的 UTF-8 JSON 字节数给出。

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
- 路由项缺少必填字段、`method` 或 `path` 非法、`pathMode` 取值不是区分大小写的 `"exact"`/`"prefix"`、`prefix` 路径未以 `/` 结尾、`bodyMode` 取值不是区分大小写的 `"fixed"`/`"template"`、`requestBody` 用于非 `POST` 路由、`requestBodyMode` 未与 `POST` 路由的显式 `requestBody` 一起出现或取值不是 `"exact"`/`"subset"`、`status` 或 `delayMs` 非法、规则重复
- 端口被占用
