"""Output shaping helpers: keep tool payloads token-bounded."""
from __future__ import annotations

DEFAULT_CAP = 50


def capped(items: list, cap: int = DEFAULT_CAP) -> tuple[list, str | None]:
    """Clip a list to ``cap`` items, returning a truncation note if clipped."""
    if len(items) <= cap:
        return items, None
    extra = len(items) - cap
    return items[:cap], f"… {extra} more (truncated)"
