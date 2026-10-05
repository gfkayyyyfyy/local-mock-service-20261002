# 连接复用与关闭流程

本文专门解释一件事：一条 TCP 连接上的请求是**何时被复用、何时被关闭**的——`GET`/`POST` 请求的正文读取与连接复用是什么关系，校验失败后连接还能不能继续用，以及非 `GET`/`POST` 方法为什么只得到一次 501 就被断开。

文中每条结论都标注了 `mock_server/__init__.py` 中对应的函数与分支，并给出 `tests/test_mock_server.py` 中锁定该行为的现有用例。两组核对均附仅依赖 Python 标准库的可复制客户端示例，请求数据与响应均已按文档原样实测。

- 产品代码：`mock_server/__init__.py`
- 命令行入口：`mock_server/__main__.py`（`python -m mock_server`）
- 测试：`tests/test_mock_server.py`

下文行号均对应当前版本，函数名是长期稳定的核对锚点；即使行号有漂移，按函数名查找即可。

## 1. 完整规则文件

把下面内容原样保存为 `connection-rules.json`（UTF-8 编码，只配置一个带正文样例的 POST 路由）：

```json
{"routes":[{"method":"POST","path":"/check","requestBody":{"ok":true},"body":{"accepted":true}}]}
```

含义：`POST /check` 要求请求正文是按 UTF-8 解析后与样例 `{"ok":true}` 整体相等的 JSON（省略 `requestBodyMode` 即 `exact`），通过时返回缺省的 `200` 与 `{"accepted":true}`；不匹配时返回 `400` 与 `{"error":"request_body_mismatch"}`。

启动方式沿用 README 公开命令，在项目根目录执行：

```bash
python -m mock_server --rules connection-rules.json --port 8765
```

（环境中命令名为 `python3` 时用 `python3 -m mock_server ...`，完全等价。）看到 `mock_server listening on http://127.0.0.1:8765 (1 route(s))` 后即可运行下文客户端示例；`Ctrl+C` 停止。

也可以先不启动服务，只校验规则文件：

```bash
python -m mock_server --check-rules --rules connection-rules.json
# 输出：mock_server rules valid (1 route(s))
```

## 2. 一句话结论

**`GET`/`POST` 请求总是先把正文按 `Content-Length` 读完，再选路由、再校验；无论结果是 200、400 还是 404，连接都保持可复用。非 `GET`/`POST` 方法则不读正文、不查路由，直接回一次 501 并携带 `Connection: close` 关闭连接，后续字节（含流水线中的下一个请求）一律不被解释。**

这条结论对应 `mock_server/__init__.py` 的两条互斥分支：

- 复用分支：`MockHandler._respond`（第 672–727 行），由 `do_GET = _respond`、`do_POST = _respond`（第 736–737 行）进入；正文读取在 `_read_body`（第 606–621 行），发送在 `_send`（第 729–734 行），全程不触碰 `close_connection`。
- 关闭分支：`MockHandler._send_method_not_supported`（第 747–761 行），由重写的 `send_error`（第 739–745 行）在标准库对未实现方法报 501 时进入，末尾显式 `self.close_connection = True`（第 761 行）。

## 3. 判断顺序（公开规则 ↔ 源码）

### 3.1 复用分支：`_respond`（GET/POST）

`MockHandler` 把 `protocol_version` 设为 `"HTTP/1.1"`（第 604 行），标准库因此默认 keep-alive：只要处理器不设置 `self.close_connection`，响应发完后连接保持打开，等待下一条请求。`_respond` 每次请求的实际顺序：

1. **先读完整正文**——第 673 行 `raw_body = self._read_body()`，这是每次请求的第一件事，发生在选路由与任何校验之前。`_read_body`（第 606–621 行）按 `Content-Length` 把正文逐块读掉（无 `Content-Length` 视为空正文），目的正是注释所写“保持 keep-alive 连接可用”：正文若残留在连接上，会被标准库误认为下一条请求的请求行。
2. **再选路由**——第 677–680 行：`urlsplit` 去掉查询串后由 `_resolve`（第 646–670 行）选出唯一路由键。
3. **未命中即 404**——第 681–684 行发送 `NOT_FOUND_BODY` 后返回；已读入的正文字节被丢弃，不做解析。
4. **命中且配置了 `requestBody` 才校验**——第 686–690 行调用 `_body_matches`（第 623–644 行）：此时正文**早已完整读入**，校验只是对已读字节做 UTF-8 解码、JSON 解析、非有限数字检查与递归比较。
5. **校验失败：400，连接不关**——第 693 行发送 `REQUEST_BODY_MISMATCH_BODY` 后返回。`_send`（第 729–734 行）只发 `Content-Type` 与 `Content-Length`，不发 `Connection` 头，`close_connection` 保持默认，连接继续复用。
6. **校验通过（或该路由无样例）：回配置响应，连接同样不关**——第 697–727 行应用 `delayMs` 后经同一个 `_send` 发出配置的 `status` 与 `body`。

也就是说，复用分支的所有出口（200/201/4xx/5xx 配置响应、400 正文不匹配、404 未命中）都汇到 `_send`，没有任何一处设置 `close_connection`——**正文被完整读掉是复用的前提，校验结果只决定响应内容，不决定连接去留**。

### 3.2 关闭分支：`_send_method_not_supported`（其余方法）

`PUT`、`DELETE`、`OPTIONS`、`HEAD` 等方法在 `MockHandler` 上没有对应的 `do_*` 方法，标准库 `BaseHTTPRequestHandler` 转而调用 `send_error(501)`；`MockHandler.send_error`（第 739–745 行）把 501 统一改写到 `_send_method_not_supported`（第 747–761 行）。该分支：

- **不读取请求体**——函数内没有任何 `_read_body` 调用，客户端声明的 `Content-Length` 字节留在连接上也不会被消费；
- **不查路由**——不调用 `_resolve`，命中路径下规则的 `body`、`status`、`delayMs` 一律不用；
- **发送一次 501 后关闭**——第 752–758 行发送状态行、`Content-Type`、按 `METHOD_NOT_SUPPORTED_BODY`（第 14 行，`{"error":"method_not_supported"}`，32 字节）给出的 `Content-Length` 以及 `Connection: close`；第 759–760 行对非 HEAD 方法写出正文；第 761 行 `self.close_connection = True` 让标准库在响应发完后关闭连接。

因为正文未读且连接随即关闭，请求体剩余字节与其后流水线中的任何字节都**不会**被当作后续请求解释——这正是第二组核对中后续 `GET /check` 得不到响应的原因。

```text
收到请求行与请求头（标准库解析）
        │
   方法有对应 do_*？
        │
   ┌────┴─────────────────────────┐
 GET/POST                     其他方法
 do_GET/do_POST                send_error(501)
   = _respond                     → _send_method_not_supported
   │                                不读正文、不查路由
   ├─ _read_body 读完整正文         501 + Connection: close
   ├─ _resolve 选路由               close_connection = True
   │    ├─ 未命中 → 404             → 连接关闭，后续字节不解释
   │    └─ 命中
   │        ├─ 有 requestBody：
   │        │   _body_matches 校验已读字节
   │        │   不过 → 400
   │        └─ 通过/无样例 → 配置 status + body
   └─ 全部经 _send 发出，不设 close_connection → 连接复用
```

## 4. 客户端示例（仅标准库）

下面示例只用 `socket`，手工构造合法 HTTP/1.1 报文（完整正文、准确 `Content-Length`），并逐条核对响应的状态行、响应头与正文。先定义读取一条响应的辅助函数，两组核对共用：

```python
import socket

HOST, PORT = "127.0.0.1", 8765


def read_response(sock_file):
    """读出一条完整 HTTP/1.1 响应，返回 (状态行, 响应头 dict, 正文字节)。"""
    status_line = sock_file.readline().decode("ascii").rstrip("\r\n")
    headers = {}
    while True:
        line = sock_file.readline().decode("ascii").rstrip("\r\n")
        if line == "":
            break
        name, _, value = line.partition(":")
        headers[name.strip()] = value.strip()
    body = sock_file.read(int(headers["Content-Length"]))
    return status_line, headers, body
```

### 4.1 第一组：同一连接上先 400 后 200

在同一条 TCP 连接上先发送正文为 `{`（1 字节，JSON 语法错误）的 `POST /check`，读完响应后再发送正文为 `{"ok":true}`（11 字节，与样例相等）的 `POST /check`：

```python
sock = socket.create_connection((HOST, PORT))
f = sock.makefile("rb")
local_addr = sock.getsockname()  # 记录本地地址，稍后核对连接未更换

# 第一个请求：正文 { 是 JSON 语法错误
sock.sendall(
    b"POST /check HTTP/1.1\r\n"
    b"Host: 127.0.0.1:8765\r\n"
    b"Content-Length: 1\r\n"
    b"\r\n"
    b"{"
)
status, headers, body = read_response(f)
print(status)                            # HTTP/1.1 400 Bad Request
print(headers["Content-Type"])           # application/json; charset=utf-8
print(headers["Content-Length"])         # 33
print(body)                              # b'{"error":"request_body_mismatch"}'
assert "Connection" not in headers       # 未要求关闭，连接保持可用

# 第二个请求：沿用同一 socket，正文与样例相等
assert sock.getsockname() == local_addr  # 同一 socket，未经重连
sock.sendall(
    b"POST /check HTTP/1.1\r\n"
    b"Host: 127.0.0.1:8765\r\n"
    b"Content-Length: 11\r\n"
    b"\r\n"
    b'{"ok":true}'
)
status, headers, body = read_response(f)
print(status)                            # HTTP/1.1 200 OK
print(headers["Content-Length"])         # 17
print(body)                              # b'{"accepted":true}'
assert sock.getsockname() == local_addr  # 第二次响应仍在原连接上收到

f.close()
sock.close()
```

实测输出：

```text
HTTP/1.1 400 Bad Request
application/json; charset=utf-8
33
b'{"error":"request_body_mismatch"}'
HTTP/1.1 200 OK
17
b'{"accepted":true}'
```

核对要点：

- **第二次沿用原连接**：两个请求经同一个 `sock` 发送，前后 `getsockname()` 相同，且第二个响应在原 socket 上正常读到——若服务端在 400 后关闭了连接，第二次发送会失败或读到 EOF。第一条响应不带 `Connection` 头（HTTP/1.1 默认 keep-alive）也印证了这一点。
- **为什么 400 之后还能复用**：第一个请求的正文 `{` 已被 `_read_body`（第 606–621 行）按 `Content-Length: 1` 完整读掉，`_respond` 第 673 行的读取发生在选路由与校验之前；`_body_matches` 只是对已读字节做 JSON 解析并判为语法错误，`_respond` 第 693 行发出 400 后经 `_send` 返回，全程不设置 `close_connection`。连接上没有残留字节，下一条请求的请求行能被正常解析。
- **Content-Length 按实际字节数**：`{"error":"request_body_mismatch"}` 为 33 字节、`{"accepted":true}` 为 17 字节，与响应头一致；`Content-Type` 均为 `application/json; charset=utf-8`。

### 4.2 第二组：PUT 得到一次 501 后连接关闭，后续 GET 无响应

另建一条连接，把携带 `hello` 正文（`Content-Length: 5`）的 `PUT /check` 与紧随其后的 `GET /check` 一次性发出（流水线）：

```python
sock = socket.create_connection((HOST, PORT))
f = sock.makefile("rb")

sock.sendall(
    b"PUT /check HTTP/1.1\r\n"
    b"Host: 127.0.0.1:8765\r\n"
    b"Content-Length: 5\r\n"
    b"\r\n"
    b"hello"
    b"GET /check HTTP/1.1\r\n"
    b"Host: 127.0.0.1:8765\r\n"
    b"\r\n"
)
status, headers, body = read_response(f)
print(status)                            # HTTP/1.1 501 Not Implemented
print(headers["Content-Type"])           # application/json; charset=utf-8
print(headers["Content-Length"])         # 32
print(headers["Connection"])             # close
print(body)                              # b'{"error":"method_not_supported"}'

rest = f.read()  # 服务端已关闭连接：读到 EOF，后续 GET 没有第二条响应
print(rest)                              # b''

f.close()
sock.close()
```

实测输出：

```text
HTTP/1.1 501 Not Implemented
application/json; charset=utf-8
32
close
b'{"error":"method_not_supported"}'
b''
```

核对要点：

- **只响应一次**：整条连接上只有一份响应，状态行为 `HTTP/1.1 501 Not Implemented`，正文 `{"error":"method_not_supported"}`（32 字节，与 `Content-Length` 一致），并携带 `Connection: close`。
- **后续 GET 不产生响应**：读完 501 响应后继续读只能得到 EOF（`b''`）。`PUT` 进入的是 `_send_method_not_supported`（第 747–761 行）：不调用 `_read_body`（`hello` 这 5 个字节留在连接上也无人消费）、不调用 `_resolve`（`/check` 路由的 `requestBody` 与 `body` 都不参与），第 761 行 `self.close_connection = True` 使标准库在响应发完后直接关闭连接，流水线中的 `GET /check` 字节随之被丢弃，不会被解析成请求。
- **与第一组的分界**：是否复用取决于方法是否进入 `_respond`。`POST` 走完“读正文 → 选路由 → 校验 → 响应”的完整流程且连接保持打开；`PUT` 在方法分派阶段就被标准库转入 501 关闭分支，连正文都不读。

## 5. 响应汇总

| 组 | 请求（同一连接内按序） | 状态行 | 响应正文 | Content-Length | 连接去向 |
| --- | --- | --- | --- | --- | --- |
| 4.1 | `POST /check`，正文 `{` | `HTTP/1.1 400 Bad Request` | `{"error":"request_body_mismatch"}` | 33 | 保持打开，复用 |
| 4.1 | `POST /check`，正文 `{"ok":true}` | `HTTP/1.1 200 OK` | `{"accepted":true}` | 17 | 保持打开 |
| 4.2 | `PUT /check`，正文 `hello` | `HTTP/1.1 501 Not Implemented` | `{"error":"method_not_supported"}` | 32 | `Connection: close`，关闭 |
| 4.2 | `GET /check`（流水线） | 无响应 | — | — | 连接已关闭 |

所有响应均带 `Content-Type: application/json; charset=utf-8`，`Content-Length` 为响应正文的实际 UTF-8 字节数：33（request_body_mismatch）、17（accepted）、32（method_not_supported）。

## 6. 现有测试佐证

均可在项目根目录用 README 的方式运行：`python3 -m unittest discover -s tests`。

| 结论 | 测试位置（`tests/test_mock_server.py`） |
| --- | --- |
| HTTP/1.1 同一连接连续多个请求（含带体 POST）均成功 | `MockServerRegressionTests.test_keep_alive_connection_reuse`（第 459 行） |
| 同一连接上 400 与 200 交替出现（含正文 `{` 的 400 之后继续复用） | `RequestBodyBehaviorTests.test_keep_alive_reused_after_400_and_200`（第 3108 行） |
| 正文未读完时服务保持静默，补齐后才响应（先读完再校验） | `RequestBodyBehaviorTests.test_response_validated_only_after_entire_body_read`（第 3133 行） |
| 带体 PUT 后紧跟流水线 GET：只得一次 501、`Connection: close`，后续字节不被解释 | `MethodNotSupportedTests.test_body_request_rejected_once_and_connection_closed`（第 2637 行） |
| PUT 命中已配置路由仍返回统一 501，不使用其状态、正文或延迟 | `MethodNotSupportedTests.test_put_on_configured_route_returns_json_501`（第 2589 行） |
| 配置了 `requestBody` 的路径上 PUT/HEAD 仍走既有 501 流程 | `RequestBodyBehaviorTests.test_other_methods_keep_501_behavior_on_guarded_route`（第 3178 行） |
