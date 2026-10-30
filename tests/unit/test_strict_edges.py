import pytest
from llm_gateway_core.contracts import GatewayError, StreamEvent
from llm_gateway_core.structured import parse_json
from llm_gateway_sdk import StreamRound


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"a":{"x":1,"x":2}}', "1e999"])
def test_ambiguous_or_nonfinite_json_rejected(raw):
    with pytest.raises(GatewayError):
        parse_json(raw)


def test_float_not_coerced_to_strict_integer():
    with pytest.raises(GatewayError):
        parse_json("1.0", {"type": "integer"})


def test_stream_sdk_preserves_index_order_for_tool_continuation():
    collected = StreamRound()
    sequence = 0

    def feed(kind, data):
        nonlocal sequence
        sequence += 1
        collected.consume(StreamEvent(type=kind, data=data, call_id="call_x", sequence=sequence))

    for i in (1, 0):
        ident = f"tc_{i}"
        feed("tool_call_start", {"tool_call_id": ident, "index": i, "name": "tool"})
        feed("tool_call_delta", {"tool_call_id": ident, "arguments_delta": "{}"})
        feed("tool_call_end", {"tool_call_id": ident, "parsed_arguments": {}})
    feed("terminal", {"status": "completed", "finish_reason": "tool_calls"})
    assert [t.id for t in collected.assistant.tool_calls] == ["tc_0", "tc_1"]
