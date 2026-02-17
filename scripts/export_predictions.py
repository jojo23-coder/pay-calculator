#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from patsy import build_design_matrices, dmatrix
from scipy.optimize import linprog


COL_ROLE = "Befattning"
COL_WORKPLACE = "Arbetsplats"
COL_YEARS = "Antal hela år med arbete i klinisk verksamhet"
COL_SPECIALIST = "Specialist eller ST-fysiker?"
COL_PHD = "Forskarutbildning"
COL_SALARY = "Månadslön"

Q_LOW = 0.10
Q_MED = 0.50
Q_HIGH = 0.90

SPLINE_DF = 4
SPLINE_DEGREE = 3
QUANTREG_MAX_ITER = 5000
QUANTREG_P_TOL = 1e-6


def _coerce_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(
        s.astype(str)
        .str.replace("\u00a0", " ", regex=False)
        .str.replace("kr", "", regex=False)
        .str.replace("år", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False),
        errors="coerce",
    )


def validate_columns(df: pd.DataFrame, required: list[str]) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Saknar kolumner i Excel: {missing}")


def prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    keep = [COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD, COL_SALARY]
    out = df[keep].copy()
    out = out.dropna(subset=[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD])
    out[COL_YEARS] = _coerce_numeric(out[COL_YEARS])
    out[COL_SALARY] = _coerce_numeric(out[COL_SALARY])
    for c in [COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD]:
        out[c] = out[c].astype(str).str.strip()
        out = out[out[c].str.lower().ne("nan") & out[c].ne("")]
    out = out.dropna(subset=[COL_YEARS, COL_SALARY])
    out = out[(out[COL_YEARS] >= 0) & (out[COL_SALARY] > 0)]
    return out


@dataclass
class SplineBasis:
    df_spline: int
    degree: int
    col_prefix: str
    lower_bound: float
    upper_bound: float

    def __post_init__(self) -> None:
        self._design_info = None
        self._colnames = None
        self._formula = (
            f"bs(years, df={self.df_spline}, degree={self.degree}, include_intercept=False, "
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
            raise RuntimeError("SplineBasis is not fitted.")
        tmp = pd.DataFrame({"years": years.astype(float).to_numpy()})
        tmp["years"] = tmp["years"].clip(self.lower_bound, self.upper_bound)
        mats = build_design_matrices([self._design_info], tmp)
        arr = np.asarray(mats[0])
        return pd.DataFrame(arr, columns=self._colnames).reset_index(drop=True)


def design_matrix(
    df: pd.DataFrame,
    categories_reference: pd.DataFrame,
    spline_basis: SplineBasis,
) -> tuple[pd.DataFrame, pd.Series]:
    y = pd.to_numeric(df[COL_SALARY], errors="coerce").where(lambda s: s > 0)
    y_log = np.log(y).reset_index(drop=True)

    x_raw = df[[COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD]].reset_index(drop=True).copy()
    x_spline = spline_basis.transform(x_raw[COL_YEARS])
    x_cat = x_raw.drop(columns=[COL_YEARS]).reset_index(drop=True)

    ref_cat = categories_reference.drop(columns=[COL_YEARS], errors="ignore")
    combined = pd.concat([x_cat, ref_cat], axis=0, ignore_index=True)
    x_cat_enc = pd.get_dummies(
        combined,
        columns=[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD],
        drop_first=True,
    ).iloc[: len(x_cat)].copy()

    x = pd.concat([x_spline, x_cat_enc.reset_index(drop=True)], axis=1)
    x = sm.add_constant(x, has_constant="add").apply(pd.to_numeric, errors="coerce")

    mask = y_log.notna() & x.notna().all(axis=1)
    return x.loc[mask].astype(float), y_log.loc[mask].astype(float)


def fit_quantile_models(x: pd.DataFrame, y_log: pd.Series) -> dict[float, object]:
    models = {}
    for q in (Q_LOW, Q_MED, Q_HIGH):
        models[q] = sm.QuantReg(y_log, x).fit(q=q, max_iter=QUANTREG_MAX_ITER, p_tol=QUANTREG_P_TOL)
    return models


def fit_quantile_models_constrained(
    x: pd.DataFrame,
    y_log: pd.Series,
    qs: tuple[float, float, float] = (Q_LOW, Q_MED, Q_HIGH),
) -> dict[float, np.ndarray]:
    """
    Joint LP fit of three quantiles with non-crossing constraints on training rows:
    X*beta_q1 <= X*beta_q2 <= X*beta_q3.
    """
    q1, q2, q3 = qs
    x_arr = x.to_numpy(dtype=float)
    y_arr = y_log.to_numpy(dtype=float)
    n, p = x_arr.shape

    # Variables: b1,b2,b3 (each p) + (u,v) residual pairs for each quantile (each n).
    n_vars = 3 * p + 6 * n
    c = np.zeros(n_vars, dtype=float)

    b1_off = 0
    b2_off = p
    b3_off = 2 * p
    u1_off = 3 * p
    v1_off = u1_off + n
    u2_off = v1_off + n
    v2_off = u2_off + n
    u3_off = v2_off + n
    v3_off = u3_off + n

    c[u1_off:v1_off] = q1
    c[v1_off:u2_off] = 1.0 - q1
    c[u2_off:v2_off] = q2
    c[v2_off:u3_off] = 1.0 - q2
    c[u3_off:v3_off] = q3
    c[v3_off:] = 1.0 - q3

    # Equality constraints: y - Xb = u - v  -> Xb + u - v = y
    a_eq = np.zeros((3 * n, n_vars), dtype=float)
    b_eq = np.concatenate([y_arr, y_arr, y_arr])
    for i in range(n):
        a_eq[i, b1_off:b2_off] = x_arr[i]
        a_eq[i, u1_off + i] = 1.0
        a_eq[i, v1_off + i] = -1.0

        r2 = n + i
        a_eq[r2, b2_off:b3_off] = x_arr[i]
        a_eq[r2, u2_off + i] = 1.0
        a_eq[r2, v2_off + i] = -1.0

        r3 = 2 * n + i
        a_eq[r3, b3_off:u1_off] = x_arr[i]
        a_eq[r3, u3_off + i] = 1.0
        a_eq[r3, v3_off + i] = -1.0

    # Inequalities for non-crossing on training rows:
    # Xb1 - Xb2 <= 0, Xb2 - Xb3 <= 0
    a_ub = np.zeros((2 * n, n_vars), dtype=float)
    b_ub = np.zeros(2 * n, dtype=float)
    for i in range(n):
        a_ub[i, b1_off:b2_off] = x_arr[i]
        a_ub[i, b2_off:b3_off] = -x_arr[i]

        r = n + i
        a_ub[r, b2_off:b3_off] = x_arr[i]
        a_ub[r, b3_off:u1_off] = -x_arr[i]

    bounds = [(None, None)] * (3 * p) + [(0.0, None)] * (6 * n)
    res = linprog(
        c=c,
        A_ub=a_ub,
        b_ub=b_ub,
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    if not res.success:
        raise RuntimeError(f"Constrained quantile LP failed: {res.message}")

    beta1 = res.x[b1_off:b2_off]
    beta2 = res.x[b2_off:b3_off]
    beta3 = res.x[b3_off:u1_off]
    return {q1: beta1, q2: beta2, q3: beta3}


def predict_log_quantile(
    model_or_beta: object,
    x_new: pd.DataFrame,
    fit_method: str,
) -> np.ndarray:
    if fit_method == "constrained":
        beta = np.asarray(model_or_beta, dtype=float)
        return x_new.to_numpy(dtype=float) @ beta
    return np.asarray(model_or_beta.predict(x_new), dtype=float)


def _profile_key(profile: dict[str, str]) -> str:
    return "||".join(
        [
            profile[COL_ROLE],
            profile[COL_WORKPLACE],
            profile[COL_SPECIALIST],
            profile[COL_PHD],
        ]
    )


def profile_matrix(
    profile: dict[str, str],
    years_grid: np.ndarray,
    x_columns: list[str],
    reference_categories: pd.DataFrame,
    spline_basis: SplineBasis,
) -> pd.DataFrame:
    n = len(years_grid)
    frame = pd.DataFrame(
        {
            COL_ROLE: [profile[COL_ROLE]] * n,
            COL_WORKPLACE: [profile[COL_WORKPLACE]] * n,
            COL_SPECIALIST: [profile[COL_SPECIALIST]] * n,
            COL_PHD: [profile[COL_PHD]] * n,
            COL_YEARS: years_grid.astype(float),
        }
    )
    x_spline = spline_basis.transform(frame[COL_YEARS])
    x_cat = frame.drop(columns=[COL_YEARS]).reset_index(drop=True)
    ref_cat = reference_categories.drop(columns=[COL_YEARS], errors="ignore")
    combined = pd.concat([x_cat, ref_cat], axis=0, ignore_index=True)
    x_cat_enc = pd.get_dummies(
        combined,
        columns=[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD],
        drop_first=True,
    ).iloc[: len(x_cat)].copy()
    x = pd.concat([x_spline, x_cat_enc.reset_index(drop=True)], axis=1)
    x = sm.add_constant(x, has_constant="add").reindex(columns=x_columns, fill_value=0)
    return x.apply(pd.to_numeric, errors="coerce").fillna(0).astype(float)


def enforce_quantile_order(q10: np.ndarray, q50: np.ndarray, q90: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Ensure non-crossing quantiles pointwise while preserving the median curve.
    This avoids visual "jumping" where q50 can otherwise switch identity.
    """
    q50c = np.asarray(q50, dtype=float)
    q10c = np.minimum(np.asarray(q10, dtype=float), q50c)
    q90c = np.maximum(np.asarray(q90, dtype=float), q50c)
    return q10c, q50c, q90c


def _profile_filter_relaxed(df: pd.DataFrame, profile: dict[str, str], min_n: int = 20) -> tuple[pd.DataFrame, str]:
    masks = [
        (
            "Befattning+Arbetsplats+Specialist+PhD",
            (df[COL_ROLE] == profile[COL_ROLE])
            & (df[COL_WORKPLACE] == profile[COL_WORKPLACE])
            & (df[COL_SPECIALIST] == profile[COL_SPECIALIST])
            & (df[COL_PHD] == profile[COL_PHD]),
        ),
        (
            "Befattning+Arbetsplats+Specialist",
            (df[COL_ROLE] == profile[COL_ROLE])
            & (df[COL_WORKPLACE] == profile[COL_WORKPLACE])
            & (df[COL_SPECIALIST] == profile[COL_SPECIALIST]),
        ),
        (
            "Befattning+Specialist",
            (df[COL_ROLE] == profile[COL_ROLE]) & (df[COL_SPECIALIST] == profile[COL_SPECIALIST]),
        ),
        ("Endast Befattning", df[COL_ROLE] == profile[COL_ROLE]),
        ("All data", pd.Series(True, index=df.index)),
    ]
    for desc, mask in masks:
        subset = df[mask].copy()
        if len(subset) >= min_n:
            return subset, desc
    return df.copy(), "All data"


def local_support_curve(
    df: pd.DataFrame,
    profile: dict[str, str],
    years_grid: np.ndarray,
    window_years: float = 3.0,
    min_profile_n: int = 20,
) -> tuple[np.ndarray, list[int], str]:
    peers, support_desc = _profile_filter_relaxed(df, profile, min_n=min_profile_n)
    yrs = peers[COL_YEARS].astype(float).to_numpy()
    support = np.zeros_like(years_grid, dtype=float)
    for i, x in enumerate(years_grid):
        support[i] = float(((yrs >= x - window_years) & (yrs <= x + window_years)).sum())
    support_sm = pd.Series(support).rolling(window=7, center=True, min_periods=1).mean().to_numpy()
    peer_indices = peers.index.astype(int).tolist()
    return support_sm, peer_indices, support_desc


def strict_peer_indices(df: pd.DataFrame, profile: dict[str, str]) -> list[int]:
    mask = (
        (df[COL_ROLE] == profile[COL_ROLE])
        & (df[COL_WORKPLACE] == profile[COL_WORKPLACE])
        & (df[COL_SPECIALIST] == profile[COL_SPECIALIST])
        & (df[COL_PHD] == profile[COL_PHD])
    )
    return df[mask].index.astype(int).tolist()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export static prediction data for the web app.")
    parser.add_argument("--excel", default="lon.xlsx", help="Path to input Excel file.")
    parser.add_argument("--sheet", default="0", help="Sheet index/name.")
    parser.add_argument("--out", default="docs/data/predictions.json", help="Output JSON path.")
    parser.add_argument("--role", default="Sjukhusfysiker", help="Filter model training/export to one role.")
    parser.add_argument(
        "--fit-method",
        choices=["independent", "constrained"],
        default="independent",
        help="Quantile fit method.",
    )
    parser.add_argument("--curve-points", type=int, default=220, help="Points per profile curve.")
    parser.add_argument("--support-window-years", type=float, default=3.0, help="Support window width in years.")
    parser.add_argument("--bootstrap-reps", type=int, default=80, help="Number of bootstrap resamples.")
    parser.add_argument("--bootstrap-seed", type=int, default=42, help="Random seed for bootstrap.")
    args = parser.parse_args()

    sheet: int | str
    sheet = int(args.sheet) if str(args.sheet).isdigit() else args.sheet

    df_raw = pd.read_excel(args.excel, sheet_name=sheet)
    required = [COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD, COL_SALARY]
    validate_columns(df_raw, required)
    df = prepare_dataframe(df_raw)
    if args.role:
        df = df[df[COL_ROLE].astype(str) == str(args.role)].copy()
    df = df.reset_index(drop=True)
    if len(df) == 0:
        raise ValueError(f"Inga rader kvar efter rollfilter: {args.role}")
    if len(df) < 30:
        raise ValueError(f"For få datapunkter efter rensning: {len(df)}")

    ref_cats = df[[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD]].drop_duplicates().reset_index(drop=True)
    years_min = float(df[COL_YEARS].min())
    years_max = float(df[COL_YEARS].max())
    years_grid = np.linspace(years_min, years_max, args.curve_points)

    spline = SplineBasis(
        SPLINE_DF,
        SPLINE_DEGREE,
        col_prefix=COL_YEARS,
        lower_bound=years_min,
        upper_bound=years_max,
    ).fit(df[COL_YEARS])

    x, y_log = design_matrix(df, categories_reference=ref_cats, spline_basis=spline)
    if args.fit_method == "constrained":
        models = fit_quantile_models_constrained(x, y_log)
    else:
        models = fit_quantile_models(x, y_log)

    def unique_sorted_str(series: pd.Series) -> list[str]:
        vals = [str(v).strip() for v in series.dropna().tolist()]
        vals = [v for v in vals if v and v.lower() != "nan"]
        return sorted(set(vals), key=lambda x: x.lower())

    options = {
        "role": unique_sorted_str(df[COL_ROLE]),
        "workplace": unique_sorted_str(df[COL_WORKPLACE]),
        "specialist": unique_sorted_str(df[COL_SPECIALIST]),
        "phd": unique_sorted_str(df[COL_PHD]),
    }

    default_profile = {
        COL_ROLE: options["role"][0],
        COL_WORKPLACE: options["workplace"][0],
        COL_SPECIALIST: options["specialist"][0],
        COL_PHD: options["phd"][0],
    }

    profile_rows = ref_cats[[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD]].drop_duplicates()
    profile_list: list[dict[str, str]] = []
    profile_x: dict[str, pd.DataFrame] = {}
    for _, row in profile_rows.iterrows():
        profile = {
            COL_ROLE: str(row[COL_ROLE]),
            COL_WORKPLACE: str(row[COL_WORKPLACE]),
            COL_SPECIALIST: str(row[COL_SPECIALIST]),
            COL_PHD: str(row[COL_PHD]),
        }
        key = _profile_key(profile)
        profile_list.append(profile)
        profile_x[key] = profile_matrix(
            profile=profile,
            years_grid=years_grid,
            x_columns=list(x.columns),
            reference_categories=ref_cats,
            spline_basis=spline,
        )

    # Bootstrap uncertainty around median curve (q50)
    rng = np.random.default_rng(args.bootstrap_seed)
    boot_q50_preds: dict[str, list[np.ndarray]] = {_profile_key(p): [] for p in profile_list}
    boot_success = 0
    for _ in range(max(0, int(args.bootstrap_reps))):
        idx = rng.integers(0, len(df), len(df))
        df_b = df.iloc[idx].copy()
        try:
            xb, yb = design_matrix(df_b, categories_reference=ref_cats, spline_basis=spline)
            xb = xb.reindex(columns=list(x.columns), fill_value=0.0).astype(float)
            if args.fit_method == "constrained":
                mb = fit_quantile_models_constrained(xb, yb)
            else:
                mb = fit_quantile_models(xb, yb)
        except Exception:
            continue
        for p in profile_list:
            key = _profile_key(p)
            xg = profile_x[key]
            pred_log = predict_log_quantile(mb[Q_MED], xg, args.fit_method)
            boot_q50_preds[key].append(np.exp(pred_log))
        boot_success += 1

    profiles = []
    for profile in profile_list:
        key = _profile_key(profile)
        xg = profile_x[key]
        q10 = np.exp(predict_log_quantile(models[Q_LOW], xg, args.fit_method))
        q50 = np.exp(predict_log_quantile(models[Q_MED], xg, args.fit_method))
        q90 = np.exp(predict_log_quantile(models[Q_HIGH], xg, args.fit_method))
        q10, q50, q90 = enforce_quantile_order(q10, q50, q90)
        support, peer_indices, support_desc = local_support_curve(
            df=df,
            profile=profile,
            years_grid=years_grid,
            window_years=args.support_window_years,
        )
        strict_indices = strict_peer_indices(df, profile)
        boot_arr = np.asarray(boot_q50_preds[key], dtype=float)
        if boot_arr.size > 0 and boot_arr.ndim == 2:
            q50_low95 = np.nanpercentile(boot_arr, 2.5, axis=0)
            q50_high95 = np.nanpercentile(boot_arr, 97.5, axis=0)
        else:
            q50_low95 = q50.copy()
            q50_high95 = q50.copy()

        profiles.append(
            {
                "role": profile[COL_ROLE],
                "workplace": profile[COL_WORKPLACE],
                "specialist": profile[COL_SPECIALIST],
                "phd": profile[COL_PHD],
                "support_desc": support_desc,
                "peer_count": int(len(peer_indices)),
                "peer_indices": peer_indices,
                "strict_peer_count": int(len(strict_indices)),
                "strict_peer_indices": strict_indices,
                "curve": {
                    "years": np.round(years_grid, 3).tolist(),
                    "q10": np.round(q10, 2).tolist(),
                    "q50": np.round(q50, 2).tolist(),
                    "q90": np.round(q90, 2).tolist(),
                    "support": np.round(support, 3).tolist(),
                },
                "bootstrap": {
                    "q50_low95": np.round(q50_low95, 2).tolist(),
                    "q50_high95": np.round(q50_high95, 2).tolist(),
                },
            }
        )

    out = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_excel": str(args.excel),
            "filtered_role": str(args.role),
            "rows_after_cleaning": int(len(df)),
            "fit_method": args.fit_method,
            "quantiles": {"low": Q_LOW, "med": Q_MED, "high": Q_HIGH},
            "quantile_postprocess": "median_preserving_non_crossing",
            "bootstrap": {
                "reps_requested": int(args.bootstrap_reps),
                "reps_used": int(boot_success),
                "interval": "95% percentile bootstrap around q50",
                "seed": int(args.bootstrap_seed),
            },
        },
        "columns": {
            "role": COL_ROLE,
            "workplace": COL_WORKPLACE,
            "years": COL_YEARS,
            "specialist": COL_SPECIALIST,
            "phd": COL_PHD,
            "salary": COL_SALARY,
        },
        "years": {
            "min": round(years_min, 3),
            "max": round(years_max, 3),
            "curve_points": int(args.curve_points),
        },
        "options": options,
        "default_profile": {
            "role": default_profile[COL_ROLE],
            "workplace": default_profile[COL_WORKPLACE],
            "specialist": default_profile[COL_SPECIALIST],
            "phd": default_profile[COL_PHD],
            "years": round((years_min + years_max) / 2.0, 1),
        },
        "profiles": profiles,
        "raw_data": {
            "years": np.round(df[COL_YEARS].astype(float).to_numpy(), 3).tolist(),
            "salary": np.round(df[COL_SALARY].astype(float).to_numpy(), 2).tolist(),
            "role": df[COL_ROLE].astype(str).tolist(),
            "workplace": df[COL_WORKPLACE].astype(str).tolist(),
            "specialist": df[COL_SPECIALIST].astype(str).tolist(),
            "phd": df[COL_PHD].astype(str).tolist(),
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out_path} with {len(profiles)} profiles.")


if __name__ == "__main__":
    main()
