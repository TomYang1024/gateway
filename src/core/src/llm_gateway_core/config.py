"""Validated bootstrap and immutable per-call snapshots."""

from __future__ import annotations

import hashlib
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class Capabilities(ConfigModel):
    streaming: bool = False
    tool_calling: bool = False
    streaming_tool_calls: bool = False
    parallel_tool_calls: bool = False
    structured_output_modes: list[Literal["json_schema", "json_object"]] = []
    input_modalities: list[str] = ["text"]
    reasoning_levels: list[str] = []
    reasoning_roundtrip: bool = False
    tool_loop: bool = False


class ProviderConnection(ConfigModel):
    vendor: Literal["glm", "deepseek", "moonshot"]
    protocol: Literal["openai_chat", "openai_responses", "anthropic_messages"]
    base_url: str
    secret_ref: str
    auth_type: Literal["bearer", "api_key"] | None = None
    credential_version: str = "1"
    credential_identity: str = "default"
    version: int = 1
    region: str = "cn"
    plan: Literal["payg"] = "payg"
    data_levels: list[str] = ["public"]
    enabled: bool = True
    connect_timeout_s: float = Field(default=5, gt=0)
    read_idle_timeout_s: float = Field(default=30, gt=0)
    first_content_timeout_s: float = Field(default=30, gt=0)
    max_connections: int = Field(default=100, gt=0)
    rpm: int = Field(default=1000, gt=0)
    tpm: int = Field(default=1000000, gt=0)
    concurrency: int = Field(default=100, gt=0)
    online_reserved: int = Field(default=0, ge=0)
    parameter_map: dict[str, str] = {}
    extension_parameters: list[str] = []
    extra_headers: dict[str, str] = {}

    @model_validator(mode="after")
    def valid_connection(self):
        if urlparse(self.base_url).scheme not in {"http", "https"}:
            raise ValueError("base_url must be http(s)")
        parsed = urlparse(self.base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query or fragment")
        if not self.secret_ref.startswith("env:"):
            raise ValueError("only env: secret references supported")
        if self.online_reserved >= self.concurrency:
            raise ValueError("online_reserved must be less than concurrency")
        if any(k.lower() in {"authorization", "x-api-key", "cookie"} for k in self.extra_headers):
            raise ValueError("authentication headers must use secret_ref")
        return self


class Pricing(ConfigModel):
    version: str
    currency: str = "CNY"
    unit: int = Field(default=1000000, gt=0)
    input: str = "0"
    output: str = "0"
    cache_read: str | None = None
    cache_write: str | None = None

    @model_validator(mode="after")
    def positive_prices(self):
        from decimal import Decimal

        for v in (self.input, self.output, self.cache_read, self.cache_write):
            if v is not None and (not Decimal(v).is_finite() or Decimal(v) < 0):
                raise ValueError("invalid price")
        return self


class ModelSpec(ConfigModel):
    upstream_model: str
    capabilities: Capabilities
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    parameter_constraints: dict[str, dict[str, Any]] = {}
    defaults: dict[str, Any] = {"max_output_tokens": 1024}
    pricing: Pricing


class Candidate(ConfigModel):
    provider: str
    model: str


class RoutePolicy(ConfigModel):
    candidates: list[Candidate] = Field(min_length=1)
    defaults: dict[str, Any] = {}
    data_level: str = "public"
    allowed_regions: list[str] = ["cn"]
    allowed_vendors: list[str] = ["glm", "deepseek", "moonshot"]
    max_retries_per_candidate: int = Field(default=2, ge=0, le=5)
    max_switches: int = Field(default=1, ge=0, le=1)
    initial_delay_s: float = Field(default=0.5, ge=0, le=10)
    max_delay_s: float = Field(default=5, ge=0, le=30)
    max_total_attempts: int = Field(default=4, ge=1, le=8)
    total_timeout_s: float = Field(default=60, gt=0, le=600)
    max_cost: str | None = None
    allow_model_repair: bool = False


class Team(ConfigModel):
    timezone: str = "Asia/Shanghai"
    monthly_budget: str | None = None
    currency: str = "CNY"
    budget_mode: Literal["alert", "reject"] = "alert"
    owner: str = ""
    recipients: list[str] = []
    channel: str = "mock"
    thresholds: list[int] = [80, 100]

    @model_validator(mode="after")
    def valid_budget(self):
        from decimal import Decimal

        ZoneInfo(self.timezone)
        if self.monthly_budget is not None:
            amount = Decimal(self.monthly_budget)
            if not amount.is_finite() or amount <= 0:
                raise ValueError("budget must be positive")
            if not self.owner or not self.recipients:
                raise ValueError("budget requires owner and recipients")
        if any(t <= 0 or t > 100 for t in self.thresholds):
            raise ValueError("threshold must be 1..100")
        return self


class Application(ConfigModel):
    team_id: str
    use_cases: list[str]
    rpm: int = Field(default=60, gt=0)
    tpm: int = Field(default=100000, gt=0)
    concurrency: int = Field(default=10, gt=0)
    pool: Literal["online", "batch"] = "online"
    allowed_parameters: list[str] = ["max_output_tokens", "temperature", "top_p", "stop"]
    parameter_limits: dict[str, dict[str, Any]] = {}
    enabled: bool = True


class AppKey(ConfigModel):
    application_id: str
    digest: str = Field(pattern="^[a-f0-9]{64}$")
    enabled: bool = True
    expires_at: float | None = None


class AdminKey(ConfigModel):
    actor: str
    digest: str = Field(pattern="^[a-f0-9]{64}$")
    role: Literal["platform_admin", "team_report_reader", "finance_report_reader"]
    teams: list[str] = []
    enabled: bool = True
    expires_at: float


class Snapshot(ConfigModel):
    providers: dict[str, ProviderConnection]
    models: dict[str, ModelSpec]
    routes: dict[str, RoutePolicy]
    teams: dict[str, Team]
    applications: dict[str, Application]
    keys: dict[str, AppKey]
    admins: dict[str, AdminKey]

    @model_validator(mode="after")
    def references(self):
        accounts = {}
        for provider in self.providers.values():
            account = (provider.vendor, provider.credential_identity)
            limits = (provider.rpm, provider.tpm, provider.concurrency, provider.online_reserved)
            if account in accounts and accounts[account] != limits:
                raise ValueError("Connections sharing a provider account must use the same account quota")
            accounts[account] = limits
        for route in self.routes.values():
            currencies = set()
            for c in route.candidates:
                if c.provider not in self.providers or c.model not in self.models:
                    raise ValueError("unknown candidate reference")
                currencies.add(self.models[c.model].pricing.currency)
            if len(currencies) != 1:
                raise ValueError("route currencies must match")
        for app in self.applications.values():
            if app.team_id not in self.teams or set(app.use_cases) - self.routes.keys():
                raise ValueError("unknown application team/use case")
            for use_case in app.use_cases:
                route = self.routes[use_case]
                if any(
                    self.models[c.model].pricing.currency != self.teams[app.team_id].currency
                    for c in route.candidates
                ):
                    raise ValueError("team and route currencies must match")
        if any(k.application_id not in self.applications for k in self.keys.values()):
            raise ValueError("unknown key application")
        if any(set(k.teams) - self.teams.keys() for k in self.admins.values()):
            raise ValueError("unknown management team scope")
        all_digests = [k.digest for k in self.keys.values()] + [k.digest for k in self.admins.values()]
        if len(set(all_digests)) != len(all_digests):
            raise ValueError("duplicate credential digest")
        return self
