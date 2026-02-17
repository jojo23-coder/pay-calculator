# Pay Calculator (Static Web App)

This project has two parts:

- `pay-calculator.py`: original local Python analysis script.
- `docs/`: static web app (GitHub Pages compatible) using precomputed model output.

## 1) Update data and precompute predictions

Put your Excel file at `lon.xlsx`, then run:

```bash
cd /Users/joakim/Documents/pay-calculator
source .venv/bin/activate
python scripts/export_predictions.py
```

This regenerates:

- `docs/data/predictions.json`

## 2) Preview locally (VS Code Live Server)

1. Open `docs/index.html` in VS Code.
2. Right-click -> **Open with Live Server**.
3. Use the controls to update profile and years.  
   The chart and "Predikterat lönespann" update instantly from precomputed data.

## 3) Publish with GitHub Pages

1. Push the repository to GitHub.
2. In GitHub -> **Settings** -> **Pages**:
   - Source: `Deploy from a branch`
   - Branch: `main` (or your default branch)
   - Folder: `/docs`
3. Save.

GitHub Pages will serve `docs/index.html`.

## Notes

- No backend is required for the website.
- If `lon.xlsx` changes, rerun `python scripts/export_predictions.py` and commit the updated `docs/data/predictions.json`.
