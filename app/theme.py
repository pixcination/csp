"""
Single source of truth for the app's color palette -- referenced by every
Plotly chart builder in app/components/charts.py so charts, tables, and the
Streamlit theme (.streamlit/config.toml) all draw from the same values
instead of each page picking its own colors ad hoc.

Palette validated with the dataviz skill's contrast/CVD-separation checker
(dark mode, surface #1a1a19) -- categorical ordering is the CVD-safety
mechanism, not cosmetic, so don't reorder CATEGORICAL without re-validating.
"""

APP_NAME = "CSP Wheel Analyzer"

# === Surfaces & ink ===
PAGE_PLANE = "#0D0D0D"
CHART_SURFACE = "#1A1A19"
PRIMARY_INK = "#FFFFFF"
SECONDARY_INK = "#C3C2B7"
MUTED_INK = "#898781"
GRIDLINE = "#2C2C2A"
BASELINE = "#383835"

# === Categorical series (fixed order -- do not cycle or reorder) ===
CATEGORICAL = [
    "#3987E5",  # 1 blue     -- primary series / price
    "#199E70",  # 2 aqua     -- premium / credit
    "#C98500",  # 3 yellow   -- IV
    "#008300",  # 4 green    -- realized vol
    "#9085E9",  # 5 violet   -- theta
    "#E66767",  # 6 red      -- downside / loss
    "#D55181",  # 7 magenta  -- gamma
    "#D95926",  # 8 orange   -- vega
]

# === Status palette (fixed -- never reused for series identity) ===
STATUS = {
    "good": "#0CA30C",
    "warning": "#FAB219",
    "serious": "#EC835A",
    "critical": "#D03B3B",
}

# === Diverging pair (P&L, price-vs-strike, above/below breakeven) ===
DIVERGING_POSITIVE = "#3987E5"  # blue
DIVERGING_NEGATIVE = "#E66767"  # red
DIVERGING_MIDPOINT = "#383835"  # neutral gray

# === Sequential ramp (single hue, light->dark) for magnitude/heatmap use ===
SEQUENTIAL_BLUE = ["#CDE2FB", "#9EC5F4", "#5598E7", "#2A78D6", "#184F95", "#0D366B"]

PLOTLY_TEMPLATE = {
    "layout": {
        "paper_bgcolor": CHART_SURFACE,
        "plot_bgcolor": CHART_SURFACE,
        "font": {"color": PRIMARY_INK, "family": "system-ui, -apple-system, 'Segoe UI', sans-serif"},
        "colorway": CATEGORICAL,
        "xaxis": {"gridcolor": GRIDLINE, "linecolor": BASELINE, "zerolinecolor": BASELINE},
        "yaxis": {"gridcolor": GRIDLINE, "linecolor": BASELINE, "zerolinecolor": BASELINE},
        "legend": {"bgcolor": "rgba(0,0,0,0)"},
        "hoverlabel": {"bgcolor": PAGE_PLANE, "font": {"color": PRIMARY_INK}},
    }
}
