"""List-price cost of one API request, from its token counts.

Subscription usage has no published per-token weight, so list price is the
common unit: it is what makes an Opus turn and a Haiku turn comparable, and
calibration converts it to quota percentage. Prices are dollars per million
tokens. Cache writes are 1.25x input (5 minute TTL) or 2x input (1 hour); cache
reads use the model's own read price where Anthropic publishes one, else 0.1x.
"""

from __future__ import annotations

# family prefix -> (input, output, cache_read or None)
_PRICES: list[tuple[str, tuple[float, float, float | None]]] = [
    ("claude-fable-5", (10.0, 50.0, 0.25)),
    ("claude-mythos-5", (10.0, 50.0, 0.25)),
    ("claude-opus-5-5", (4.0, 20.0, 0.20)),
    ("claude-opus-5", (5.0, 25.0, None)),
    ("claude-opus-4", (5.0, 25.0, None)),
    ("claude-sonnet-5", (2.0, 10.0, 0.20)),
    ("claude-sonnet-4", (3.0, 15.0, None)),
    ("claude-haiku-5", (0.10, 0.50, None)),
    ("claude-haiku-4", (1.0, 5.0, None)),
]
# Unknown models are priced as the current Opus, the common case here.
_DEFAULT = (4.0, 20.0, 0.20)


def _rates(model: str | None) -> tuple[float, float, float]:
    m = (model or "").lower().split("[", 1)[0]
    for prefix, (inp, out, read) in _PRICES:
        if m.startswith(prefix):
            return inp, out, read if read is not None else inp * 0.1
    inp, out, read = _DEFAULT
    return inp, out, read


def cost_usd(
    model: str | None,
    tok_in: int,
    tok_out: int,
    cache_read: int,
    cache_write_5m: int,
    cache_write_1h: int,
) -> float:
    if model == "<synthetic>":
        return 0.0
    inp, out, read = _rates(model)
    total = (
        tok_in * inp
        + tok_out * out
        + cache_read * read
        + cache_write_5m * inp * 1.25
        + cache_write_1h * inp * 2.0
    )
    return total / 1_000_000
