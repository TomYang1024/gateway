import copy

import pytest
from test_gateway import publish

pytestmark = pytest.mark.integration


async def test_connections_share_provider_account_quota(client, basic, upstream):
    def configure(snapshot):
        snapshot["providers"]["glm"]["rpm"] = 1
        alias = copy.deepcopy(snapshot["providers"]["glm"])
        alias["protocol"] = "openai_responses"
        snapshot["providers"]["glm_alias"] = alias
        snapshot["routes"]["test_glm_alias"] = {"candidates": [{"provider": "glm_alias", "model": "glm"}]}
        snapshot["applications"]["demo"]["use_cases"].append("test_glm_alias")

    await publish(client, configure)
    first = await client.post("/v1/generate", json={**basic, "logical_model": "test_glm"})
    assert first.status_code == 200
    second = await client.post("/v1/generate", json={**basic, "logical_model": "test_glm_alias"})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "GATEWAY_RATE_LIMITED"
    assert len(upstream.requests) == 1
