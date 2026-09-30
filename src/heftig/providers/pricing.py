"""Token usage of AI calls and list-price cost estimates (USD per million tokens)."""

from __future__ import annotations

import threading

# (input, output) USD per million tokens, list prices; unknown models are not priced
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # embeddings (search by meaning): input only
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}


def price(model: str | None) -> tuple[float, float] | None:
    if not model:
        return None
    return next((p for m, p in MODEL_PRICES.items() if model.startswith(m)), None)


# prompt caching (Anthropic): writing the cache costs 1.25x, reading it 0.1x the input price
CACHE_WRITE = 1.25
CACHE_READ = 0.1


def cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float | None:
    """``input_tokens`` without the cached ones, which are priced separately."""
    p = price(model)
    if p is None:
        return None
    return (
        input_tokens / 1e6 * p[0]
        + cache_write_tokens / 1e6 * p[0] * CACHE_WRITE
        + cache_read_tokens / 1e6 * p[0] * CACHE_READ
        + output_tokens / 1e6 * p[1]
    )


class UsageMeter:
    """Thread-safe token counter of one provider instance (pages may run in parallel)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cost_usd = 0.0
        self.priced = True

    def add(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """``input_tokens``: uncached input only; the counters report all input tokens."""
        c = cost(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
        with self._lock:
            self.input_tokens += input_tokens + cache_write_tokens + cache_read_tokens
            self.cache_read_tokens += cache_read_tokens
            self.output_tokens += output_tokens
            if c is None:
                self.priced = False
            else:
                self.cost_usd += c

    def snapshot(self) -> tuple[int, int, float]:
        with self._lock:
            return self.input_tokens, self.output_tokens, self.cost_usd
