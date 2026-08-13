import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from charts import candlestick_with_ma, institutional_flow_chart, intraday_line_chart, kd_chart, margin_balance_chart
from stocks.config import load_config
from stocks.daily_update import add_symbol_to_watchlist, check_and_update, should_check_for_updates
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
    get_disabled_strategies,
    get_setting,
    init_db,
    move_watchlist_symbol,
    remove_from_watchlist,
    set_setting,
)
from stocks.shioaji_client import ShioajiClient
from stocks.strategies import STRATEGY_LABELS, STRATEGY_REGISTRY, strategy_label
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades, summarize_trades
from stocks.watchlist_view import build_overview_rows, build_paper_trades, build_strategy_recommendations

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

config = load_config()
init_db(config.db_path)  # 確保schema是最新的(例如app_settings表)，下面馬上要用get_setting()


TRACK_RECORD_STRATEGIES = ["chip_momentum", "trust_momentum", "atr_breakout", "trend_following", "breakout", "long_swing"]  # 這幾個自己的BUY/SELL事件本來就是配好對的
# golden_cross_scaleout一次進場配兩次出場(先賣一半、再賣剩餘一半)，跟上面幾個「一買配一賣」
# 的形狀不一樣，直接套simulate_round_trips會把第一次半倉出場當成整筆平倉、報酬率算錯，
# 要用simulate_scaleout_trades另外配對，所以不放進TRACK_RECORD_STRATEGIES一起迴圈處理。
SCALEOUT_STRATEGY = "golden_cross_scaleout"


@st.cache_data(ttl=300, show_spinner=False)
def _compute_track_records(_config, symbols: tuple):
    """算「策略」類(NOTIFIABLE_STRATEGIES)在每支股票自己歷史資料上的勝率/平均報酬，
    給使用者參考「這個策略在這支股票的過去表現」，不是自動下單依據。快取5分鐘——這是跑
    全部歷史資料的策略運算，不用每次▲▼/新增股票都重算一次。

    「（已排除）」標記對應scripts/recompute_strategy_selection.py寫進symbols.
    disabled_strategies的排除清單——run_live.py/run_batch.py評估這支股票時會跳過這些
    策略，不會通知/寫進signal_events，但這裡的歷史勝率分析不受影響，照樣完整顯示，
    讓使用者知道「這個策略對這支股票表現不好，所以被排除」的理由是什麼。"""
    rows = []
    with connect(_config.db_path) as conn:
        names = {row["code"]: row["name"] for row in fetch_watchlist(conn)}
        for code in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
            if bars.empty:
                continue
            disabled = set(get_disabled_strategies(conn, code))

            def cell_text(name: str, summary: dict | None) -> str:
                text = (
                    f"{summary['win_rate']:.0f}%勝率 / {summary['avg_return_pct']:+.1f}%平均 / "
                    f"{summary['total_return_pct']:+.1f}%加總（{summary['n']}筆）"
                    if summary
                    else "尚無完整交易紀錄"
                )
                return f"{text} (已排除)" if name in disabled else text

            row = {"代號": code, "名稱": names.get(code) or "—"}
            for name in TRACK_RECORD_STRATEGIES:
                events = STRATEGY_REGISTRY[name].evaluate(code, bars, _config.strategy_params.get(name, {}))
                trades, _ = simulate_round_trips(events)
                row[STRATEGY_LABELS[name].split("(")[0]] = cell_text(name, summarize_trades(trades))

            scaleout_events = STRATEGY_REGISTRY[SCALEOUT_STRATEGY].evaluate(
                code, bars, _config.strategy_params.get(SCALEOUT_STRATEGY, {})
            )
            scaleout_trades, _ = simulate_scaleout_trades(scaleout_events)
            row[STRATEGY_LABELS[SCALEOUT_STRATEGY].split("(")[0]] = cell_text(
                SCALEOUT_STRATEGY, summarize_trades(scaleout_trades)
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


@st.fragment(run_every="30s")
def render_watchlist_table(config, symbols: tuple):
    """總覽表格獨立成fragment，每30秒自己重新跑一次(不影響頁面其他部分)，這樣「目前
    價位」/「漲跌」/「今日走勢」才會自動反映最新資料，使用者不用手動整頁重新整理——
    這幾個欄位背後看的是bars_5min(run_live.py即時累積)跟Shioaji現場連線，資料本身
    是活的，只差頁面沒有自動重新渲染。▲▼/移除按鈕維持原本st.rerun()預設的整頁重新
    執行(fragment內呼叫st.rerun()預設scope="app"，不用特別處理)。"""
    overview_rows = build_overview_rows(config)
    try:
        intraday_bars = _fetch_today_intraday(config, symbols) if symbols else {}
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


# st.session_state在瀏覽器重新整理時會重置(每次整頁重新載入=新的session)，靠它做「只檢查
# 一次」完全沒用，實測每次F5都還是會重打一次API。改成把「上次檢查時間」存進DB(跨session/
# 跨重新整理都留著)，讓should_check_for_updates()決定今天還要不要再檢查一次。
_now = datetime.now()
with connect(config.db_path) as conn:
    _last_check_str = get_setting(conn, "last_data_check")
_last_check = datetime.fromisoformat(_last_check_str) if _last_check_str else None

if should_check_for_updates(_last_check, _now):
    with st.spinner("檢查有沒有新的盤後資料..."):
        result = check_and_update(config)
    with connect(config.db_path) as conn:
        set_setting(conn, "last_data_check", _now.isoformat())
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

tab_watchlist, tab_chart, tab_fundamentals, tab_history, tab_strategy_logic = st.tabs(
    ["觀察清單", "K線圖", "籌碼/基本面", "訊號紀錄", "策略邏輯"]
)

with connect(config.db_path) as conn:
    watchlist_rows = fetch_watchlist(conn)

watchlist = [dict(r) for r in watchlist_rows]
symbols = [w["code"] for w in watchlist]

with tab_watchlist:
    st.subheader("觀察清單")

    with st.form("add_symbol_form", clear_on_submit=True):
        add_col1, add_col2 = st.columns([3, 1])
        new_code = add_col1.text_input(
            "新增股票代號", placeholder="輸入股票代號或中文名稱，例如 2603 或 華電網", label_visibility="collapsed"
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
            f"每檔股票套用 {len(indicator_keys)} 種指標訊號 + {len(strategy_keys)} 種策略，"
            "策略部分依scripts/recompute_strategy_selection.py的backtest結果各自排除表現不好的（見下方「策略歷史勝率參考」的「(已排除)」標記）："
        )
        st.caption(f"📊 策略（會推播Telegram）：{'、'.join(STRATEGY_LABELS[k] for k in strategy_keys)}")
        st.caption(f"📈 指標訊號（只記錄不推播）：{'、'.join(STRATEGY_LABELS[k] for k in indicator_keys)}")
        st.markdown(
            "#### 總覽（價位/均線/指標，暫用最新收盤價，之後接即時報價會自動換資料源；▲▼可調整順序，每30秒自動更新）"
        )
        render_watchlist_table(config, tuple(symbols))

        st.markdown("#### 買進/賣出策略訊號（一列一個策略，標示觸發當天的價格/日期，現價供對照；預設依觸發日期新到舊排序）")
        filter_col1, filter_col2, _filter_spacer = st.columns([1, 2, 3])
        today_only = filter_col1.checkbox("只顯示今天觸發", key="buy_recommendations_today_only")
        symbol_options = [f"{w['code']} {w['name']}" for w in watchlist]
        selected_symbols = filter_col2.multiselect(
            "只看特定股票", symbol_options, key="buy_recommendations_symbol_filter"
        )

        today_str = date.today().strftime("%Y-%m-%d")
        recommendations = build_strategy_recommendations(config)
        if today_only:
            recommendations = [r for r in recommendations if r["觸發日期"] == today_str]
        if selected_symbols:
            selected_codes = {s.split(" ", 1)[0] for s in selected_symbols}
            recommendations = [r for r in recommendations if r["代號"] in selected_codes]
        recommendations = sorted(recommendations, key=lambda r: r["觸發日期"], reverse=True)

        if recommendations:
            display_rows = [
                {
                    "代號": r["代號"],
                    "名稱": r["名稱"],
                    "買進策略": STRATEGY_LABELS.get(r["買進策略"], r["買進策略"]).split("(")[0] if r["買進策略"] else "",
                    "賣出策略": STRATEGY_LABELS.get(r["賣出策略"], r["賣出策略"]).split("(")[0] if r["賣出策略"] else "",
                    "觸發價格": r["觸發價格"],
                    "現價": r["現價"],
                    "觸發日期": r["觸發日期"],
                }
                for r in recommendations
            ]
            st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
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
        format_func=lambda k: k if k == "全部" else f"[{'策略' if k in NOTIFIABLE_STRATEGIES else '指標訊號'}] {strategy_label(k)}",
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
        df["strategy"] = df["strategy"].apply(strategy_label)
        st.dataframe(df[["ts", "symbol", "strategy", "direction", "price", "detail", "tier"]], use_container_width=True)

    st.markdown("#### 模擬交易紀錄（觀察策略是否可行）")
    st.caption(
        "從下面選的日期開始，每個策略每次BUY訊號當作買進、配對到SELL訊號當作賣出，純粹照訊號模擬記錄，"
        "不是真的下單；「持有中」代表還沒配到出場訊號，報酬率用現價估算(未實現)。"
    )
    paper_start = st.date_input("模擬起始日期", value=date(2026, 7, 1), key="paper_trades_start")
    paper_trades = build_paper_trades(config, start_date=paper_start.strftime("%Y-%m-%d"))

    if not paper_trades:
        st.info("這段時間沒有任何策略觸發買進訊號")
    else:
        paper_trades = [{**r, "策略": strategy_label(r["策略"])} for r in paper_trades]
        by_symbol_df = pd.DataFrame(paper_trades)
        by_symbol = (
            by_symbol_df.groupby(["代號", "名稱"])["報酬率(%)"]
            .agg(交易筆數="count", 平均報酬="mean", 加總報酬="sum")
            .round(1)
            .reset_index()
            .sort_values("加總報酬", ascending=False)
        )
        st.caption(
            "「平均報酬」是這支股票在這段時間所有策略交易(已平倉+持有中未實現)報酬率的平均；"
            "「加總報酬」是把每一筆報酬率直接加起來(不是複利)，2026-08-08使用者指出只看平均會"
            "低估——例如2408這段期間漲了16倍，但每筆交易各自進出、只吃到片段的漲幅，平均自然"
            "遠低於整體漲幅，加總報酬能看出「這些交易合計貢獻了多少」。兩者都不是實際下單報酬，"
            "只是訊號品質的參考。"
        )
        st.dataframe(by_symbol, use_container_width=True, hide_index=True)

        closed = [r for r in paper_trades if r["狀態"] == "已平倉"]
        if closed:
            summary_df = pd.DataFrame(closed)
            summary = (
                summary_df.groupby("策略")["報酬率(%)"]
                .agg(筆數="count", 勝率=lambda s: (s > 0).mean() * 100, 平均報酬="mean")
                .round(1)
                .reset_index()
            )
            st.dataframe(summary, use_container_width=True, hide_index=True)

        trades_df = pd.DataFrame(paper_trades).sort_values("買進日期", ascending=False)
        trades_df["賣出日期"] = trades_df["賣出日期"].fillna("持有中")
        # 賣出價位維持數字型別(NaN)讓Arrow序列化不會因為跟已平倉的float混在一起而出錯，
        # st.dataframe本身就會把NaN顯示成空白，不需要另外塞"—"字串
        st.dataframe(trades_df, use_container_width=True, hide_index=True)

with tab_strategy_logic:
    st.subheader("策略邏輯")
    st.caption(
        "「策略」進場+出場邏輯完整綁在一起，可以直接依據行動，會推播Telegram，每個策略類別本身的"
        "docstring就是這裡的說明文字；「指標訊號」單獨一個不構成完整交易系統，只記錄不推播，"
        "用一行帶過。目前套用的參數(config.json的strategy_params)列在每個策略說明下面。"
    )

    st.markdown("### 📊 策略（會推播Telegram）")
    for name in TRACK_RECORD_STRATEGIES + [SCALEOUT_STRATEGY]:
        strategy = STRATEGY_REGISTRY.get(name)
        if strategy is None:
            continue
        with st.expander(strategy_label(name), expanded=False):
            st.text((type(strategy).__doc__ or "（沒有說明文件）").strip())
            params = config.strategy_params.get(name, {})
            if params:
                st.caption("目前參數：")
                st.json(params)

    st.markdown("### 📈 指標訊號（只記錄不推播）")
    indicator_rows = [
        {"策略": strategy_label(name), "說明": STRATEGY_LABELS.get(name, name)}
        for name in STRATEGY_REGISTRY
        if name not in NOTIFIABLE_STRATEGIES
    ]
    st.dataframe(pd.DataFrame(indicator_rows), use_container_width=True, hide_index=True)
