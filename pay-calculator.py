"""
Lönespann-kalkylator för svenska sjukhusfysiker
- Läser Excel
- Bygger multivariat quantile regression (10/50/90-percentil) via statsmodels
- Modell: log(Månadslön) ~ spline(år) + kategorier (one-hot)
- Predikterar individuellt lönespann för en "person" som du anger i scriptet
- Skapar interaktiva Plotly-figurer (scatter, box, prediktionskurvor med intervallband)

This version changes the "band coloring" approach:
- Instead of coloring by CV error (which can be flat), the band is colored by LOCAL DATA SUPPORT.
- The band color reflects the local number of "peer" datapoints near each year (within ±window_years).
- A peer overlay highlights datapoints that match the profile categories (strict → relaxed if too few).
- A colorbar explains the mapping: "Datatäthet (lokalt N)".

Smoothness:
Plotly cannot gradient-fill a polygon continuously along x. We approximate a smooth transition by
splitting the band into many narrow segments (CURVE_POINTS). Increase CURVE_POINTS if needed.

Requirements:
pip install pandas openpyxl statsmodels scikit-learn plotly patsy
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import numpy as np
import pandas as pd

import statsmodels.api as sm
from patsy import dmatrix, build_design_matrices

from sklearn.model_selection import KFold

import plotly.express as px
import plotly.graph_objects as go


# =========================
# CONFIG (UI strings in Swedish; internals in English)
# =========================

EXCEL_PATH = "lon.xlsx"
SHEET_NAME = 0

COL_ROLE = "Befattning"
COL_WORKPLACE = "Arbetsplats"
COL_YEARS = "Antal hela år med arbete i klinisk verksamhet"
COL_SPECIALIST = "Specialist eller ST-fysiker?"
COL_PHD = "Forskarutbildning"
COL_SALARY = "Månadslön"

PERSON_INPUT = {
    COL_ROLE: "Sjukhusfysiker",
    COL_WORKPLACE: "Universitetssjukhus",
    COL_YEARS: 15,
    COL_SPECIALIST: "Nej",
    COL_PHD: "Nej",
    "Faktisk månadslön": 52900,
}

Q_LOW, Q_MED, Q_HIGH = 0.10, 0.50, 0.90

# Spline settings
SPLINE_DF = 4
SPLINE_DEGREE = 3

# QuantReg options
QUANTREG_MAX_ITER = 5000
QUANTREG_P_TOL = 1e-6

# Curve resolution (controls how smooth the support-colored band looks)
CURVE_POINTS = 420  # bump this up if you see "striping"

# Local support smoothing parameters
SUPPORT_WINDOW_YEARS = 3.0
SUPPORT_MIN_POINTS_IN_WINDOW = 6
SUPPORT_MIN_PROFILE_N = 20

PROFILE_FOR_CURVES = None

# Pastel palette for lines/points
PASTEL = {
    "median": "#6BAED6",
    "upper":  "#FDAE6B",
    "lower":  "#74C476",
    "data":   "rgba(120,120,120,0.20)",
    "person": "#9E9AC8",
    "actual": "#FDD0A2",
    "delta":  "rgba(120,120,120,0.8)",
    "peers":  "rgba(107,174,214,0.75)",  # blue-ish peer overlay points
}

# Support colorscale (low support -> high support)
# Chosen as single-hue blue to avoid "red=wrong" interpretation.
SUPPORT_COLORSCALE = [
    [0.00, "rgb(230,245,255)"],  # very light
    [0.50, "rgb(158,202,225)"],  # mid
    [1.00, "rgb(49,130,189)"],   # strong
]


# =========================
# Helpers (internals in English)
# =========================

def _coerce_numeric(s: pd.Series) -> pd.Series:
    """Robust numeric parsing for Swedish-style values (spaces, commas, 'kr', etc.)."""
    return pd.to_numeric(
        s.astype(str)
         .str.replace("\u00a0", " ", regex=False)
         .str.replace("kr", "", regex=False)
         .str.replace("år", "", regex=False)
         .str.replace(" ", "", regex=False)
         .str.replace(",", ".", regex=False),
        errors="coerce"
    )

def validate_columns(df: pd.DataFrame, required: list[str]) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Saknar kolumner i Excel: {missing}\nFinns: {list(df.columns)}")

def prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Select columns, parse numeric fields, trim categorical strings, drop invalid rows."""
    keep = [COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD, COL_SALARY]
    df = df[keep].copy()

    df[COL_YEARS] = _coerce_numeric(df[COL_YEARS])
    df[COL_SALARY] = _coerce_numeric(df[COL_SALARY])

    for c in [COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD]:
        df[c] = df[c].astype(str).str.strip()

    df = df.dropna(subset=[COL_YEARS, COL_SALARY])
    df = df[(df[COL_YEARS] >= 0) & (df[COL_SALARY] > 0)]
    return df

def validate_person_input(person: dict, df: pd.DataFrame) -> None:
    """Fail fast if PERSON_INPUT contains unknown categorical labels."""
    checks = [
        (COL_ROLE, df[COL_ROLE].unique()),
        (COL_WORKPLACE, df[COL_WORKPLACE].unique()),
        (COL_SPECIALIST, df[COL_SPECIALIST].unique()),
        (COL_PHD, df[COL_PHD].unique()),
    ]
    for col, valid in checks:
        if col not in person:
            raise ValueError(f"PERSON_INPUT saknar nyckel: {col}")
        if person[col] not in set(valid):
            raise ValueError(
                f"Ogiltigt värde i PERSON_INPUT för '{col}': '{person[col]}'\n"
                f"Giltiga värden i filen: {sorted(set(valid))}"
            )
    if COL_YEARS not in person:
        raise ValueError(f"PERSON_INPUT saknar nyckel: {COL_YEARS}")
    yrs = float(person[COL_YEARS])
    if not np.isfinite(yrs) or yrs < 0:
        raise ValueError(f"Ogiltigt värde i PERSON_INPUT för '{COL_YEARS}': {person[COL_YEARS]}")

def print_support_summary(person: dict, df: pd.DataFrame) -> None:
    """Print simple data support diagnostics for the requested profile."""
    role = person[COL_ROLE]
    work = person[COL_WORKPLACE]
    spec = person[COL_SPECIALIST]
    phd = person[COL_PHD]

    n_total = len(df)
    n_role = int((df[COL_ROLE] == role).sum())
    n_work = int((df[COL_WORKPLACE] == work).sum())
    n_spec = int((df[COL_SPECIALIST] == spec).sum())
    n_phd = int((df[COL_PHD] == phd).sum())

    n_combo = int((
        (df[COL_ROLE] == role) &
        (df[COL_WORKPLACE] == work) &
        (df[COL_SPECIALIST] == spec) &
        (df[COL_PHD] == phd)
    ).sum())

    print("\n=== Datastöd för vald profil ===")
    print(f"Totalt antal rader: {n_total}")
    print(f"Antal med {COL_ROLE}='{role}': {n_role}")
    print(f"Antal med {COL_WORKPLACE}='{work}': {n_work}")
    print(f"Antal med {COL_SPECIALIST}='{spec}': {n_spec}")
    print(f"Antal med {COL_PHD}='{phd}': {n_phd}")
    print(f"Antal med exakt kombination (alla fyra): {n_combo}")

class SplineBasis:
    """
    Fits a patsy bs()-basis on training years and can transform new years with identical knots.
    Explicit lower/upper bounds avoid out-of-knot issues.
    """
    def __init__(self, df_spline: int, degree: int, col_prefix: str, lower_bound: float, upper_bound: float):
        self.df_spline = df_spline
        self.degree = degree
        self.col_prefix = col_prefix
        self.lower_bound = float(lower_bound)
        self.upper_bound = float(upper_bound)

        self._design_info = None
        self._colnames = None
        self._formula = (
            f"bs(years, df={df_spline}, degree={degree}, include_intercept=False, "
            f"lower_bound={self.lower_bound}, upper_bound={self.upper_bound}) - 1"
        )

    def fit(self, years: pd.Series) -> "SplineBasis":
        tmp = pd.DataFrame({"years": years.astype(float).to_numpy()})
        mat = dmatrix(self._formula, tmp, return_type="dataframe")
        self._design_info = mat.design_info
        self._colnames = [f"{self.col_prefix}_bs{i}" for i in range(mat.shape[1])]
        return self

    def transform(self, years: pd.Series) -> pd.DataFrame:
        if self._design_info is None:
            raise RuntimeError("SplineBasis is not fitted. Call fit() first.")
        tmp = pd.DataFrame({"years": years.astype(float).to_numpy()})
        tmp["years"] = tmp["years"].clip(self.lower_bound, self.upper_bound)
        mats = build_design_matrices([self._design_info], tmp)
        arr = np.asarray(mats[0])
        return pd.DataFrame(arr, columns=self._colnames).reset_index(drop=True)

def design_matrix(
    df: pd.DataFrame,
    categories_reference: pd.DataFrame | None,
    spline_basis: SplineBasis,
) -> tuple[pd.DataFrame, pd.Series]:
    """Build design matrix X and log-salary target y_log."""
    y = pd.to_numeric(df[COL_SALARY], errors="coerce").where(lambda s: s > 0)
    y_log = np.log(y)

    X_raw = df[[COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD]].copy()

    X_spline = spline_basis.transform(X_raw[COL_YEARS])

    X_cat = X_raw.drop(columns=[COL_YEARS]).reset_index(drop=True)

    if categories_reference is not None:
        ref_cat = categories_reference.drop(columns=[COL_YEARS], errors="ignore")
        combined = pd.concat([X_cat, ref_cat], axis=0, ignore_index=True)
        X_cat_enc = pd.get_dummies(
            combined,
            columns=[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD],
            drop_first=True
        ).iloc[:len(X_cat)].copy().reset_index(drop=True)
    else:
        X_cat_enc = pd.get_dummies(
            X_cat,
            columns=[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD],
            drop_first=True
        ).reset_index(drop=True)

    X = pd.concat([X_spline, X_cat_enc], axis=1)
    X = sm.add_constant(X, has_constant="add")

    X = X.apply(pd.to_numeric, errors="coerce")
    mask = y_log.notna() & X.notna().all(axis=1)

    return X.loc[mask].astype(float), y_log.loc[mask].astype(float)

def fit_quantile_models(X: pd.DataFrame, y_log: pd.Series, qs=(Q_LOW, Q_MED, Q_HIGH)):
    """Fit separate quantile regression models in log-space."""
    models = {}
    for q in qs:
        res = sm.QuantReg(y_log, X).fit(q=q, max_iter=QUANTREG_MAX_ITER, p_tol=QUANTREG_P_TOL)
        models[q] = res
    return models

def make_person_row(
    person: dict,
    X_columns: list[str],
    reference_categories: pd.DataFrame,
    spline_basis: SplineBasis,
) -> pd.DataFrame:
    """Create a single-row X aligned with training columns."""
    person_df = pd.DataFrame([person])[[COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD]].copy()

    X_spline = spline_basis.transform(person_df[COL_YEARS])

    X_cat = person_df.drop(columns=[COL_YEARS]).copy()
    ref_cat = reference_categories.drop(columns=[COL_YEARS], errors="ignore")
    combined = pd.concat([X_cat, ref_cat], axis=0, ignore_index=True)

    X_cat_enc = pd.get_dummies(
        combined,
        columns=[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD],
        drop_first=True
    ).iloc[:1].copy().reset_index(drop=True)

    Xp = pd.concat([X_spline, X_cat_enc], axis=1)
    Xp = sm.add_constant(Xp, has_constant="add")

    Xp = Xp.reindex(columns=X_columns, fill_value=0)
    return Xp.apply(pd.to_numeric, errors="coerce").fillna(0).astype(float)

def _profile_filter_relaxed(df: pd.DataFrame, profile: dict, min_n: int = 20) -> tuple[pd.DataFrame, str]:
    """
    Filter rows to resemble the plotted profile. If too few rows, relax constraints.
    Returns (subset_df, description).
    """
    masks = [
        ("Befattning+Arbetsplats+Specialist+PhD",
         (df[COL_ROLE] == profile[COL_ROLE]) &
         (df[COL_WORKPLACE] == profile[COL_WORKPLACE]) &
         (df[COL_SPECIALIST] == profile[COL_SPECIALIST]) &
         (df[COL_PHD] == profile[COL_PHD])),
        ("Befattning+Arbetsplats+Specialist",
         (df[COL_ROLE] == profile[COL_ROLE]) &
         (df[COL_WORKPLACE] == profile[COL_WORKPLACE]) &
         (df[COL_SPECIALIST] == profile[COL_SPECIALIST])),
        ("Befattning+Specialist",
         (df[COL_ROLE] == profile[COL_ROLE]) &
         (df[COL_SPECIALIST] == profile[COL_SPECIALIST])),
        ("Endast Befattning",
         (df[COL_ROLE] == profile[COL_ROLE])),
        ("All data",
         pd.Series(True, index=df.index)),
    ]
    for desc, m in masks:
        sub = df[m].copy()
        if len(sub) >= min_n:
            return sub, desc
    return df.copy(), "All data"

def local_support_curve(
    df: pd.DataFrame,
    profile: dict,
    years_grid: np.ndarray,
    window_years: float = 3.0,
    min_points_in_window: int = 6,
    min_profile_n: int = 20,
) -> tuple[np.ndarray, pd.DataFrame, str]:
    """
    Compute a local support curve along years for the given profile.
    Support(x) = number of peer datapoints with years in [x-window, x+window].
    Returns (support_values, peer_df, support_desc).
    """
    peers, support_desc = _profile_filter_relaxed(df, profile, min_n=min_profile_n)
    yrs = peers[COL_YEARS].astype(float).to_numpy()

    support = np.zeros_like(years_grid, dtype=float)
    for i, x in enumerate(years_grid):
        mask = (yrs >= x - window_years) & (yrs <= x + window_years)
        support[i] = float(mask.sum())

    # Ensure there is at least some variation; if not, keep as-is (color will be uniform)
    # No interpolation needed; support is discrete but mapped smoothly via many segments.
    # However, we can lightly smooth it to avoid abrupt steps:
    support_sm = pd.Series(support).rolling(window=7, center=True, min_periods=1).mean().to_numpy()

    # Enforce minimum support threshold for display (optional):
    # If too low, still show it; it conveys sparseness.
    return support_sm, peers, support_desc

def _parse_rgb(rgb_str: str) -> tuple[float, float, float]:
    rgb_str = rgb_str.strip().lower().replace("rgb(", "").replace(")", "")
    r, g, b = [float(x.strip()) for x in rgb_str.split(",")]
    return r, g, b

def _interp_color(colorscale: list, t: float) -> tuple[float, float, float]:
    t = float(np.clip(t, 0.0, 1.0))
    for i in range(len(colorscale) - 1):
        p0, c0 = colorscale[i]
        p1, c1 = colorscale[i + 1]
        if t <= p1:
            if p1 == p0:
                return _parse_rgb(c1)
            u = (t - p0) / (p1 - p0)
            r0, g0, b0 = _parse_rgb(c0)
            r1, g1, b1 = _parse_rgb(c1)
            return (r0 + u * (r1 - r0), g0 + u * (g1 - g0), b0 + u * (b1 - b0))
    return _parse_rgb(colorscale[-1][1])

def normalize_support(support: np.ndarray) -> tuple[np.ndarray, float, float]:
    """
    Normalize support values to t in [0,1] using robust percentiles.
    Returns (t, lo, hi) where lo/hi are support values used for colorbar.
    """
    s = np.asarray(support, dtype=float)
    lo = float(np.nanpercentile(s, 10))
    hi = float(np.nanpercentile(s, 90))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.nanmin(s))
        hi = float(np.nanmax(s))
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            t = np.zeros_like(s, dtype=float)
            return t, 0.0, 1.0
    t = np.clip((s - lo) / (hi - lo), 0.0, 1.0)
    return t, lo, hi

def band_segment_colors_and_alpha(t: np.ndarray, alpha_min: float = 0.08, alpha_max: float = 0.28) -> list[str]:
    """
    Convert normalized t-values (0=low support, 1=high support) into RGBA fill colors.
    Uses SUPPORT_COLORSCALE and also increases alpha with support (more solid where data is dense).
    """
    cols = []
    for u in np.asarray(t, dtype=float):
        r, g, b = _interp_color(SUPPORT_COLORSCALE, float(u))
        a = float(alpha_min + u * (alpha_max - alpha_min))
        cols.append(f"rgba({r:.0f},{g:.0f},{b:.0f},{a:.3f})")
    return cols


# =========================
# Main
# =========================

def main():
    # ---- Load and clean
    df = pd.read_excel(EXCEL_PATH, sheet_name=SHEET_NAME)
    required = [COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD, COL_SALARY]
    validate_columns(df, required)

    df = prepare_dataframe(df)
    if len(df) < 30:
        raise ValueError(f"För få datapunkter efter rensning: {len(df)}")

    validate_person_input(PERSON_INPUT, df)
    print_support_summary(PERSON_INPUT, df)

    # ---- Reference categories for stable one-hot encoding
    ref_cats = df[[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD]].drop_duplicates().reset_index(drop=True)

    # ---- Global spline bounds
    years_min = float(df[COL_YEARS].min())
    years_max = float(df[COL_YEARS].max())

    spline = SplineBasis(
        SPLINE_DF, SPLINE_DEGREE,
        col_prefix=COL_YEARS,
        lower_bound=years_min,
        upper_bound=years_max
    ).fit(df[COL_YEARS])

    # ---- Fit quantile models in log space
    X, y_log = design_matrix(df, categories_reference=ref_cats, spline_basis=spline)
    models = fit_quantile_models(X, y_log, qs=(Q_LOW, Q_MED, Q_HIGH))

    # ---- Individual prediction
    Xp = make_person_row(PERSON_INPUT, X_columns=list(X.columns), reference_categories=ref_cats, spline_basis=spline)
    pred_low = float(np.exp(models[Q_LOW].predict(Xp).iloc[0]))
    pred_med = float(np.exp(models[Q_MED].predict(Xp).iloc[0]))
    pred_high = float(np.exp(models[Q_HIGH].predict(Xp).iloc[0]))

    print("\n=== Predikterat lönespann (log-lön + spline på år) ===")
    print("Input:", PERSON_INPUT)
    print(f"{int(Q_LOW*100)}e percentil: {pred_low:,.0f} kr/mån")
    print(f"{int(Q_MED*100)}e percentil: {pred_med:,.0f} kr/mån")
    print(f"{int(Q_HIGH*100)}e percentil: {pred_high:,.0f} kr/mån")

    # =========================
    # Plotly figures (UI in Swedish)
    # =========================

    # 1) Scatter: salary vs years
    fig_scatter = px.scatter(
        df,
        x=COL_YEARS,
        y=COL_SALARY,
        color=COL_SPECIALIST,
        symbol=COL_ROLE,
        hover_data=[COL_WORKPLACE, COL_PHD],
        title="Månadslön vs klinisk erfarenhet",
    )

    # 2) Box: salary distributions
    fig_box = px.box(
        df,
        x=COL_ROLE,
        y=COL_SALARY,
        color=COL_SPECIALIST,
        points="all",
        hover_data=[COL_WORKPLACE, COL_PHD, COL_YEARS],
        facet_col=COL_WORKPLACE,
        facet_col_wrap=2,
        title="Lönefördelning per befattning (facet: arbetsplats)",
    )

    # 3) Prediction curve with SUPPORT-colored band + peer overlay
    profile = PROFILE_FOR_CURVES if PROFILE_FOR_CURVES is not None else PERSON_INPUT
    years_grid = np.linspace(years_min, years_max, CURVE_POINTS)

    rows = []
    for yrs in years_grid:
        p = dict(profile)
        p[COL_YEARS] = float(yrs)
        Xg = make_person_row(p, X_columns=list(X.columns), reference_categories=ref_cats, spline_basis=spline)
        rows.append({
            "years": float(yrs),
            "q_low": float(np.exp(models[Q_LOW].predict(Xg).iloc[0])),
            "q_med": float(np.exp(models[Q_MED].predict(Xg).iloc[0])),
            "q_high": float(np.exp(models[Q_HIGH].predict(Xg).iloc[0])),
        })
    pred_df = pd.DataFrame(rows)

    # Local support curve + peer subset (strict→relaxed) for overlay points
    support_curve, peers_df, support_desc = local_support_curve(
        df=df,
        profile=profile,
        years_grid=pred_df["years"].to_numpy(),
        window_years=SUPPORT_WINDOW_YEARS,
        min_points_in_window=SUPPORT_MIN_POINTS_IN_WINDOW,
        min_profile_n=SUPPORT_MIN_PROFILE_N,
    )

    t_norm, n_lo, n_hi = normalize_support(support_curve)
    seg_colors = band_segment_colors_and_alpha(t_norm, alpha_min=0.08, alpha_max=0.30)

    fig_band = go.Figure()

    # Raw data (background)
    fig_band.add_trace(go.Scatter(
        x=df[COL_YEARS], y=df[COL_SALARY],
        mode="markers",
        name="Data",
        marker=dict(size=5, color=PASTEL["data"]),
        hoverinfo="skip",
    ))

    # Peer overlay (on top of grey points)
    # This makes it immediately obvious which datapoints are "relevant peers" to the profile.
    fig_band.add_trace(go.Scatter(
        x=peers_df[COL_YEARS],
        y=peers_df[COL_SALARY],
        mode="markers",
        name="Jämförbara datapunkter",
        marker=dict(size=7, color=PASTEL["peers"]),
        hovertemplate=(
            f"{COL_YEARS}: %{{x:.1f}}<br>"
            f"{COL_SALARY}: %{{y:,.0f}} kr/mån<br>"
            f"{COL_ROLE}: %{{customdata[0]}}<br>"
            f"{COL_WORKPLACE}: %{{customdata[1]}}<br>"
            f"{COL_SPECIALIST}: %{{customdata[2]}}<br>"
            f"{COL_PHD}: %{{customdata[3]}}<extra></extra>"
        ),
        customdata=np.stack([
            peers_df[COL_ROLE].astype(str).to_numpy(),
            peers_df[COL_WORKPLACE].astype(str).to_numpy(),
            peers_df[COL_SPECIALIST].astype(str).to_numpy(),
            peers_df[COL_PHD].astype(str).to_numpy(),
        ], axis=1),
    ))

    # Upper quantile line
    fig_band.add_trace(go.Scatter(
        x=pred_df["years"], y=pred_df["q_high"],
        mode="lines",
        name=f"{int(Q_HIGH*100)}e percentil",
        line=dict(width=2, color=PASTEL["upper"]),
    ))

    # Lower quantile line (no fill here; band is drawn as colored segments)
    fig_band.add_trace(go.Scatter(
        x=pred_df["years"], y=pred_df["q_low"],
        mode="lines",
        name=f"{int(Q_LOW*100)}e percentil",
        line=dict(width=2, color=PASTEL["lower"]),
    ))

    # Colored band segments (drawn behind the median line)
    x_arr = pred_df["years"].to_numpy()
    low_arr = pred_df["q_low"].to_numpy()
    high_arr = pred_df["q_high"].to_numpy()

    for i in range(len(x_arr) - 1):
        x0, x1 = x_arr[i], x_arr[i + 1]
        y0_low, y1_low = low_arr[i], low_arr[i + 1]
        y0_high, y1_high = high_arr[i], high_arr[i + 1]

        fig_band.add_trace(go.Scatter(
            x=[x0, x1, x1, x0],
            y=[y0_high, y1_high, y1_low, y0_low],
            mode="lines",
            line=dict(width=0),
            fill="toself",
            fillcolor=seg_colors[i],
            hoverinfo="skip",
            showlegend=False,
        ))

    # Median line (on top)
    fig_band.add_trace(go.Scatter(
        x=pred_df["years"], y=pred_df["q_med"],
        mode="lines",
        name=f"{int(Q_MED*100)}e percentil (median)",
        line=dict(width=4, color=PASTEL["median"]),
        line_shape="spline",
    ))

    # Person marker at median prediction
    fig_band.add_trace(go.Scatter(
        x=[float(PERSON_INPUT[COL_YEARS])],
        y=[pred_med],
        mode="markers",
        name="Person",
        marker=dict(size=11, color=PASTEL["person"], symbol="circle"),
    ))

    # Actual salary marker + delta line
    actual_salary = PERSON_INPUT.get("Faktisk månadslön", None)
    if actual_salary is not None and pd.notna(actual_salary):
        actual_salary = float(actual_salary)

        fig_band.add_trace(go.Scatter(
            x=[float(PERSON_INPUT[COL_YEARS])],
            y=[actual_salary],
            mode="markers",
            name="Person (faktisk lön)",
            marker=dict(size=12, color=PASTEL["actual"], symbol="diamond"),
        ))

        fig_band.add_trace(go.Scatter(
            x=[float(PERSON_INPUT[COL_YEARS]), float(PERSON_INPUT[COL_YEARS])],
            y=[pred_med, actual_salary],
            mode="lines",
            name="Avvikelse (faktisk − median)",
            line=dict(width=2, dash="dot", color=PASTEL["delta"]),
        ))

    # --- Add a colorbar (dummy trace) for local support (lokalt N) ---
    # This does not add any visual clutter in the plot, only the color scale legend.
    fig_band.add_trace(go.Scatter(
        x=[None], y=[None],
        mode="markers",
        marker=dict(
            size=0.1,
            color=[n_lo],  # dummy
            cmin=n_lo,
            cmax=n_hi,
            colorscale=SUPPORT_COLORSCALE,
            showscale=True,
            colorbar=dict(
                title="Datatäthet (lokalt N)<br><sup>fler = bättre stöd</sup>",
                thickness=14,
                len=0.55,
                y=0.55,
                x=1.02,
            ),
        ),
        hoverinfo="skip",
        showlegend=False,
    ))

    # Short profile line in Swedish UI
    profile_line = (
        f"{profile[COL_ROLE]} · {profile[COL_WORKPLACE]} · "
        f"{profile[COL_SPECIALIST]} · {COL_PHD}={profile[COL_PHD]}"
    )

    fig_band.update_layout(
        title=(
            "Predikterat lönespann över erfarenhet"
            f"<br><sup>{profile_line} · Bandfärg ≈ lokalt datastöd ({support_desc}, n={len(peers_df)})</sup>"
        ),
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", y=-0.35, x=0.5, xanchor="center", title=None),
        margin=dict(l=70, r=90, t=110, b=160),
        xaxis=dict(
            title=COL_YEARS,
            showgrid=True,
            zeroline=False,
            rangeslider=dict(visible=True, thickness=0.12),
        ),
        yaxis=dict(
            title=f"{COL_SALARY} (kr/mån)",
            showgrid=True,
            zeroline=False,
            tickformat=",.0f",
        ),
    )

    # ---- Show figures
    fig_scatter.show()
    fig_box.show()
    fig_band.show()


if __name__ == "__main__":
    main()
