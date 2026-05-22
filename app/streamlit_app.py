import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import torch
import torch.nn as nn
import joblib
import holidays
from pathlib import Path

# Page config
st.set_page_config(
    page_title="Energy Demand Forecasting",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .metric-card {
        background: #1e1e2e; border-radius: 8px; padding: 16px;
        text-align: center; border: 1px solid #313244;
    }
    .metric-value { font-size: 1.8rem; font-weight: bold; color: #cdd6f4; }
    .metric-label { font-size: 0.85rem; color: #a6adc8; margin-top: 4px; }
</style>
""", unsafe_allow_html=True)

BASE_DIR   = Path(__file__).parent.parent
MODELS_DIR = BASE_DIR / "models"
DATA_DIR   = BASE_DIR / "data"
SEQ_LEN    = 168

# LSTM model definition (must match forecasting.ipynb)
class QuantileLSTM(nn.Module):
    def __init__(self, input_size, hidden_size=128, num_layers=2, dropout=0.2):
        super().__init__()
        self.lstm    = nn.LSTM(input_size, hidden_size, num_layers,
                               batch_first=True, dropout=dropout)
        self.dropout = nn.Dropout(dropout)
        self.head    = nn.Linear(hidden_size, 3)

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.head(self.dropout(out[:, -1, :]))

# Feature engineering
US_HOLIDAYS = holidays.US(years=range(2005, 2026))

def build_features(df):
    d = df.copy()
    d['hour']       = d.index.hour
    d['dayofweek']  = d.index.dayofweek
    d['month']      = d.index.month
    d['quarter']    = d.index.quarter
    d['dayofyear']  = d.index.dayofyear
    d['weekofyear'] = d.index.isocalendar().week.astype(int)
    d['is_weekend'] = d['dayofweek'].isin([5, 6]).astype(int)
    d['is_holiday'] = d.index.normalize().isin(US_HOLIDAYS).astype(int)
    d['hour_sin']   = np.sin(2 * np.pi * d['hour'] / 24)
    d['hour_cos']   = np.cos(2 * np.pi * d['hour'] / 24)
    d['month_sin']  = np.sin(2 * np.pi * d['month'] / 12)
    d['month_cos']  = np.cos(2 * np.pi * d['month'] / 12)
    d['dow_sin']    = np.sin(2 * np.pi * d['dayofweek'] / 7)
    d['dow_cos']    = np.cos(2 * np.pi * d['dayofweek'] / 7)
    d['lag_1h']     = d['demand_mw'].shift(1)
    d['lag_24h']    = d['demand_mw'].shift(24)
    d['lag_168h']   = d['demand_mw'].shift(168)
    d['roll_mean_24h']  = d['demand_mw'].shift(1).rolling(24).mean()
    d['roll_std_24h']   = d['demand_mw'].shift(1).rolling(24).std()
    d['roll_mean_168h'] = d['demand_mw'].shift(1).rolling(168).mean()
    d['roll_std_168h']  = d['demand_mw'].shift(1).rolling(168).std()
    return d.dropna()

# Load data
@st.cache_data
def load_data():
    import glob
    # Kaggle historical DOM (2005-2018)
    kaggle = pd.read_csv(DATA_DIR / 'DOM_hourly.csv')
    kaggle.columns = ['Datetime', 'MW']
    kaggle['Datetime'] = pd.to_datetime(kaggle['Datetime'])

    # PJM yearly files (2018-2025)
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

# Load models
@st.cache_resource
def load_models():
    feat_scaler   = joblib.load(MODELS_DIR / 'feat_scaler.pkl')
    target_scaler = joblib.load(MODELS_DIR / 'target_scaler.pkl')
    feature_cols  = joblib.load(MODELS_DIR / 'feature_cols.pkl')

    lgbm = {
        'q10': joblib.load(MODELS_DIR / 'lgbm_q10.pkl'),
        'q50': joblib.load(MODELS_DIR / 'lgbm_q50.pkl'),
        'q90': joblib.load(MODELS_DIR / 'lgbm_q90.pkl'),
    }

    lstm = QuantileLSTM(input_size=len(feature_cols))
    lstm.load_state_dict(torch.load(
        MODELS_DIR / 'lstm_model.pt', map_location='cpu', weights_only=True
    ))
    lstm.eval()

    return lgbm, lstm, feat_scaler, target_scaler, feature_cols

# Precompute all test predictions (cached - runs once)
@st.cache_data
def get_predictions():
    df = load_data()
    lgbm, lstm, feat_scaler, target_scaler, feature_cols = load_models()

    train = df[df.index.year < 2024]
    test  = df[df.index.year == 2024]
    X_test = test[feature_cols]

    # LightGBM
    lgbm_q10 = lgbm['q10'].predict(X_test)
    lgbm_q50 = lgbm['q50'].predict(X_test)
    lgbm_q90 = lgbm['q90'].predict(X_test)

    # LSTM - batched sliding window
    X_train_sc = feat_scaler.transform(train[feature_cols])
    X_test_sc  = feat_scaler.transform(X_test)
    X_combined = np.vstack([X_train_sc[-SEQ_LEN:], X_test_sc])
    X_tensor   = torch.FloatTensor(X_combined)

    lstm_raw = []
    indices  = list(range(SEQ_LEN, len(X_tensor)))
    batch_size = 512
    with torch.no_grad():
        for i in range(0, len(indices), batch_size):
            batch_idx = indices[i:i + batch_size]
            seqs = torch.stack([X_tensor[j - SEQ_LEN:j] for j in batch_idx])
            out  = lstm(seqs).numpy()
            lstm_raw.append(out)

    lstm_raw = np.vstack(lstm_raw)
    inv = lambda x: target_scaler.inverse_transform(x.reshape(-1, 1)).ravel()
    lstm_q10 = inv(lstm_raw[:, 0])
    lstm_q50 = inv(lstm_raw[:, 1])
    lstm_q90 = inv(lstm_raw[:, 2])

    return pd.DataFrame({
        'actual':   test['demand_mw'].values,
        'lgbm_q10': lgbm_q10, 'lgbm_q50': lgbm_q50, 'lgbm_q90': lgbm_q90,
        'lstm_q10': lstm_q10,  'lstm_q50': lstm_q50,  'lstm_q90': lstm_q90,
    }, index=test.index)


FORECAST_PATH = DATA_DIR / "future_forecast_5yr.parquet"

@st.cache_data
def future_forecast_5yr():
    """Load pre-generated 5-year forecast from parquet (instant).
    Regenerate with generate_forecast.py if models are retrained."""
    return pd.read_parquet(FORECAST_PATH)

# Metric helpers
def pinball(y, yhat, q):
    e = y - yhat
    return float(np.mean(np.where(e >= 0, q * e, (q - 1) * e)))

def compute_metrics(pred_df, prefix):
    y   = pred_df['actual'].values
    q10 = pred_df[f'{prefix}_q10'].values
    q50 = pred_df[f'{prefix}_q50'].values
    q90 = pred_df[f'{prefix}_q90'].values
    mae      = float(np.mean(np.abs(y - q50)))
    avg_pb   = (pinball(y, q10, 0.1) + pinball(y, q50, 0.5) + pinball(y, q90, 0.9)) / 3
    coverage = float(np.mean((y >= q10) & (y <= q90)) * 100)
    width    = float(np.mean(q90 - q10))
    return {
        'MAE (MW)': round(mae, 1),
        'Avg Pinball': round(avg_pb, 1),
        'Coverage %': round(coverage, 1),
        'Interval Width (MW)': round(width, 1)
    }

# Plotly forecast chart
def forecast_chart(df_slice, prefix, color, title):
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_slice.index, y=df_slice[f'{prefix}_q90'],
        line=dict(width=0), showlegend=False, hoverinfo='skip'
    ))
    fig.add_trace(go.Scatter(
        x=df_slice.index, y=df_slice[f'{prefix}_q10'],
        fill='tonexty', fillcolor=f'rgba({color},0.15)',
        line=dict(width=0), name='80% interval'
    ))
    fig.add_trace(go.Scatter(
        x=df_slice.index, y=df_slice[f'{prefix}_q50'],
        line=dict(color=f'rgb({color})', width=2), name='Q50 forecast'
    ))
    fig.add_trace(go.Scatter(
        x=df_slice.index, y=df_slice['actual'],
        line=dict(color='rgb(243,139,168)', width=1.5, dash='dot'), name='Actual demand'
    ))
    fig.update_layout(
        title=title, xaxis_title='Date', yaxis_title='Demand (MW)',
        height=400, template='plotly_dark', hovermode='x unified',
        legend=dict(orientation='h', y=1.1)
    )
    return fig

# ========================
# MAIN APP
# ========================
st.title("⚡ Probabilistic Energy Demand Forecasting")
st.markdown("**Dominion (DOM)** hourly energy consumption · Virginia/DC region · 2005-2025 · XGBoost · LightGBM · LSTM")
st.divider()

with st.spinner("Loading data and models..."):
    df      = load_data()
    pred_df = get_predictions()

tab1, tab2, tab3, tab4 = st.tabs(["📈 Forecast", "🏆 Model Comparison", "🔍 EDA", "🔮 Future Forecast"])

# Tab 1: Forecast
with tab1:
    col_ctrl, col_main = st.columns([1, 3])

    with col_ctrl:
        st.subheader("Settings")
        model_choice = st.selectbox("Model", ["LightGBM", "LSTM", "Both"])

        # Initialize defaults
        if 'sel_start' not in st.session_state:
            st.session_state['sel_start'] = pd.Timestamp("2024-01-08").date()
        if 'sel_end' not in st.session_state:
            st.session_state['sel_end'] = pd.Timestamp("2024-01-14").date()

        def set_dates(start, end):
            st.session_state['sel_start'] = pd.Timestamp(start).date()
            st.session_state['sel_end']   = pd.Timestamp(end).date()

        st.markdown("**Select date range (2024)**")
        start_date = st.date_input(
            "Start", key='sel_start',
            min_value=pd.Timestamp("2024-01-08"),
            max_value=pd.Timestamp("2024-12-24")
        )
        end_date = st.date_input(
            "End", key='sel_end',
            min_value=pd.Timestamp("2024-01-09"),
            max_value=pd.Timestamp("2024-12-25")
        )
        st.markdown("---")
        st.markdown("**Quick select**")
        st.button("❄️ Winter Peak (Jan)",  on_click=set_dates, args=("2024-01-08", "2024-01-14"))
        st.button("☀️ Summer Peak (Jul)",  on_click=set_dates, args=("2024-07-08", "2024-07-14"))
        st.button("🎄 Holiday Week (Dec)", on_click=set_dates, args=("2024-12-17", "2024-12-25"))

    with col_main:
        s = str(start_date)
        e = str(end_date)
        slice_df = pred_df.loc[s:e]

        if model_choice in ("LightGBM", "Both"):
            st.plotly_chart(
                forecast_chart(slice_df, 'lgbm', '137,180,250',
                               f'LightGBM — {s} to {e}'),
                use_container_width=True
            )
        if model_choice in ("LSTM", "Both"):
            st.plotly_chart(
                forecast_chart(slice_df, 'lstm', '166,227,161',
                               f'LSTM — {s} to {e}'),
                use_container_width=True
            )

        st.subheader("Metrics for selected period")
        prefix = 'lgbm' if model_choice != 'LSTM' else 'lstm'
        m = compute_metrics(slice_df, prefix)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("MAE", f"{m['MAE (MW)']:,.0f} MW")
        c2.metric("Avg Pinball Loss", f"{m['Avg Pinball']:.1f}")
        c3.metric("Coverage", f"{m['Coverage %']:.1f}%", delta="target: 80%")
        c4.metric("Interval Width", f"{m['Interval Width (MW)']:,.0f} MW")

# Tab 2: Model Comparison
with tab2:
    st.subheader("Full 2024 Test Set — All Models")

    metrics_all = {
        'XGBoost*':  {'MAE (MW)': 201.9, 'Avg Pinball': 66.2, 'Coverage %': 76.6, 'Interval Width (MW)': 586.4},
        'LightGBM':  compute_metrics(pred_df, 'lgbm'),
        'LSTM':      compute_metrics(pred_df, 'lstm'),
    }
    metrics_df = pd.DataFrame(metrics_all).T
    st.dataframe(
        metrics_df.style
            .highlight_min(axis=0, subset=['MAE (MW)', 'Avg Pinball'], color='#a6e3a1')
            .highlight_max(axis=0, subset=['Coverage %'], color='#a6e3a1'),
        use_container_width=True
    )
    st.caption("*XGBoost results from notebook — model not loaded in app to keep repo size small")

    # Bar charts
    models_list = list(metrics_all.keys())
    colors = ['#89b4fa', '#a6e3a1', '#cba6f7']
    fig_cmp = make_subplots(rows=1, cols=3,
                             subplot_titles=('MAE (MW) lower=better',
                                             'Avg Pinball lower=better',
                                             'Coverage % higher=better'))
    for i, metric in enumerate(['MAE (MW)', 'Avg Pinball', 'Coverage %']):
        vals = [metrics_all[m][metric] for m in models_list]
        fig_cmp.add_trace(
            go.Bar(x=models_list, y=vals, marker_color=colors, showlegend=False),
            row=1, col=i + 1
        )
    fig_cmp.add_hline(y=80, line_dash='dash', line_color='red',
                       annotation_text='target 80%', row=1, col=3)
    fig_cmp.update_layout(height=350, template='plotly_dark')
    st.plotly_chart(fig_cmp, use_container_width=True)

    # Side-by-side comparison for summer week
    st.subheader("Prediction Intervals — Summer Peak Week (Jul 8-14)")
    zoom = pred_df.loc['2024-07-08':'2024-07-14']
    fig3 = make_subplots(rows=2, cols=1,
                          subplot_titles=('LightGBM', 'LSTM'),
                          shared_xaxes=True, vertical_spacing=0.12)
    for row, (prefix, color) in enumerate([('lgbm', '137,180,250'), ('lstm', '166,227,161')], 1):
        fig3.add_trace(go.Scatter(
            x=zoom.index, y=zoom[f'{prefix}_q90'],
            line=dict(width=0), showlegend=False), row=row, col=1)
        fig3.add_trace(go.Scatter(
            x=zoom.index, y=zoom[f'{prefix}_q10'],
            fill='tonexty', fillcolor=f'rgba({color},0.2)',
            line=dict(width=0), name='80% interval',
            showlegend=(row == 1)), row=row, col=1)
        fig3.add_trace(go.Scatter(
            x=zoom.index, y=zoom[f'{prefix}_q50'],
            line=dict(color=f'rgb({color})', width=2),
            name='LightGBM Q50' if prefix == 'lgbm' else 'LSTM Q50'), row=row, col=1)
        fig3.add_trace(go.Scatter(
            x=zoom.index, y=zoom['actual'],
            line=dict(color='rgb(243,139,168)', width=1.5, dash='dot'),
            name='Actual', showlegend=(row == 1)), row=row, col=1)
    fig3.update_layout(height=550, template='plotly_dark', hovermode='x unified')
    st.plotly_chart(fig3, use_container_width=True)

# Tab 3: EDA
with tab3:
    st.subheader("Exploratory Data Analysis — DOM 2005-2025")

    col1, col2 = st.columns(2)
    with col1:
        hourly = df.groupby('hour')['demand_mw'].mean().reset_index()
        fig_h = px.line(hourly, x='hour', y='demand_mw', markers=True,
                        title='Average Demand by Hour of Day',
                        labels={'demand_mw': 'Avg Demand (MW)', 'hour': 'Hour'},
                        template='plotly_dark', color_discrete_sequence=['#89b4fa'])
        st.plotly_chart(fig_h, use_container_width=True)

        monthly = df.groupby('month')['demand_mw'].mean().reset_index()
        month_names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
        monthly['month_name'] = monthly['month'].apply(lambda x: month_names[x - 1])
        fig_m = px.bar(monthly, x='month_name', y='demand_mw',
                       title='Average Demand by Month',
                       labels={'demand_mw': 'Avg Demand (MW)', 'month_name': 'Month'},
                       template='plotly_dark', color_discrete_sequence=['#89b4fa'])
        st.plotly_chart(fig_m, use_container_width=True)

    with col2:
        daily = df.groupby('dayofweek')['demand_mw'].mean().reset_index()
        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        daily['day_name'] = daily['dayofweek'].apply(lambda x: day_names[x])
        fig_d = px.bar(daily, x='day_name', y='demand_mw',
                       title='Average Demand by Day of Week',
                       labels={'demand_mw': 'Avg Demand (MW)', 'day_name': 'Day'},
                       template='plotly_dark', color_discrete_sequence=['#89b4fa'])
        st.plotly_chart(fig_d, use_container_width=True)

        yearly = df.groupby(df.index.year)['demand_mw'].mean().reset_index()
        yearly.columns = ['year', 'avg_demand']
        fig_y = px.line(yearly, x='year', y='avg_demand', markers=True,
                        title='Yearly Mean Demand Trend',
                        labels={'avg_demand': 'Avg Demand (MW)', 'year': 'Year'},
                        template='plotly_dark', color_discrete_sequence=['#a6e3a1'])
        st.plotly_chart(fig_y, use_container_width=True)

    st.subheader("Full Time Series 2005-2025")
    daily_avg = df['demand_mw'].resample('D').mean()
    fig_full = px.line(daily_avg, template='plotly_dark',
                       labels={'value': 'Daily Avg Demand (MW)', 'Datetime': ''},
                       color_discrete_sequence=['#89b4fa'])
    fig_full.update_layout(height=300, showlegend=False)
    st.plotly_chart(fig_full, use_container_width=True)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total observations", f"{len(df):,}")
    k2.metric("Peak demand", f"{df['demand_mw'].max():,.0f} MW")
    k3.metric("Min demand",  f"{df['demand_mw'].min():,.0f} MW")
    k4.metric("Years of data", f"{df.index.year.nunique()}")

# Tab 4: Future Forecast
with tab4:
    st.subheader("Future Demand Forecast — 2026 to 2030")
    st.info(
        "**Recursive forecasting:** LightGBM predicts one hour at a time — each Q50 output "
        "feeds back as the lag input for the next step. Seasonal patterns (hour-of-day, "
        "day-of-week, month, holidays) from 2005–2023 training data drive the projection. "
        "The full 5-year window (43,824 steps) is pre-generated and loads instantly."
    )

    # Date range state
    if 'f_start' not in st.session_state:
        st.session_state['f_start'] = pd.Timestamp("2026-06-01").date()
    if 'f_end' not in st.session_state:
        st.session_state['f_end'] = pd.Timestamp("2026-06-30").date()

    def set_future_dates(start, end):
        st.session_state['f_start'] = pd.Timestamp(start).date()
        st.session_state['f_end']   = pd.Timestamp(end).date()

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        f_start = st.date_input(
            "From", key='f_start',
            min_value=pd.Timestamp("2026-01-01").date(),
            max_value=pd.Timestamp("2030-12-31").date(),
        )
    with col_f2:
        f_end = st.date_input(
            "To", key='f_end',
            min_value=pd.Timestamp("2026-01-01").date(),
            max_value=pd.Timestamp("2030-12-31").date(),
        )

    st.markdown("**Quick select**")
    qs1, qs2, qs3, qs4, qs5 = st.columns(5)
    qs1.button("☀️ Summer 2026",  on_click=set_future_dates, args=("2026-07-01", "2026-07-31"))
    qs2.button("❄️ Winter 2027",  on_click=set_future_dates, args=("2027-01-01", "2027-01-31"))
    qs3.button("📅 Full 2027",    on_click=set_future_dates, args=("2027-01-01", "2027-12-31"))
    qs4.button("☀️ Summer 2028",  on_click=set_future_dates, args=("2028-07-01", "2028-07-31"))
    qs5.button("📅 Full 2030",    on_click=set_future_dates, args=("2030-01-01", "2030-12-31"))

    if st.button("▶ Run Forecast", type="primary", key="btn_future"):
        if f_end <= f_start:
            st.error("'To' date must be after 'From' date.")
        else:
            with st.spinner("Loading forecast..."):
                fcast = future_forecast_5yr()

            window = fcast.loc[str(f_start):str(f_end)]

            if window.empty:
                st.warning("No forecast data for the selected range.")
            else:
                # Auto-resample to daily for wide windows (keeps chart readable)
                n_days = (f_end - f_start).days
                if n_days > 60:
                    plot_df = window.resample('D').agg({'q10': 'min', 'q50': 'mean', 'q90': 'max'})
                    res_label = "daily avg"
                else:
                    plot_df = window
                    res_label = "hourly"

                fig_f = go.Figure()
                fig_f.add_trace(go.Scatter(
                    x=plot_df.index, y=plot_df['q90'],
                    line=dict(width=0), showlegend=False, hoverinfo='skip'
                ))
                fig_f.add_trace(go.Scatter(
                    x=plot_df.index, y=plot_df['q10'],
                    fill='tonexty', fillcolor='rgba(137,180,250,0.15)',
                    line=dict(width=0), name='80% interval'
                ))
                fig_f.add_trace(go.Scatter(
                    x=plot_df.index, y=plot_df['q50'],
                    line=dict(color='rgb(137,180,250)', width=1.5),
                    name=f'Q50 forecast ({res_label})'
                ))
                fig_f.update_layout(
                    title=f'LightGBM Recursive Forecast — {f_start} to {f_end}',
                    xaxis_title='Date', yaxis_title='Demand (MW)',
                    height=440, template='plotly_dark', hovermode='x unified',
                    legend=dict(orientation='h', y=1.1)
                )
                st.plotly_chart(fig_f, use_container_width=True)

                days_ahead = (window.index[0] - df.index[-1]).days
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Peak Q50",   f"{window['q50'].max():,.0f} MW")
                c2.metric("Min Q50",    f"{window['q50'].min():,.0f} MW")
                c3.metric("Avg Width",  f"{(window['q90'] - window['q10']).mean():,.0f} MW")
                c4.metric("Horizon",    f"{days_ahead} days")
                st.caption(
                    f"Seeded from last known data (Dec 2025). "
                    f"Intervals reflect seasonal uncertainty patterns — not accumulated prediction error. "
                    f"{'Chart resampled to daily average for readability.' if n_days > 60 else 'Showing hourly resolution.'}"
                )
