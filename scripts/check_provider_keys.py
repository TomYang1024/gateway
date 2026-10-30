"""Read-only official model-list checks. Never sends generation requests or prints credentials."""

import asyncio
import json
import os
import shlex
from pathlib import Path

import httpx

ENDPOINTS = {
    "glm": ("GLM_API_KEY", "https://open.bigmodel.cn/api/paas/v4/models"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/models"),
    "kimi": ("KIMI_API_KEY", "https://api.moonshot.cn/v1/models"),
}


def credentials():
    values = {}
    path = Path(".env")
    if path.exists():
        for line in path.read_text().splitlines():
            name, separator, value = line.removeprefix("export ").partition("=")
            if separator and name.strip() in {v[0] for v in ENDPOINTS.values()}:
                parsed = shlex.split(value.strip(), comments=True)
                values[name.strip()] = parsed[0] if len(parsed) == 1 else ""
    for name, _ in ENDPOINTS.values():
        if os.environ.get(name):
            values[name] = os.environ[name]
    return values


async def check(vendor, variable, endpoint, values):
    key = values.get(variable)
    result = {"vendor": vendor, "endpoint": endpoint, "key_configured": bool(key)}
    if not key or key in {"local-mock-only", "REPLACE_WITH_API_KEY"}:
        return {**result, "status": "missing_or_placeholder"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = await client.get(endpoint, headers={"Authorization": f"Bearer {key}"})
        result["http_status"] = response.status_code
        if response.status_code == 200:
            body = response.json()
            data = body.get("data", [])
            models = [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]
            result.update(status="model_list_received", models=models, model_count=len(models))
        else:
            result["status"] = (
                "authentication_rejected" if response.status_code in (401, 403) else "endpoint_unverified"
            )
    except Exception as error:
        result.update(status="connection_unverified", error_type=type(error).__name__)
    return result


async def main():
    values = await asyncio.to_thread(credentials)
    results = await asyncio.gather(*(check(vendor, *args, values) for vendor, args in ENDPOINTS.items()))
    await asyncio.to_thread(save, results)
    print(json.dumps(results, ensure_ascii=False, indent=2))


def save(results):
    Path("test-results").mkdir(exist_ok=True)
    Path("test-results/official-model-check.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(main())
