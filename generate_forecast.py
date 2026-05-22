"""
Run this script once to pre-generate the 5-year future forecast and save as parquet.
The Streamlit app will load this file instantly instead of recomputing.

Usage:
    python generate_forecast.py
"""
import glob
import warnings
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
import joblib
import holidays
from pathlib import Path

BASE_DIR   = Path(__file__).parent
MODELS_DIR = BASE_DIR / "models"
DATA_DIR   = BASE_DIR / "data"
OUTPUT     = DATA_DIR / "future_forecast_5yr.parquet"

US_HOLIDAYS = holidays.US(years=range(2005, 2031))

def build_features(df):
    d = df.copy()
    d['hour']        = d.index.hour
    d['dayofweek']   = d.index.dayofweek
    d['month']       = d.index.month
    d['quarter']     = d.index.quarter
    d['dayofyear']   = d.index.dayofyear
    d['weekofyear']  = d.index.isocalendar().week.astype(int)
    d['is_weekend']  = d['dayofweek'].isin([5, 6]).astype(int)
    d['is_holiday']  = d.index.normalize().isin(US_HOLIDAYS).astype(int)
    d['hour_sin']    = np.sin(2 * np.pi * d['hour'] / 24)
    d['hour_cos']    = np.cos(2 * np.pi * d['hour'] / 24)
    d['month_sin']   = np.sin(2 * np.pi * d['month'] / 12)
    d['month_cos']   = np.cos(2 * np.pi * d['month'] / 12)
    d['dow_sin']     = np.sin(2 * np.pi * d['dayofweek'] / 7)
    d['dow_cos']     = np.cos(2 * np.pi * d['dayofweek'] / 7)
    d['lag_1h']      = d['demand_mw'].shift(1)
    d['lag_24h']     = d['demand_mw'].shift(24)
    d['lag_168h']    = d['demand_mw'].shift(168)
    d['roll_mean_24h']  = d['demand_mw'].shift(1).rolling(24).mean()
    d['roll_std_24h']   = d['demand_mw'].shift(1).rolling(24).std()
    d['roll_mean_168h'] = d['demand_mw'].shift(1).rolling(168).mean()
    d['roll_std_168h']  = d['demand_mw'].shift(1).rolling(168).std()
    return d.dropna()

def load_data():
    kaggle = pd.read_csv(DATA_DIR / 'DOM_hourly.csv')
    kaggle.columns = ['Datetime', 'MW']
    kaggle['Datetime'] = pd.to_datetime(kaggle['Datetime'])

    files = sorted(glob.glob(str(DATA_DIR / 'hrl_load_metered_*.csv')))
    frames = []
    for f in files:
        tmp = pd.read_csv(f, usecols=['datetime_beginning_ept', 'mw'])
        tmp.columns = ['Datetime', 'MW']
        tmp['Datetime'] = pd.to_datetime(tmp['Datetime'])
        frames.append(tmp)
    pjm = pd.concat(frames)

    combined = pd.concat([kaggle, pjm])
    combined = combined.drop_duplicates(subset='Datetime').sort_values('Datetime')
    combined = combined.set_index('Datetime')
    combined.columns = ['demand_mw']
    combined.index.name = 'Datetime'
    return build_features(combined)

def generate():
    print("Loading data...")
    df = load_data()
    print(f"  Data: {len(df):,} rows, last timestamp: {df.index[-1]}")

    print("Loading models...")
    feature_cols = joblib.load(MODELS_DIR / 'feature_cols.pkl')
    lgbm = {
        'q10': joblib.load(MODELS_DIR / 'lgbm_q10.pkl'),
        'q50': joblib.load(MODELS_DIR / 'lgbm_q50.pkl'),
        'q90': joblib.load(MODELS_DIR / 'lgbm_q90.pkl'),
    }

    SEED = 200
    last_ts    = df.index[-1]
    future_idx = pd.date_range(
        start=last_ts + pd.Timedelta(hours=1),
        end=pd.Timestamp("2030-12-31 23:00"),
        freq='h'
    )
    n = len(future_idx)
    print(f"  Forecasting {n:,} steps ({future_idx[0]} to {future_idx[-1]})")

    hours  = future_idx.hour.values.astype(np.float64)
    dows   = future_idx.dayofweek.values.astype(np.float64)
    months = future_idx.month.values.astype(np.float64)
    time_cols = {
        'hour':        hours,
        'dayofweek':   dows,
        'month':       months,
        'quarter':     future_idx.quarter.values.astype(np.float64),
        'dayofyear':   future_idx.dayofyear.values.astype(np.float64),
        'weekofyear':  future_idx.isocalendar().week.astype(int).values.astype(np.float64),
        'is_weekend':  (dows >= 5).astype(np.float64),
        'is_holiday':  np.array([int(t.normalize() in US_HOLIDAYS) for t in future_idx], dtype=np.float64),
        'hour_sin':    np.sin(2 * np.pi * hours  / 24),
        'hour_cos':    np.cos(2 * np.pi * hours  / 24),
        'month_sin':   np.sin(2 * np.pi * months / 12),
        'month_cos':   np.cos(2 * np.pi * months / 12),
        'dow_sin':     np.sin(2 * np.pi * dows   / 7),
        'dow_cos':     np.cos(2 * np.pi * dows   / 7),
    }

    fi = {f: i for i, f in enumerate(feature_cols)}
    X_mat = np.zeros((n, len(feature_cols)), dtype=np.float64)
    for name, arr in time_cols.items():
        X_mat[:, fi[name]] = arr

    ext = np.empty(SEED + n, dtype=np.float64)
    ext[:SEED] = df['demand_mw'].values[-SEED:]

    q10s = np.empty(n, dtype=np.float64)
    q50s = np.empty(n, dtype=np.float64)
    q90s = np.empty(n, dtype=np.float64)

    i_l1    = fi['lag_1h'];    i_l24   = fi['lag_24h'];   i_l168  = fi['lag_168h']
    i_rm24  = fi['roll_mean_24h'];  i_rs24  = fi['roll_std_24h']
    i_rm168 = fi['roll_mean_168h']; i_rs168 = fi['roll_std_168h']

    print("  Running recursive forecast", end="", flush=True)
    milestone = n // 10
    for i in range(n):
        if i % milestone == 0:
            print(f" {i * 100 // n}%", end="", flush=True)
        w = ext[i: i + SEED]
        X_mat[i, i_l1]    = w[-1]
        X_mat[i, i_l24]   = w[-24]
        X_mat[i, i_l168]  = w[-168]
        X_mat[i, i_rm24]  = w[-24:].mean()
        X_mat[i, i_rs24]  = w[-24:].std()
        X_mat[i, i_rm168] = w[-168:].mean()
        X_mat[i, i_rs168] = w[-168:].std()
        row = X_mat[i: i + 1]
        q50s[i] = lgbm['q50'].predict(row)[0]
        q10s[i] = lgbm['q10'].predict(row)[0]
        q90s[i] = lgbm['q90'].predict(row)[0]
        ext[SEED + i] = q50s[i]
    print(" 100% done")

    result = pd.DataFrame({'q10': q10s, 'q50': q50s, 'q90': q90s}, index=future_idx)
    result.to_parquet(OUTPUT)
    print(f"Saved to {OUTPUT} ({OUTPUT.stat().st_size / 1024:.0f} KB)")
    print(f"Peak Q50: {q50s.max():,.0f} MW  |  Min Q50: {q50s.min():,.0f} MW")

if __name__ == "__main__":
    generate()
