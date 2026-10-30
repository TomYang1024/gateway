import json
import shutil
from pathlib import Path

import pytest
from llm_gateway_core.contracts import GatewayError, GatewayRequest, Identity
from llm_gateway_services.prompts import PromptStore


@pytest.fixture
def store(tmp_path):
    source = Path(__file__).resolve().parents[2] / "examples/prompt_bundles"
    target = tmp_path / "bundles"
    shutil.copytree(source, target)
    index = tmp_path / "index.json"
    store = PromptStore(target, index)
    _, digest = store.bundle_bytes("extract_order/1.0.0")
    index.write_text(
        json.dumps(
            [
                {
                    "artifact": "extract_order/1.0.0",
                    "hash": digest,
                    "owner_team_id": "team",
                    "applications": ["app"],
                    "use_cases": ["extract"],
                }
            ]
        )
    )
    store.refresh()
    return store


def test_bundle_hash_immutable_and_atomic(store):
    original = store.bundles
    path = store.root / "extract_order/1.0.0/user.jinja2"
    path.write_text("changed {{ order_text }}")
    with pytest.raises(GatewayError):
        store.refresh()
    assert store.bundles is original
    data = json.loads(store.index.read_text())
    _, data[0]["hash"] = store.bundle_bytes("extract_order/1.0.0")
    store.index.write_text(json.dumps(data))
    with pytest.raises(GatewayError) as exc:
        store.refresh()
    assert "immutable" in exc.value.message
    assert store.bundles is original


@pytest.mark.parametrize("path", ["../escape", "/etc", "../../../../"])
def test_artifact_path_traversal(store, path):
    with pytest.raises(GatewayError):
        store.bundle_bytes(path)


def test_artifact_symlink_rejected(store, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (store.root / "extract_order/1.0.0/link").symlink_to(outside)
    with pytest.raises(GatewayError):
        store.bundle_bytes("extract_order/1.0.0")


def test_new_version_visible_without_restarting(store):
    directory = store.root / "extract_order/1.0.1"
    shutil.copytree(store.root / "extract_order/1.0.0", directory)
    manifest = directory / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace("1.0.0", "1.0.1"))
    _, digest = store.bundle_bytes("extract_order/1.0.1")
    data = json.loads(store.index.read_text())
    data.append({**data[0], "artifact": "extract_order/1.0.1", "hash": digest})
    store.index.write_text(json.dumps(data))
    store.refresh()
    assert set(store.bundles) == {("extract_order", "1.0.0"), ("extract_order", "1.0.1")}
    identity = Identity(team_id="team", application_id="app", api_key_id="key", auth_policy_version=1)
    request = GatewayRequest.model_validate(
        {
            "logical_model": "extract",
            "prompt": {"name": "extract_order", "version": "1.0.1", "variables": {"order_text": "R123"}},
        }
    )
    messages, bundle = store.render(request, identity)
    assert bundle["hash"] == digest
    assert "R123" in messages[-1].content[0].text
