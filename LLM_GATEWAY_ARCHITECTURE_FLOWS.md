# LLM Gateway 整体架构与流程泳道图

- 日期：2026-10-30
- 基线：技术方案V1.3、实施边界文档
- 范围：V1；文本Streaming、Tool Calling Streaming均为必选
- 图例：实线为调用/数据流，虚线为策略下发或观测。时序图的每一列代表一个职责泳道。

## 1. 整体架构：调用面、管理面、洞察面

```mermaid
flowchart TB
 subgraph USERS[使用者]
  APP[业务应用 / Python HTTP SDK]
  OPS[平台管理员]
  OWNER[团队负责人 / 财务]
 end
 subgraph CP[管理面]
  ADMIN[Admin API与独立身份]
  GOV[团队 / 应用 / Key / 用例授权]
  POLICY[Provider / 路由 / 配额 / 数据边界]
  PROMPT[Bundle索引刷新与授权]
  AUDIT[版本发布 / 回滚 / 审计]
  ADMIN --> GOV
  ADMIN --> POLICY
  ADMIN --> PROMPT
  GOV --> AUDIT
  POLICY --> AUDIT
  PROMPT --> AUDIT
 end
 subgraph DP[调用面：FastAPI多实例]
  API[HTTP入口：generate / stream]
  AUTH[可信身份 / 授权 / RPM / 并发]
  RENDER[Prompt加载 / 变量校验 / 受限渲染]
  ROUTER[能力 / 数据边界 / Token预算 / 候选计划]
  ADMIT[应用TPM预留 / 团队费用预算]
  EXEC[Executor：Attempt / 重试 / Fallback / 超时]
  FACTORY[客户端工厂与协议Adapter]
  OUT[统一结果 / 工具组装 / Schema校验 / SSE]
  API --> AUTH --> RENDER --> ROUTER --> ADMIT --> EXEC
  EXEC --> FACTORY
  FACTORY --> OUT
  OUT --> API
 end
 subgraph EXTERNAL[模型服务]
  GLM[GLM]
  DS[DeepSeek]
  KIMI[Kimi K3]
 end
 subgraph IP[洞察面]
  REC[调用账本 / Trace / 日志 / Metrics]
  REPORT[授权报表 / 调用检索 / 月度对账]
  ALERT[预算Outbox / 通知重试 / 送达记录]
  REC --> REPORT
  REC --> ALERT
 end
 subgraph STORE[存储与协调]
  PG[(PostgreSQL：策略 / 账本 / 审计 / Outbox)]
  REDIS[(Redis：共享额度 / 租约 / 通知)]
  STATE[(受限加密短期续接状态)]
  BUNDLE[(业务仓库发布的不可变Bundle)]
 end
 APP --> API
 OPS --> ADMIN
 OWNER --> REPORT
 AUDIT -.原子版本快照.-> AUTH
 AUDIT -.原子版本快照.-> ROUTER
 AUDIT -.原子版本快照.-> EXEC
 PROMPT --> BUNDLE
 RENDER --> BUNDLE
 FACTORY --> GLM
 FACTORY --> DS
 FACTORY --> KIMI
 AUTH --> REDIS
 ADMIT --> REDIS
 EXEC --> REDIS
 OUT --> STATE
 ADMIN --> PG
 REC --> PG
 EXEC -.调用与尝试.-> REC
 OUT -.结果用量与延迟.-> REC
```

**边界：**业务应用执行工具；Gateway不执行工具、不编排Agent。协议Adapter转换请求响应，不私自重试。Executor控制唯一终态。人员报表凭据与应用Key分离。

## 2. 单次调用主泳道图

```mermaid
sequenceDiagram
 autonumber
 actor B as 业务应用
 participant A as FastAPI入口
 participant G as 治理准入
 participant P as Prompt服务
 participant R as Router与Token预算
 participant E as Executor
 participant U as Adapter与上游
 participant O as 输出处理
 participant L as 账本与观测
 B->>A: generate或stream，请求与业务关联ID
 A->>G: 认证Key，解析team/application/key
 G->>G: 用例授权、RPM、并发租约
 G-->>A: 可信上下文与策略版本
 A->>L: 创建Call，记录可信归属
 A->>P: messages直通或加载指定Bundle
 P->>P: 输入Schema校验、受限渲染
 P-->>A: 消息、Schema、工具、Prompt版本/hash
 A->>R: 解析逻辑模型与要求
 R->>R: 能力/数据边界/参数过滤，逐候选估算Token
 R-->>E: 固定候选计划与拒绝原因
 E->>G: 应用TPM预留、团队费用预算检查
 loop 每次允许的Attempt
  E->>E: 检查总次数、截止时间与提交状态
  E->>G: 紧急禁用/权限复查、厂商RPM与TPM准入
  E->>L: 创建Attempt并记录实际配置
  E->>U: 统一请求与候选
  U-->>E: 统一响应或内部流事件
  E->>O: 完整结果或允许发布的增量
  O-->>A: JSON结果或SSE事件
  A-->>B: 返回结果或持续发送内容
  E->>L: 记录本次Usage、延迟、错误与费用
 end
 O->>E: 完成或校验失败
 E->>E: 决定唯一终态
 E->>L: 汇总Call与所有Attempt，未知费用标记不完整
 E->>G: 释放租约并结算预留
 E-->>A: 最终结果或唯一terminal
 A-->>B: 完整响应或终态事件
```

图中loop只代表预算允许的执行，不表示流提交后还能重试。任何阶段取消、失败均进入统一清理路径；预处理失败不会调用上游。普通调用不会提前向客户端返回未校验结果。

## 3. 能力路由与候选规划泳道图

```mermaid
sequenceDiagram
 participant C as 调用上下文
 participant R as Router
 participant M as 模型与Adapter能力目录
 participant D as 数据与授权策略
 participant T as Token计数器
 participant H as 健康状态
 participant L as 路由记录
 C->>R: logical_model、消息、工具、Schema、必需能力
 R->>M: 获取允许候选及能力/参数约束
 M-->>R: 连接、协议、模型、能力版本
 R->>D: 验证应用授权、数据类别、地域和批准厂商
 D-->>R: 合格候选与拒绝原因
 R->>R: 校验Streaming、工具、Schema及参数组合
 loop 每个仍合格的候选
  R->>T: 按该模型估算完整输入与预留输出
  T-->>R: Token数量、估算来源、Context是否足够
 end
 R->>H: 查询连接/模型熔断与冷却状态
 H-->>R: 可用候选
 alt 有候选
  R->>R: 按静态优先级排序并固定计划
  R->>L: 选择原因、拒绝原因、配置版本
  R-->>C: 执行计划
 else 无候选
  R->>L: 失败原因
  R-->>C: 稳定错误码，不发上游
 end
```

工具续接请求不重新选择其他连接或模型，只检查固定候选是否仍合法可用。能力未知不视为支持；Fallback不能绕过硬要求。

## 4. Streaming与Tool Calling泳道图

```mermaid
sequenceDiagram
 autonumber
 participant U as 上游流
 participant A as 协议Adapter
 participant E as Executor与流状态
 participant T as 工具参数组装器
 participant S as FastAPI SSE
 actor B as 业务应用
 U->>A: 厂商原始事件
 A->>E: 统一文本或工具增量
 alt 文本输出
  E->>E: 首次模型内容发送前标记committed
  E->>S: delta
  S-->>B: SSE文本增量
 else 工具输出
  E->>T: 按ID/index隔离组装
  T-->>E: 身份稳定的tool_call_start
  E->>E: start对外发送即committed
  E->>S: tool_call_start
  S-->>B: 工具ID与名称
  loop 各工具交错参数片段
   A->>E: tool_call_delta
   E->>T: 追加原始参数片段
   E->>S: tool_call_delta
   S-->>B: 参数字符串增量
  end
  T->>T: 完整JSON解析与工具Schema校验
  T-->>E: tool_call_end或明确错误
  E->>S: 工具完成事件或failed终态
  S-->>B: 完成或失败
 end
 alt 上游正常完成且校验通过
  E->>S: usage（若可得）与terminal completed
  S-->>B: 唯一终态
 else 提交后上游错误或参数校验失败
  E->>E: 禁止重试和Fallback
  E->>S: terminal failed，partial=true
  S-->>B: 唯一终态
 else 客户端断开
  S->>E: 取消信号
  E->>A: 关闭上游
  E->>E: 内部terminal cancelled
 end
```

正常完成、错误、取消为互斥分支。真实实现通过终态闩锁保证只产生一个terminal。心跳不提交；工具未完整不能执行。V1不发送业务structured_delta。慢客户端使用有界缓冲，超限取消，不无限积压。

## 5. 工具多轮调用泳道图

```mermaid
sequenceDiagram
 autonumber
 actor B as 上层应用与工具执行器
 participant G as Gateway
 participant S as 受限续接状态
 participant P as 固定Provider与模型
 B->>G: 第一次请求，run_id与工具定义
 G->>P: 路由选择后的调用
 P-->>G: 工具流事件及必要推理材料
 G->>S: 保存固定候选、原始续接材料、归属、TTL
 S-->>G: state_ref与tool_loop_id
 G-->>B: 工具事件、续接引用、completed/tool_calls
 B->>B: 执行完整且已校验的工具
 B->>G: 新call_id，同run/loop，assistant调用与tool结果
 G->>S: 校验归属、TTL、状态版本和工具引用
 S-->>G: 原始推理材料与固定连接/模型
 G->>G: 权限与预算复查，禁止跨候选Fallback
 G->>P: 协议还原后的第二轮请求
 P-->>G: 最终回答或下一组工具调用
 G-->>B: 结果或新一轮工具事件及续接引用
```

同一run可以有多个tool_loop_id，不能单凭run_id固定模型。第二轮允许同候选的提交前网络重试；原连接不可用则明确失败。Gateway不把推理材料解析成业务内容，不泄露至日志。

## 6. 重试、Fallback与取消边界

```mermaid
flowchart TD
 A[当前候选] --> B{次数、时间与费用预算允许?}
 B -- 否 --> X[失败终态并清理]
 B -- 是 --> C[额度检查与Attempt开始]
 C --> D[上游请求]
 D --> E{执行结果}
 E -- 成功 --> F[输出校验]
 F -- 通过 --> Y[成功终态并结算]
 F -- 不通过 --> Q[有限修复策略或失败]
 Q --> X
 E -- 取消 --> Z[关闭上游、取消终态、释放租约]
 E -- 错误 --> G{流是否已提交?}
 G -- 是 --> X
 G -- 否 --> H{同候选可重试且有额度?}
 H -- 是 --> I[可取消退避，尊重Retry-After]
 I --> B
 H -- 否 --> J{工具续接固定候选?}
 J -- 是 --> X
 J -- 否 --> K{有合格备用且允许切换?}
 K -- 是 --> L[下一个候选]
 L --> A
 K -- 否 --> X
```

有限模型修复仅在未提交、明确开启且预算允许时作为新Attempt执行；图中失败出口不代表修复成功也失败。修复详情以技术方案为准。重试关闭SDK内部自动机制，所有真实请求均可入账。

## 7. 管理热更新与Prompt刷新泳道图

```mermaid
sequenceDiagram
 actor O as 管理员或业务CI
 participant A as 管理API
 participant D as 策略数据库与审计
 participant N as 通知与周期对账
 participant W as 各Gateway实例
 participant P as Bundle制品源
 O->>A: 变更策略或手动刷新Bundle
 A->>A: 独立身份、角色及团队权限检查
 alt 策略变更
  A->>D: 校验基准版本并事务保存新版本与审计
  D->>N: 发布版本变更通知
  N->>W: 通知加载，丢失时周期对账补偿
  W->>D: 拉取完整版本快照
  W->>W: 校验后原子切换
 else Bundle刷新
  A->>W: 创建授权刷新任务
  W->>P: 允许制品源，定期或手动下载
  W->>W: hash、路径、Schema、模板白名单检查
  W->>W: 通过后原子安装版本，失败保留旧版
 end
 W-->>A: 版本与任务结果
 A-->>O: 生效状态
```

新策略/Prompt对新Call生效。在途Call固定版本；紧急连接禁用和Key吊销在后续Attempt前额外检查。V1显式指定Prompt版本，V1.1才增加标签与灰度。

## 8. 账本、成本、报表与预算告警泳道图

```mermaid
sequenceDiagram
 participant E as Executor
 participant U as Usage与价格模块
 participant L as Call/Attempt账本
 participant B as 团队预算与Outbox
 participant N as 通知Worker
 actor R as 负责人或财务
 E->>U: 每次尝试原始Usage与固定价格版本
 U->>U: 解释缓存/推理包含关系，Decimal计费
 U-->>E: 分项费用、来源、完整性
 E->>L: 持久化Attempt与Call汇总
 L->>B: 更新预算累计并判定跨阈值
 alt 跨80%或100%阈值
  B->>B: 去重并事务写Outbox
  N->>B: 领取通知任务
  N->>R: 按团队配置渠道发送
  N->>B: 送达结果，失败有限重试
 end
 R->>L: 只读报表身份查询授权团队
 L-->>R: 调用、Token、费用、失败率与未知金额标记
```

账本不依赖Trace采样；应用Key只看本应用，人员角色按团队授权。失败Attempt费用也累计，未知用量不填零。通知实现阶段使用模拟渠道，真实发送需启用对应渠道与授权。

## 9. 启动与依赖故障流程

```mermaid
flowchart TD
 A[FastAPI启动] --> B[校验静态配置]
 B --> C[连接高可用PostgreSQL与Redis]
 C --> D[加载有效策略快照和模型价格]
 D --> E[加载Bundle索引、启动轮询]
 E --> F[订阅版本通知、启动周期对账]
 F --> G{依赖与快照满足要求?}
 G -- 否 --> H[Not Ready、告警并重试]
 G -- 是 --> I[Ready接收调用]
 I --> J{运行中依赖故障}
 J -- Trace或Metrics --> K[有限缓冲，调用继续]
 J -- Redis或认证 --> L[默认拒绝新调用，仅批准的有界降级例外]
 J -- 数据库 --> M[使用有效策略快照，账本进入持久缓冲]
 M --> N{缓冲可用?}
 N -- 否 --> L
 N -- 是 --> O[继续并告警、恢复后补写]
```

Redis/PostgreSQL及账本缓冲故障导致的失败计入网关自身失败率。高可用是部署前提，仍需故障演练。所有失败路径都需要清理租约和取消上游，不因观测故障悄悄丢失计费记录。

## 10. 建议阅读顺序

1. 第1图看平台全貌与职责。
2. 第2、3图看一次调用如何准入、选择并执行。
3. 第4、5、6图看Streaming、工具循环与失败边界。
4. 第7、8、9图看无重启运维、费用治理与依赖故障。

以上图用于架构审核；字段和错误码以技术方案为准，不新增实现范围。
