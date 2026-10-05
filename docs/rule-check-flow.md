# `--check-rules` 规则预检查流程

本文专门解释一件事：`python -m mock_server --check-rules` 这个入口依据什么判断一份 JSON 规则“可用”，以及它和正常启动在源码的哪里分开、共用什么、各自多做或少做什么。

文中沿公开入口一路讲到进程退出，每个关键结论都标注源码文件与函数名，并给出 `tests/test_mock_server.py` 中用**测试名**定位的现有用例。第 4、6 节的两份示例（输入文件、命令、退出码与两条输出流）均已按文档原样实测（Python 3.14.4、Linux）。

- 产品代码：`mock_server/__init__.py`
- 命令行入口：`mock_server/__main__.py`（`python -m mock_server`）
- 测试：`tests/test_mock_server.py`

下文行号均对应当前版本，函数名是长期稳定的核对锚点；即使行号有漂移，按函数名查找即可。

## 1. 一句话结论

**`--check-rules` 与正常启动走的是同一条规则加载校验（同一个 `load_rules`），区别只在校验通过之后：预检查打印一行成功提示并以退出码 0 退出，正常启动则继续绑定端口、打印监听提示并进入服务循环。** 因此预检查会完成文件读取、JSON 解析、逐路由结构与取值校验、`body` 的 UTF-8 可编码校验以及重复路由判定，但**不渲染模板、不按 `delayMs` 等待、不创建 HTTP 服务、不绑定或探测端口**，也不打印监听或停止提示。规则文件本身或其中路由非法时，两条路径在同一个 `except RulesError` 处以相同的 `error: ...` 文本和退出码 2 结束。

## 2. 公开入口到退出：完整链路

### 2.1 公开入口：`python -m mock_server`（`mock_server/__main__.py`）

`mock_server/__main__.py` 全部内容就是把命令行交给包内的 `main`，并把其返回值作为进程退出码：

```python
from . import main
if __name__ == "__main__":
    sys.exit(main())
```

所以“退出码”就是 `main`（`mock_server/__init__.py` 第 769–824 行）的返回值；`python -m mock_server` 与 `python3 -m mock_server` 等价。

### 2.2 参数解析：`main` 内的 argparse（第 770–789 行）

`main(argv=None)` 用 `argparse.ArgumentParser`（`prog="mock_server"`）声明三个参数：

| 参数 | 定义位置 | 说明 |
| --- | --- | --- |
| `--rules` | 第 774 行 | 必填，规则文件路径 |
| `--port` | 第 775–780 行 | 可选，`type=_port`，默认 `8765`；`_port`（第 81–92 行）要求十进制整数且在 1–65535 |
| `--check-rules` | 第 781–788 行 | `action="store_true"` 的开关；帮助文本即写明 “no port is bound or probed” |

`parser.parse_args(argv)`（第 789 行）先于一切文件操作执行。参数缺失或非法（如 `--port abc`、`--port 0`）时由 argparse **自行**以退出码 2 退出，标准错误形如 `mock_server: error: argument --port: ...`，标准输出为空——注意它带程序名前缀，与规则错误的 `error: ...` 不同。这条分支由 `CheckRulesEntryTests.test_port_non_integer_exits_2`、`test_port_zero_exits_2` 锁定。`--check-rules` 只是布尔开关，不带值；`--port` 即使在预检查时仍会经过 `_port` 的整数与范围校验，但校验完不再使用。

### 2.3 共用的规则加载：`load_rules`（第 556–599 行）

参数解析后，第 791–795 行无条件调用 `load_rules(args.rules)`——**这是正常启动与预检查共用同一份校验的源头**：

```python
try:
    routes = load_rules(args.rules)
except RulesError as exc:
    print(f"error: {exc}", file=sys.stderr)
    return 2
```

`RulesError` 定义于第 77–78 行。`load_rules` 及它调用的函数覆盖以下检查（任一不过即抛 `RulesError`，不区分预检查还是启动）：

1. **文件级检查 `_read_route_items`（第 349–380 行）**：以二进制打开文件（不可读报 `cannot read rules file ...`）；按 UTF-8 解码；`json.loads(..., parse_constant=_reject_constant)` 解析（`_reject_constant` 第 132–135 行拒绝 `NaN`/`Infinity`/`-Infinity`）；`_ensure_finite_numbers`（第 138–153 行）递归拒绝 `1e400` 等溢出为 `inf`/`-inf` 的数字；最后要求顶层是含 `routes` 数组的对象。此阶段不查看单条路由内容。
2. **逐路由身份检查 `_check_route_identity`（第 383–411 行）**：每项必须是对象，且含 `method`、`path`、`body` 三个必填字段；`method` 必须属于 `ALLOWED_METHODS`（仅 `GET`/`POST`，第 12 行）；`path` 必须是以 `/` 开头、不含 `?`/`#` 的字符串。
3. **唯一性判定（`load_rules` 第 583–585 行）**：键是 `key = (method, route_path)`，键已存在即抛 `f"{where}: duplicate route {method} {route_path}"`，其中 `where` 是 `routes[{index}]`（第 580–581 行），即后出现那条路由的下标。判定只看方法与路径文本，**不看 `pathMode`、`body` 等任何其他字段**——同一 `method`+`path` 即使分别配成 `exact` 与 `prefix` 仍是重复（详见第 6 节）。
4. **逐路由选项检查 `_check_route_options`（第 451–553 行）**：`pathMode`（第 459–473 行，取值区分大小写，`prefix` 还要求路径以 `/` 结尾）、`bodyMode`（第 474–481 行）、`requestBody`/`requestBodyMode`（第 482–519 行，只允许 POST 组合）、`status`（第 520–525 行经 `_valid_status` 第 95–102 行）、`delayMs`（第 526–531 行经 `_valid_delay_ms` 第 105–110 行，限 0–2000 的整数）。
5. **响应体预序列化与 UTF-8 可编码校验（第 532–542 行）**：对每条路由的 `body` 执行 `json.dumps(item["body"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")`；字符串值或对象键含未配对代理码点（如孤立 `\ud800`）时抛 `RulesError`，整份规则拒绝加载。**这一校验对 `bodyMode: "template"` 的路由同样执行**——模板在加载期只是普通 JSON 值，序列化通过后原始值才被额外保存在 `routes.template_bodies`（第 549 行、第 591–595 行）；加载期不扫描、不校验占位符写法（第 41–46 行注释明确），更不替换占位符。

### 2.4 分叉点：第 797–801 行

加载成功后，两条路径在 `main` 第 797 行的 `if args.check_rules:` 分开：

```python
if args.check_rules:
    # 仅做与正常启动完全一致的规则加载校验：不模拟请求、不渲染模板、
    # 不按 delayMs 等待，也不绑定或探测端口；校验通过即结束
    print(f"mock_server rules valid ({len(routes)} route(s))")
    return 0
```

- **预检查分支（第 797–801 行）**：向标准输出打印唯一一行 `mock_server rules valid (N route(s))`（`N` 为 `load_rules` 返回的路由数），返回 0。进程在此结束。
- **正常启动分支（第 803–824 行）**：第 804 行 `ThreadingHTTPServer(("127.0.0.1", args.port), _make_handler(routes))` 才真正创建服务并绑定端口；绑定失败（如端口被占用）在第 805–810 行打印 `error: cannot bind 127.0.0.1:<port>: ...` 并返回 2。绑定成功后第 813–816 行打印 `mock_server listening on http://...`，第 818 行 `serve_forever()` 进入循环，`Ctrl+C` 触发 `KeyboardInterrupt` 后经 `finally` 关闭服务（第 819–822 行），最后第 823 行打印 `mock_server stopped` 并返回 0。

流程图：

```text
python -m mock_server                mock_server/__main__.py: sys.exit(main())
   │
   ▼  main（mock_server/__init__.py 第 769 行）
argparse：--rules（必填）、--port（_port 校验）、--check-rules（开关）
   │  参数非法 → argparse 自行 exit(2)，stderr: mock_server: error: argument ...
   ▼
load_rules(args.rules)               第 792 行 —— 正常启动与预检查共用
   ├─ _read_route_items        读文件 / UTF-8 / JSON / 有限数字 / 顶层结构
   ├─ _check_route_identity    必填字段、method、path
   ├─ (method, path) 唯一性     duplicate route → RulesError
   ├─ _check_route_options     pathMode/bodyMode/status/delayMs/requestBody
   └─ body 预序列化 + UTF-8 编码校验
   │  抛 RulesError → stderr 打印 "error: ..."，return 2（第 793–795 行）
   ▼
if args.check_rules:                第 797 行 —— 分叉点
   ├─ 是：stdout 打印 "mock_server rules valid (N route(s))"，return 0
   │       （不绑定端口、不打印 listening/stopped）
   └─ 否：ThreadingHTTPServer 绑定端口（第 804 行）
              ├─ 绑定失败 → "error: cannot bind ..."，return 2
              └─ 打印 listening → serve_forever → Ctrl+C → 打印 stopped，return 0
```

## 3. 预检查“做什么、不做什么”的源码依据

对第 4 节那样的合法模板 + 延迟路由，预检查分支的行为可以逐行落实：

- **完成规则加载**：第 792 行 `load_rules` 完整执行（2.3 节全部检查），返回的 `routes` 随即只用于取 `len(routes)` 拼成功提示。
- **完成正文可编码校验**：`_check_route_options` 第 532–535 行对 `body` 做紧凑 JSON 序列化并 `.encode("utf-8")`，模板路由也不例外；失败走第 536–542 行成为 `RulesError`。
- **不渲染模板**：占位符替换函数 `_render_template`（第 273–346 行）在整个加载阶段没有任何调用点；它只在请求处理函数 `MockHandler._respond` 的模板分支（第 715–726 行）中按**本次请求**调用。预检查不模拟任何请求，也不会执行到 `_make_handler`（第 602 行起，仅正常启动分支第 804 行使用）。加载时模板正文只以原始 JSON 值存入 `routes.template_bodies`（第 591–595 行），连占位符写法都不检查。
- **不等待配置的延迟**：`time.sleep(delay_ms / 1000)` 只出现在 `_respond` 第 697–699 行，即每次请求命中之后；`delayMs: 2000` 在加载期只经过 `_valid_delay_ms` 的取值校验（第 526–531 行），不会被睡眠。
- **不启动 HTTP 服务、不触碰端口**：`ThreadingHTTPServer(...)` 在分叉点之后的第 804 行，预检查分支第 800–801 行已 `return 0`。因此既不 `bind`/`listen`，也不探测端口是否可用；`--port` 仅经 `_port` 做语法与范围校验。
- **不打印监听或停止提示**：`mock_server listening ...`（第 813–816 行）与 `mock_server stopped`（第 823 行）都在正常启动分支，预检查的标准输出只有成功一行。

## 4. 完整正例：合法的单条模板延迟路由

### 4.1 输入

把下面内容原样保存为项目根目录下的 `check-rules.json`（完整 UTF-8 JSON，仅含一条 `GET /echo`：`bodyMode` 为 `template`、`delayMs` 为 2000、`body` 为 `{"path":"{{request.path}}"}`；`status` 与 `pathMode` 省略，分别缺省为 `200` 与 `exact`）：

```json
{
  "routes": [
    {
      "method": "GET",
      "path": "/echo",
      "bodyMode": "template",
      "delayMs": 2000,
      "body": {"path": "{{request.path}}"}
    }
  ]
}
```

### 4.2 命令与预期结果

在**项目根目录**执行：

```bash
python -m mock_server --rules check-rules.json --check-rules --port 8765
```

预期（已实测）：

| 项目 | 预期 |
| --- | --- |
| 退出码 | `0` |
| 标准输出 | 恰好一行 `mock_server rules valid (1 route(s))` **加行尾换行**，共 37 字节，除此之外无任何内容 |
| 标准错误 | 空（0 字节） |

终端实际显示：

```text
mock_server rules valid (1 route(s))
```

没有 `mock_server listening ...`，没有 `mock_server stopped`；进程立即退出（实测约 0.1s，与 2000ms 延迟无关），无需 `Ctrl+C`。

### 4.3 对应分支说明

命中的是 2.4 节的**预检查分支**：参数解析通过 → `load_rules` 成功（`body` 通过第 532–535 行的 UTF-8 序列化校验，`bodyMode: "template"` 与 `delayMs: 2000` 经选项校验，`("GET", "/echo")` 唯一）→ 第 797 行条件为真 → 第 800 行打印成功行 → 第 801 行返回 0。`{{request.path}}` 自始至终只是字符串文本：只有正常启动后真正收到 `GET /echo` 请求时，`_respond`（第 701–726 行）才会把它渲染成 `/echo`（用同一份文件正常启动并 `curl http://127.0.0.1:<port>/echo` 可得到 `{"path":"/echo"}`，本文已实测）。

### 4.4 端口被占用时仍然成功

即使本机 `8765` 已被其他监听器占用，上面的命令结果**完全不变**：退出码 0、同样的一行标准输出、标准错误为空（已用一个占用 `127.0.0.1:8765` 的本地监听器实测，检查进程不触碰该端口，原监听器在检查结束后仍可接受连接）。

原因见 2.4 节：端口绑定只发生在正常启动分支的第 804 行，预检查在第 801 行就已退出；它无法发现端口冲突。反过来，用同一份合法文件**正常启动**到被占用的端口会在第 805–810 行失败，实测输出为标准错误 `error: cannot bind 127.0.0.1:8765: Address already in use`、退出码 2、标准输出为空。

因此：**检查成功只证明规则文件可被同一套加载逻辑接受，不证明 `--port` 指定的端口可供正常启动使用。** 端口可用性要等正常启动真正绑定时才知道。

## 5. 正例的直接测试佐证

下列用例都在 `tests/test_mock_server.py` 中，以真实 `python -m mock_server --check-rules` 子进程核对退出码与两条流（辅助函数 `run_check_rules`，第 7157 行），属于对该入口的**直接覆盖**：

| 结论 | 测试名（位置） |
| --- | --- |
| 合法规则（含一条配 2000ms 延迟的模板路由）省略 `--port` 检查成功：退出码 0、stdout 恰为成功行（带换行）、stderr 空，且耗时小于 `CHECK_RULES_NO_WAIT_MAX_SECONDS`（1 秒，第 7141 行），从时间上排除了等待延迟或渲染请求 | `CheckRulesEntryTests.test_valid_rules_without_port`（第 7349 行）；样例路由 `CHECK_RULES_VALID_ROUTES`（第 7145 行） |
| 传入被占用端口检查仍成功，且原监听器事后仍可用 | `CheckRulesEntryTests.test_valid_rules_on_occupied_port`（第 7356 行）；占用助手 `occupied_local_port`（第 7210 行） |
| 空 `routes` 数组输出 `mock_server rules valid (0 route(s))` | `CheckRulesEntryTests.test_empty_routes_reports_zero`（第 7368 行） |
| JSON 语法错误：退出码 2、stdout 空、stderr 以 `error: ` 开头且含 `is not valid JSON`、无 `Traceback` | `CheckRulesEntryTests.test_json_syntax_error_exits_2`（第 7374 行）；断言助手 `assert_check_rules_error`（第 7305 行） |
| `bodyMode` 大小写非法值：退出码 2 且 stderr 给出完整原因 | `CheckRulesEntryTests.test_invalid_body_mode_value_exits_2`（第 7382 行） |
| 额外字段中的溢出数字同样拒绝 | `CheckRulesEntryTests.test_overflow_number_in_ignored_extra_field_exits_2`（第 7391 行） |
| 端口非整数/为 0：argparse 分支退出码 2，保留 `mock_server: error: argument --port:` 形式 | `CheckRulesEntryTests.test_port_non_integer_exits_2`（第 7397 行）、`test_port_zero_exits_2`（第 7406 行） |
| 含 201 路由的规则经 `--check-rules` 通过，即使端口被占用，且不等待 | `Status201BehaviorTests.test_check_rules_accepts_single_201_route`（第 651 行） |
| 不合规则的占位符写法不影响规则加载与 `--check-rules` | `HeaderTemplateTests.test_rules_load_and_check_rules_unaffected_by_placeholders`（第 6916 行） |

成功/失败两套流的精确断言集中在 `CheckRulesEntryTests.assert_check_success`（第 7280 行，含“stdout 只有一行、stderr 为空、不含监听与停止提示、耗时小于 1 秒”）与 `assert_check_rules_error`（第 7305 行，含“退出码 2、stdout 空、stderr 以 `error: ` 开头、无 `Traceback`”）。

## 6. 完整反例：`exact` 与 `prefix` 同路径仍是重复

### 6.1 输入

把下面内容保存为项目根目录下的 `check-rules-duplicate.json`（完整 UTF-8 JSON。两条路由都是 `GET /api/`，第一条 `pathMode` 为 `exact`、`body` 为 `1`，第二条 `pathMode` 为 `prefix`、`body` 为 `2`；其余配置全部合法——第一条的 `exact` 路径 `/api/` 合法，第二条 `prefix` 路径也满足以 `/` 结尾的要求）：

```json
{
  "routes": [
    {
      "method": "GET",
      "path": "/api/",
      "pathMode": "exact",
      "body": 1
    },
    {
      "method": "GET",
      "path": "/api/",
      "pathMode": "prefix",
      "body": 2
    }
  ]
}
```

### 6.2 命令与预期结果

在项目根目录执行：

```bash
python -m mock_server --rules check-rules-duplicate.json --check-rules --port 8765
```

预期（已实测）：

| 项目 | 预期 |
| --- | --- |
| 退出码 | `2` |
| 标准输出 | 空（0 字节，没有成功行，也没有监听或停止提示） |
| 标准错误 | 一行，以 `error: ` 开头，完整内容为 `error: routes[1]: duplicate route GET /api/` 加行尾换行；其中同时包含 `routes[1]` 与 `duplicate route GET /api/` |
| 异常回溯 | 无——标准错误中不出现 `Traceback`（错误被第 793–795 行捕获并转为普通错误行） |

终端实际显示：

```text
error: routes[1]: duplicate route GET /api/
```

### 6.3 对应分支说明

两条路由都先通过身份检查与各自的选项检查（`exact`、`prefix` 取值都合法，prefix 路径 `/api/` 也以 `/` 结尾），但 `load_rules` 处理到第二条时，键 `("GET", "/api/")` 已由第一条占用：第 583–585 行立即抛 `RulesError("routes[1]: duplicate route GET /api/")`。异常冒泡到 `main` 第 793–795 行，打印 `error: ...` 到标准错误并返回 2；第 797 行的分叉点**根本没有到达**，所以既没有成功行，也没有任何后续分支的输出。`--check-rules` 是否开启不影响这个结果——加载在分叉之前。

### 6.4 同一错误同样阻止正常启动

去掉 `--check-rules` 用同一份文件正常启动（换到空闲端口也一样），实测结果仍是退出码 2、标准输出为空、标准错误同样是 `error: routes[1]: duplicate route GET /api/`：进程在第 792 行加载失败，第 804 行的 `ThreadingHTTPServer` 不会执行，**服务对象从未被创建**，自然也没有监听提示。这正是“预检查与正常启动共用规则加载校验”的含义：同一份非法规则在两个入口上得到同一个错误。

### 6.5 重复判定 ≠ 路径匹配优先级

这是两件不同阶段、不同函数负责的事，不要混淆：

- **加载期的重复判定**（`load_rules` 第 583–585 行）：只比较身份键 `(method, path)` 的字面相等，与 `pathMode` 无关。同方法同路径的两条规则不允许共存，哪怕一条 `exact`、一条 `prefix`。因此“exact 和 prefix 各配一条同路径规则，运行时让谁优先”的情形在合法规则文件里不可能出现。
- **请求期的路径匹配优先级**（`MockHandler._resolve`，第 646–670 行）：规则已成功加载、收到请求后才执行——先找同方法同路径的 `exact` 规则（第 657–659 行），没有 exact 命中时再在 `prefix` 候选中选 `path` 最长者（第 660–670 行），规则排列顺序不影响结果。它处理的是**路径文本不同**的多条规则（如 `/api/ping` 与前缀 `/api/`），从不裁决两个相同键。

## 7. 反例的测试佐证：直接覆盖与共用加载逻辑

引用现有测试时需区分两类：

**（一）直接覆盖 `--check-rules` 入口**——第 5 节所列 `CheckRulesEntryTests` 各用例及第 651、6916 行两个用例，它们都通过 `run_check_rules`（第 7157 行）启动真实子进程并断言退出码与 stdout/stderr。第 6.2 节“退出码 2、stdout 空、stderr 以 `error: ` 开头、无 `Traceback`”的形态由 `assert_check_rules_error`（第 7305 行）锁定；JSON 语法错误、非法 `bodyMode`、溢出数字等用例证明该形态对各类 `RulesError` 一视同仁。

**（二）共用加载逻辑的源码依据**——下列用例不经过 `--check-rules`，但它们锁定的正是预检查在第 792 行复用的同一个 `load_rules` 与同一处启动分支，因此是第 6 节结论的直接依据：

| 结论 | 测试名（位置） | 依据方式 |
| --- | --- | --- |
| 同方法同路径不能靠 `pathMode` 区分；`先 exact 后 prefix` 的 `GET /api/`、body 分别为 1/2 这一与第 6.1 节**同构**的子例，`load_rules` 抛 `RulesError` 且消息含 `duplicate route`、`routes[1]`、`GET /api/`；同路径不同方法、`/api` 与 `/api/` 仍可共存 | `PathModeDuplicateTests.test_duplicate_same_method_and_path_rejected`（第 5025 行，`exact` 后 `prefix` 子例在第 5047–5053 行）、`test_same_path_different_methods_or_paths_still_load`（第 5068 行） | 直接调用 `load_rules`（共用函数本身） |
| 重复规则令**正常启动**以退出码 2 失败，stderr 含 `duplicate route`、方法、路径与后出现规则下标 | `DuplicateRouteTests.test_duplicate_routes_rejected_at_startup`（第 852 行）；启动助手 `start_and_wait_exit`（第 805 行） | 经公开入口启动真实子进程（正常启动分支） |
| 重复规则在监听提示之前退出，退出码 2 且错误中含 `duplicate route`；失败后同一端口可正常启动合法规则 | `StartupWaitFlowTests.test_exit_before_marker_raises_with_exit_code_and_output`（第 278 行） | 经公开入口启动真实子进程（正常启动分支） |
| 带 `requestBody` 的 POST 同路径重复同样被 `load_rules` 拒绝 | `RequestBodyRulesValidationTests.test_duplicate_routes_with_request_body_still_rejected`（第 3444 行） | 直接调用 `load_rules` |
| 请求期 exact 优先于 prefix、前缀取最长、规则顺序不影响选择——与加载期唯一性对照 | `PrefixMatchingBehaviorTests.test_exact_is_literal_and_prefixes_cover_neighbors`（第 4384 行）、`test_declaration_order_does_not_affect_selection`（第 4405 行） | 真实 HTTP 请求（`_resolve` 行为） |

此外 `README.md` 的规则格式说明明确写有“不允许重复的 `method` + `path` 组合，即使两条规则的 `pathMode` 不同也视为重复”，与第 6 节一致。

## 8. 核对清单

| 核对项 | 位置 |
| --- | --- |
| 公开入口 `sys.exit(main())` | `mock_server/__main__.py` 第 8 行 |
| 参数解析（`--rules`/`--port`/`--check-rules`） | `main`，`mock_server/__init__.py` 第 770–789 行；`_port` 第 81–92 行 |
| 文件读取与文件级校验 | `_read_route_items` 第 349–380 行 |
| 路由身份与选项校验 | `_check_route_identity` 第 383–411 行、`_check_route_options` 第 451–553 行 |
| 正文 UTF-8 可编码校验 | `_check_route_options` 第 532–542 行 |
| 重复路由判定与 `RulesError` | `load_rules` 第 583–585 行；`RulesError` 第 77 行 |
| 加载错误统一出口（`error: ` + 退出码 2） | `main` 第 791–795 行 |
| **正常启动与预检查的分叉点** | `main` 第 797–801 行 |
| 正常启动独有：绑定端口、监听/停止提示 | `main` 第 803–824 行 |
| 模板渲染与延迟等待只发生在请求期 | `MockHandler._respond` 第 697–726 行；`_render_template` 第 273–346 行 |
| 请求期路径优先级（与重复判定区分） | `MockHandler._resolve` 第 646–670 行 |
| `--check-rules` 直接测试 | `CheckRulesEntryTests`（`tests/test_mock_server.py` 第 7245 行）等，见第 5、7 节 |

全部测试可在项目根目录按 README 的方式运行：`python3 -m unittest discover -s tests`。
