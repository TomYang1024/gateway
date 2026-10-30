# V1 HTTP 与 Python 契约

精确字段定义见 `src/core/src/llm_gateway_core/contracts.py`；运行时 `/openapi.json` 为完整 HTTP 文档。所有应用接口使用 `Authorization: Bearer <application_key>`；管理和报表使用独立凭据。

## 调用

| 接口 | 行为 |
|---|---|
| `POST /v1/generate` | 普通、工具或非流式结构化完整响应 |
| `POST /v1/stream` | 文本及工具 SSE，预处理失败仍返回 JSON 错误 |
| `GET /v1/models` | 当前应用授权的逻辑用例和候选声明能力 |
| `GET /v1/calls` | 本应用分页检索；支持 business_ref、logical_model、status、since、until |
| `GET /v1/calls/{call_id}` | 本应用调用摘要、Attempt 和路由拒绝记录 |
| `GET /v1/reports/usage` | 本应用聚合，支持用例、模型、Prompt 版本、时间和分页 |

请求必须有 `logical_model`，`messages` 与 `prompt` 二选一。`generation` 按模型→Prompt→路由→授权调用方覆盖的顺序合并，再执行模型与应用硬限制。类型、参数、权限或上下文超限均明确拒绝。`context` 只接受业务关联字段；team/application/key 和策略版本由可信认证生成，调用方不能覆盖。

```json
{
  "logical_model": "assistant",
  "messages": [{"role":"user","content":[{"type":"text","text":"查询订单R123"}]}],
  "tools": [{"name":"lookup_order","version":"1","parameters":{"type":"object","properties":{"order_no":{"type":"string"}},"required":["order_no"],"additionalProperties":false}}],
  "tool_choice": "auto",
  "generation": {"max_output_tokens":1024},
  "execution": {"total_timeout_s":60.0,"max_total_attempts":4,"max_tool_rounds":8,"tool_loop_timeout_s":300.0,"tool_loop_max_total_attempts":32},
  "context": {"run_id":"run_order_1","business_ref":"R123"}
}
```

`requirements.structured_output` 为 `none`、`json_schema` 或 `json_object`；`json_schema` 需要请求或 Bundle 提供 `output_schema`。`json_object` 仍需要候选原生能力声明。流式请求不能使用业务结构化模式。

`execution.max_cost` 是本轮保守费用准入；路由还可设置更严格上限。`tool_loop_max_cost` 和 `tool_loop_max_total_attempts` 约束整个循环，不能通过后续轮次或重复引用扩大；共享金额预留向上取整到百万分之一币种单位。它们不构成供应商账单绝对封顶承诺。未知历史费用遇到明确费用上限时拒绝继续尝试。

## 工具与续接

非流式响应包含 `content`、已验证 `tool_calls`、`finish_reason` 和可选 `continuation`。收到 `completed` 且 `finish_reason=tool_calls` 后，上层应用负责执行工具。新请求携带完整原始历史、assistant 工具消息、每个工具结果和续接引用；多工具结果可以任意顺序回传，但不能遗漏或重复。

工具结果消息使用 `role=tool`、`tool_call_id`、`content` 和可选 `name`、`is_error`。失败由 `is_error=true` 表达。修改历史、工具定义、归属、循环或实际候选会被拒绝。后续轮允许同候选提交前重试，禁止跨候选 Fallback；原连接不可用需要上层显式开启新循环。

SDK 的 `assistant_message`、`tool_result`、`StreamRound.assistant` 保留与状态匹配的消息、原始参数及工具顺序。`StreamRound.assistant` 在流未成功完成时不可取得可执行消息。

## SSE

事件类型为 `delta`、`tool_call_start`、`tool_call_delta`、`tool_call_end`、`usage`、`terminal`。不发送业务 `structured_delta`。数据统一为：

```text
id: 1
event: delta
data: {"type":"delta","call_id":"call_...","sequence":1,"data":{"text":"你好"}}

id: 2
event: terminal
data: {"type":"terminal","call_id":"call_...","sequence":2,"data":{"status":"completed","finish_reason":"stop","partial":false,"continuation":null}}
```

工具 start 包含稳定的 `tool_call_id`、`index`、`name`；delta 包含原始 `arguments_delta`；end 包含 `parsed_arguments`、`validation_result=valid`、`executable=true`。参数未完整校验不发送可执行 end。

首个文本或工具 start 提交流。之后错误产生唯一 failed 终态，不能重试或拼接其他模型。客户端断开或背压超时会取消上游、记录 cancelled，无法保证终态仍能送达。心跳为 SSE 注释，不提交流；不支持断线续传。

## 结构化校验

支持有界 JSON Schema 子集：object/array/string/number/integer/boolean/null、properties/required/additionalProperties、items、enum/const、anyOf、无环本地 `$defs/$ref`、数值/长度/数组数量约束及描述性元数据。拒绝远程引用、递归 Schema、正则与未声明关键字；最大 Schema 64 KiB、嵌套深度 16。

校验不做字符串转数字、浮点转整数或字段补造；重复 JSON 字段和非有限数字拒绝。确定性修复仅可提取一层完整 Markdown JSON 代码围栏。模型修复必须路由和请求同时开启，最多一次，独立入账并共享次数、时间和费用预算。

## 管理和洞察

| 接口 | 权限与语义 |
|---|---|
| `GET /admin/v1/policies/current` | platform_admin 查看完整配置版本，秘密仅为引用/摘要 |
| `POST /admin/v1/policies` | `base_version`、完整 snapshot、reason；事务校验、审计、原子生效 |
| `POST /admin/v1/policies/rollback` | 历史内容发布为新版本，保留当前凭据生命周期状态 |
| `POST /admin/v1/connections/{id}` | 带基准版本紧急启停 |
| `POST /admin/v1/keys` | 为应用签发 Key，原文只返回一次；轮换允许重叠 |
| `POST /admin/v1/keys/{id}` | 启用或吊销指定应用 Key |
| `POST /admin/v1/admin-keys` / `/{id}` | 签发有期限、有限团队范围的人员/BI 凭据，支持吊销 |
| `POST /admin/v1/prompts/refresh` | platform_admin 创建异步刷新任务；只读预先挂载的制品源 |
| `GET /admin/v1/prompts/refresh/{job_id}` | 刷新任务结果及当前实例 Bundle hash |
| `GET /admin/v1/reports/usage` | 按人员授权团队查询，访问审计；支持分组、时间、分页 |
| `POST /admin/v1/cost-adjustments` | platform_admin 追加带原因和分类的对账项 |
| `GET /admin/v1/cost-adjustments` | 在报表角色团队范围内分页读取调整项，访问审计 |

普通策略发布可维护团队、应用、UseCase 授权、路由、配额、预算、数据边界和版本化价格；不需要为每个对象建立另一套事实来源。审计保存操作人、角色、原因、前后版本、变更字段路径和结果；可以从对应不可变版本复核差异。

team_report_reader、finance_report_reader 都只看显式授权团队；财务角色不默认得到跨团队数据或内容。费用以字符串输出，报表按币种分别聚合，包含未知费用/用量调用数量；对账调整不重写原始费用。

## 错误

```json
{"error":{"code":"ACCESS_DENIED","message":"Use case is not authorized","call_id":"call_...","retryable":false,"details":{}}}
```

401/403 为认证和权限；422 为输入与工具结果校验；400 为不支持的参数、Schema 组合或功能；429 为网关额度、团队/费用/循环预算；409 为续接过期、不匹配和配置基准版本冲突；502 为上游失败、输出无效、工具参数无效或尝试预算耗尽；503 为必要依赖、无可用候选；504 为调用总截止或上游超时。客户端取消在账本记录 CALL_CANCELLED，连接仍可返回时使用 499。

代码与完整 HTTP 映射由 `contracts.HTTP_CODES` 定义。SSE 已开始后的错误只通过终态表达。错误不回显业务实例、凭据或上游原始错误体。
