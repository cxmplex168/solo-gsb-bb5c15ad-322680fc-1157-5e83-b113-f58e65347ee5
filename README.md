# 配置变更三方合并 API（FastAPI + SQLite）

客户端提交一份配置的 **base（共同祖先）/ left（左分支）/ right（右分支）** 三份 JSON，
服务端按键（路径为 [RFC 6901 JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901)）递归合并：

- 只有一侧相对 base 改动 → **自动采用**该侧取值；
- 两侧都改且取值相同 → 合并该取值；
- 两侧都改但取值不同 → 在结果中保留 **冲突占位**，并同时返回 base / left / right 三方候选，
  由客户端分批携带当前 `revision` 提交冲突取值；
- `revision` 过期（乐观锁）→ 返回 **409 Conflict**，响应体带回 `current_revision`；
- 全部冲突解决后生成 **不可变结果**（之后再次提交解决请求返回 409）；
- 创建合并单需要 `Idempotency-Key` 请求头；同一幂等键 + 同一负载重放返回原合并单，
  **不会创建新单**（状态码 200）；同一幂等键搭配不同负载返回 422。

数组与标量按原子值整体比较；对象递归合并。

---

## 一条命令启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000 | 服务根路径（接口列表） |
| http://localhost:8000/docs | Swagger UI（可直接调试） |
| http://localhost:8000/openapi.json | OpenAPI 描述 |
| http://localhost:8000/health | 健康检查 |

- 监听端口：宿主机 **8000** → 容器 8000（在 `docker-compose.yml` 的 `ports` 中修改）。
- Python 依赖安装与 SQLite 建表均在**启动时自动完成**，无需手工初始化。
- SQLite 数据库位于容器内 `/data/merges.db`，通过命名卷 `merge-data` 持久化。
- 停止并保留数据：`docker compose down`；连同数据卷一起清除：`docker compose down -v`。

不使用 Docker 时的本地启动：

```bash
python3 -m pip install -r requirements.txt
MERGE_DB_PATH=./data/merges.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 数据模型

合并单（merge）核心字段：

```jsonc
{
  "merge_id": "uuid",
  "revision": 1,                 // 每成功提交一批冲突解决 +1
  "status": "open",              // open（仍有冲突）| resolved（已生成不可变结果）
  "conflicts": [                 // 未解决冲突列表，resolved 时为空
    { "pointer": "/db/host", "base": "old", "left": "new-a", "right": "new-b" }
  ],
  "merged": { ... },             // 工作文档，冲突位置为占位对象
  "result": null,                // status=resolved 后为不可变最终文档
  "created_at": "...",
  "updated_at": "..."
}
```

工作文档中冲突处的占位结构：

```json
{ "__conflict__": { "base": "old", "left": "new-a", "right": "new-b" } }
```

> 删除语义：候选值中的 `{"__delete__": true}` 表示该键在该侧被删除（区别于显式的 JSON `null`）。
> 客户端解决冲突时，也可以把 `{"__delete__": true}` 作为取值提交以删除该键。

---

## API

### 1. 创建合并单 `POST /merges`

必须携带请求头 `Idempotency-Key: <任意非空字符串>`。首次创建返回 **201**，幂等重放返回 **200**。

```bash
curl -X POST http://localhost:8000/merges \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-001' \
  -d '{
    "base":  {"name": "svc", "port": 8080, "db": {"host": "old", "pool": 5}},
    "left":  {"name": "svc", "port": 9090, "db": {"host": "new-a", "pool": 5}},
    "right": {"name": "svc", "port": 8080, "db": {"host": "new-b", "pool": 10}}
  }'
```

响应：单侧改动 `port=9090`、`db.pool=10` 已自动合并；`db.host` 双侧分歧成为冲突：

```json
{
  "merge_id": "…",
  "revision": 1,
  "status": "open",
  "conflicts": [
    { "pointer": "/db/host", "base": "old", "left": "new-a", "right": "new-b" }
  ],
  "merged": {
    "name": "svc",
    "port": 9090,
    "db": { "host": { "__conflict__": { "base": "old", "left": "new-a", "right": "new-b" } }, "pool": 10 }
  },
  "result": null,
  "created_at": "…",
  "updated_at": "…"
}
```

没有任何冲突时，创建即终态：`status="resolved"`、`revision=1`，`merged` 与 `result` 相同。

### 2. 查询状态 `GET /merges/{merge_id}`

返回上述合并单的最新状态（用于客户端在 409 后刷新 `revision` 与剩余冲突）。不存在返回 **404**。

### 3. 分批提交冲突取值 `POST /merges/{merge_id}/resolve`

请求体：`revision` 为客户端当前看到的版本号，`resolutions` 为 `JSON Pointer -> 取值`，
一次可以提交一个或多个冲突。成功后 `revision +1`。

```bash
curl -X POST http://localhost:8000/merges/<merge_id>/resolve \
  -H 'Content-Type: application/json' \
  -d '{"revision": 1, "resolutions": {"/db/host": "new-a"}}'
```

- 最后一个冲突解决后：`status="resolved"`，`result` 冻结为最终文档（之后不可再修改）。
- 删除键：取值传 `{"__delete__": true}`。
- 整份文档根节点冲突的 pointer 为 `""`。
- 错误码：
  - **409 `revision_mismatch`**：revision 过期，响应含 `current_revision`，请先 GET 刷新；
  - **409 `already_resolved`**：合并单已定稿，结果不可变；
  - **422 `invalid_resolution` / `invalid_pointer`**：指针不是当前未解决冲突、JSON Pointer
    格式非法、批次为空等；整批校验失败时**不会**应用任何取值，revision 不变。

409 示例：

```json
{ "detail": { "code": "revision_mismatch", "message": "Expected revision 2 but client submitted 1",
              "merge_id": "…", "current_revision": 2 } }
```

### 4. 获取不可变结果 `GET /merges/{merge_id}/result`

仅 `resolved` 可用；仍是 `open` 时返回 **409 `not_resolved`**。

---

## 合并规则速查（单键，b/l/r 分别为 base/left/right）

| 情况 | 结果 |
| --- | --- |
| l == r（都没改，或改成一样，或都删除） | 取 l（删除则键消失） |
| l == b，r != b（仅 right 改） | 取 right |
| r == b，l != b（仅 left 改） | 取 left |
| l、r 都不同于 b 且互不相同（三者都是对象） | 递归合并子对象 |
| l、r 都不同于 b 且互不相同（标量/数组/涉及删除） | 冲突占位，保留三方候选 |

特殊键转义按 RFC 6901：路径段中的 `~` 写作 `~0`，`/` 写作 `~1`。

---

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── merge.py      # 三方合并 / JSON Pointer / 冲突解决纯算法
│   ├── schemas.py    # Pydantic 请求与响应模型
│   └── main.py       # FastAPI 路由、SQLite、revision 乐观锁、幂等键、启动建表
├── tests/            # pytest：算法单测 + API 端到端（33 个用例）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

数据表随启动自动创建：

- `merges`：合并单主表（含 revision/status/working 文档/冲突列表/不可变 result/幂等键唯一约束）；
- `merge_events`：创建与每批解决的审计事件。

运行测试：

```bash
python3 -m pip install -r requirements.txt pytest httpx
python3 -m pytest tests/ -q
```
