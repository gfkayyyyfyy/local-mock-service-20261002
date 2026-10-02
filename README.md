# mock_server

用 Python 3 标准库实现的本地 mock 服务：启动时加载 JSON 规则，之后对匹配的请求返回固定响应。不包含模板、场景、请求记录或 JSON-RPC。

## 用法

```bash
python -m mock_server --rules rules.json [--port 8765]
```

- `--rules`：必填，规则文件路径（UTF-8 编码的 JSON）。
- `--port`：可选，监听端口，默认为 `8765`，仅接受 1 至 65535 的整数。

服务始终绑定 `127.0.0.1`。启动成功后向标准输出打印监听地址，按 `Ctrl+C` 停止并释放端口。规则仅在启动时加载一次，之后修改规则文件不影响响应。

## 规则文件格式

顶层为包含 `routes` 数组的对象，数组每项为含 `method`、`path`、`body` 三个字段的对象：

```json
{"routes":[{"method":"GET","path":"/hello","body":{"message":"你好"}}]}
```

- `method`：仅接受大写 `GET`、`POST`。
- `path`：以 `/` 开头且不含 `?`、`#` 的字符串。
- `body`：任意 JSON 值，包括 `null`。
- 空 `routes` 合法；额外字段被忽略；`method` 与 `path` 组合重复时拒绝加载。

## 匹配与响应

- 匹配时忽略查询字符串和请求体；路径的大小写、尾部斜杠、百分号转义按原样比较。
- `GET` 或 `POST` 命中：返回 HTTP 200 及配置的 `body`。
- 未命中：返回 HTTP 404 及 `{"error":"route_not_found"}`。
- 响应均为 UTF-8 JSON，`Content-Type: application/json; charset=utf-8`，`Content-Length` 为实际字节数。
- 其他 HTTP 方法不予处理（返回 501）。

## 示例

```bash
$ python -m mock_server --rules rules.json --port 8765
mock_server 正在监听 http://127.0.0.1:8765（已加载 1 条规则）
```

```bash
$ curl -i "http://127.0.0.1:8765/hello?x=1"
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
Content-Length: 21

{"message":"你好"}

$ curl -i "http://127.0.0.1:8765/missing"
HTTP/1.1 404 Not Found
Content-Type: application/json; charset=utf-8
Content-Length: 27

{"error":"route_not_found"}
```

## 错误处理

以下情况均向标准错误输出原因并以退出码 2 结束，不接收请求、不输出监听信息：

- 参数缺失或无效（如端口非整数或超出 1–65535）
- 规则文件不存在或不可读
- UTF-8 解码失败
- JSON 语法错误
- 规则结构非法、必填字段缺失、非法 `method` 或 `path`、重复规则
- 端口被占用
