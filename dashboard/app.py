import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from charts import candlestick_with_ma, institutional_flow_chart, intraday_line_chart, kd_chart, margin_balance_chart
from stocks.config import load_config
from stocks.daily_update import add_symbol_to_watchlist, check_and_update
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.db import (
    attach_institutional_flows,
    bars_list_to_dataframe,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_ex_dividend_schedule,
    fetch_institutional_flows,
    fetch_margin_balances,
    fetch_signal_events,
    fetch_valuations,
    fetch_watchlist,
    move_watchlist_symbol,
    remove_from_watchlist,
)
from stocks.shioaji_client import ShioajiClient
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import simulate_round_trips, summarize_trades
from stocks.watchlist_view import build_buy_recommendations, build_overview_rows

st.set_page_config(page_title="台股訊號監控", layout="wide")

# st.button內容預設靠左，這裡讓它在欄位裡水平置中；只影響watchlist_rows容器內的按鈕(▲▼移除)，
# 不動到其他地方的按鈕(例如新增表單的按鈕本來就use_container_width=True，置中沒有視覺差異)。
# 按鈕本身要先縮小(預設padding讓它比▲▼那兩欄還寬，置中的flex容器根本沒有多餘空間可以置中)。
# nowrap是為了數字欄位(季線/月線等)在窄欄位裡不要被硬擠成兩行；三大法人是刻意用<br>換行，
# nowrap不影響<br>造成的換行，只擋掉瀏覽器自動換行。
st.markdown(
    """
<style>
.st-key-watchlist_rows div[data-testid="stColumn"] div[data-testid="stElementContainer"] { width: 100%; }
.st-key-watchlist_rows div[data-testid="stButton"] { display: flex; justify-content: center; width: 100%; }
.st-key-watchlist_rows div[data-testid="stButton"] button {
    width: auto !important;
    padding: 0.15rem 0.15rem;
    min-width: 0;
}
/* ▲是第1欄、▼是第2欄，把▼再往左拉近一點，兩顆按鈕之間不用留一整欄的間距 */
.st-key-watchlist_rows div[data-testid="stColumn"]:nth-of-type(2) {
    margin-left: -10px;
}
.st-key-watchlist_rows > div[data-testid="stLayoutWrapper"]:nth-of-type(even) {
    background-color: rgba(255, 255, 255, 0.04);
}
.st-key-watchlist_rows [data-testid="stMarkdownContainer"] p { white-space: nowrap; }
</style>
""",
    unsafe_allow_html=True,
)

STRATEGY_LABELS = {
    "ma_crossover": "均線交叉 (5/20日)",
    "rsi": "RSI超買超賣",
    "macd": "MACD交叉",
    "bollinger": "布林通道",
    "volume_anomaly": "成交量異常",
    "price_alert": "到價提醒",
    "ma_alignment": "多空排列 (5/10/20日線)",
    "kd": "KD低檔黃金交叉/高檔死亡交叉",
    "institutional_streak": "三大法人連續買賣超",
    "ma_trend": "站上5/20日均線且20日線上揚",
    "atr_breakout": "ATR動態通道突破(創20日新高進場，2倍ATR移動停損出場)",
    "chip_momentum": "外資買超動能(連3日買超+未超買進場，2.5倍ATR移動停損出場)",
    "buy_formula": "極簡買進公式(籌碼+趨勢環境成立時，爆量突破布林或KD黃金交叉即買)",
    "sell_formula": "極簡賣出公式(跌破5日線+RSI超買/法人連3賣，或跌破10日線)",
}
# buy_formula/sell_formula是多條件組合、可以直接依據行動的完整判斷，稱為「策略」；
# 其他都只是單一指標的訊號，可信度較低，稱為「指標訊號」——跟notifier.NOTIFIABLE_STRATEGIES
# (只有這兩個會推播Telegram)是同一個區分，直接沿用避免兩處各自維護一份清單。

config = load_config()


TRACK_RECORD_STRATEGIES = ["chip_momentum", "atr_breakout"]  # 這兩個自己的BUY/SELL事件本來就是配好對的


@st.cache_data(ttl=300, show_spinner=False)
def _compute_track_records(_config, symbols: tuple):
    """算「策略」類(NOTIFIABLE_STRATEGIES)在每支股票自己歷史資料上的勝率/平均報酬，
    給使用者參考「這個策略在這支股票的過去表現」，不是自動下單依據。快取5分鐘——這是跑
    全部歷史資料的策略運算，不用每次▲▼/新增股票都重算一次。buy_formula/sell_formula
    只定義單邊，個別算沒意義，這裡把兩者的事件合併成一組配對的進出場來看(跟使用者設計
    這兩個公式的原意一致：一個負責進場條件，一個負責出場條件)。"""
    rows = []
    with connect(_config.db_path) as conn:
        for code in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            if bars.empty:
                continue

            row = {"代號": code}
            for name in TRACK_RECORD_STRATEGIES:
                events = STRATEGY_REGISTRY[name].evaluate(code, bars, _config.strategy_params.get(name, {}))
                trades, _ = simulate_round_trips(events)
                summary = summarize_trades(trades)
                row[STRATEGY_LABELS[name].split("(")[0]] = (
                    f"{summary['win_rate']:.0f}%勝率 / {summary['avg_return_pct']:+.1f}%平均（{summary['n']}筆）"
                    if summary
                    else "尚無完整交易紀錄"
                )

            combined_events = STRATEGY_REGISTRY["buy_formula"].evaluate(
                code, bars, _config.strategy_params.get("buy_formula", {})
            ) + STRATEGY_REGISTRY["sell_formula"].evaluate(code, bars, _config.strategy_params.get("sell_formula", {}))
            trades, _ = simulate_round_trips(combined_events)
            summary = summarize_trades(trades)
            row["極簡買賣公式"] = (
                f"{summary['win_rate']:.0f}%勝率 / {summary['avg_return_pct']:+.1f}%平均（{summary['n']}筆）"
                if summary
                else "尚無完整交易紀錄"
            )
            rows.append(row)
    return rows


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_today_intraday(_config, symbols: tuple):
    """現場連線Shioaji抓觀察清單今天的分K，供「今日走勢」小圖用，不用等run_live.py
    整天掛著累積。快取60秒——▲▼/移除按鈕每次點擊都會讓整頁重新執行，沒有快取的話
    每次點擊都要重新登入Shioaji。"""
    client = ShioajiClient(_config)
    client.connect()
    try:
        return client.fetch_today_kbars(list(symbols))
    finally:
        client.disconnect()


if "checked_for_updates" not in st.session_state:
    st.session_state.checked_for_updates = True
    with st.spinner("檢查有沒有新的盤後資料..."):
        result = check_and_update(config)
    if result["watchlist_empty"]:
        pass  # 觀察清單是空的，下面的頁籤本來就會提示要先跑fetch_historical.py
    elif result["new_price_days"] == 0 and result["new_market_days"] == 0 and not result["otc_synced"]:
        st.toast("資料已經是最新的，沒有新的盤後資料", icon="✅")
    else:
        parts = [f"股價 {result['new_price_days']} 天", f"上市籌碼 {result['new_market_days']} 天"]
        if result["otc_synced"]:
            parts.append("上櫃籌碼已同步最新一天")
        st.toast(f"已更新：{'、'.join(parts)}", icon="🔄")

    for error in result["errors"]:
        st.warning(f"⚠️ {error}（不影響其他資料，稍後重新整理再試一次即可）")

tab_watchlist, tab_chart, tab_fundamentals, tab_history = st.tabs(["觀察清單", "K線圖", "籌碼/基本面", "訊號紀錄"])

with connect(config.db_path) as conn:
    watchlist_rows = fetch_watchlist(conn)

watchlist = [dict(r) for r in watchlist_rows]
symbols = [w["code"] for w in watchlist]

with tab_watchlist:
    st.subheader("觀察清單")

    with st.form("add_symbol_form", clear_on_submit=True):
        add_col1, add_col2 = st.columns([3, 1])
        new_code = add_col1.text_input(
            "新增股票代號", placeholder="輸入股票代號，例如 2603", label_visibility="collapsed"
        )
        add_submitted = add_col2.form_submit_button("新增到觀察清單", use_container_width=True)
    if add_submitted and new_code.strip():
        with st.spinner(f"抓取 {new_code.strip()} 資料..."):
            add_result = add_symbol_to_watchlist(config, new_code.strip())
        (st.success if add_result["ok"] else st.warning)(add_result["message"])
        st.rerun()

    if watchlist:
        strategy_keys = [k for k in STRATEGY_LABELS if k in NOTIFIABLE_STRATEGIES]
        indicator_keys = [k for k in STRATEGY_LABELS if k not in NOTIFIABLE_STRATEGIES]
        st.caption(
            f"目前每檔股票都套用同一組 {len(indicator_keys)} 種指標訊號 + {len(strategy_keys)} 種策略"
            "（還沒有支援每檔各自挑）："
        )
        st.caption(f"📊 策略（會推播Telegram）：{'、'.join(STRATEGY_LABELS[k] for k in strategy_keys)}")
        st.caption(f"📈 指標訊號（只記錄不推播）：{'、'.join(STRATEGY_LABELS[k] for k in indicator_keys)}")
        st.markdown("#### 總覽（價位/均線/指標，暫用最新收盤價，之後接即時報價會自動換資料源；▲▼可調整順序）")

        overview_rows = build_overview_rows(config)
        try:
            intraday_bars = _fetch_today_intraday(config, tuple(symbols)) if symbols else {}
        except Exception as exc:
            intraday_bars = {}
            st.warning(f"⚠️ 抓即時盤中資料失敗，今日走勢欄位暫時顯示「尚無盤中資料」：{exc}")

        headers = ["", "", "代號", "名稱", "今日走勢", "漲跌", "目前價位", "5日", "10日", "月線", "季線", "RSI", "MACD", "布林通道", "成交量", "KD", "三大法人", ""]
        widths = [0.35, 0.35, 0.8, 0.9, 1.3, 1.2, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 1.3, 1.6, 0.9]

        header_cols = st.columns(widths, vertical_alignment="center", gap="xxsmall")
        for col, label in zip(header_cols, headers):
            if label:
                col.markdown(f"**{label}**")

        with st.container(key="watchlist_rows"):
            for i, row in enumerate(overview_rows):
                cols = st.columns(widths, vertical_alignment="center", gap="xxsmall")
                code = row["代號"]

                if cols[0].button("▲", key=f"up_{code}", disabled=(i == 0)):
                    with connect(config.db_path) as conn:
                        move_watchlist_symbol(conn, code, direction=-1)
                    st.rerun()
                if cols[1].button("▼", key=f"down_{code}", disabled=(i == len(overview_rows) - 1)):
                    with connect(config.db_path) as conn:
                        move_watchlist_symbol(conn, code, direction=1)
                    st.rerun()

                cols[2].write(row["代號"])
                cols[3].write(row["名稱"])

                today_bars_raw = intraday_bars.get(code, [])
                prev_close = row["昨收"]
                if len(today_bars_raw) >= 2 and prev_close is not None:
                    # 分時線至少要2個點才能畫出一段線，只有1筆的話畫不出東西
                    today_bars = bars_list_to_dataframe(today_bars_raw)
                    cols[4].plotly_chart(
                        intraday_line_chart(today_bars, prev_close),
                        use_container_width=True,
                        config={"displayModeBar": False},
                    )
                else:
                    cols[4].write("尚無盤中資料")

                cols[5].markdown(row["漲跌"], unsafe_allow_html=True)
                cols[6].markdown(row["目前價位"], unsafe_allow_html=True)

                for col, field in zip(
                    cols[7:15],
                    ["5日", "10日", "月線", "季線", "RSI", "MACD", "布林通道", "成交量"],
                ):
                    col.markdown(row[field], unsafe_allow_html=True)

                kd_df = row["KD"].dropna()
                if len(kd_df) >= 2:
                    cols[15].plotly_chart(
                        kd_chart(kd_df),
                        use_container_width=True,
                        config={"displayModeBar": False},
                    )
                else:
                    cols[15].write("—")

                cols[16].markdown(row["三大法人"].replace("\n", "<br>"), unsafe_allow_html=True)

                if cols[17].button("移除", key=f"remove_{code}", use_container_width=True):
                    with connect(config.db_path) as conn:
                        remove_from_watchlist(conn, code)
                    st.rerun()

        st.markdown("#### 建議買進（目前符合極簡買進公式3步驟，不是edge-triggered，訊號沒被打破就會一直列著）")
        recommendations = build_buy_recommendations(config)
        if recommendations:
            st.dataframe(pd.DataFrame(recommendations), use_container_width=True, hide_index=True)
        else:
            st.caption("目前沒有股票符合")

        with st.expander("📊 策略歷史勝率參考（不是自動下單依據，只是這個策略在這支股票過去表現如何）"):
            track_records = _compute_track_records(config, tuple(symbols))
            if track_records:
                st.dataframe(pd.DataFrame(track_records), use_container_width=True, hide_index=True)
            else:
                st.caption("歷史資料不足，算不出任何一次完整的進出場")
    else:
        st.info("觀察清單是空的，用上面欄位新增股票，或先跑 `python scripts/fetch_historical.py` 填範例資料")

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
    col_symbol, col_strategy = st.columns(2)
    filter_symbol = col_symbol.selectbox("篩選股票（可選）", ["全部"] + symbols)
    filter_strategy = col_strategy.selectbox(
        "篩選訊號/策略（可選）",
        ["全部"] + list(STRATEGY_REGISTRY),
        format_func=lambda k: k if k == "全部" else f"[{'策略' if k in NOTIFIABLE_STRATEGIES else '指標訊號'}] {k}",
    )
    with connect(config.db_path) as conn:
        rows = fetch_signal_events(
            conn,
            symbol=None if filter_symbol == "全部" else filter_symbol,
            strategy=None if filter_strategy == "全部" else filter_strategy,
            limit=200,
        )

    if not rows:
        st.info("目前沒有任何訊號紀錄（backtest.py不會寫入signal_events，要跑live/batch才會有）")
    else:
        df = pd.DataFrame([dict(r) for r in rows])
        st.dataframe(df[["ts", "symbol", "strategy", "direction", "price", "detail", "tier"]], use_container_width=True)
