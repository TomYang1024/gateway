# 三家供应商与协议验收状态

核对日期：2026-10-31。供应商身份和协议分开建模；一个协议 Adapter 可以用于多个经过批准的供应商连接。当前仅允许 GLM、DeepSeek、Moonshot/Kimi，业务套餐只接受 payg。

| 对象 | 本地验证 | 真实验证 |
|---|---|---|
| Chat Completions Adapter | 普通、文本流、工具流、缓存/推理 Usage、两轮工具与推理材料回传通过 | GLM、kimi-k2.6、kimi-k2.7-code 真实各 6 项通过；Kimi K3.1 对照调用返回 404 |
| Responses Adapter | 普通、文本流、工具流、不透明 reasoning 输出项按原顺序回传通过 | DeepSeek 真实 6 项通过 |
| Messages Adapter | 普通、文本流、工具流、thinking/signature、tool_result/is_error、两轮回传通过 | kimi-k3、kimi-k3.1-pro、kimi-k2.6 真实各 6 项通过 |
| GLM | 开发连接映射到 Chat 模拟端点 | glm-5.3 普通/JSON/工具多轮及流式通过 |
| DeepSeek | 开发连接映射到 Responses 模拟端点 | deepseek-flash 普通/原生 Schema/工具多轮及流式通过 |
| Kimi K3 | 开发连接映射到 Messages 模拟端点 | 官方 Messages 上 kimi-k3、kimi-k3.1-pro 普通/原生 Schema/工具多轮及流式各 6 项通过 |
| Kimi K2（非批准目标） | 无专用开发连接，真实验收时临时配置 | kimi-k2.6、kimi-k2.7-code 的 Chat 各 6 项通过；kimi-k2.6 的 Messages 6 项通过 |

以上模拟映射用于覆盖三协议，不构成“该厂商必定支持该端点或全部能力”的声明。不能把 `config/development.yaml` 中 mock 模型、价格、上下文限制和布尔能力复制成真实供应商配置。

## 真实联调记录

通过独立 `gateway_official_test` 数据库、Redis DB 14 及 FastAPI/治理/执行/账本完整链路测试，开发和生产路由未修改。

| 实际连接与模型 | 真实结果 |
|---|---|
| GLM 官方 Chat / glm-5.3 | 普通文本、JSON Mode、非流式工具两轮、流式工具两轮通过，共 6 项；不将 JSON Mode 声称为原生 Schema |
| DeepSeek 官方 Responses / deepseek-flash | 普通文本、原生 JSON Schema、非流式工具两轮、流式工具两轮通过，共 6 项 |
| Kimi 官方 Messages / kimi-k3 | 普通文本、原生 JSON Schema、非流式工具两轮、流式工具两轮通过，共 6 项 |
| Kimi 官方 Messages / kimi-k3.1-pro | 同上 6 项通过 |
| Kimi 官方 Messages / kimi-k2.6 | 同上 6 项通过 |
| Kimi 官方 Chat / kimi-k2.6 | 同上 6 项通过 |
| Kimi 官方 Chat / kimi-k2.7-code | 同上 6 项通过 |

K2 结果不改变已批准模型。kimi-k2.7-code 尚未做 Messages 验收，K3 系未做 Chat 验收。

Kimi 使用 [用户指定的官方快速开始](https://platform.kimi.com/docs/get-api-key) 和 [官方 Messages](https://platform.kimi.com/docs/api/messages) 端点；Messages 配置 Bearer 鉴权而非假定所有兼容协议都用 x-api-key。

官方端点资料：GLM 开放平台通用根地址见 [GLM 官方文档](https://docs.bigmodel.cn/cn/api/introduction)；DeepSeek 协议根地址见 [DeepSeek 官方入门](https://api-docs.deepseek.com/)；Kimi 国内 Chat 端点见 [Kimi 官方参考](https://platform.kimi.com/docs/api/chat)。具体模型名称、地域和能力仍需在账号授权范围内实测。保留原方案 K3 产品目标，不根据文档新出现的名称擅自切换已批准模型。

## 接入步骤

1. 提供测试 Key 的受控环境引用、批准地域、payg 账号身份、模型白名单、总测试预算和币种；不在仓库或普通日志保存 Key。
2. 新连接初始禁用，能力初始 false/空。选择实际支持的三协议之一；按厂商差异配置参数映射、额度和超时。未知能力不满足硬要求。
3. 按目标模型核对上下文、输出上限、允许参数、Schema 子集、工具多轮所需推理材料及 Usage 包含关系；价格使用批准的不可变版本。
4. 为验收创建单候选测试路由，确保自动 Fallback 不会掩盖实际被测连接。用合成输入验证文本、结构化输出、文本流和工具流。声明需要推理往返的工具模型必须完成两轮及断流验证后才能标记可用。
5. 留存 call_id、Attempt、供应商请求 ID、使用模型、Usage/价格版本、结果和失败原因，再更新能力声明。三家结果分别记录。

## 显式启用真实验收

普通测试默认跳过 `tests/live`。获得对应环境和付费测试授权后设置：

```text
GATEWAY_LIVE_AUTHORIZED=1
GATEWAY_LIVE_URL=<test gateway URL>
GATEWAY_LIVE_KEY=<application credential from secret manager>
GATEWAY_LIVE_ROUTES={"glm":"approved_glm_test","deepseek":"approved_deepseek_test","kimi":"approved_kimi_k3_test"}
GATEWAY_LIVE_TOTAL_COST=<approved positive total in route currency>
```

```sh
uv run --frozen pytest tests/live -q
```

该套件为三家各做文本和两轮合成工具验证，共最多 9 次上游请求，每轮限定一次 Attempt 并按总预算九等分作保守准入；只回传固定合成工具结果，不执行业务工具。必须确认测试路由币种一致，输入/输出保留和数据边界已经批准。供应商最终账单仍需对账。

真实流式、原生 Structured Output、特殊参数和故障测试需按声明能力补充目标账号的验收记录；未运行项保持“未验证”，不能把通用本地契约通过标为真实支持。
