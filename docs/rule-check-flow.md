# `--check-rules` 规则预检查流程

本文专门解释一件事：`python -m mock_server --check-rules` 这个入口**如何判断一份 JSON 规则文件是否可用**——它从公开入口开始，依次做了哪些解析、读取与校验，在哪里与“正常启动服务”分道扬镳，成功与失败各自以什么退出码、标准输出、标准错误结束。

文中每个结论都标注了对应的源码文件与函数，并给出 `tests/test_mock_server.py` 中锁定该行为的现有用例名称。第 4、5 两节是两份可原样保存、完整复现的示例（一份合法、一份重复规则），均已按文档原样实测。

- 产品代码：`mock_server/__init__.py`
- 命令行入口：`mock_server/__main__.py`（`python -m mock_server`）
- 测试：`tests/test_mock_server.py`

下文行号均对应当前版本，函数名是长期稳定的核对锚点；即使行号有漂移，按函数名查找即可。

## 1. 一句话结论

**`--check-rules` 与正常启动共用同一次 `load_rules` 规则加载校验；加载通过后，检查入口只打印一行结果并以 0 退出，不再创建 HTTP 服务，而正常启动入口才去绑定端口并进入 `serve_forever`。** 规则加载抛 `RulesError` 时，两条路径得到完全相同的处理：向标准错误打印 `error: 原因`、以退出码 2 结束。

这个分叉点在 `mock_server/__init__.py` 的 `main`（第 769–824 行）中：第 791–795 行先 `load_rules`；第 797–801 行 `if args.check_rules:` 分支打印 `mock_server rules valid (N route(s))` 后 `return 0`；只有不成立时才执行第 803–804 行的 `ThreadingHTTPServer(("127.0.0.1", args.port), ...)` 绑定端口。

## 2. 完整链路（公开入口 → 解析 → 读取 → 校验 → 退出）

### 2.1 公开入口：`python -m mock_server`

`mock_server/__main__.py`（全文 8 行）就是公开入口：

```python
from . import main
if __name__ == "__main__":
    sys.exit(main())
```

`python -m mock_server ...` 执行该文件，把 `main()` 的返回值原样作为进程退出码（`sys.exit`）。因此后文的 `return 0` / `return 2` 就是 shell 看到的退出码。

### 2.2 参数解析：`main` 中的 argparse（第 770–789 行）

`main(argv=None)`（第 769 行）先用 `argparse.ArgumentParser(prog="mock_server", ...)` 声明三个参数：

| 参数 | 定义 | 说明 |
| --- | --- | --- |
| `--rules` | `required=True`（第 774 行） | JSON 规则文件路径，必填 |
| `--port` | `type=_port`，默认 `8765`（第 775–780 行） | `_port`（第 81–92 行）把值按十进制转整数并限定 1–65535 |
| `--check-rules` | `action="store_true"`（第 781–788 行） | 开关型标志，缺省为 `False`；帮助文本即写明 “no port is bound or probed” |

第 789 行 `args = parser.parse_args(argv)` 完成解析。要点：

- 参数解析发生在读取规则文件**之前**。`--port abc`、`--port 0` 等非法端口由 `_port` 抛 `argparse.ArgumentTypeError`，argparse 自行打印 `usage:` 用法段与 `mock_server: error: argument --port: ...` 后以退出码 2 终止——**即使带了 `--check-rules` 也一样**，且不经过 `load_rules`。
- `--check-rules` 只是一个布尔标志，本身不改变任何参数校验规则；带不带它，`--port` 都照常解析与校验。
- `--rules` 缺失同样由 argparse 直接以退出码 2 拒绝。

### 2.3 共用的规则加载：`load_rules`（第 556–599 行）

参数解析之后，第 791–795 行是**两条路径共用的唯一一次规则加载**：

```python
try:
    routes = load_rules(args.rules)
except RulesError as exc:
    print(f"error: {exc}", file=sys.stderr)
    return 2
```

无论是否带 `--check-rules`，这里调用的都是同一个 `load_rules`，错误类型都是 `RulesError`（第 77–78 行），输出前缀都是 `error: `，退出码都是 2。`load_rules` 内部分三段：

1. **文件级检查**——`_read_route_items(path)`（第 349–380 行）：
   - 以二进制读文件，读不到抛 `RulesError("cannot read rules file ...")`（第 357–361 行）；
   - `raw.decode("utf-8")` 校验 UTF-8（第 362–365 行）；
   - `json.loads(text, parse_constant=_reject_constant)`（第 366–370 行）：JSON 语法错误，以及 `_reject_constant`（第 132–135 行）拒绝的 `NaN`/`Infinity`/`-Infinity` 非标准字面量，都判为非法 JSON；
   - `_ensure_finite_numbers(data)`（第 138–153 行，第 371–376 行调用）：`1e400` 这种语法合法但溢出为 `inf`/`-inf` 的数字也拒绝，且递归覆盖**整份文档**（含会被忽略的额外字段）；
   - 顶层必须是含 `routes` 数组的对象（第 378–379 行）。
   此阶段不查看任何单条路由内容。
2. **逐条路由校验**——对 `routes` 按下标遍历（第 580 行起），`where = f"routes[{index}]"`：
   - `_check_route_identity(item, where)`（第 383–411 行）：必须是对象，必填 `method`/`path`/`body`，`method` 仅 `GET`/`POST`，`path` 以 `/` 开头且不含 `?`、`#`；
   - **重复判定**——第 583–585 行：`key = (method, route_path)`，键已存在即 `raise RulesError(f"{where}: duplicate route {method} {route_path}")`，因此错误信息同时含**后出现路由的下标 `routes[i]`** 与重复的 `METHOD /path`；
   - `_check_route_options(...)`（第 451–553 行）：校验 `pathMode`/`bodyMode`/`requestBodyMode` 取值、`requestBody` 仅限 POST、`status`（`_valid_status`，第 95–102 行）、`delayMs`（`_valid_delay_ms`，第 105–110 行），并把 `body` 预序列化为紧凑 UTF-8 JSON 字节（第 532–542 行）；body 中字符串值或对象键含未配对代理码点、无法编码为 UTF-8 时在此抛错。
3. **组装 `Routes`**（第 573–599 行）：每条通过校验的路由写入映射及 `delays`、`path_modes`、`body_modes`、`template_bodies`、`request_bodies` 等附加映射后返回。

注意：对 `bodyMode: "template"` 的路由，加载阶段只做两件事——第 532–535 行把 body 预序列化以确认**可编码为 UTF-8**，第 591–595 行保存 body 的**原始 JSON 值**到 `template_bodies`。占位符替换函数 `_render_template`（第 273–346 行）只在请求处理路径 `MockHandler._respond`（第 701–727 行）中被调用，**加载与检查阶段从不调用它**；同样，`delayMs` 只被读取、校验范围（0–2000）并存入 `routes.delays`，没有任何 `time.sleep`（等待只发生在 `_respond` 第 697–699 行）。

### 2.4 分叉点：检查退出 vs 启动服务

加载成功后，第 797–801 行就是两条路径的分界：

```python
if args.check_rules:
    # 仅做与正常启动完全一致的规则加载校验：不模拟请求、不渲染模板、
    # 不按 delayMs 等待，也不绑定或探测端口；校验通过即结束
    print(f"mock_server rules valid ({len(routes)} route(s))")
    return 0
```

- **检查路径**：打印一行 `mock_server rules valid (N route(s))`（`print` 自带行尾 `\n`）到标准输出，`return 0`。不模拟任何请求、不调用 `_render_template`、不读 `delayMs` 等待、**不触碰端口**（既不绑定也不探测），因此不会执行第 803 行之后的任何代码。
- **正常启动路径**（不带 `--check-rules`）：第 803–810 行 `ThreadingHTTPServer(("127.0.0.1", args.port), _make_handler(routes))` 真正绑定端口——端口被占用时这里抛 `OSError`，打印 `error: cannot bind 127.0.0.1:PORT: ...` 并 `return 2`；绑定成功后第 813–816 行打印监听提示 `mock_server listening on http://127.0.0.1:PORT (N route(s))`，第 818 行 `serve_forever()` 阻塞等待请求，`Ctrl+C` 后第 823 行打印 `mock_server stopped`。

这两行监听/停止文本**只可能出现在正常启动路径**。

```text
python -m mock_server --rules R [--port P] [--check-rules]
        │
 argparse 解析参数（--rules 必填；--port 经 _port 校验）
        │ 参数非法 ──► argparse 打印用法 + mock_server: error: ...，退出码 2
        ▼
 load_rules(R)  ← 正常启动与 --check-rules 完全共用
   _read_route_items：可读 → UTF-8 → JSON（拒 NaN/Infinity）
                              → 非有限数字 → 顶层含 routes 数组
   逐条 _check_route_identity / (method,path) 重复判定
        / _check_route_options（模式、status、delayMs、body 可编码为 UTF-8）
        │ 抛 RulesError ──► stderr: error: <原因>，stdout 为空，退出码 2
        ▼
 args.check_rules ?
   ├─ 是：stdout 打印 mock_server rules valid (N route(s))，return 0
   │       （不渲染模板、不等待 delayMs、不绑定/探测端口、
   │        无 listening / stopped 提示）
   └─ 否：ThreadingHTTPServer 绑定 127.0.0.1:port
              ├─ 绑定失败（端口占用）：stderr 报错，退出码 2
              └─ 成功：打印 listening …，serve_forever()，
                       Ctrl+C 后打印 mock_server stopped，return 0
```

### 2.5 三种退出结果一览

| 情形 | 退出码 | 标准输出 | 标准错误 |
| --- | --- | --- | --- |
| `--check-rules` 规则合法 | 0 | 仅 `mock_server rules valid (N route(s))\n` 一行 | 空 |
| 规则加载抛 `RulesError`（两条路径相同） | 2 | 空 | `error: ` 开头的原因，无 Python 回溯 |
| 参数非法（缺 `--rules`、端口非整数/越界） | 2 | 空 | `usage:` 用法段 + `mock_server: error: argument ...`，无 Python 回溯 |
| 正常启动但端口被占用 | 2 | 空 | `error: cannot bind 127.0.0.1:PORT: ...` |

所有错误分支都只打印一行人类可读原因，**不会**出现 `Traceback`：`RulesError` 与绑定 `OSError` 都被显式捕获，argparse 错误也由 argparse 自己处理。

## 3. “检查通过”到底证明了什么、没证明什么

检查入口完成的工作，**恰好等于一次正常启动在绑定端口之前完成的规则加载**，即 `load_rules` 的全部内容：

- 文件可读、是合法 UTF-8、是合法 JSON（拒绝 `NaN`/`Infinity`/`-Infinity` 与溢出为 `inf` 的数字）、顶层结构正确；
- 每条路由的必填字段、`method`/`path`、各模式枚举值、`status`、`delayMs` 合法；
- 没有重复的 `(method, path)`；
- 每条路由的 `body`（以及 POST 的 `requestBody`）都能序列化为合法 UTF-8 JSON 字节——**这就是“正文可编码校验”的全部**。

检查入口**明确不做**的事：

1. **不渲染模板**：不调用 `_render_template`，`{{request.path}}` 等占位符在检查时只是普通字符串文本，不验证占位符写法（README 已说明：不合规则的占位符写法不影响加载与 `--check-rules`）；
2. **不等待 `delayMs`**：即使路由配置了 `delayMs: 2000`，检查也在毫秒级时间内结束，不睡眠 2 秒；
3. **不启动 HTTP 服务**：不创建 `ThreadingHTTPServer`、不调用 `_make_handler`，不接收任何请求；
4. **不绑定或探测 `--port`**：因此**即使该端口已被占用，合法规则仍检查成功**。

所以检查成功只证明“这份规则文件能通过启动期加载校验”，**不能证明端口可供正常启动使用**：端口是否空闲、是否有绑定权限，只有正常启动路径第 803–810 行实际绑定时才知道。反过来，`--port` 的取值仍会被解析校验（非整数或越界直接退出码 2），只是可用性不检查。

## 4. 正例：单条模板路由，检查成功

### 4.1 输入文件

把下面内容原样保存为项目根目录下的 `check-rules.json`（完整 UTF-8 JSON，仅含一条 `GET /echo`，`bodyMode` 为 `template`、`delayMs` 为 `2000`、`body` 为 `{"path":"{{request.path}}"}`）：

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

在项目根目录执行（环境中命令名为 `python3` 时用 `python3`，完全等价）：

```bash
python -m mock_server --rules check-rules.json --check-rules --port 8765
```

### 4.2 预期结果（已实测）

- 退出码：`0`；
- 标准输出：**只有** `mock_server rules valid (1 route(s))` 一行（含行尾换行），共 37 个字节（文本 36 字节 + `\n`，与 `od -c` 结果一致）：

  ```
  mock_server rules valid (1 route(s))\n
  ```

- 标准错误：空。

可用一条命令同时核对三项：

```bash
python -m mock_server --rules check-rules.json --check-rules --port 8765 \
  >/tmp/ok.out 2>/tmp/ok.err
echo "exit=$?"                       # exit=0
od -c /tmp/ok.out                   # 末尾确为 \n，无其他字节
wc -c /tmp/ok.err                    # 0 个字节
```

### 4.3 对应分支说明

1. argparse 接受全部参数：`--rules` 给出，`--port 8765` 经 `_port` 校验为合法端口，`--check-rules` 置位。
2. `load_rules("check-rules.json")` 走完全部三段：`_read_route_items` 读文件、UTF-8 解码、JSON 解析、数字检查、顶层结构均通过；唯一一条路由通过 `_check_route_identity`（`GET`、`/echo` 合法）与 `_check_route_options`：`bodyMode` 取区分大小写的合法值 `"template"`，`delayMs` 取 `_valid_delay_ms` 允许的上界整数 `2000`，body 经第 532–535 行序列化成功（可编码为 UTF-8），原始 body 值被存入 `routes.template_bodies`；下标 0 不构成重复。
3. 进入第 797–801 行的 `args.check_rules` 分支：打印 `mock_server rules valid (1 route(s))` 后 `return 0`。
4. 因此：占位符 `{{request.path}}` **没有被渲染**（`_render_template` 不会被调用）；`delayMs: 2000` **没有被等待**（实测整体耗时约 0.2 秒，而非 2 秒）；**没有 HTTP 服务被创建**，也**没有任何端口绑定或探测**。
5. 端口 8765 即使已被本机其他进程占用，上述结果也完全不变——检查分支在绑定代码（第 803 行）之前就已退出；这一点不代表能用 8765 正常启动，去掉 `--check-rules` 后该端口若被占用，会在第 805–810 行以退出码 2 报 `error: cannot bind ...`。

## 5. 反例：两条 `GET /api/` 重复，检查失败

### 5.1 输入文件

把下面内容原样保存为项目根目录下的 `check-duplicate.json`（两条路由均为 `GET /api/`，分别用 `exact` 与 `prefix`，`body` 分别为 `1` 与 `2`，其余配置合法；`prefix` 路径 `/api/` 以 `/` 结尾，满足前缀规则）：

```json
{
  "routes": [
    {"method": "GET", "path": "/api/", "pathMode": "exact", "body": 1},
    {"method": "GET", "path": "/api/", "pathMode": "prefix", "body": 2}
  ]
}
```

在项目根目录执行：

```bash
python -m mock_server --rules check-duplicate.json --check-rules --port 8765
```

### 5.2 预期结果（已实测）

- 退出码：`2`；
- 标准输出：空（0 字节）；
- 标准错误：恰好一行，以 `error: ` 开头，同时包含 `routes[1]` 与 `duplicate route GET /api/`：

  ```
  error: routes[1]: duplicate route GET /api/
  ```

  逐字由三段拼成：`main` 第 794 行的前缀 `error: `、`load_rules` 第 581 行的位置 `routes[1]: `、第 585 行的消息 `duplicate route GET /api/`。

- 标准错误中**不会**出现 Python 异常回溯（无 `Traceback`），标准输出与标准错误中都**不会**出现 `mock_server listening` 或 `mock_server stopped`。

可用一条命令核对：

```bash
python -m mock_server --rules check-duplicate.json --check-rules --port 8765 \
  >/tmp/bad.out 2>/tmp/bad.err
echo "exit=$?"                       # exit=2
wc -c /tmp/bad.out                   # 0 字节
cat /tmp/bad.err                       # error: routes[1]: duplicate route GET /api/
grep -Traceback /tmp/bad.err || echo "no traceback"
```

### 5.3 对应分支说明

1. 参数解析同样全部通过，进入共用的 `load_rules`。
2. 下标 0 的路由经 `_check_route_identity` 与 `_check_route_options` 校验通过：`/api/` 以 `/` 开头并以 `/` 结尾，作 `exact` 合法，body 数字 `1` 可编码。
3. 处理下标 1 的路由时，`_check_route_identity` 返回的键同样是 `("GET", "/api/")`；第 584 行 `if key in routes:` 命中，第 585 行**在调用 `_check_route_options` 之前**就抛 `RulesError("routes[1]: duplicate route GET /api/")`——所以第二条的 `pathMode: "prefix"`、body `2` 根本不会被进一步处理，`where` 定格在后出现者的下标 `routes[1]`。
4. 异常被 `main` 第 793–795 行捕获：`error: ...` 写入标准错误，`return 2`。**检查分支（第 797 行）没有机会执行**，故无成功行、退出码为 2。
5. 该错误与正常启动**共用同一个加载点**：去掉 `--check-rules` 后，`main` 同样在第 792 行抛错、第 793–795 行退出码 2，进程在第 803 行创建 `ThreadingHTTPServer` **之前**就结束——因此这份重复规则同样会阻止正常启动创建服务，也不会有任何监听提示。

### 5.4 重复判定 ≠ 路径匹配优先级

本例被拒与“exact 优先于 prefix”的**请求期匹配规则是两件事**，不要混淆：

- **加载期（启动/检查共用）的重复判定**：键就是字面的 `(method, path)` 二元组，在第 583–585 行完成，**不看 `pathMode`**。README 明确：“不允许重复的 `method` + `path` 组合，即使两条规则的 `pathMode` 不同也视为重复。”因此本例的 exact/prefix 差异不能挽救重复；同理，两条都是 prefix 也重复。路径按字面比较：不做大小写折叠、尾斜杠合并或百分号解码（`/api` 与 `/api/` 是两条路由）。
- **请求期的匹配优先级**：只有规则全部加载成功、服务已启动后，`MockHandler._resolve`（第 646–670 行）才在每次请求时先找 exact、再选最长 prefix；它解决的是“**不同**路径之间谁先命中”，永远不可能被用来在两条同 `(method, path)` 规则之间做选择，因为那种文件根本加载不到这一步。

## 6. 现有测试佐证

均可在项目根目录用 README 的方式运行：`python3 -m unittest discover -s tests`。

### 6.1 直接覆盖 `--check-rules` 入口的测试

| 结论 | 测试位置（`tests/test_mock_server.py`） |
| --- | --- |
| 检查成功：退出码 0、stdout 恰为一行 `mock_server rules valid (2 route(s))\n`、stderr 为空、无监听/停止提示、耗时不等待 | `CheckRulesEntryTests.test_valid_rules_without_port`（第 7349 行），共用断言辅助 `CheckRulesEntryTests.assert_check_success`（第 7280 行）；入口夹具 `run_check_rules`（第 7157 行）真实拉起 `python -m mock_server --check-rules` 子进程并要求其自行退出 |
| **端口已被占用时合法规则仍检查成功**，且不影响占用方（直接对应第 4 节第 5 点） | `CheckRulesEntryTests.test_valid_rules_on_occupied_port`（第 7356 行，配合 `occupied_local_port`，第 7210 行）；另有 `Status201BehaviorTests.test_check_rules_accepts_single_201_route`（第 651 行）同样在占用端口上检查并逐字断言 `mock_server rules valid (1 route(s))\n` |
| 空 `routes` 成功行计数为 `0` | `CheckRulesEntryTests.test_empty_routes_reports_zero`（第 7368 行） |
| 加载失败：退出码 2、stdout 为空、stderr 以 `error: ` 开头且含原因、无 `Traceback` | `CheckRulesEntryTests.assert_check_rules_error`（第 7305 行）及其实例 `test_json_syntax_error_exits_2`（第 7374 行）、`test_invalid_body_mode_value_exits_2`（第 7382 行）、`test_overflow_number_in_ignored_extra_field_exits_2`（第 7391 行） |
| 参数错误（端口非整数、端口为 0）同样退出码 2，但保留 argparse 表达 `mock_server: error: argument --port:` | `CheckRulesEntryTests.test_port_non_integer_exits_2`（第 7397 行）、`test_port_zero_exits_2`（第 7406 行），断言辅助 `assert_check_argparse_error`（第 7325 行） |
| 模板占位符不影响加载与检查（检查不校验占位符写法） | `QueryParamTemplateTests.test_rules_load_and_check_rules_unaffected_by_placeholders`（第 6442 行）、`HeaderTemplateTests.test_rules_load_and_check_rules_unaffected_by_placeholders`（第 6916 行） |

### 6.2 覆盖“共用加载逻辑”的源码依据（非检查入口专属）

下列测试不经过 `--check-rules`，但它们锁定的正是第 2.3 节那条**被两条路径共用**的 `load_rules` 各分支；按“`main` 第 792 行调用的就是同一个函数”这一源码事实，这些行为在检查入口必然同样发生：

| 共用逻辑 | 测试位置 |
| --- | --- |
| 重复 `(method, path)` 在启动期即被拒绝（退出码 2、stderr 含 `duplicate route`、方法、路径与后出现者下标、无监听提示），直接对应第 5 节 | `DuplicateRouteTests.test_duplicate_routes_rejected_at_startup`（第 852 行，样例表 `DUPLICATE_ROUTE_CASES` 第 740 行），经公开启动入口 `start_and_wait_exit`（第 805 行）验证 |
| 同路径但 `pathMode` 不同（含两条 prefix、先 exact 后 prefix）仍判重复，错误含 `routes[1]` 与 `GET /api/`（第 5.4 节的直接源码佐证） | `PathModeDuplicateTests.test_duplicate_same_method_and_path_rejected`（第 5025 行，直接断言 `load_rules` 抛 `RulesError`）；带 `requestBody` 的重复见 `RequestBodyRulesValidationTests.test_duplicate_routes_with_request_body_still_rejected`（第 3444 行） |
| 重复判定与匹配优先级互不相同：`/api` 与 `/api/` 是不同路由，`GET /api/` 对 prefix 剩余为空不算命中 | `PathModeDuplicateTests.test_same_path_different_methods_or_paths_still_load`（第 5068 行） |
| `load_rules` 抛 `RulesError` 的其余共用分支：非法状态/延迟、非标准数字与溢出、未配对代理、结构与必填字段、非法路径、`requestBody`/`requestBodyMode` 等 | 如 `InvalidStatusTests.test_invalid_statuses_rejected`（第 695 行）、`NonStandardNumberTests`、`OverflowNumberTests`、`UnpairedSurrogates`、`RulesStructureValidationTests`（第 1586 行）、`InvalidPathTests`（第 1884 行）、`InvalidDelayMsTests`（第 2189 行）中的 `test_load_rules_raises_rules_error` / `test_cli_rejects_..._with_exit_code_2` 用例 |
| body 可编码为 UTF-8 校验是加载的一部分（第 532–542 行），模板 body 同样先过此关 | `UnpairedSurrogates.test_cli_rejects_unpaired_surrogates_with_exit_code_2`（第 1389 行）；模板渲染只发生在请求期见 `BodyModeTemplateTests`（第 5145 行）与 `DelayedTemplateCheckedRouteTests`（第 7029 行），延迟只发生在请求期见 `DelayBehaviorTests`（第 2071 行） |

## 7. 复现核对清单

- [ ] 按第 4.1 节保存 `check-rules.json`，执行第 4.1 节命令：退出码 `0`，stdout 逐字节等于 `mock_server rules valid (1 route(s))\n`，stderr 为空。
- [ ] 在 8765 被占用（如另起一个监听）时重跑同一命令：结果不变，仍为退出码 0。
- [ ] 按第 5.1 节保存反例文件并执行：退出码 `2`，stdout 为空，stderr 为 `error: routes[1]: duplicate route GET /api/`，无 `Traceback`、无监听或停止提示。
- [ ] 在源码中找到所指函数与分支：入口 `mock_server/__main__.py` → `main`（`mock_server/__init__.py` 第 769 行）→ `load_rules`（第 556 行，重复判定第 583–585 行）→ 检查分支（第 797–801 行）与绑定分支（第 803–816 行）。
- [ ] 确认第 4 节的检查没有渲染 `{{request.path}}`、没有等待 2000ms、没有创建服务；第 5 节的错误在去掉 `--check-rules` 后同样阻止正常启动。
