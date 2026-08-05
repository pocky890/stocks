import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from charts import candlestick_with_ma, institutional_flow_chart, margin_balance_chart
from stocks.config import load_config
from stocks.daily_update import check_and_update
from stocks.db import (
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_ex_dividend_schedule,
    fetch_institutional_flows,
    fetch_margin_balances,
    fetch_signal_events,
    fetch_valuations,
    fetch_watchlist,
)

st.set_page_config(page_title="台股訊號監控", layout="wide")

STRATEGY_LABELS = {
    "ma_crossover": "均線交叉 (5/20日)",
    "rsi": "RSI超買超賣",
    "macd": "MACD交叉",
    "bollinger": "布林通道",
    "volume_anomaly": "成交量異常",
    "price_alert": "到價提醒",
    "ma_alignment": "多空排列 (5/10/20日線)",
}

config = load_config()

if "checked_for_updates" not in st.session_state:
    st.session_state.checked_for_updates = True
    with st.spinner("檢查有沒有新的盤後資料..."):
        result = check_and_update(config)
    if result["watchlist_empty"]:
        pass  # 觀察清單是空的，下面的頁籤本來就會提示要先跑fetch_historical.py
    elif result["new_price_days"] == 0 and result["new_market_days"] == 0:
        st.toast("資料已經是最新的，沒有新的盤後資料", icon="✅")
    else:
        st.toast(
            f"已更新：股價 {result['new_price_days']} 天、三大法人/融資融券/估值 {result['new_market_days']} 天",
            icon="🔄",
        )

tab_watchlist, tab_chart, tab_fundamentals, tab_history = st.tabs(["觀察清單", "K線圖", "籌碼/基本面", "訊號紀錄"])

with connect(config.db_path) as conn:
    watchlist_rows = fetch_watchlist(conn)

watchlist = [dict(r) for r in watchlist_rows]
symbols = [w["code"] for w in watchlist]

with tab_watchlist:
    st.subheader("觀察清單")
    if watchlist:
        st.dataframe(pd.DataFrame(watchlist)[["code", "name"]], use_container_width=True)
        strategy_list = "、".join(STRATEGY_LABELS.get(k, k) for k in STRATEGY_LABELS)
        st.caption(f"目前每檔股票都套用同一組 {len(STRATEGY_LABELS)} 種策略（還沒有支援每檔各自挑策略）：{strategy_list}")
    else:
        st.info("觀察清單是空的，先跑 `python scripts/fetch_historical.py` 填範例資料")

with tab_chart:
    st.subheader("K線圖 + 均線")
    if not symbols:
        st.info("觀察清單是空的，沒有資料可以畫圖")
    else:
        selected = st.selectbox("選擇股票", symbols)
        with connect(config.db_path) as conn:
            bars = bars_to_dataframe(fetch_bars_daily(conn, selected), ts_field="date")
        if bars.empty:
            st.warning(f"{selected} 沒有歷史K棒資料")
        else:
            fig = candlestick_with_ma(bars, ma_windows=[5, 10, 20, 60])
            st.plotly_chart(fig, use_container_width=True)

with tab_fundamentals:
    st.subheader("籌碼面 / 基本面（證交所免費公開資料）")
    if not symbols:
        st.info("觀察清單是空的，沒有資料可以顯示")
    else:
        selected_f = st.selectbox("選擇股票", symbols, key="fundamentals_symbol")
        with connect(config.db_path) as conn:
            flow_rows = fetch_institutional_flows(conn, selected_f)
            margin_rows = fetch_margin_balances(conn, selected_f)
            valuation_rows = fetch_valuations(conn, selected_f)
            ex_div_rows = fetch_ex_dividend_schedule(conn, selected_f)

        st.markdown("#### 三大法人買賣超")
        if flow_rows:
            flow_df = pd.DataFrame([dict(r) for r in flow_rows])
            st.plotly_chart(institutional_flow_chart(flow_df), use_container_width=True)
        else:
            st.info("沒有三大法人資料，先跑 `python scripts/fetch_market_data.py`")

        st.markdown("#### 融資融券餘額")
        if margin_rows:
            margin_df = pd.DataFrame([dict(r) for r in margin_rows])
            st.plotly_chart(margin_balance_chart(margin_df), use_container_width=True)
        else:
            st.info("沒有融資融券資料，先跑 `python scripts/fetch_market_data.py`")

        st.markdown("#### 目前估值")
        if valuation_rows:
            latest = dict(valuation_rows[-1])
            col1, col2, col3 = st.columns(3)
            col1.metric("本益比(PE)", latest["pe_ratio"] if latest["pe_ratio"] is not None else "N/A")
            col2.metric("殖利率(%)", latest["dividend_yield"])
            col3.metric("股價淨值比(PB)", latest["pb_ratio"])
        else:
            st.info("沒有估值資料，先跑 `python scripts/fetch_market_data.py`")

        st.markdown("#### 近期除權息")
        if ex_div_rows:
            ex_div_df = pd.DataFrame([dict(r) for r in ex_div_rows])
            st.dataframe(
                ex_div_df[["ex_date", "cash_dividend", "stock_dividend_ratio", "detail"]],
                use_container_width=True,
            )
        else:
            st.info("目前沒有排定中的除權息")

with tab_history:
    st.subheader("訊號歷史紀錄")
    filter_symbol = st.selectbox("篩選股票（可選）", ["全部"] + symbols)
    with connect(config.db_path) as conn:
        if filter_symbol == "全部":
            rows = fetch_signal_events(conn, symbol=None, limit=200)
        else:
            rows = fetch_signal_events(conn, symbol=filter_symbol, limit=200)

    if not rows:
        st.info("目前沒有任何訊號紀錄（backtest.py不會寫入signal_events，要跑live/batch才會有）")
    else:
        df = pd.DataFrame([dict(r) for r in rows])
        st.dataframe(df[["ts", "symbol", "strategy", "direction", "price", "detail", "tier"]], use_container_width=True)
