"""HTTP-only SDK. Provider credentials and governance always stay on the gateway."""

from .client import GatewayClient, GatewaySDKError, StreamRound, assistant_message, tool_result

__all__ = ["GatewayClient", "GatewaySDKError", "StreamRound", "assistant_message", "tool_result"]
