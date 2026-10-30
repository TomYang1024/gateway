# LLM Gateway V1.3

按 [技术方案 V1.3](LLM_GATEWAY_DESIGN.md) 和 [实施边界](LLM_GATEWAY_EXECUTION_SCOPE.md) 实现的 Python LLM 网关。当前交付包括本地实现、三协议模拟契约测试、真实 PostgreSQL/Redis 集成测试和开发容器。真实供应商、生产高可用、账单及 SLO 验收另行记录，不能用模拟结果替代。

## 分项目结构

```text
src/
  core/             # llm-gateway-core：契约、策略类型、路由、执行器、严格校验、计价
  services/         # llm-gateway-services：调用服务、Prompt、策略生命周期
  infrastructure/   # llm-gateway-infrastructure：三协议、PG、Redis、观测、Outbox
  api/              # llm-gateway-api：FastAPI、管理/报表接口、SSE、依赖装配
sdk/python/         # llm-gateway-sdk：仅通过 HTTP 调用网关
config/             # 启动引导；发布后以 PostgreSQL 策略为准
migrations/         # 冻结的 Alembic 初始迁移
tests/              # unit / adapter_contract / integration / acceptance / live
docs/               # 契约、运维、供应商状态与测试矩阵
```

每个项目有独立 `pyproject.toml`，根目录通过 uv workspace 管理统一 `uv.lock`。`core` 不依赖 HTTP/数据库/Redis；`services` 不导入具体基础设施；SDK 不依赖服务端运行包。依赖方向由测试约束。详见 [架构](docs/architecture.md)。

## 启动指南

需要 Python 3.12+、uv、Docker Compose，以及用于解析响应的 `jq`。

```sh
uv sync --frozen --all-packages
uv run --frozen python scripts/dev_setup.py
docker compose up -d --build
curl -s http://127.0.0.1:8000/health/ready
```

就绪检查返回 200 后即可调用。`docker compose ps` 可查看各容器状态。

- **地址：** Gateway 为 http://127.0.0.1:8000，OpenAPI 为 http://127.0.0.1:8000/docs。PostgreSQL、Redis 仅向本机暴露开发端口 55432、56379。
- **开发凭据：** 应用 Key 为 `dev-app-key`，管理 Key 为 `dev-admin-key`。生产环境需要独立引导配置和凭据。
- **上游：** 示例配置只连 `mock` 容器，模型能力、价格和 Key 都是开发数据，不需要供应商 Key。真实供应商接入见 [供应商验收](docs/provider-capabilities.md)。
- **`.env`：** 初始化脚本创建仅用于本地的 `.env` 并生成随机续接加密密钥，不覆盖现有文件。
- **升级旧环境：** 路由配置只在空库首次引导时写入。如果 `glm`、`deepseek`、`kimi` 被拒绝，执行 `docker compose down -v` 后重新启动，这会清空本地开发数据。

迁移服务成功后才启动网关，运行镜像使用 UID 10001，持久化账本缓冲使用独立卷。

## curl 示例

`model` 决定路由到哪个协议适配器，请求和响应格式保持一致，鉴权、请求体和返回格式的差异由适配器屏蔽：

| `model` | 协议 |
|---|---|
| `glm` | Chat Completions |
| `deepseek` | Responses |
| `kimi` | Messages |
| `order_extraction`、`assistant` | 按路由策略选择候选并支持 Fallback |

先设置公共变量：

```sh
GATEWAY=http://127.0.0.1:8000
AUTH='Authorization: Bearer dev-app-key'
ADMIN='Authorization: Bearer dev-admin-key'
JSON='Content-Type: application/json'
```

### 统一调用

```sh
curl -s $GATEWAY/v1/generate -H "$AUTH" -H "$JSON" \
  -d '{"model":"glm","messages":[{"role":"user","content":[{"type":"text","text":"你好"}]}]}' | jq
```

### 流式输出

`stream: true` 以 SSE 逐块返回 `delta` 事件，最后一个事件是 `terminal`。`-N` 关闭 curl 缓冲。

```sh
curl -N $GATEWAY/v1/generate -H "$AUTH" -H "$JSON" \
  -d '{"model":"kimi","stream":true,"messages":[{"role":"user","content":[{"type":"text","text":"你好"}]}]}'
```

### 结构化输出

校验通过的 JSON 在响应的 `structured_data` 字段，不符合 Schema 时返回错误。结构化输出不支持同时流式。

```sh
curl -s $GATEWAY/v1/generate -H "$AUTH" -H "$JSON" \
  -d '{"model":"deepseek","messages":[{"role":"user","content":[{"type":"text","text":"提取订单 R123"}]}],"response_format":{"type":"json_schema","json_schema":{"schema":{"type":"object","properties":{"order_no":{"type":"string"}},"required":["order_no"],"additionalProperties":false}}}}' | jq .structured_data
```

### 提示词模板

模板以版本化 Bundle 存储在 `examples/prompt_bundles/`，请求按名称和版本引用，并传入变量。版本不存在时返回 404 `PROMPT_NOT_FOUND`。

```sh
curl -s $GATEWAY/v1/generate -H "$AUTH" -H "$JSON" \
  -d '{"model":"order_extraction","prompt":{"name":"extract_order","version":"1.0.0","variables":{"order_text":"订单R123"}}}' | jq
```

### 可观测数据

每次调用记录 Token 分类（输入、输出、缓存读取、缓存写入、推理）和延迟。首 Token 延迟 `ttft_ms` 只有流式调用才有值。

```sh
CALL_ID=$(curl -s $GATEWAY/v1/generate -H "$AUTH" -H "$JSON" \
  -d '{"model":"glm","messages":[{"role":"user","content":[{"type":"text","text":"你好"}]}]}' | jq -r .call_id)

curl -s $GATEWAY/v1/calls/$CALL_ID -H "$AUTH" \
  | jq '{usage: .payload.usage, total_ms: .payload.total_ms, attempts: [.attempts[] | {status, ttft_ms, first_content_ms, total_ms}]}'

curl -s $GATEWAY/metrics -H "$ADMIN"
```

### 错误码与重试

所有错误使用同一结构：

```json
{"error": {"code": "GATEWAY_RATE_LIMITED", "message": "RPM or concurrency limit reached", "call_id": "call_...", "retryable": false, "details": {}}}
```

可重试错误（如上游 5xx、429）按指数退避重试，每个候选最多 3 次。退避从 `initial_delay_s` 开始逐次翻倍，不超过 `max_delay_s`，并带随机抖动；上游返回 `Retry-After` 时优先使用。mock 上游不注入故障，重试由 `make verify` 验证。

### 按模型限流

在模型配置中设置 `rpm` 即启用独立限流，默认不限制。下面把 `glm` 限制为每分钟 2 次，第 3 次起返回 429，其他模型不受影响：

```sh
curl -s $GATEWAY/admin/v1/policies/current -H "$ADMIN" \
  | jq '{base_version: .version, snapshot: (.snapshot | .models.glm.rpm = 2), reason: "limit glm"}' \
  | curl -s $GATEWAY/admin/v1/policies -H "$ADMIN" -H "$JSON" -d @- | jq

for i in 1 2 3; do
  curl -s -o /dev/null -w '%{http_code}\n' $GATEWAY/v1/generate -H "$AUTH" -H "$JSON" \
    -d '{"model":"glm","messages":[{"role":"user","content":[{"type":"text","text":"你好"}]}]}'
done
```

限流窗口为 60 秒。流式请求超限时响应已经开始，HTTP 状态为 200，错误在 `terminal` 事件中返回。上游自身的 429 在重试耗尽后以 502 `UPSTREAM_RATE_LIMITED` 返回。

Python SDK 示例见 [examples/client.py](examples/client.py)。完整字段和错误见 [API 契约](docs/api-contract.md)；管理热更新与故障恢复见 [运维手册](docs/operations.md)。

## 开发与测试

```sh
# 全套容器测试：自动创建独立 gateway_test 数据库，仅清理测试库与 Redis DB 15。
docker compose --profile test run --build --rm tests

# 功能验证：覆盖统一抽象、流式、结构化输出、模板、可观测、错误码、重试和限流，
# 结果写入 test-results/feature-verification.xml。
make verify

# 单元和协议测试，不需要外部服务。
uv run --frozen pytest tests/unit tests/adapter_contract -q

# 本机全套测试，先启动开发依赖；不要与容器测试同时执行。
docker compose up -d postgres redis
uv run --frozen python scripts/test_setup.py
uv run --frozen pytest -q

uv run --frozen ruff check src sdk migrations scripts tests
uv run --frozen ruff format --check src sdk migrations scripts tests
```

测试不发送真实告警，不执行模型生成的业务工具。真实上游测试默认跳过，必须显式设置授权、测试路由、凭据和费用预算，见 [供应商验收](docs/provider-capabilities.md)。容器测试生成 `test-results/container-junit.xml` 和 `container-coverage.xml`。

正式验收状态与尚缺环境输入见 [测试矩阵](docs/acceptance.md)。停止开发环境使用 `docker compose stop`，数据卷保留。
