#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import statsmodels.api as sm
from plotly.subplots import make_subplots

try:
    from scripts.export_predictions import (
        COL_PHD,
        COL_ROLE,
        COL_SALARY,
        COL_SPECIALIST,
        COL_WORKPLACE,
        COL_YEARS,
        Q_HIGH,
        Q_LOW,
        Q_MED,
        SPLINE_DEGREE,
        SPLINE_DF,
        SplineBasis,
        design_matrix,
        fit_quantile_models,
        predict_log_quantile,
        prepare_dataframe,
        validate_columns,
    )
except ModuleNotFoundError:
    from export_predictions import (  # type: ignore
        COL_PHD,
        COL_ROLE,
        COL_SALARY,
        COL_SPECIALIST,
        COL_WORKPLACE,
        COL_YEARS,
        Q_HIGH,
        Q_LOW,
        Q_MED,
        SPLINE_DEGREE,
        SPLINE_DF,
        SplineBasis,
        design_matrix,
        fit_quantile_models,
        predict_log_quantile,
        prepare_dataframe,
        validate_columns,
    )


def parse_sheet(value: str) -> int | str:
    return int(value) if str(value).isdigit() else value


def build_aligned_design_matrix(
    df: pd.DataFrame,
    reference_categories: pd.DataFrame,
    spline_basis: SplineBasis,
    x_columns: list[str],
) -> pd.DataFrame:
    x_raw = df[[COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD]].reset_index(drop=True).copy()
    x_spline = spline_basis.transform(x_raw[COL_YEARS])
    x_cat = x_raw.drop(columns=[COL_YEARS]).reset_index(drop=True)
    ref_cat = reference_categories.drop(columns=[COL_YEARS], errors="ignore")
    combined = pd.concat([x_cat, ref_cat], axis=0, ignore_index=True)
    x_cat_enc = pd.get_dummies(
        combined,
        columns=[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD],
        drop_first=True,
    ).iloc[: len(x_cat)].copy()
    x = pd.concat([x_spline, x_cat_enc.reset_index(drop=True)], axis=1)
    x = sm.add_constant(x, has_constant="add").reindex(columns=x_columns, fill_value=0.0)
    return x.apply(pd.to_numeric, errors="coerce").fillna(0.0).astype(float)


def percentile_rank(reference_sorted: np.ndarray, value: float) -> float:
    return float(100.0 * np.searchsorted(reference_sorted, value, side="right") / len(reference_sorted))


def ecdf(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if len(values) == 0:
        return np.array([], dtype=float), np.array([], dtype=float)
    xs = np.sort(values.astype(float))
    ys = np.arange(1, len(xs) + 1, dtype=float) / len(xs)
    return xs, ys


def safe_median(values: pd.Series) -> float | None:
    if len(values) == 0:
        return None
    out = float(np.median(values.to_numpy(dtype=float)))
    return out if np.isfinite(out) else None


def format_money(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:,.0f} kr".replace(",", " ")


def identify_unseen_categories(
    cohort_df: pd.DataFrame,
    national_df: pd.DataFrame,
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for col in [COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD]:
        cohort_vals = {str(v).strip() for v in cohort_df[col].dropna().tolist()}
        national_vals = {str(v).strip() for v in national_df[col].dropna().tolist()}
        unseen = sorted(v for v in cohort_vals if v and v not in national_vals)
        if unseen:
            out[col] = unseen
    return out


def build_gap_groups(df: pd.DataFrame) -> tuple[str, dict[str, list[float]]]:
    phd_yes_mask = df[COL_PHD].astype(str).str.strip().str.lower().eq("ja")
    specialist_yes_mask = df[COL_SPECIALIST].astype(str).str.strip().str.lower().eq("specialist")

    groups = {
        "Forskarutbildning = Ja": df.loc[phd_yes_mask, "actual_minus_pred50"].astype(float).to_numpy().tolist(),
        "Specialist = Ja": df.loc[specialist_yes_mask, "actual_minus_pred50"].astype(float).to_numpy().tolist(),
    }
    groups = {k: [float(v) for v in vals] for k, vals in groups.items()}
    return "Indikator", groups


def build_group_rankings(df: pd.DataFrame) -> dict[str, list[dict[str, float | int | str]]]:
    def summarize_by(series: pd.Series, label: str) -> list[dict[str, float | int | str]]:
        rows: list[dict[str, float | int | str]] = []
        for grp, sub in df.groupby(series):
            if len(sub) == 0:
                continue
            rows.append(
                {
                    "group": str(grp),
                    "n": int(len(sub)),
                    "median_percentile": float(np.median(sub["pred_q50_percentile"].to_numpy(dtype=float))),
                    "median_gap": float(np.median(sub["actual_minus_pred50"].to_numpy(dtype=float))),
                }
            )
        rows.sort(key=lambda r: float(r["median_percentile"]), reverse=True)
        return rows

    phd_group = np.where(
        df[COL_PHD].astype(str).str.strip().str.lower().eq("ja"),
        "Ja",
        "Övrigt",
    )
    specialist_group = np.where(
        df[COL_SPECIALIST].astype(str).str.strip().str.lower().eq("specialist"),
        "Ja",
        "Övrigt",
    )
    workplace_group = df[COL_WORKPLACE].astype(str)

    return {
        "Forskarutbildning": summarize_by(pd.Series(phd_group, index=df.index), "Forskarutbildning"),
        "Specialist": summarize_by(pd.Series(specialist_group, index=df.index), "Specialist"),
        "Arbetsplats": summarize_by(workplace_group, "Arbetsplats"),
    }


def build_typical_profile_curve(
    national_df: pd.DataFrame,
    years_grid: np.ndarray,
    reference_categories: pd.DataFrame,
    spline_basis: SplineBasis,
    x_columns: list[str],
    model_q50: object,
) -> np.ndarray:
    mode_role = str(national_df[COL_ROLE].mode().iloc[0])
    mode_workplace = str(national_df[COL_WORKPLACE].mode().iloc[0])
    mode_specialist = str(national_df[COL_SPECIALIST].mode().iloc[0])
    mode_phd = str(national_df[COL_PHD].mode().iloc[0])
    frame = pd.DataFrame(
        {
            COL_ROLE: [mode_role] * len(years_grid),
            COL_WORKPLACE: [mode_workplace] * len(years_grid),
            COL_YEARS: years_grid.astype(float),
            COL_SPECIALIST: [mode_specialist] * len(years_grid),
            COL_PHD: [mode_phd] * len(years_grid),
            COL_SALARY: [1.0] * len(years_grid),
        }
    )
    x_typical = build_aligned_design_matrix(frame, reference_categories, spline_basis, x_columns)
    return np.exp(predict_log_quantile(model_q50, x_typical, fit_method="independent"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare a cohort against national predicted salaries and generate an interactive HTML dashboard."
    )
    parser.add_argument("--national-excel", default="lon.xlsx", help="Path to national input Excel file.")
    parser.add_argument("--cohort-excel", default=None, help="Optional cohort Excel path. If omitted, a demo cohort is sampled.")
    parser.add_argument("--sheet", default="0", help="Sheet index or sheet name.")
    parser.add_argument("--role", default="Sjukhusfysiker", help="Role filter for baseline and cohort.")
    parser.add_argument("--sample-size", type=int, default=18, help="Demo sample size if cohort file is not provided.")
    parser.add_argument("--sample-seed", type=int, default=42, help="Random seed for deterministic demo sampling.")
    parser.add_argument("--out-demo-excel", default="docs/data/demo_18.xlsx", help="Output demo Excel path.")
    parser.add_argument(
        "--out-json",
        default="docs/data/population_report.json",
        help="Output JSON data for the population dashboard.",
    )
    parser.add_argument("--out-html", default="docs/data/demo_vs_national_report.html", help="Output HTML report path.")
    args = parser.parse_args()

    sheet = parse_sheet(args.sheet)
    required = [COL_ROLE, COL_WORKPLACE, COL_YEARS, COL_SPECIALIST, COL_PHD, COL_SALARY]

    nat_raw = pd.read_excel(args.national_excel, sheet_name=sheet)
    validate_columns(nat_raw, required)
    national = prepare_dataframe(nat_raw)
    national = national[national[COL_ROLE].astype(str) == str(args.role)].copy().reset_index(drop=True)
    if len(national) < 30:
        raise ValueError(f"For få nationella datapunkter efter rensning/rollfilter ({args.role}): {len(national)}")

    if args.cohort_excel:
        cohort_source = str(args.cohort_excel)
        cohort_raw = pd.read_excel(args.cohort_excel, sheet_name=sheet)
        validate_columns(cohort_raw, required)
        cohort = prepare_dataframe(cohort_raw)
        cohort = cohort[cohort[COL_ROLE].astype(str) == str(args.role)].copy().reset_index(drop=True)
        if len(cohort) == 0:
            raise ValueError("Cohort-filen gav 0 giltiga rader efter rensning och rollfilter.")
    else:
        if args.sample_size <= 0:
            raise ValueError("--sample-size måste vara >= 1.")
        if args.sample_size > len(national):
            raise ValueError(f"--sample-size ({args.sample_size}) är större än antal nationella rader ({len(national)}).")
        cohort = national.sample(n=args.sample_size, random_state=args.sample_seed, replace=False).copy().reset_index(drop=True)
        out_demo = Path(args.out_demo_excel)
        out_demo.parent.mkdir(parents=True, exist_ok=True)
        cohort.to_excel(out_demo, index=False)
        cohort_source = str(out_demo)

    ref_cats = national[[COL_ROLE, COL_WORKPLACE, COL_SPECIALIST, COL_PHD]].drop_duplicates().reset_index(drop=True)
    years_min = float(national[COL_YEARS].min())
    years_max = float(national[COL_YEARS].max())

    spline = SplineBasis(
        SPLINE_DF,
        SPLINE_DEGREE,
        col_prefix=COL_YEARS,
        lower_bound=years_min,
        upper_bound=years_max,
    ).fit(national[COL_YEARS])

    x_nat_train, y_nat_train = design_matrix(national, categories_reference=ref_cats, spline_basis=spline)
    models = fit_quantile_models(x_nat_train, y_nat_train)
    x_columns = list(x_nat_train.columns)

    x_nat = build_aligned_design_matrix(national, ref_cats, spline, x_columns)
    x_cohort = build_aligned_design_matrix(cohort, ref_cats, spline, x_columns)

    national["pred_q10"] = np.exp(predict_log_quantile(models[Q_LOW], x_nat, fit_method="independent"))
    national["pred_q50"] = np.exp(predict_log_quantile(models[Q_MED], x_nat, fit_method="independent"))
    national["pred_q90"] = np.exp(predict_log_quantile(models[Q_HIGH], x_nat, fit_method="independent"))

    cohort["pred_q10"] = np.exp(predict_log_quantile(models[Q_LOW], x_cohort, fit_method="independent"))
    cohort["pred_q50"] = np.exp(predict_log_quantile(models[Q_MED], x_cohort, fit_method="independent"))
    cohort["pred_q90"] = np.exp(predict_log_quantile(models[Q_HIGH], x_cohort, fit_method="independent"))
    cohort["actual_minus_pred50"] = cohort[COL_SALARY].astype(float) - cohort["pred_q50"]
    cohort["actual_over_pred50"] = cohort[COL_SALARY].astype(float) / cohort["pred_q50"]

    sorted_nat_q50 = np.sort(national["pred_q50"].to_numpy(dtype=float))
    cohort["pred_q50_percentile"] = [
        percentile_rank(sorted_nat_q50, float(v)) for v in cohort["pred_q50"].to_numpy(dtype=float)
    ]

    gap_edges = [-np.inf, -5000.0, -2500.0, 0.0, 2500.0, 5000.0, np.inf]
    gap_labels = [
        "< -5000",
        "-5000 till -2500",
        "-2500 till 0",
        "0 till +2500",
        "+2500 till +5000",
        "> +5000",
    ]
    gap_bins = pd.cut(
        cohort["actual_minus_pred50"],
        bins=gap_edges,
        labels=gap_labels,
        include_lowest=True,
        right=True,
    )
    gap_bin_counts = gap_bins.value_counts(sort=False).reindex(gap_labels, fill_value=0)

    nat_ecdf_x, nat_ecdf_y = ecdf(national["pred_q50"].to_numpy(dtype=float))
    cohort_ecdf_x, cohort_ecdf_y = ecdf(cohort["pred_q50"].to_numpy(dtype=float))

    years_grid = np.linspace(years_min, years_max, 180)
    typ_curve = build_typical_profile_curve(
        national_df=national,
        years_grid=years_grid,
        reference_categories=ref_cats,
        spline_basis=spline,
        x_columns=x_columns,
        model_q50=models[Q_MED],
    )

    group_col, gap_groups = build_gap_groups(cohort)
    group_rankings = build_group_rankings(cohort)
    unseen = identify_unseen_categories(cohort, national)
    unseen_summary = "None"
    if unseen:
        unseen_parts = [f"{col}: {', '.join(vals)}" for col, vals in unseen.items()]
        unseen_summary = "; ".join(unseen_parts)

    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=[
            "Populationens lönegap mot predikterad median (q50)",
            "ECDF: Cohort vs National Predicted Salary (q50)",
            "Years vs Salary (Actual) with Model Median Trend",
            f"Actual - Predicted (q50) by {group_col}",
        ],
        horizontal_spacing=0.12,
        vertical_spacing=0.18,
    )

    fig.add_trace(
        go.Bar(
            x=gap_labels,
            y=gap_bin_counts.values.astype(float),
            name="Antal personer",
            marker_color="rgba(49,130,189,0.75)",
        ),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=nat_ecdf_x,
            y=nat_ecdf_y,
            mode="lines",
            name="National q50 ECDF",
            line=dict(color="rgba(150,150,150,0.9)", width=2),
        ),
        row=1,
        col=2,
    )
    fig.add_trace(
        go.Scatter(
            x=cohort_ecdf_x,
            y=cohort_ecdf_y,
            mode="lines+markers",
            name="Cohort q50 ECDF",
            line=dict(color="rgba(49,130,189,0.95)", width=2),
            marker=dict(size=5),
        ),
        row=1,
        col=2,
    )

    fig.add_trace(
        go.Scatter(
            x=national[COL_YEARS],
            y=national[COL_SALARY],
            mode="markers",
            name="National actual salary",
            marker=dict(color="rgba(120,120,120,0.22)", size=6),
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=cohort[COL_YEARS],
            y=cohort[COL_SALARY],
            mode="markers",
            name="Cohort actual salary",
            marker=dict(color="rgba(49,130,189,0.95)", size=11, line=dict(color="white", width=1)),
            text=[f"Person {i+1}" for i in range(len(cohort))],
            hovertemplate="%{text}<br>År: %{x}<br>Lön: %{y:.0f} kr<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=years_grid,
            y=typ_curve,
            mode="lines",
            name="Model median (typical profile)",
            line=dict(color="rgba(214,39,40,0.9)", width=2),
        ),
        row=2,
        col=1,
    )

    for grp, vals_list in gap_groups.items():
        vals = np.asarray(vals_list, dtype=float)
        fig.add_trace(
            go.Box(
                y=vals,
                name=grp,
                boxmean=True,
                marker_color="rgba(49,130,189,0.85)",
                showlegend=False,
            ),
            row=2,
            col=2,
        )

    fig.update_xaxes(title_text="Intervall för faktisk - predikterad q50 (kr/mån)", row=1, col=1)
    fig.update_yaxes(title_text="Cohort count", row=1, col=1)
    fig.update_xaxes(title_text="Predicted salary q50 (kr)", row=1, col=2)
    fig.update_yaxes(title_text="ECDF", row=1, col=2)
    fig.update_xaxes(title_text="Years in clinical work", row=2, col=1)
    fig.update_yaxes(title_text="Salary (kr/month)", row=2, col=1)
    fig.update_xaxes(title_text=group_col, row=2, col=2)
    fig.update_yaxes(title_text="Actual - Predicted q50 (kr)", row=2, col=2)

    fig.update_layout(
        height=980,
        width=1400,
        template="plotly_white",
        title_text="Demo Cohort vs National Predicted Salaries",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0.0),
    )

    med_pred = safe_median(cohort["pred_q50"])
    med_gap = safe_median(cohort["actual_minus_pred50"])
    generated_at = datetime.now(timezone.utc).isoformat()
    summary_html = (
        "<h2>Population jämfört med nationell referens</h2>"
        f"<p><b>N nationellt:</b> {len(national)} | <b>N population:</b> {len(cohort)} | "
        f"<b>Median predikterad q50 (population):</b> {format_money(med_pred)} | "
        f"<b>Median avvikelse (faktisk - predikterad q50):</b> {format_money(med_gap)}</p>"
        f"<p><b>Rollfilter:</b> {args.role} | <b>Nationell källa:</b> {args.national_excel} | "
        f"<b>Population-källa:</b> {cohort_source}</p>"
        f"<p><b>Urvalsfrö:</b> {args.sample_seed} | <b>Skapad (UTC):</b> {generated_at}</p>"
        f"<p><b>Okända kategorier i populationen:</b> {unseen_summary}</p>"
    )

    out_html = Path(args.out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Demo Cohort Salary Report</title></head><body style='font-family:Arial,sans-serif'>"
        f"{summary_html}{fig.to_html(full_html=False, include_plotlyjs='cdn')}"
        "</body></html>"
    )
    out_html.write_text(html, encoding="utf-8")

    gap_counts_dict = {label: int(gap_bin_counts.loc[label]) for label in gap_labels}
    out_json = {
        "meta": {
            "generated_at_utc": generated_at,
            "role": str(args.role),
            "national_source": str(args.national_excel),
            "cohort_source": cohort_source,
            "sample_seed": int(args.sample_seed),
            "unseen_categories_summary": unseen_summary,
        },
        "summary": {
            "n_national": int(len(national)),
            "n_population": int(len(cohort)),
            "median_pred_q50": med_pred,
            "median_gap_actual_minus_pred50": med_gap,
        },
        "gap_bins": {
            "labels": gap_labels,
            "counts": gap_counts_dict,
        },
        "ecdf": {
            "national_q50_x": [float(v) for v in nat_ecdf_x.tolist()],
            "national_q50_y": [float(v) for v in nat_ecdf_y.tolist()],
            "population_q50_x": [float(v) for v in cohort_ecdf_x.tolist()],
            "population_q50_y": [float(v) for v in cohort_ecdf_y.tolist()],
        },
        "scatter": {
            "national_years": [float(v) for v in national[COL_YEARS].astype(float).to_numpy().tolist()],
            "national_salary": [float(v) for v in national[COL_SALARY].astype(float).to_numpy().tolist()],
            "population_years": [float(v) for v in cohort[COL_YEARS].astype(float).to_numpy().tolist()],
            "population_salary": [float(v) for v in cohort[COL_SALARY].astype(float).to_numpy().tolist()],
            "trend_years": [float(v) for v in years_grid.tolist()],
            "trend_q50": [float(v) for v in typ_curve.tolist()],
        },
        "gaps": {
            "group_column": group_col,
            "groups": gap_groups,
        },
        "individuals": [
            {
                "id": int(i + 1),
                "years": float(row[COL_YEARS]),
                "actual_salary": float(row[COL_SALARY]),
                "pred_q50": float(row["pred_q50"]),
                "pred_percentile": float(row["pred_q50_percentile"]),
                "gap_actual_minus_pred50": float(row["actual_minus_pred50"]),
                "workplace": str(row[COL_WORKPLACE]),
                "specialist": str(row[COL_SPECIALIST]),
                "phd": str(row[COL_PHD]),
            }
            for i, row in cohort.reset_index(drop=True).iterrows()
        ],
        "group_rankings": group_rankings,
    }

    out_json_path = Path(args.out_json)
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_json_path.write_text(json.dumps(out_json, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote HTML report: {out_html}")
    print(f"Wrote JSON dashboard data: {out_json_path}")
    if not args.cohort_excel:
        print(f"Wrote demo cohort Excel: {args.out_demo_excel}")


if __name__ == "__main__":
    main()
