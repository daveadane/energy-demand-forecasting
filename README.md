# Assignment 4 — Probabilistic Energy Demand Forecasting

**Topic:** Time Series Forecasting with Quantile Regression  
**Dataset:** PJM East (PJME) Hourly Energy Consumption 2001–2018  
**Question:** Can we forecast energy demand with calibrated prediction intervals using gradient boosting models?

## Project structure

```
├── eda.ipynb            # Exploratory data analysis — seasonality, calendar effects, anomalies, weather
├── forecasting.ipynb    # Feature engineering, XGBoost & LightGBM quantile regression, evaluation
├── data/
│   └── PJME_hourly.csv  # PJM East hourly demand data (145K rows, 2001–2018)
└── README.md
```

## How to run

1. Open `eda.ipynb` or `forecasting.ipynb` in Google Colab
2. Upload `data/PJME_hourly.csv` via the Colab file sidebar (folder icon on the left)
3. Run all cells — no GPU needed, CPU runtime is sufficient

## Results

| Model    | MAE (MW) | Avg Pinball Loss | Coverage | Interval Width |
|----------|----------|------------------|----------|----------------|
| XGBoost  | 340.6    | 111.1            | 68.6%    | 952.2 MW       |
| LightGBM | **334.4**| **108.1**        | **71.0%**| 957.0 MW       |

LightGBM outperforms XGBoost on all accuracy metrics. Coverage of ~70% (target 80%) indicates the prediction intervals are slightly narrow — a known limitation when weather data is excluded from features.

## Key findings

- Lag features (`lag_24h`, `lag_168h`) are the strongest predictors — energy demand is highly autocorrelated
- Daily, weekly, and seasonal cycles are strong and well-captured by calendar features
- Holidays reduce demand by ~4% vs normal days
- Temperature has a U-shaped relationship with demand (both extreme cold and heat drive high consumption)
- LightGBM trains faster and produces better-calibrated intervals than XGBoost
