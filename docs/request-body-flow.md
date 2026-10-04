# 路由命中与请求正文校验流程

本文用一份只含两个 POST 路由的完整规则，串起“先选定路由、再只对选中路由做正文校验”的实际判断顺序，并把每条公开规则对应到源码函数与现有测试用例。读者按 README 的方式启动服务后，可逐条复现本文的全部请求与确定响应，无需补全任何配置。

- 产品代码：[`mock_server/__init__.py`](../mock_server/__init__.py)
- 测试用例：[`tests/test_mock_server.py`](../tests/test_mock_server.py)

## 1. 可直接加载的完整规则

把以下内容保存为 `rules-flow.json`（UTF-8、两个 POST 路由，可直接被 `--rules` 加载，已通过 `--check-rules` 校验）：

```json
{"routes":[{"method":"POST","path":"/api/","pathMode":"prefix","body":{"via":"prefix"}},{"method":"POST","path":"/api/check","requestBodyMode":"subset","requestBody":{"user":{"id":1},"tags":["a"]},"status":503,"delayMs":200,"body":{"accepted":true}}]}
```

两条路由分别是：

| 方法 | path | pathMode | requestBody | status | delayMs | 响应 body |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/api/` | `prefix` | 不配置（忽略正文） | 缺省 `200` | 缺省 `0` | `{"via":"prefix"}` |
| POST | `/api/check` | 缺省 `exact` | `subset`，样例 `{"user":{"id":1},"tags":["a"]}` | `503` | `200` | `{"accepted":true}` |

加载侧的约束由 `_check_route_options`（`mock_server/__init__.py`）保证：`prefix` 的 `path` 必须以 `/` 结尾（`/api/` 满足）；`requestBodyMode` 只能与 POST 路由上显式的 `requestBody` 一起出现，取值区分大小写。

## 2. 启动方式

沿用 README 的启动方式，在项目根目录执行：

```bash
python3 -m mock_server --rules rules-flow.json --port 8765
```

看到 `mock_server listening on http://127.0.0.1:8765 (2 route(s))` 后即可用 curl 发请求。本文示例中的 `curl` 均不设置 `Content-Type`——正文一律按 UTF-8 JSON 解析，与该头无关（见 `RequestBodyBehaviorTests.test_content_type_does_not_affect_validation`）。

## 3. 一句话关系与处理时序

**路由选定只看方法和去除查询串后的路径，与正文无关；正文只有在路由已经唯一选定、且该路由显式配置了 `requestBody` 时才会被读取后的字节拿去校验，且只校验这一条路由。校验失败返回固定的 400，不会再尝试任何其他 exact/prefix 候选，也不采用该路由配置的状态、正文与延迟。**

`MockHandler._respond`（`mock_server/__init__.py`）按以下顺序编排，每一步都标注了对应函数：

1. **读正文原始字节**：`MockHandler._read_body` 按 `Content-Length` 把正文完整读成字节（在选路由之前就读完，以复用 keep-alive 连接）；没有 `Content-Length` 视为空正文，长度非法或连接中断返回 `None`。
2. **去掉查询串再选路由**：`urlsplit(self.path)` 得到不含查询串的 `path` 与不参与路由的 `query`，随后 `MockHandler._resolve(path)` 选择路由——先查完整路径相等且 `pathMode` 为 `exact` 的规则；没有 exact 命中时，再在剩余部分非空的前提下选 `path` 最长的 `prefix` 候选。
3. **未命中即 404**：`_resolve` 返回空时，`_respond` 直接发送 `404` 与 `{"error":"route_not_found"}` 并返回，**不对正文做任何解析**。
4. **只对选中路由校验正文**：仅当选中的键存在于 `routes.request_bodies`（即该 POST 路由配置了 `requestBody`）时，才调用 `MockHandler._body_matches(sample, raw, mode)`。前缀路由 `/api/` 没有配置 `requestBody`，这一步整个跳过，正文是什么都不影响它的响应。
5. **`_body_matches` 的内部顺序**（任一环节不通过即返回 `False`）：
	1. `raw is None`（读取失败）→ 不匹配；
	2. `raw.decode("utf-8")`：不是合法 UTF-8 → 不匹配；
	3. `json.loads(text, parse_constant=_reject_constant)`：JSON 语法错误，或出现 `NaN`/`Infinity`/`-Infinity` 非标准字面量（`_reject_constant` 一律拒绝）→ 不匹配；
	4. `_ensure_finite_numbers(data)`：递归检查**整份**文档（含 subset 下不会被比较的额外字段），`1e400` 等溢出被解析为 `inf`/`-inf`，发现第一个非有限数字 → 不匹配；
	5. 通过全部解析检查后才做递归比较：`subset` 调 `_json_subset`，`exact` 调 `_json_equal`，二者共用 `_json_matches`。
6. **校验失败**：`_respond` 立即发送 `400` 与 `{"error":"request_body_mismatch"}` 并返回——不执行 `time.sleep`，不读取配置的 `status`/`body`。
7. **校验通过（或该路由无需校验）**：先 `time.sleep(delayMs/1000)`，再发送配置的状态码与正文。本文两条路由都是缺省的 `fixed` bodyMode，不做模板渲染。

## 4. 主例：命中精确路由、subset 校验通过

请求（注意目标带查询串 `?x=1`，正文把 `id` 写成 `1.0` 并给 `user` 增加了 `name` 键）：

```bash
curl -i -X POST "http://127.0.0.1:8765/api/check?x=1" \
  --data-binary '{"user":{"id":1.0,"name":"A"},"tags":["a"]}'
```

确定响应（稳定部分；`Server`、`Date` 头随环境变化，略）：

```http
HTTP/1.1 503 Service Unavailable
Content-Type: application/json; charset=utf-8
Content-Length: 17

{"accepted":true}
```

响应在请求体读完后**至少等待 200ms** 才发出（`delayMs: 200`）。

为什么是这个结果：

1. **查询串不参与路由**：`urlsplit` 把 `/api/check?x=1` 拆成路径 `/api/check` 与查询串 `x=1`，`_resolve` 只拿 `/api/check` 去匹配（`RequestBodyBehaviorTests.test_query_string_still_ignored_in_matching` 锁定了带查询串仍按路径匹配）。
2. **exact 优先于 prefix**：`(POST, /api/check)` 正是精确路由（未配 `pathMode`，记为 `exact`），`_resolve` 第一步就命中并返回，剩余文本为空。虽然 `/api/check` 在字面上也以前缀 `/api/` 开头，但前缀候选只有在**没有** exact 命中时才会考虑。路由在读取并检查正文之前已经唯一确定。
3. **subset 允许额外键、数字按数值比较**：`_body_matches` 依次通过 UTF-8 解码、JSON 解析、非有限数字检查后，`_json_subset`（即 `_json_matches(..., allow_extra_keys=True)`）比较样例 `{"user":{"id":1},"tags":["a"]}` 与实际正文：
	- 顶层：样例键 `user`、`tags` 都存在，实际多出的键不检查，允许；
	- `user`：样例只约束 `id`，实际多出的 `name:"A"` 允许；`id` 的 `1.0` 与样例的 `1` 数值相等（`1 == 1.0`）；
	- `tags`：`["a"]` 与 `["a"]` 长度相同、顺序一致。
	比较通过（语义直接见 `JsonSubsetHelperTests.test_subset_semantics`，等价的验收正文见 `SubsetRequestBodyTests.test_task_acceptance_body_with_extra_keys_returns_configured_503` 与 `RequestBodyComparisonRefactorTests.test_acceptance_extra_key_distinguishes_modes`）。
4. **通过后才应用延迟与配置响应**：`_respond` 先等待 200ms，再发送配置的 `503` 与 `{"accepted":true}`（`RequestBodyBehaviorTests.test_match_still_applies_delay_and_configured_response` 验证通过后才等待并返回 503）。

## 5. 校验失败：`id` 改成 `true` → 400，不回退前缀

```bash
curl -i -X POST "http://127.0.0.1:8765/api/check?x=1" \
  --data-binary '{"user":{"id":true},"tags":["a"]}'
```

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json; charset=utf-8
Content-Length: 33

{"error":"request_body_mismatch"}
```

依据：

- 路由选择不看正文，`/api/check?x=1` 选中的仍是精确路由 `/api/check`。
- `_json_matches` 在比较数字之前先处理布尔值（Python 中 `bool` 是 `int` 的子类，必须显式区分）：样例 `id` 是数字 `1`，实际是布尔 `true`，类型不同即不匹配（`JsonSubsetHelperTests.test_subset_semantics` 中的 `({"a":1},{"a":True})` 与 `SubsetRequestBodyTests.test_task_acceptance_rejections` 的 “id 为布尔 true” 用例）。
- `_body_matches` 返回 `False`，`_respond` 直接发送 400：**不会**回退到前缀路由 `/api/`（那条路由不校验正文、本会返回 200 `{"via":"prefix"}`，但根本不会被尝试），也**不会**采用精确路由配置的 503、`{"accepted":true}` 与 200ms 延迟（立即返回，无等待）。不回退这一点由 `PrefixRequestBodyTests.test_mismatching_body_returns_400_without_fallback`、`PrefixRequestBodyTests.test_exact_route_validated_with_its_own_sample` 与 `SelectedRouteOnlyValidationTests`（`ROUTE_COMPETITION_CASES` 中 “精确路径按自身样例校验失败：返回 400，不回退前缀”）锁定；不用配置状态/正文/延迟由 `SubsetRequestBodyTests.test_mismatch_skips_configured_status_body_and_delay` 锁定。

## 6. 五种输入得到同一份 400 错误正文

下列请求都发往已经命中的精确路由 `POST /api/check`。它们在 `_body_matches` 的不同环节失败，但出口完全相同：HTTP 400、`Content-Type: application/json; charset=utf-8`、`Content-Length: 33`、响应体 `{"error":"request_body_mismatch"}`，且都不等待、不用配置响应。

| # | 输入（完整请求数据） | curl 写法 | 失败环节（函数） |
| --- | --- | --- | --- |
| 1 | 空正文（0 字节） | `curl -i -X POST http://127.0.0.1:8765/api/check --data-binary ''` | `json.loads("")` 语法错误（读取中断得到 `None` 时则在第一步即判否，同一出口） |
| 2 | JSON 语法错误 `{` | `curl -i -X POST http://127.0.0.1:8765/api/check --data-binary '{'` | `json.loads` 抛 `ValueError` |
| 3 | 非法 UTF-8 字节 `FF FE` | `printf '\xff\xfe' \| curl -i -X POST http://127.0.0.1:8765/api/check --data-binary @-` | `raw.decode("utf-8")` 抛 `UnicodeDecodeError` |
| 4 | `tags` 多一个元素：`{"user":{"id":1},"tags":["a","b"]}` | `curl -i -X POST http://127.0.0.1:8765/api/check --data-binary '{"user":{"id":1},"tags":["a","b"]}'` | `_json_matches` 数组分支：长度 1≠2，subset 也不接受数组前缀匹配 |
| 5 | 额外字段含 `1e400`：`{"user":{"id":1},"tags":["a"],"extra":1e400}` | `curl -i -X POST http://127.0.0.1:8765/api/check --data-binary '{"user":{"id":1},"tags":["a"],"extra":1e400}'` | `json.loads` 把 `1e400` 解析为 `inf`，`_ensure_finite_numbers` 在比较前递归整个文档（含额外键 `extra`）时拒绝 |

五种情况的响应均为：

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json; charset=utf-8
Content-Length: 33

{"error":"request_body_mismatch"}
```

测试佐证：

- 空正文、非法 UTF-8、`{`、被截断的 JSON、裸文本、`NaN`、`Infinity`、`1e400` 统一 400：`RequestBodyBehaviorTests.test_malformed_bodies_return_400` 与常量列表 `MALFORMED_BODIES`；
- subset 下额外字段中的 `NaN`、`1e400`、非法 UTF-8 与语法错误不能因为“是额外键”而绕过检查，空正文同样 400：`SubsetRequestBodyTests.test_full_body_checks_still_apply_to_extra_fields`；
- `tags` 多元素（数组长度参与比较）：`SubsetRequestBodyTests.test_task_acceptance_rejections`（“tags 多一个元素”）与 `test_subset_matching_and_mismatching_bodies`；
- 两种模式共用同一套失败出口与“不等待、不用 503/正文”行为：`RequestBodyComparisonRefactorTests.test_malformed_bodies_rejected_in_both_modes`、`test_mismatch_ignores_configured_status_body_and_delay`。

## 7. 对照：未命中时连正文都不校验（错误 JSON 仍返回 404）

```bash
curl -i -X POST http://127.0.0.1:8765/missing --data-binary '{'
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json; charset=utf-8
Content-Length: 27

{"error":"route_not_found"}
```

`/missing` 既不等于任何 exact 路径，也不以 `/api/` 开头，`_resolve` 在 `_respond` 的第 3 步就返回空；函数随即发送 404 并返回，**根本不会调用 `_body_matches`**。所以同样是语法错误的正文 `{`，在 `/api/check` 上是 400（路由命中、校验失败），在 `/missing` 上是 404（路由未命中、正文不被检查）。这由 `RequestBodyBehaviorTests.test_unmatched_route_with_invalid_body_returns_404`（对 `/missing` 发送非法 UTF-8、`{` 等均返回 `route_not_found`）锁定。

一个相关的侧证——把第 6 节的错误正文发给命中前缀路由但未配置 `requestBody` 的路径：

```bash
curl -i -X POST http://127.0.0.1:8765/api/other --data-binary '{'
```

```http
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 16

{"via":"prefix"}
```

`/api/other` 去掉前缀 `/api/` 后剩余 `other` 非空，命中前缀路由；该路由没有配置 `requestBody`，`_respond` 跳过正文校验，错误正文被忽略，直接返回 200 与配置正文。这说明 400 只可能来自“被选中路由自身的 requestBody 校验”，而不是正文本身的全局合法性检查。

## 8. subset 递归比较的四条语义

均由共享引擎 `_json_matches`（`_json_subset` 传 `allow_extra_keys=True`，`_json_equal` 传 `False`）实现，直接对照 `JsonSubsetHelperTests.test_subset_semantics` 与 `JsonEqualityHelperTests.test_comparison_semantics`：

1. **对象额外键只在 subset 中允许**：subset 只要求样例的键全部存在于实际对象中，实际对象可以多出任意键（嵌套对象、数组里的对象也一样）；exact 要求键集合完全相同。键的书写顺序与 JSON 排版空白不影响结果。空对象样例 `{}` 在 subset 下匹配任意对象，但不匹配数组或标量。
2. **数字按数值比较，`1` 与 `1.0` 相等**：整数与浮点数不区分类别，`1 == 1.0`、`0 == 0.0`、`1e0 == 1` 均视为相等（主例的 `id: 1.0` 因此通过）。
3. **布尔值与数字互不相等**：`true` 不等于 `1`，`false` 不等于 `0`。因为 Python 的 `bool` 是 `int` 子类，`_json_matches` 把布尔判断放在数字判断之前，先按类型区分（第 5 节的 `id: true` 因此失败）。
4. **数组的长度和顺序都参与比较**：subset 也不接受数组前缀匹配，多元素、少元素、同长度但顺序不同均失败；逐元素再递归套用同一规则（第 6 节 `tags` 增加元素因此失败）。

此外：字符串区分大小写；`null` 只等于 `null`；类型不符（如数字对字符串、对象对数组）即不匹配。

## 9. 源码与测试索引

关键结论到源码函数（均在 `mock_server/__init__.py`）：

| 结论 | 函数 |
| --- | --- |
| 读正文 → 去查询串选路由 → 未命中 404 → 仅校验选中路由 → 失败 400 / 通过后延迟并响应的总编排 | `MockHandler._respond` |
| 按 `Content-Length` 读取原始字节，空正文与读取失败的处理 | `MockHandler._read_body` |
| exact 优先、其次最长 prefix（剩余非空）、忽略查询串 | `MockHandler._resolve` |
| 正文校验顺序：读取失败 → UTF-8 → JSON（拒 `NaN`/`Infinity`）→ 非有限数字 → 递归比较 | `MockHandler._body_matches` |
| 拒绝 `NaN`/`Infinity`/`-Infinity` 非标准字面量 | `_reject_constant` |
| 递归检查整份文档的非有限数字（`1e400` → `inf`），额外字段也不例外 | `_ensure_finite_numbers` |
| exact/subset 共用递归比较：布尔先于数字、数值相等、数组等长同序、subset 允许额外键 | `_json_matches`（封装为 `_json_equal`、`_json_subset`） |
| 固定错误正文常量 | `REQUEST_BODY_MISMATCH_BODY`、`NOT_FOUND_BODY` |
| prefix 路径须以 `/` 结尾、`requestBodyMode` 仅限 POST 显式 `requestBody` | `_check_route_options` |

主要测试佐证（`tests/test_mock_server.py`）：

- `SubsetRequestBodyTests.test_task_acceptance_body_with_extra_keys_returns_configured_503`：额外键 + `1.0` 放行返回配置 503；
- `SubsetRequestBodyTests.test_task_acceptance_rejections`：`id:true` 与 `tags` 多元素均 400；
- `SubsetRequestBodyTests.test_full_body_checks_still_apply_to_extra_fields`：额外字段中的 `NaN`/`1e400`、非法 UTF-8、语法错误、空正文均 400；
- `SubsetRequestBodyTests.test_mismatch_skips_configured_status_body_and_delay`：失败不等待、不用 503 与配置正文，通过才等待并返回；
- `PrefixRequestBodyTests.test_mismatching_body_returns_400_without_fallback` / `test_exact_route_validated_with_its_own_sample`：只校验选中路由、exact 优先、失败不回退前缀；
- `SelectedRouteOnlyValidationTests`（含 `ROUTE_COMPETITION_CASES`，正序与逆序各启一个服务）：精确路径带查询串按自身样例校验失败即 400、额外字段 `1e400` 400、不回退根前缀；
- `RequestBodyBehaviorTests.test_malformed_bodies_return_400`（`MALFORMED_BODIES`）：各类畸形正文统一 400；
- `RequestBodyBehaviorTests.test_unmatched_route_with_invalid_body_returns_404`：`/missing` 携带非法正文仍 404；
- `RequestBodyBehaviorTests.test_query_string_still_ignored_in_matching`：查询串不影响匹配；
- `RequestBodyBehaviorTests.test_match_still_applies_delay_and_configured_response`：通过后才等满 200ms 返回 503；
- `JsonSubsetHelperTests.test_subset_semantics`、`JsonEqualityHelperTests.test_comparison_semantics`：额外键、`1 == 1.0`、布尔≠数字、数组长度与顺序的直接单测。
