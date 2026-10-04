# POST 路由命中与请求正文校验流程

本文专门解释一件事：一条 `POST` 请求是**先命中路由**、**再通过（或不通过）请求正文校验**的，两者是什么关系，失败后会不会回头改选别的路由。

文中每条公开规则都标注了对应的源码文件与函数，并给出 `tests/test_mock_server.py` 中锁定该行为的现有用例。所有示例共用第 1 节那份可直接加载的规则文件，请求数据与响应均已按文档原样实测，无需补全规则或猜测预期。

- 产品代码：`mock_server/__init__.py`
- 命令行入口：`mock_server/__main__.py`（`python -m mock_server`）
- 测试：`tests/test_mock_server.py`

下文行号均对应当前版本，函数名是长期稳定的核对锚点；即使行号有漂移，按函数名查找即可。

## 1. 完整规则文件

把下面内容原样保存为 `flow-rules.json`（只配置两个 POST 路由）：

```json
{
  "routes": [
    {
      "method": "POST",
      "path": "/api/",
      "pathMode": "prefix",
      "body": {"via": "prefix"}
    },
    {
      "method": "POST",
      "path": "/api/check",
      "requestBodyMode": "subset",
      "requestBody": {"user": {"id": 1}, "tags": ["a"]},
      "status": 503,
      "delayMs": 200,
      "body": {"accepted": true}
    }
  ]
}
```

两条路由的含义：

| 路由 | 匹配方式 | requestBody | 配置响应 |
| --- | --- | --- | --- |
| `POST /api/` | `pathMode: "prefix"`，前缀之后必须有**非空**剩余 | 不配置（忽略正文，但仍读完正文） | `200`、`{"via":"prefix"}` |
| `POST /api/check` | 精确路径（省略 `pathMode` 即 `exact`） | `requestBodyMode: "subset"`，样例 `{"user":{"id":1},"tags":["a"]}` | `503`、延迟 200ms、`{"accepted":true}` |

启动方式沿用 README：在项目根目录执行

```bash
python -m mock_server --rules flow-rules.json --port 8765
```

（环境中命令名为 `python3` 时用 `python3 -m mock_server ...`，完全等价。）看到 `mock_server listening on http://127.0.0.1:8765 (2 route(s))` 后即可用 curl 核对下文示例；`Ctrl+C` 停止。

也可以先不启动服务，只用与启动完全一致的标准校验规则文件：

```bash
python -m mock_server --check-rules --rules flow-rules.json
# 输出：mock_server rules valid (2 route(s))
```

## 2. 一句话结论

**先按方法与路径选出唯一一条路由，再只对这条被选中的路由做 `requestBody` 校验。** 正文不匹配时返回固定的 `400 {"error":"request_body_mismatch"}`，**不会**回退去尝试其他 exact/prefix 候选，也**不会**采用选中路由配置的状态码、响应体或延迟。未命中任何路由时根本不解析正文，直接 `404`。

这条结论在 `mock_server/__init__.py` 的 `MockHandler._respond`（第 623–674 行）中：第 631 行 `_resolve(path)` 先选出 `key`，第 632–635 行未命中即发 404 返回，第 637–645 行只针对选中的 `key` 调用一次 `_body_matches`，失败即发 400 返回——此后代码再没有回到 `_resolve` 的路径。

## 3. 判断顺序（公开规则 ↔ 源码）

`MockHandler._respond`（第 623–674 行）每次请求的实际顺序：

1. **读取正文原始字节**——`MockHandler._read_body`（第 557–572 行）。按 `Content-Length` 把正文完整读掉（保证 keep-alive 连接可复用），此阶段不解释内容；无 `Content-Length` 视为空正文，读取中断返回 `None`。注意这一步对所有请求都发生，包括最终 404 的请求。
2. **去掉查询串再选路由**——第 628–631 行：`urlsplit(self.path)` 取出不含 `?x=1` 的路径，`MockHandler._resolve`（第 597–621 行）选路。
3. **未命中即 404**——第 632–635 行：`_resolve` 返回 `None` 时直接发送 `NOT_FOUND_BODY`（第 13 行，`{"error":"route_not_found"}`），已读到的正文字节被丢弃，不做任何解析或校验。
4. **命中后决定是否校验正文**——第 637 行：只有选中的路由键存在于 `routes.request_bodies` 中（即该 POST 路由显式配置了 `requestBody`）才调用 `MockHandler._body_matches`（第 574–595 行）；否则正文被忽略。
5. **正文四道检查**（全部在 `_body_matches` 内，任一不过即“不匹配”，且不区分失败原因）：
   1. 原始字节是否存在（`None` 直接不匹配，第 579–580 行）；
   2. **按 UTF-8 解码**——`raw.decode("utf-8")`，第 581–584 行，与 `Content-Type` 无关；
   3. **解析 JSON**——`json.loads(..., parse_constant=_reject_constant)`，第 585–588 行；`_reject_constant`（第 118–121 行）把 `NaN`/`Infinity`/`-Infinity` 这三个非标准字面量一律拒掉；
   4. **递归检查非有限数字**——`_ensure_finite_numbers`（第 124–139 行），第 589–592 行；`1e400` 这种语法合法但溢出为 `inf` 的数字在此被拒，且检查覆盖整份文档，包括 subset 下“会被忽略”的额外字段；
   5. **递归比较**——第 593–595 行按模式分发：`subset` 走 `_json_subset`，其余走 `_json_equal`；两者都只是 `_json_matches`（第 142–197 行）的薄封装（第 200–216 行）。
6. **不匹配：立即 400**——第 644 行发送 `REQUEST_BODY_MISMATCH_BODY`（第 15 行，`{"error":"request_body_mismatch"}`）后返回；第 648–650 行的 `time.sleep(delayMs / 1000)` 在它之后，故 400 不等待。
7. **通过（或该路由本就无样例）：先延迟、再回配置响应**——第 648–674 行：先应用 `delayMs`，再发送配置的 `status` 与 `body`。

流程图：

```text
读取正文字节 _read_body
        │
urlsplit 去掉查询串，_resolve(path)  ← exact 完整路径相等优先；
        │                              否则选 path 最长、剩余非空的 prefix
        ├─ 未命中 ───────────────► 404 {"error":"route_not_found"}（不解析正文）
        │
   命中唯一路由 key
        │
   该路由配置了 requestBody？
        │否                     ┐
        │                        ├─► 等待 delayMs（若有）► 配置 status + body
        │是：_body_matches       │
        │  UTF-8 → JSON →       │
        │  非有限数字 → 递归比较 ┘
        └─ 任一不过 ──────────► 400 {"error":"request_body_mismatch"}
                                  （不回退候选、不用 status/body、不延迟）
```

路由选择本身的规则（`_resolve`，第 597–621 行）：

- 查询串不参与：`_resolve` 收到的 `path` 已由 `urlsplit` 去掉查询串；
- exact 优先：第 608–610 行先查 `(method, path)` 是否为 `exact` 规则，命中即定，不再看前缀；
- prefix 候选条件：第 616 行 `path.startswith(route_path) and len(path) > len(route_path)`——必须以前缀开头**且前缀之后剩余非空**；多个候选取 `path` 最长者（第 617–618 行），与规则排列顺序无关。

## 4. 主示例：命中精确路由、subset 校验通过

### 4.1 请求

```bash
curl -s -i -X POST "http://127.0.0.1:8765/api/check?x=1" \
  --data-binary '{"user":{"id":1.0,"name":"A"},"tags":["a"]}'
```

- 请求行：`POST /api/check?x=1`
- 请求正文（逐字节）：`{"user":{"id":1.0,"name":"A"},"tags":["a"]}`

### 4.2 确定响应

```http
HTTP/1.1 503 Service Unavailable
Content-Type: application/json; charset=utf-8
Content-Length: 17

{"accepted":true}
```

响应在正文读完后**至少等待 200ms** 才发出（实测约 210ms 上下，取决于调度）。

### 4.3 为什么命中的是精确路由而不是前缀路由

1. `urlsplit` 去掉查询串后用于选路的路径是 `/api/check`，`?x=1` 完全不参与匹配（`_respond` 第 628–631 行；`_resolve` 第 603 行注释）。
2. `_resolve` 第 608–610 行先查 exact：键 `("POST", "/api/check")` 恰好存在且其 `pathMode` 为 `exact`，**当场选定并返回**。
3. `/api/` 前缀其实也是候选（`/api/check` 以 `/api/` 开头、剩余文本 `check` 非空），但只有在没有 exact 命中时才会进入前缀比较（第 611–621 行），所以它不会被选中。

### 4.4 为什么 subset 校验通过

进入 `_body_matches`：UTF-8 解码成功、JSON 语法合法、全文无 `NaN`/`Infinity` 与溢出数字，随后 `_json_subset`（即 `_json_matches(..., allow_extra_keys=True)`）比较样例 `{"user":{"id":1},"tags":["a"]}` 与实际正文：

- 顶层：样例的两个键 `user`、`tags` 都存在（第 186–188 行只要求 `set(want) <= set(got)`），实际对象没有其他额外键，无所谓；
- `user`：样例键 `id` 存在；`1.0` 与 `1` 按**数值**比较相等（第 166–171 行，`int`/`float` 混合直接 `want != got`）；实际对象多出的 `name` 键在 subset 下允许（第 186–188 行）；
- `tags`：两边都是数组、长度都为 1、第 0 个元素都是区分大小写的字符串 `"a"`（第 176–181 行要求长度相等并按下标压栈，第 172–175 行字符串严格相等）。

通过后执行第 648–674 行：先 `time.sleep(0.2)`，再发送该路由配置的 `503` 与 `{"accepted":true}`，与正文固定响应（非 template）一致。

## 5. id 改成 true：400，不回退、不采用配置

### 5.1 请求

```bash
curl -s -i -X POST "http://127.0.0.1:8765/api/check?x=1" \
  --data-binary '{"user":{"id":true},"tags":["a"]}'
```

### 5.2 确定响应

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json; charset=utf-8
Content-Length: 33

{"error":"request_body_mismatch"}
```

响应**立即**返回（实测约 10ms，200ms 延迟未应用）。

### 5.3 依据

- 路由选择与 4.3 完全相同：命中的仍是 exact 路由 `/api/check`，与正文内容无关。
- 前四道检查都过（`true` 是合法 JSON 布尔、有限值），递归比较到 `id` 时：`_json_matches` 第 162–165 行在数字比较**之前**先处理布尔——样例侧是 `int` 的 `1`、实际侧是 `bool` 的 `True`，`type(want) is not type(got)`，返回不匹配。Python 中 `bool` 是 `int` 的子类，因此必须用这段显式分支把布尔与数字区分开。
- `_body_matches` 返回假值后，`_respond` 第 644 行直接发 400 返回：不执行第 648–650 行的延迟，不使用 `503` 与 `{"accepted":true}`，**也不会回头让 `_resolve` 改选前缀 `/api/`**——所以绝不会出现 `200 {"via":"prefix"}`。是否回退与前缀路由“不校验正文”无关：被选中的精确路由一旦校验失败，请求即终结。

## 6. 五类输入得到同一个 400 正文

下列请求都命中 exact 路由 `/api/check`（路径、方法不变），都在 `_body_matches` 的某道检查上失败，因而都得到第 5.2 节那份完全相同的响应：

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json; charset=utf-8
Content-Length: 33

{"error":"request_body_mismatch"}
```

`_body_matches`（第 574–595 行）对任何失败原因都只返回 `False`，不带原因；`_respond` 也只有这一种 400 正文，所以错误正文与失败阶段无关。

### 6.1 空正文 —— JSON 解析阶段失败

```bash
curl -s -i -X POST "http://127.0.0.1:8765/api/check" --data-binary ''
```

`--data-binary ''` 发送 `Content-Length: 0`。`_read_body` 返回 `b""`（不是 `None`），UTF-8 解码空串成功，但 `json.loads("")` 抛 `ValueError`，第 585–588 行捕获判为不匹配。（完全不带 `Content-Length` 也视为空正文，结论相同。）

### 6.2 JSON 语法错误 —— JSON 解析阶段失败

```bash
curl -s -i -X POST "http://127.0.0.1:8765/api/check" \
  --data-binary '{"user":{"id":1},"tags":["a"],'
```

对象内部以多余逗号结尾，`json.loads` 抛 `JSONDecodeError`（`ValueError` 子类），第 585–588 行判为不匹配。

### 6.3 非法 UTF-8 —— 解码阶段失败

```bash
printf '\xff\xfe' > bad-utf8.bin
curl -s -i -X POST "http://127.0.0.1:8765/api/check" --data-binary @bad-utf8.bin
```

请求正文为两个原始字节 `0xff 0xfe`，第 582 行 `raw.decode("utf-8")` 抛 `UnicodeDecodeError`，第 583–584 行判为不匹配（不看 `Content-Type`）。

### 6.4 tags 数组增加元素 —— 递归比较阶段失败

```bash
curl -s -i -X POST "http://127.0.0.1:8765/api/check" \
  --data-binary '{"user":{"id":1},"tags":["a","b"]}'
```

UTF-8、JSON、非有限数字三关都过；到 `_json_matches` 第 176–181 行：样例数组长度 1、实际数组长度 2，`len(want) != len(got)` 直接不匹配——**subset 放宽的只是对象的额外键，数组既不允许变长/变短，也不接受前缀匹配**，顺序同样参与比较。

### 6.5 额外字段含 1e400 —— 非有限数字检查阶段失败

```bash
curl -s -i -X POST "http://127.0.0.1:8765/api/check" \
  --data-binary '{"user":{"id":1},"tags":["a"],"extra":1e400}'
```

样例约束的两个键值本身全部满足；但 `1e400` 是语法合法的 JSON 数字，`json.loads` 把它解析成浮点 `inf`（`parse_constant` 拦不住它，因为它不是 `NaN`/`Infinity` 字面量）。`_ensure_finite_numbers`（第 124–139 行）用显式栈递归遍历**整份文档**，在额外键 `extra` 中发现非有限值即抛错，第 589–592 行判为不匹配。也就是说 subset 下额外字段虽然不参与键集合比较，却**不能绕过**任何整文档检查。

> 同类输入：额外字段写成 `NaN`/`Infinity` 会在更早的 JSON 解析阶段被 `_reject_constant` 拒绝，响应正文相同。

## 7. 对照：未命中路由时，错误正文得到 404 而不是 400

```bash
curl -s -i -X POST "http://127.0.0.1:8765/missing" --data-binary '{'
```

确定响应：

```http
HTTP/1.1 404 Not Found
Content-Type: application/json; charset=utf-8
Content-Length: 27

{"error":"route_not_found"}
```

`/missing` 不与任何 exact 路径相等，也不在 `/api/` 前缀之下，`_resolve` 返回 `None`；`_respond` 第 632–635 行直接发送 404，`_body_matches` 根本不会被调用——所以正文 `{` 虽是 JSON 语法错误，也不会变成 400，已读入的正文字节被直接丢弃。这与第 6 节的差别仅在于“先选中的路由是否存在”：校验是命中之后才发生的事。

附带一个前缀边界对照：`POST /api/`（前缀剩余为空）对前缀 `/api/` 不构成命中（`_resolve` 第 616 行要求剩余非空），本规则又没有根前缀 `/`，因此同样返回 404 `{"error":"route_not_found"}`：

```bash
curl -s -i -X POST "http://127.0.0.1:8765/api/" --data-binary 'x'
```

## 8. subset 递归比较语义汇总

全部位于 `_json_matches`（第 142–197 行），exact 与 subset 共用同一套递归流程，唯一差异是对象键集合的处理：

| 要点 | 行为 | 源码位置 |
| --- | --- | --- |
| 对象额外键 | subset 允许：只要求样例键都在实际对象中（`set(want) <= set(got)`），嵌套对象、数组元素中的对象同理；exact 要求键集合完全相同；键的书写顺序与 JSON 空白不影响 | 第 182–193 行；封装 `_json_equal` 第 200–205 行、`_json_subset` 第 208–216 行 |
| 空对象样例 | subset 下 `{}` 匹配任意对象，但也**只匹配对象**（`[]`、`null` 不行，第 183 行先要求实际侧是 dict） | 第 182–188 行 |
| 数字 | 按数值比较，`1` 与 `1.0` 相等；`int`/`float` 同属数字分支 | 第 166–171 行 |
| 布尔与数字 | 互不相等：`true` ≠ `1`、`false` ≠ `0`；布尔分支先于数字分支，刻意避开 `bool` 是 `int` 子类的陷阱 | 第 162–165 行 |
| 字符串 | 类型必须就是 `str` 且区分大小写 | 第 172–175 行 |
| null | 只等于 `null`；`null` 不等于 `0`、`false`、`""` | 第 157–160 行 |
| 数组 | 长度必须相等、逐元素按下标顺序比较；不接受前缀/子序列匹配；元素为对象时在 subset 下仍允许额外键 | 第 176–181 行 |
| 类型不同 | 一律不匹配（如 `"1"` ≠ `1`、`"a"` ≠ `["a"]`、对象 ≠ 数组） | 各类型分支 |

## 9. 响应汇总

| # | 请求（方法 路径） | 请求正文 | 状态码 | 响应正文 | 延迟 |
| --- | --- | --- | --- | --- | --- |
| 4 | `POST /api/check?x=1` | `{"user":{"id":1.0,"name":"A"},"tags":["a"]}` | 503 | `{"accepted":true}` | 200ms |
| 5 | `POST /api/check?x=1` | `{"user":{"id":true},"tags":["a"]}` | 400 | `{"error":"request_body_mismatch"}` | 无 |
| 6.1 | `POST /api/check` | 空 | 400 | `{"error":"request_body_mismatch"}` | 无 |
| 6.2 | `POST /api/check` | `{"user":{"id":1},"tags":["a"],` | 400 | 同上 | 无 |
| 6.3 | `POST /api/check` | 字节 `ff fe`（非法 UTF-8） | 400 | 同上 | 无 |
| 6.4 | `POST /api/check` | `{"user":{"id":1},"tags":["a","b"]}` | 400 | 同上 | 无 |
| 6.5 | `POST /api/check` | `{"user":{"id":1},"tags":["a"],"extra":1e400}` | 400 | 同上 | 无 |
| 7 | `POST /missing` | `{` | 404 | `{"error":"route_not_found"}` | 无 |
| 7 对照 | `POST /api/` | `x` | 404 | `{"error":"route_not_found"}` | 无 |
| 前缀放行 | `POST /api/other` | 任意（如 `anything`） | 200 | `{"via":"prefix"}` | 无 |

所有响应均带 `Content-Type: application/json; charset=utf-8`，`Content-Length` 为响应正文的 UTF-8 字节数：33（request_body_mismatch）、17（accepted）、27（route_not_found）、16（via prefix）。

## 10. 现有测试佐证

均可在项目根目录用 README 的方式运行：`python3 -m unittest discover -s tests`。

| 结论 | 测试位置（`tests/test_mock_server.py`） |
| --- | --- |
| 验收正文（id 为 1.0、user 带额外键）subset 放行，返回配置的 503 与正文 | `SubsetRequestBodyTests.test_task_acceptance_body_with_extra_keys_returns_configured_503`（第 3615 行） |
| id 为 `true`、tags 多一个元素 → 400 | `SubsetRequestBodyTests.test_task_acceptance_rejections`（第 3625 行） |
| subset 放行/拒绝的完整样例矩阵（额外键、缺键、类型不符、数组前缀等） | `SubsetRequestBodyTests.test_subset_matching_and_mismatching_bodies`（第 3634 行） |
| 额外字段中的 `NaN`、`1e400`、非法 UTF-8、语法错误、空正文一律 400 | `SubsetRequestBodyTests.test_full_body_checks_still_apply_to_extra_fields`（第 3717 行）；通用畸形正文清单 `MALFORMED_BODIES`（第 2863 行）与 `RequestBodyBehaviorTests.test_malformed_bodies_return_400`（第 2980 行） |
| 校验失败不等待 200ms、不使用 503 与配置正文；通过时延迟并返回配置响应 | `SubsetRequestBodyTests.test_mismatch_skips_configured_status_body_and_delay`（第 3731 行）、`RequestBodyBehaviorTests.test_mismatch_does_not_apply_configured_status_body_or_delay`（第 3006 行）与 `test_match_still_applies_delay_and_configured_response`（第 3025 行） |
| 未命中（`POST /missing`）即使正文非法也返回 404，不做正文校验 | `RequestBodyBehaviorTests.test_unmatched_route_with_invalid_body_returns_404`（第 3056 行） |
| 选中的前缀路由校验失败不回退根前缀；exact 路由只按自己的样例校验、失败不回退前缀 | `PrefixRequestBodyTests.test_mismatching_body_returns_400_without_fallback`（第 4621 行）、`test_exact_route_validated_with_its_own_sample`（第 4650 行） |
| 多候选竞争时只校验最终选中路由、规则逆序结果不变 | `SelectedRouteOnlyValidationTests`（第 4727 行） |
| exact/subset 两模式下数组长度与顺序参与比较、畸形正文同拒、额外字段不能绕过整文档检查 | `RequestBodyComparisonRefactorTests.test_array_length_and_order_rejected_in_both_modes`（第 4168 行）、`test_malformed_bodies_rejected_in_both_modes`（第 4195 行）、`test_subset_extra_fields_cannot_bypass_full_body_checks`（第 4207 行） |
| `_json_subset` 的标量/数组/嵌套语义（1 与 1.0、布尔与数字、数组顺序等） | `JsonSubsetHelperTests.test_subset_semantics`（第 3773 行） |
| 空正文在无 `Content-Length` 时同样判 400 | `RequestBodyBehaviorTests.test_no_content_length_with_empty_body_is_mismatch`（第 3097 行） |
| 正文校验与 `Content-Type` 无关（一律按 UTF-8 JSON） | `RequestBodyBehaviorTests.test_content_type_does_not_affect_validation`（第 3078 行） |
