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
tests/              # unit / adapter_contract / integration / live
docs/               # 契约、运维、供应商状态与测试矩阵
```

每个项目有独立 `pyproject.toml`，根目录通过 uv workspace 管理统一 `uv.lock`。`core` 不依赖 HTTP/数据库/Redis；`services` 不导入具体基础设施；SDK 不依赖服务端运行包。依赖方向由测试约束。详见 [架构](docs/architecture.md)。

## 启动本地开发栈

需要 Python 3.12+、uv、Docker Compose。

```sh
uv sync --frozen --all-packages
uv run --frozen python scripts/dev_setup.py
docker compose up -d --build
docker compose ps
```

初始化脚本创建仅用于本地的 `.env`，生成随机续接加密密钥，不覆盖现有 `.env`。示例配置只连 `mock` 容器，示例模型能力、价格和 Key 均为开发数据。初始开发应用 Key 为 `dev-app-key`，管理 Key 为 `dev-admin-key`；生产环境需要独立引导配置和凭据。

Gateway：http://127.0.0.1:8000；OpenAPI：http://127.0.0.1:8000/docs。迁移服务成功后才启动网关；运行镜像使用 UID 10001。PostgreSQL、Redis 仅向本机暴露开发端口 55432、56379，持久化账本缓冲使用独立卷。

```sh
curl http://127.0.0.1:8000/v1/generate \
  -H 'Authorization: Bearer dev-app-key' \
  -H 'Content-Type: application/json' \
  -d '{"logical_model":"order_extraction","prompt":{"name":"extract_order","version":"1.0.0","variables":{"order_text":"订单R123"}},"requirements":{"structured_output":"json_schema"}}'
```

Python SDK 示例见 [examples/client.py](examples/client.py)。完整字段和错误见 [API 契约](docs/api-contract.md)；管理热更新与故障恢复见 [运维手册](docs/operations.md)。

## 开发与测试

```sh
# 全套容器测试：自动创建独立 gateway_test 数据库，仅清理测试库与 Redis DB 15。
docker compose --profile test run --build --rm tests

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
