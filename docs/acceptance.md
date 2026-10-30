# V1.3 实施与测试矩阵

本地实现按技术方案 V1.3 和实施边界执行；新增工程组织为用户确认的 `core/services/api/infrastructure` 分项目结构。原始四份方案文档保持不变。

## 自动验证

| 边界 | 测试/证据 | 验证类型 |
|---|---|---|
| 独立项目构建、禁止反向依赖 | `test_project_boundaries.py`、workspace wheel 构建 | 本地静态/构建 |
| 请求、身份覆盖、非法输入、能力/参数/上下文 | `test_boundaries.py`、`test_gateway.py` | 单元 + HTTP/PG/Redis |
| 三协议普通、文本流、工具流 | `adapter_contract/test_protocols.py` | HTTP 契约模拟，单字节 TCP 切片 |
| 工具 ID/名称碎片、交错参数、未完成/错误参数 | `test_boundaries.py`、`test_strict_edges.py` | 单元与 SDK 顺序边界 |
| 三协议两轮工具调用及原样推理材料 | `test_two_round_tool_loop`、`test_wire_tool_result_and_opaque_roundtrip` | 契约模拟 + 真实账本/Redis |
| 提交前重试/Fallback、提交后禁止切换、tool start 提交 | `test_gateway.py` | HTTP 故障注入 |
| Retry-After、尝试/时间/费用预算 | `test_gateway.py`、`test_additional_boundaries.py` | 故障注入 |
| 唯一终态、取消、慢客户端背压、租约释放 | `test_cancel_closes_attempt_and_releases_lease`、`test_bounded_backpressure_cancels_upstream` | 真实共享设施 + 任务/流取消 |
| 续接过期、归属、循环与模型固定、引用完整性 | `test_faults.py`、`test_gateway.py` | 加密 Redis 状态 + HTTP |
| 循环共享轮数，复用引用不绕过预算 | `test_shared_loop_round_limit_prevents_reference_replay` | 真实 Redis 原子计数 |
| 非流式 Schema 严格校验、禁止业务结构化流 | `test_boundaries.py`、`test_strict_edges.py`、`test_gateway.py` | 单元 + HTTP |
| 一次代码围栏提取、模型修复有限且独立入账 | `test_fenced_json_once`、`test_model_repair_once_is_a_visible_attempt` | 单元 + HTTP/账本 |
| Bundle hash/路径/symlink/模板、原子更新、新版本可见 | `test_prompt_artifacts.py`、手动刷新任务测试 | 文件制品 + HTTP/PG |
| 跨团队应用隔离、报表角色、Key 轮换和吊销 | `test_gateway.py`、`test_key_rotation_and_readonly_credential` | HTTP/PG/Redis |
| 多实例额度、TPM/RPM、在线保留容量、租约恢复 | `test_faults.py` | 真实 Redis 并发操作 |
| 同供应商账号不同协议连接共享额度 | `test_provider_account.py` | 真实 Redis + HTTP |
| 原子策略 CAS/回滚、多实例刷新、丢通知补偿 | `test_gateway.py`、`test_snapshot_loss_notification_compensation` | 真实 PG/Redis |
| 已固定计划的紧急禁用/吊销、熔断恢复 | `test_additional_boundaries.py`、`test_faults.py` | 真实共享状态 + 故障注入 |
| 缓存/推理 Token 不重复收费、未知费用不置零 | `test_boundaries.py`、Fallback 账本测试 | 单元 + 账本 |
| 账本 PG 故障、缓冲耗尽、进程生命周期后回放 | `test_persistent_spool_recovery_and_exhaustion` | 持久 SQLite + 真实 PG |
| Outbox 80/100 去重、模拟失败重试和 dead 队列 | `test_budget_outbox_dedup_and_failure_queue` | 真实事务与模拟渠道 |
| 团队硬预算、追加对账、不重写原费用 | `test_hard_budget_and_append_only_adjustments` | HTTP/PG |
| 日志不含敏感扩展、健康和内部 Metrics | `test_log_does_not_include_sensitive_extra`、健康/指标测试 | 单元 + HTTP |
| 非 root 镜像、迁移、健康、真实容器网络调用 | Docker Compose + `scripts/container_smoke.py` | 本地容器 |

全套容器测试通过 `make test` 执行，JUnit 与覆盖率输出到 `test-results/`。最近一次（2026-10-31，`make test`）：168 项中 162 项通过、6 项真实测试默认跳过、0 失败，行覆盖率 88%。错误路径包含请求拒绝和失败 Attempt，不把 Mock 输出认定为真实厂商验收。测试仅重置专用 `*_test` 数据库及 Redis DB 15，不改开发/生产策略，不发真实通知。

## 功能验证

`make verify` 运行 `tests/acceptance/test_features.py`，经真实 API、PostgreSQL、Redis 和模拟上游逐项验证统一抽象、流式、结构化输出、Prompt 模板、可观测数据、统一错误、指数退避重试（最多 3 次）和按模型限流，结果写入 `test-results/feature-verification.xml`。

## 阶段出口与外部环境

M0 工程和契约基线已落地；M1 已通过 DeepSeek 真实原生 Schema 抽取链路。GLM、DeepSeek、kimi-k2.6 Chat、kimi-k2.7-code Chat、kimi-k2.6 Messages、kimi-k3 Messages、kimi-k3.1-pro Messages 各 6 项真实调用验证通过，详情见供应商验收记录。M1–M4 所需的本地代码链路和测试覆盖可运行；生产设施条件尚未满足，不能标记整个 V1 生产验收通过。

| 待完成验收 | 当前缺少的输入/环境 |
|---|---|
| 生产 Key/人员角色/告警渠道 | 首批团队应用、管理员与只读人员身份、owner/收件人及受控渠道 |
| Redis/PG 自动切换和网络分区 | 高可用部署、故障切换权限、卷恢复与备份设施 |
| 生产 SLO 和两个试点稳定两周 | 明确的负载、输入规模、并发、样本量、备用容量和试点数据 |
| 月度账单差异目标 | 完整账单周期、采购价格/折扣/税费与币种口径 |
| 生产内容/日志/账本保留及密钥轮换 | 环境保留期、加密密钥设施和运维责任人 |

本地部署是单节点开发设施，不能据此承诺 P95 ≤50ms、自身失败率 ≤0.1%、自动切换无损或月度账单差异 ≤3%。这些项目需要单独测量并填入真实结果。
