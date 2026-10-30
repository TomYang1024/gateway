"""Authorized CNY 5 acceptance run, isolated from the development and production policies.

All paid requests pass FastAPI, governance, execution and the real PG/Redis ledger.
An atomic local journal reserves CNY .25 before each request, retains unknown
charges conservatively and never resets previous reservations on a rerun.
"""

import argparse
import asyncio
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import asyncpg
import httpx
from asgi_lifespan import LifespanManager
from check_provider_keys import credentials
from cryptography.fernet import Fernet
from llm_gateway_api.app import create_app
from llm_gateway_api.development import development_snapshot
from llm_gateway_core.config import Snapshot
from llm_gateway_infrastructure.settings import Settings
from llm_gateway_infrastructure.storage import Database, metadata
from llm_gateway_sdk import GatewayClient, GatewaySDKError, StreamRound, assistant_message, tool_result

JOURNAL = Path("test-results/official-acceptance.json")
DATABASE_URL = "postgresql+asyncpg://gateway:gateway@localhost:55432/gateway_official_test"
APP_KEY = "dev-app-key"
TICKET = Decimal("0.25")
LIMIT = Decimal("5")
MAX_REQUESTS = 52


def store(data):
    JOURNAL.parent.mkdir(exist_ok=True)
    temporary = JOURNAL.with_suffix(".tmp")
    with temporary.open("w") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(JOURNAL)


def reserve(vendor, case, model=None, protocol=None):
    data = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {"limit_cny": "5", "requests": []}
    used = sum((Decimal(r["reserved_cny"]) for r in data["requests"]), Decimal(0))
    if used + TICKET > LIMIT or len(data["requests"]) >= MAX_REQUESTS:
        raise RuntimeError("Approved live-test budget or finite request bound exhausted")
    data["requests"].append(
        {
            "vendor": vendor,
            "model": model,
            "protocol": protocol,
            "case": case,
            "reserved_cny": str(TICKET),
            "status": "reserved",
        }
    )
    store(data)
    return len(data["requests"]) - 1


def complete(index, result):
    data = json.loads(JOURNAL.read_text())
    entry = data["requests"][index]
    entry.update(result)
    cost = result.get("cost") or {}
    if cost.get("complete") and cost.get("amount") is not None:
        entry["reserved_cny"] = cost["amount"]
    data["known_cost_cny"] = str(
        sum((Decimal(r.get("cost", {}).get("known_amount", "0")) for r in data["requests"]), Decimal(0))
    )
    data["conservative_consumed_cny"] = str(
        sum((Decimal(r["reserved_cny"]) for r in data["requests"]), Decimal(0))
    )
    store(data)


def configuration(args):
    snapshot = development_snapshot()
    settings = {
        "glm": ("https://open.bigmodel.cn/api/paas/v4", "openai_chat", "glm-5.3", "8", "28", "2"),
        "deepseek": ("https://api.deepseek.com", "openai_responses", "deepseek-flash", "2", "8", "0.04"),
        "kimi": ("https://api.moonshot.cn/anthropic/v1", "anthropic_messages", "kimi-k3", "20", "100", "2"),
    }
    for vendor, (url, protocol, model_id, input_rate, output_rate, cache_rate) in settings.items():
        if vendor == "kimi":
            model_id = args.kimi_model
            if args.kimi_chat:
                url, protocol = "https://api.moonshot.cn/v1", "openai_chat"
            if model_id in {"kimi-k2.6", "kimi-k2.7-code"}:
                input_rate, output_rate = "6.5", "27"
                cache_rate = "1.1" if model_id == "kimi-k2.6" else "1.3"
        provider = snapshot.providers[vendor]
        provider.base_url, provider.protocol, provider.auth_type = url, protocol, "bearer"
        provider.read_idle_timeout_s = 45
        provider.first_content_timeout_s = 45
        model = snapshot.models[vendor]
        model.upstream_model = model_id
        model.context_window = 1000000
        model.max_output_tokens = 512
        if model_id in {"kimi-k2.6", "kimi-k2.7-code"}:
            model.context_window = 262144
            model.max_output_tokens = 2048
        model.defaults = {"max_output_tokens": 512}
        model.pricing.input, model.pricing.output, model.pricing.cache_read = (
            input_rate,
            output_rate,
            cache_rate,
        )
        model.pricing.version = f"official-cny-2026-10-30-{model_id}"
        model.pricing.cache_write = None  # do not invent a cache-write rate when the price is unverified
        model.capabilities.reasoning_levels = (
            ["low", "high", "max"] if vendor != "deepseek" else ["none", "low", "high", "max"]
        )
        if model_id in {"kimi-k2.6", "kimi-k2.7-code"}:
            model.capabilities.reasoning_levels = []
            model.parameter_constraints["temperature"] = {"type": "number", "enum": [1.0]}
            model.parameter_constraints["top_p"] = {"type": "number", "enum": [0.95]}
        model.capabilities.structured_output_modes = (
            ["json_object"] if vendor == "glm" else ["json_schema", "json_object"]
        )
    for route in snapshot.routes.values():
        route.max_retries_per_candidate, route.max_switches, route.max_total_attempts = 0, 0, 1
        route.max_cost = str(TICKET)
    snapshot.teams["demo"].monthly_budget = "5"
    snapshot.teams["demo"].owner = "authorized-local-acceptance"
    snapshot.teams["demo"].recipients = ["mock-only"]
    return Snapshot.model_validate(snapshot.model_dump())


async def main(args):
    for name, value in (await asyncio.to_thread(credentials)).items():
        os.environ[name] = value
    admin = await asyncpg.connect("postgresql://gateway:gateway@localhost:55432/postgres")
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname='gateway_official_test'"):
            await admin.execute("CREATE DATABASE gateway_official_test")
    finally:
        await admin.close()
    db = Database(DATABASE_URL)
    async with db.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    snapshot = configuration(args)
    await db.bootstrap(snapshot)
    version, _ = await db.current()
    await db.publish(
        snapshot,
        version,
        {"actor": "authorized-local-acceptance", "role": "platform_admin"},
        "User authorized official GLM/DeepSeek/Kimi tests, CNY 5 total",
    )
    await db.engine.dispose()
    settings = Settings(
        database_url=DATABASE_URL,
        redis_url="redis://localhost:56379/14",
        spool_dir="var/official-spool",
        continuation_key=Fernet.generate_key().decode(),
        policy_poll_s=30,
    )
    app = create_app(settings)
    async with (
        LifespanManager(app),
        GatewayClient("http://official-acceptance", APP_KEY, transport=httpx.ASGITransport(app=app)) as sdk,
    ):
        last_request_at = time.monotonic()

        async def run(vendor, case, payload, streaming=False):
            nonlocal last_request_at
            delay = args.min_interval_s - (time.monotonic() - last_request_at)
            if vendor == "kimi" and delay > 0:
                await asyncio.sleep(delay)
            model_id = snapshot.models[vendor].upstream_model
            index = await asyncio.to_thread(
                reserve, vendor, case, model_id, snapshot.providers[vendor].protocol
            )
            last_request_at = time.monotonic()
            call_id = None
            try:
                if streaming:
                    collected = StreamRound()
                    async for event in sdk.stream(payload):
                        call_id = event.call_id
                        collected.consume(event)
                    if collected.status != "completed":
                        raise GatewaySDKError(event.data.get("error", {"code": "STREAM_INTERRUPTED"}), 200)
                    result = collected
                else:
                    result = await sdk.generate(payload)
                    call_id = result.call_id
                if case == "text" and not result.content.strip():
                    raise RuntimeError("empty_text_response")
                if case == "structured" and (
                    not isinstance(result.structured_data, dict)
                    or result.structured_data.get("order_no") != "R123"
                ):
                    raise RuntimeError("structured_assertion_failed")
                if case in {"tool", "tool_stream"}:
                    generated = result.assistant.tool_calls if streaming else result.tool_calls
                    if not generated:
                        raise RuntimeError("tool_not_produced")
                row = await sdk.call(call_id)
                await asyncio.to_thread(
                    complete,
                    index,
                    {
                        "status": "passed",
                        "call_id": call_id,
                        "model": row["payload"]["actual_model"],
                        "cost": row["payload"]["cost"],
                        "usage": row["payload"]["usage"],
                    },
                )
                print(f"{vendor}/{case}: passed ({call_id})", flush=True)
                return result
            except Exception as error:
                call_id = getattr(error, "call_id", None) or call_id
                recorded = {
                    "status": "failed",
                    "error_code": getattr(error, "code", type(error).__name__),
                    "call_id": call_id,
                }
                if call_id:
                    row = await sdk.call(call_id)
                    recorded.update(cost=row["payload"]["cost"], usage=row["payload"]["usage"])
                    recorded["attempts"] = [
                        {k: a.get(k) for k in ("provider_id", "model_id", "status", "error_code")}
                        for a in row["attempts"]
                    ]
                if isinstance(error, GatewaySDKError):
                    recorded["details"] = error.details
                await asyncio.to_thread(complete, index, recorded)
                print(f"{vendor}/{case}: {recorded['error_code']} ({call_id})", flush=True)
                return None

        for vendor in args.vendors:
            base = {
                "logical_model": f"test_{vendor}",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Reply with the single word OK."}]}
                ],
                "generation": {"max_output_tokens": 512},
                "requirements": {"reasoning_level": "none" if vendor == "deepseek" else "low"},
                "execution": {"max_total_attempts": 1, "max_cost": str(TICKET), "total_timeout_s": 60.0},
            }
            if vendor == "kimi" and args.kimi_model in {"kimi-k2.6", "kimi-k2.7-code"}:
                base["generation"]["max_output_tokens"] = 2048
                base["requirements"] = {}
            if "text" in args.cases and not await run(vendor, "text", base):
                continue
            mode = "json_object" if vendor == "glm" else "json_schema"
            structured = {
                **base,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": 'Extract order R123 and return JSON only: {"order_no":"R123"}.',
                            }
                        ],
                    }
                ],
                "requirements": {**base["requirements"], "structured_output": mode},
            }
            if mode == "json_schema":
                structured["output_schema"] = {
                    "type": "object",
                    "properties": {"order_no": {"type": "string"}},
                    "required": ["order_no"],
                    "additionalProperties": False,
                }
            if "structured" in args.cases:
                await run(vendor, "structured", structured)
            tools = [
                {
                    "name": "lookup_order",
                    "description": "Read the synthetic test order status.",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_no": {"type": "string"}},
                        "required": ["order_no"],
                        "additionalProperties": False,
                    },
                }
            ]
            for streaming in (False, True):
                if ("tool_stream" if streaming else "tool") not in args.cases:
                    continue
                payload = {
                    **base,
                    "tools": tools,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Call lookup_order with order_no R123. Do not guess. Then summarize the supplied result.",
                                }
                            ],
                        }
                    ],
                }
                first = await run(vendor, "tool_stream" if streaming else "tool", payload, streaming)
                if first is None:
                    continue
                assistant = first.assistant if streaming else assistant_message(first)
                continuation = (
                    first.continuation
                    if streaming
                    else first.continuation.model_dump()
                    if first.continuation
                    else None
                )
                if not assistant.tool_calls or continuation is None:
                    print(f"{vendor}: tool request did not produce a complete tool call", flush=True)
                    continue
                second = {
                    **payload,
                    "continuation": continuation,
                    "messages": [
                        *payload["messages"],
                        assistant.model_dump(mode="json"),
                        *[
                            tool_result(t, '{"synthetic_status":"shipped"}').model_dump(mode="json")
                            for t in assistant.tool_calls
                        ],
                    ],
                }
                await run(vendor, "tool_stream_result" if streaming else "tool_result", second, streaming)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorized-budget-cny", required=True, choices=["5"])
    parser.add_argument(
        "--vendors", nargs="+", choices=["glm", "deepseek", "kimi"], default=["glm", "deepseek", "kimi"]
    )
    parser.add_argument("--kimi-chat", action="store_true")
    parser.add_argument("--min-interval-s", type=float, default=0.0)
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=["text", "structured", "tool", "tool_stream"],
        default=["text", "structured", "tool", "tool_stream"],
    )
    parser.add_argument(
        "--kimi-model", choices=["kimi-k3", "kimi-k3.1-pro", "kimi-k2.6", "kimi-k2.7-code"], default="kimi-k3"
    )
    asyncio.run(main(parser.parse_args()))
