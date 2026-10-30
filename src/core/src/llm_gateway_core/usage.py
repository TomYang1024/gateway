"""Token inclusion and versioned Decimal pricing rules."""

from decimal import Decimal

from llm_gateway_core.contracts import Cost, Usage


def normalize_usage(protocol, raw):
    if not raw:
        return Usage()
    if protocol == "openai_chat":
        inp, out = raw.get("prompt_tokens"), raw.get("completion_tokens")
        cached = raw.get(
            "prompt_cache_hit_tokens", raw.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        )
        reasoning = raw.get("completion_tokens_details", {}).get("reasoning_tokens")
        write = raw.get("cache_creation_input_tokens", 0)
    elif protocol == "openai_responses":
        inp, out = raw.get("input_tokens"), raw.get("output_tokens")
        cached = raw.get("input_tokens_details", {}).get("cached_tokens", 0)
        reasoning = raw.get("output_tokens_details", {}).get("reasoning_tokens")
        write = 0
    else:
        cached, write = raw.get("cache_read_input_tokens", 0), raw.get("cache_creation_input_tokens", 0)
        inp, out = raw.get("input_tokens"), raw.get("output_tokens")
        if inp is not None:
            inp += cached + write  # Messages input excludes both cache categories.
        reasoning = None
    complete = inp is not None and out is not None
    if complete and (cached + write > inp or (reasoning or 0) > out):
        complete = False
    return Usage(
        input_tokens=inp,
        output_tokens=out,
        cached_input_tokens=cached,
        cache_write_tokens=write,
        reasoning_tokens=reasoning,
        usage_source="provider",
        usage_complete=complete,
        raw_usage=raw,
    )


def price(usage, pricing):
    items = {}
    complete = usage.usage_complete
    if usage.input_tokens is not None:
        read, write = usage.cached_input_tokens or 0, usage.cache_write_tokens or 0
        regular = usage.input_tokens - read - write
        if regular < 0:
            complete = False
        else:
            items["input"] = str(Decimal(regular) * Decimal(pricing.input) / pricing.unit)
        for kind, count, rate in (
            ("cache_read", read, pricing.cache_read),
            ("cache_write", write, pricing.cache_write),
        ):
            if count:
                if rate is None:
                    complete = False
                else:
                    items[kind] = str(Decimal(count) * Decimal(rate) / pricing.unit)
    if usage.output_tokens is not None:
        # Reasoning is already included in output_tokens.
        items["output"] = str(Decimal(usage.output_tokens) * Decimal(pricing.output) / pricing.unit)
    known = sum((Decimal(v) for v in items.values()), Decimal(0))
    return Cost(
        amount=str(known) if complete else None,
        known_amount=str(known),
        currency=pricing.currency,
        pricing_version=pricing.version,
        complete=complete,
        items=items,
    )


def aggregate_usage(usages):
    if not usages:
        return Usage()
    values = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    ):
        entries = [getattr(u, name) for u in usages]
        values[name] = sum(entries) if all(v is not None for v in entries) else None
    return Usage(
        **values,
        usage_source="provider" if all(u.usage_source == "provider" for u in usages) else "unknown",
        usage_complete=all(u.usage_complete for u in usages),
    )
