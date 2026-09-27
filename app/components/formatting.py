"""Shared number formatting so every page/table renders the same way."""


def fmt_currency(x, decimals: int = 2) -> str:
    if x is None or x != x:  # NaN check without importing pandas here
        return "--"
    return f"${x:,.{decimals}f}"


def fmt_dollars_compact(x) -> str:
    """e.g. 9,570,544,482 -> $9.57B -- for ADV columns where a Scanner table
    row full of $12,345,678,900 numbers reads as noise, not signal."""
    if x is None or x != x:
        return "--"
    abs_x = abs(x)
    if abs_x >= 1e9:
        return f"${x / 1e9:,.2f}B"
    if abs_x >= 1e6:
        return f"${x / 1e6:,.1f}M"
    if abs_x >= 1e3:
        return f"${x / 1e3:,.0f}K"
    return f"${x:,.0f}"


def fmt_pct(x, decimals: int = 1, already_pct: bool = False) -> str:
    """x is a fraction (0.25 -> 25.0%) unless already_pct=True (25 -> 25.0%)."""
    if x is None or x != x:
        return "--"
    val = x if already_pct else x * 100
    return f"{val:,.{decimals}f}%"


def fmt_delta(x, decimals: int = 3) -> str:
    if x is None or x != x:
        return "--"
    return f"{x:+.{decimals}f}"


def fmt_number(x, decimals: int = 0) -> str:
    if x is None or x != x:
        return "--"
    return f"{x:,.{decimals}f}"
