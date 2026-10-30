"""Generate local-only bootstrap artifacts; never overwrite .env or a published database policy."""

import json
from pathlib import Path

import yaml
from cryptography.fernet import Fernet
from llm_gateway_api.development import development_snapshot
from llm_gateway_services.prompts import PromptStore

root = Path(__file__).resolve().parents[1]
(root / "config").mkdir(exist_ok=True)
config = root / "config/development.yaml"
if not config.exists():
    config.write_text(
        "# MOCK ONLY: simulated capabilities, model IDs and pricing.\n"
        + yaml.safe_dump(development_snapshot().model_dump(mode="json"), allow_unicode=True, sort_keys=False)
    )
store = PromptStore(str(root / "examples/prompt_bundles"), str(root / "config/prompt-index.json"))
_, digest = store.bundle_bytes("extract_order/1.0.0")
(root / "config/prompt-index.json").write_text(
    json.dumps(
        [
            {
                "owner_team_id": "demo",
                "applications": ["demo"],
                "use_cases": ["order_extraction"],
                "artifact": "extract_order/1.0.0",
                "hash": digest,
            }
        ],
        indent=2,
    )
)
env = root / ".env"
if not env.exists():
    env.write_text(
        "GATEWAY_CONTINUATION_KEY="
        + Fernet.generate_key().decode()
        + "\n"
        + "GLM_API_KEY=local-mock-only\nDEEPSEEK_API_KEY=local-mock-only\nKIMI_API_KEY=local-mock-only\n"
    )
    env.chmod(0o600)
print("Local bootstrap ready. Application: dev-app-key; management: dev-admin-key (development only).")
