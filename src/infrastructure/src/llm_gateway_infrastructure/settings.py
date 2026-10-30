"""Runtime and bootstrap configuration; domain policy lives in core."""

import json
import os
from pathlib import Path

import yaml
from llm_gateway_core.config import ConfigModel, Snapshot
from pydantic import Field


class Settings(ConfigModel):
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:55432/gateway"
    redis_url: str = "redis://localhost:56379/0"
    bootstrap_path: str = "config/development.yaml"
    bundle_root: str = "examples/prompt_bundles"
    bundle_index: str = "config/prompt-index.json"
    spool_dir: str = "var/spool"
    spool_max_bytes: int = Field(default=67108864, ge=1024)
    continuation_key: str
    continuation_ttl_s: int = Field(default=900, ge=1, le=3600)
    policy_poll_s: float = Field(default=5, gt=0, le=30)
    prompt_poll_s: float = Field(default=60, gt=0)
    snapshot_ttl_s: float = Field(default=60, gt=0, le=60)
    stream_buffer_size: int = Field(default=16, ge=1, le=128)
    backpressure_timeout_s: float = Field(default=10, gt=0)
    environment: str = "development"
    notification_webhooks: dict[str, str] = {}
    notifications_enabled: bool = False

    @classmethod
    def from_env(cls):
        fields = {
            k: os.environ[f"GATEWAY_{k.upper()}"]
            for k in cls.model_fields
            if f"GATEWAY_{k.upper()}" in os.environ
        }
        if "notification_webhooks" in fields:
            fields["notification_webhooks"] = json.loads(fields["notification_webhooks"])
        return cls.model_validate(fields)


def load_bootstrap(path: str) -> Snapshot:
    return Snapshot.model_validate(yaml.safe_load(Path(path).read_text()))
