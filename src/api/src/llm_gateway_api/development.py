"""Explicit simulated capabilities and prices; never use as a real-provider capability claim."""

import time

from llm_gateway_core.config import Snapshot, secret_hash


def development_snapshot(base="http://mock:8001"):
    providers, models = {}, {}
    for vendor, protocol in (
        ("glm", "openai_chat"),
        ("deepseek", "openai_responses"),
        ("moonshot", "anthropic_messages"),
    ):
        ident = "kimi" if vendor == "moonshot" else vendor
        providers[ident] = {
            "vendor": vendor,
            "protocol": protocol,
            "base_url": f"{base}/v1",
            "secret_ref": f"env:{ident.upper()}_API_KEY",
            "data_levels": ["public", "internal"],
            "rpm": 10000,
            "tpm": 10000000,
            "concurrency": 100,
            "parameter_map": {"stop": "stop_sequences"} if ident == "kimi" else {},
        }
        models[ident] = {
            "upstream_model": f"mock-{ident}",
            "context_window": 32768,
            "max_output_tokens": 4096,
            "capabilities": {
                "streaming": True,
                "tool_calling": True,
                "streaming_tool_calls": True,
                "parallel_tool_calls": True,
                "structured_output_modes": ["json_schema"],
                "tool_loop": True,
                "reasoning_roundtrip": True,
            },
            "defaults": {"max_output_tokens": 1024},
            "parameter_constraints": {
                "max_output_tokens": {"type": "integer", "minimum": 1, "maximum": 4096},
                "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                "top_p": {"type": "number", "minimum": 0, "maximum": 1},
                "stop": {"type": "array", "maxItems": 4},
            },
            "pricing": {
                "version": "mock-price-1",
                "currency": "CNY",
                "input": "1",
                "output": "2",
                "cache_read": "0.1",
                "cache_write": "1",
            },
        }
    candidates = [{"provider": v, "model": v} for v in providers]
    return Snapshot.model_validate(
        {
            "providers": providers,
            "models": models,
            "routes": {
                "order_extraction": {"candidates": candidates},
                "assistant": {"candidates": candidates},
                **{f"test_{v}": {"candidates": [{"provider": v, "model": v}]} for v in providers},
                **{
                    v: {"candidates": [{"provider": v, "model": v}], "max_retries_per_candidate": 3}
                    for v in providers
                },
            },
            "teams": {"demo": {}},
            "applications": {
                "demo": {
                    "team_id": "demo",
                    "use_cases": [
                        "order_extraction",
                        "assistant",
                        *[f"test_{v}" for v in providers],
                        *providers,
                    ],
                    "rpm": 10000,
                    "tpm": 10000000,
                }
            },
            "keys": {"demo": {"application_id": "demo", "digest": secret_hash("dev-app-key")}},
            "admins": {
                "demo": {
                    "actor": "local-developer",
                    "role": "platform_admin",
                    "teams": ["demo"],
                    "digest": secret_hash("dev-admin-key"),
                    "expires_at": time.time() + 86400 * 365,
                }
            },
        }
    )
