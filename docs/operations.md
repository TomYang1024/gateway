# 开发部署与运维

## 容器和迁移

`Dockerfile` 使用 Python 3.12、统一冻结依赖和多阶段构建。`runtime` 为非 root 用户；`test` 是带开发依赖的独立测试阶段。Compose 为本地开发环境，包含 PostgreSQL 17、Redis 7.4、模拟上游、一次性迁移和 Gateway；不等同于生产高可用部署。

`migrate` 执行 `alembic upgrade head`，网关在迁移成功后启动。初始迁移固定 PostgreSQL DDL，后续结构变化必须新增迁移。业务回滚通过新策略版本完成，不通过删除账本的 schema downgrade。

生产部署需要独立凭据和测试过的 PostgreSQL/Redis 高可用、TLS、备份及恢复；入口代理配置请求体、连接与慢请求限制。每实例一个持久缓冲卷，不能共享同一个 SQLite 文件，也不能依赖临时容器文件系统保存未回放账本。

## 运行参数

配置通过 `GATEWAY_` 前缀环境变量读取，精确定义见 infrastructure 的 `settings.py`。

| 参数 | 默认/用途 |
|---|---|
| DATABASE_URL / REDIS_URL | 开发地址；生产显式配置连接和安全设施 |
| BOOTSTRAP_PATH | `config/development.yaml`；只初始化空策略库，不覆盖已发布配置 |
| CONTINUATION_KEY | 必填 Fernet 密钥；由受控环境注入，各副本保持一致 |
| CONTINUATION_TTL_S | 900 秒；只用于协议续接状态 |
| BUNDLE_ROOT / BUNDLE_INDEX | 只读挂载的制品根目录及可信授权索引 |
| PROMPT_POLL_S | 60 秒；下载/校验耗时另计 |
| POLICY_POLL_S / SNAPSHOT_TTL_S | 5 / 60 秒；通知丢失由轮询补偿，超期拒绝认证放行 |
| SPOOL_DIR / SPOOL_MAX_BYTES | `var/spool` / 64 MiB 新调用准入阈值；在途尽力持久化收尾 |
| STREAM_BUFFER_SIZE / BACKPRESSURE_TIMEOUT_S | 16 个事件 / 10 秒；避免慢客户端无限积压 |
| NOTIFICATIONS_ENABLED | 默认 false；测试始终使用 mock 渠道 |
| NOTIFICATION_WEBHOOKS | JSON 对象，渠道引用到管理员允许 URL 的映射 |

供应商配置只保存 `env:` 密钥引用，不保存原文。环境凭据变更会改变客户端缓存键；配置和凭据身份版本也参与缓存键。旧连接池在进程收尾关闭。在频繁轮换的部署中应采用有界发布节奏并监测进程连接数。

`OTEL_EXPORTER_OTLP_ENDPOINT` 配置后启用批量 OTLP Trace。普通 JSON 日志写 stdout，由部署平台负责轮转；不记录 Prompt、输出、工具参数、Authorization 或 Cookie。`/metrics` 使用独立管理/报表凭据，网络也应限制为内部访问。

账本、stdout 日志和短期续接保留口径分开管理。业务内容默认不长期留存；账本/日志期限与归档、删除作业需由环境负责人根据实际合规要求配置。本交付不自动清理生产历史数据。

## 热配置与 Prompt

先读取当前版本，提交完整 Snapshot、基准版本和原因。引用、价格、地域、参数和能力检查通过后，数据库事务保存新版本及审计。通知促使其他实例刷新，周期对账补偿丢失通知。并发写入旧版本返回 CONFIG_CONFLICT。

回滚将旧业务策略发布为新版本，同时保留当前 Key/管理凭据状态。紧急禁用连接与吊销 Key 在下一 Attempt 再检查；已有在途请求默认不强断。普通调用固定开始时快照。

Prompt 制品只读挂载，可信索引指定 owner、应用/用例、artifact 相对目录与 hash。hash 覆盖相对文件名和全部行为文件字节。大小、路径、symlink、Schema 和模板白名单全部通过才原子安装；同名同版本不可改 hash。新版本通过轮询或管理刷新可用，无需服务重启。在途调用已保存消息及版本，不重新渲染。

## 故障行为和演练

| 故障 | 行为与恢复核验 |
|---|---|
| Redis 不可用 | 新调用默认拒绝；在途 Attempt 尽力记录；租约最终按期限回收。恢复后重新建立连接 |
| 配置 PG 暂不可读 | 仅使用未过期最后快照；超期或新实例无快照保持 Not Ready；管理写入拒绝 |
| 账本 PG 不可写 | 顺序进入实例持久缓冲；恢复后幂等回放，费用结算和 Outbox 不重复 |
| 缓冲不可写或达到阈值 | 拒绝新调用；在途尽力写入收尾并记录故障日志，不能假装成功落账 |
| 首内容前连接/限流失败 | 在总尝试、时间和费用内重试，尊重 Retry-After；最多一次 Fallback |
| 提交后断流 | 唯一 failed 终态，不重试、不跨模型拼接；未知用量保留不完整 |
| 客户端断开/慢客户端 | 取消上游并释放租约；账本 cancelled，终态可能无法送达客户端 |
| 策略通知丢失 | 轮询补偿；验证各实例 version 收敛和缓存最大有效时间 |
| 通知投递失败 | Outbox 保持可重试，三次后 dead；接收端用 Idempotency-Key 去重 |

本地故障验证见 `tests/integration/test_faults.py` 和 `test_additional_boundaries.py`。生产演练必须另外覆盖 Redis 主节点切换、PG 切换、网络分区、进程崩溃后的卷接管，以及未知用量。记录起止时间、失败调用数、恢复耗时、缓冲数量和版本收敛；不能用本地异常注入宣称高可用验收完成。

就绪检查包括有效策略、Redis 和账本缓冲准入；PG 账本暂时失败且缓冲有效时按既定策略继续服务。关闭时停止新调用、限时等待在途任务、取消超时任务、关闭连接池和回放缓冲。

## 预算与报表

预算以团队时区划分月份，并保持团队币种。结算与通知任务在同一数据库事务内完成；同团队、月份和阈值只产生一次任务，策略改版不会无限重复。未知费用产生独立质量事件。

Outbox 默认 mock 模拟送达，不向人员发消息。正式渠道需配置 owner、收件人组、允许的 Webhook、显式启用标志，以及对应环境授权。Webhook 按至少一次语义投递，目标系统需使用事件 ID 去重。

硬预算属于保守准入，未知月度金额或数据不可读取时拒绝新调用；已发生的供应商费用和并发在途调用可能超出最终预算，不能承诺精确账单封顶。价格发布不重写历史 Call；税费、折扣、币种、外部账单差异用追加对账项分类。
