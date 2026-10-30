import json
import logging
from decimal import Decimal

import pytest
from llm_gateway_core.config import Pricing, Snapshot
from llm_gateway_core.contracts import GatewayError, GatewayRequest, Tool, Usage
from llm_gateway_core.execution import ToolAssembler
from llm_gateway_core.routing import plan
from llm_gateway_core.security import authenticate, authenticate_admin, authorize
from llm_gateway_core.structured import check_schema, parse_json
from llm_gateway_core.usage import normalize_usage, price
from llm_gateway_infrastructure.observability import JSONFormatter
from llm_gateway_services.prompts import PromptStore
from pydantic import ValidationError


@pytest.mark.parametrize(
    "patch",
    [
        {"messages": None},
        {"prompt": {"name": "x", "version": "1.0.0"}},
        {"team_id": "evil"},
        {"context": {"team_id": "evil"}},
        {"context": {"api_key_id": "evil"}},
        {"context": {"auth_policy_version": 0}},
        {"execution": {"max_total_attempts": 0}},
        {"execution": {"max_total_attempts": True}},
        {"generation": "bad"},
        {"messages": [{"role": "tool", "content": []}]},
        {"logical_model": ""},
        {"requirements": {"unknown": True}},
        {"prompt": {"name": "../x", "version": "1.0.0"}, "messages": None},
        {"prompt": {"name": "x", "version": "production"}, "messages": None},
        {"requirements": {"structured_output": "json_schema"}},
        {"tools": [{"name": "x", "parameters": {}}, {"name": "x", "parameters": {}}]},
    ],
)
def test_request_boundary(basic, patch):
    with pytest.raises(ValidationError):
        GatewayRequest.model_validate({**basic, **patch})


@pytest.mark.parametrize(
    "schema,value,code",
    [
        ({"type": "integer"}, '"1"', "OUTPUT_VALIDATION_FAILED"),
        ({"type": "integer"}, "true", "OUTPUT_VALIDATION_FAILED"),
        ({"type": "object", "required": ["a"]}, "{}", "OUTPUT_VALIDATION_FAILED"),
        ({"type": "object", "additionalProperties": False}, '{"a":1}', "OUTPUT_VALIDATION_FAILED"),
        ({"type": "object"}, "{bad", "OUTPUT_PARSE_FAILED"),
        ({"type": "object"}, "prefix {} suffix", "OUTPUT_PARSE_FAILED"),
        ({"type": "number"}, "NaN", "OUTPUT_PARSE_FAILED"),
    ],
)
def test_strict_json(schema, value, code):
    with pytest.raises(GatewayError) as error:
        parse_json(value, schema)
    assert error.value.code == code


def test_fenced_json_once():
    assert parse_json('```json\n{"a": 1}\n```', {"type": "object"}) == {"a": 1}
    with pytest.raises(GatewayError):
        parse_json("```json\n```json\n{}\n```\n```")


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://evil/schema"},
        {"pattern": "(a+)+"},
        {"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
        {"type": "bad"},
        {"$ref": "#/$defs/missing"},
    ],
)
def test_schema_subset(schema):
    with pytest.raises(GatewayError):
        check_schema(schema)


@pytest.mark.parametrize(
    "template",
    [
        "{{ x.__class__ }}",
        "{{ x['a'] }}",
        "{{ fn() }}",
        "{% for x in y %}x{% endfor %}",
        "{% import 'x' as x %}",
        "{% include 'x' %}",
        "{{ x|safe }}",
        "{% set x=1 %}",
        "{{ x * 100000000 }}",
        "{{ self }}{{ self.__init__ }}",
    ],
)
def test_template_whitelist(tmp_path, template):
    with pytest.raises(GatewayError):
        PromptStore(tmp_path, tmp_path / "index").template(template)


def test_restricted_condition(tmp_path):
    store = PromptStore(tmp_path, tmp_path / "index")
    assert (
        store.template("{% if flag %}{{ name }}{% else %}no{% endif %}").render(flag=True, name="ok") == "ok"
    )


def test_price_inclusion():
    pricing = Pricing(version="1", unit=100, input="2", output="4", cache_read="1", cache_write="3")
    u = normalize_usage(
        "openai_chat",
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 20},
            "completion_tokens_details": {"reasoning_tokens": 30},
        },
    )
    c = price(u, pricing)
    assert Decimal(c.amount) == Decimal("3.8")  # 80 regular, 20 cached, 50 total output
    assert (
        normalize_usage(
            "anthropic_messages", {"input_tokens": 80, "cache_read_input_tokens": 20, "output_tokens": 50}
        ).input_tokens
        == 100
    )


def test_unknown_cost_and_cache():
    p = Pricing(version="1", input="2", output="4")
    assert price(Usage(), p).amount is None
    assert not price(
        normalize_usage(
            "openai_chat", {"prompt_tokens": 10, "completion_tokens": 10, "prompt_cache_hit_tokens": 4}
        ),
        p,
    ).complete


@pytest.mark.parametrize(
    "change,error",
    [
        ({"enabled": False}, "DATA_POLICY_DENIED"),
        ({"region": "eu"}, "DATA_POLICY_DENIED"),
        ({"data_levels": []}, "DATA_POLICY_DENIED"),
    ],
)
def test_route_data_policy(snapshot, basic, change, error):
    req = GatewayRequest.model_validate({**basic, "logical_model": "test_glm"})
    for k, v in change.items():
        setattr(snapshot.providers["glm"], k, v)
    with pytest.raises(GatewayError) as exc:
        plan(snapshot, req, req.messages, snapshot.applications["demo"])
    assert exc.value.code == error


@pytest.mark.parametrize(
    "generation",
    [
        {"max_output_tokens": 5000},
        {"temperature": 3},
        {"temperature": "0"},
        {"max_output_tokens": True},
        {"stop": [1]},
        {"top_p": -1},
    ],
)
def test_parameters_never_clamped(snapshot, basic, generation):
    request = GatewayRequest.model_validate({**basic, "generation": generation})
    with pytest.raises(GatewayError):
        plan(snapshot, request, request.messages, snapshot.applications["demo"])


def test_merge_and_fixed_plan(snapshot, basic):
    req = GatewayRequest.model_validate({**basic, "generation": {"temperature": 0.2}})
    snapshot.models["glm"].defaults["temperature"] = 1
    snapshot.routes["assistant"].defaults["temperature"] = 0.5
    plans, _ = plan(
        snapshot, req, req.messages, snapshot.applications["demo"], prompt_defaults={"temperature": 0.8}
    )
    assert plans[0].parameters["temperature"] == 0.2
    snapshot.routes["assistant"].candidates.reverse()
    assert plans[0].provider_id == "glm"


def test_capabilities_not_invented(snapshot, basic, tools):
    req = GatewayRequest.model_validate({**basic, "tools": tools})
    snapshot.models["glm"].capabilities.streaming_tool_calls = False
    plans, rejected = plan(snapshot, req, req.messages, snapshot.applications["demo"], True)
    assert all(c.provider_id != "glm" for c in plans)
    assert rejected[0]["reason"] == "FEATURE_NOT_SUPPORTED"


def test_context_rejects_without_truncation(snapshot, basic):
    for model in snapshot.models.values():
        model.context_window = 50
    request = GatewayRequest.model_validate(basic)
    with pytest.raises(GatewayError) as exc:
        plan(snapshot, request, request.messages, snapshot.applications["demo"])
    assert exc.value.code == "CONTEXT_BUDGET_EXCEEDED"


def test_credentials_separated(snapshot):
    with pytest.raises(GatewayError):
        authenticate(snapshot, 1, "dev-admin-key")
    with pytest.raises(GatewayError):
        authenticate_admin(snapshot, "dev-app-key")
    identity = authenticate(snapshot, 1, "dev-app-key")
    snapshot.keys["demo"].enabled = False
    with pytest.raises(GatewayError):
        authorize(snapshot, identity, "assistant")


def test_coding_plan_rejected(snapshot):
    value = snapshot.model_dump()
    value["providers"]["glm"]["plan"] = "coding"
    with pytest.raises(ValidationError):
        Snapshot.model_validate(value)


def test_tool_interleaving_identity_and_argument_fragments(tools):
    assembler = ToolAssembler([Tool.model_validate(t) for t in tools])
    events = []
    for index, ident, name in (
        (0, "tc_", "lookup_"),
        (1, "other_", "look"),
        (0, "1", "order"),
        (1, "2", "up_order"),
    ):
        assert (
            assembler.feed(
                {
                    "kind": "tool_fragment",
                    "index": index,
                    "id": ident,
                    "name": name,
                    "arguments": "",
                    "stable": False,
                }
            )
            == []
        )
    raw = '{"order_no":"你好R123"}'
    for char in raw:
        for index in (1, 0):
            events.extend(
                assembler.feed(
                    {
                        "kind": "tool_fragment",
                        "index": index,
                        "id": "",
                        "name": "",
                        "arguments": char,
                        "stable": True,
                    }
                )
            )
    assert [d["tool_call_id"] for kind, d in events if kind == "tool_call_start"] == ["other_2", "tc_1"]
    for index in (1, 0):
        events.extend(assembler.feed({"kind": "tool_done", "index": index}))
    assert len(assembler.calls()) == 2
    assert all(t.arguments == {"order_no": "你好R123"} for t in assembler.calls())


@pytest.mark.parametrize("raw", ['{"order_no":1}', '{"extra":1}', '{"order_no":', "[]"])
def test_invalid_tools_not_executable(tools, raw):
    assembler = ToolAssembler([Tool.model_validate(t) for t in tools])
    assembler.feed(
        {
            "kind": "tool_fragment",
            "index": 0,
            "id": "tc",
            "name": "lookup_order",
            "arguments": raw,
            "stable": True,
        }
    )
    with pytest.raises(GatewayError):
        assembler.feed({"kind": "tool_done", "index": 0})
    with pytest.raises(GatewayError):
        assembler.calls()


def test_log_does_not_include_sensitive_extra():
    record = logging.LogRecord("llm_gateway", logging.ERROR, "", 1, "gateway.call.failed", (), None)
    record.context = {"call_id": "call_1", "Authorization": "Bearer SECRET", "output": "private"}
    encoded = JSONFormatter().format(record)
    assert "SECRET" not in encoded and "private" not in encoded
    assert json.loads(encoded)["call_id"] == "call_1"
