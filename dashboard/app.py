import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import streamlit as st

from charts import intraday_line_chart, kd_chart, price_and_chip_chart
from stocks.config import load_config
from stocks.daily_update import add_symbol_to_watchlist, check_and_update, is_market_open_now, should_check_for_updates
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.db import (
    attach_institutional_flows,
    attach_monthly_revenue_growth,
    bars_list_to_dataframe,
    bars_to_dataframe,
    connect,
    export_watchlist_snapshot,
    fetch_bars_daily,
    fetch_ex_dividend_schedule,
    fetch_institutional_flows,
    fetch_monthly_revenue,
    fetch_signal_events,
    fetch_valuations,
    fetch_watchlist,
    get_disabled_strategies,
    get_setting,
    get_symbol_groups,
    import_watchlist_snapshot,
    init_db,
    move_watchlist_symbol,
    remove_from_watchlist,
    set_setting,
    set_symbol_groups,
    watchlist_sync_path,
)
from stocks.shioaji_client import ShioajiClient
from stocks.strategies import STRATEGY_LABELS, STRATEGY_REGISTRY, strategy_label
from stocks.strategy_selection import (
    MIN_AVG_RETURN_PCT,
    MIN_PROFIT_FACTOR,
    MIN_TOTAL_RETURN_PCT,
    MIN_TRADES_FOR_RANKING,
)
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades
from stocks.watchlist_view import (
    build_overview_row_for_symbol,
    build_paper_trades_for_symbol,
    build_strategy_recommendations_for_symbol,
)

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

# 2026-08-17：使用者有兩台電腦各自跑這個專案，觀察清單/群組用watchlist_shared.json
# (跟db_path同目錄，沒有被.gitignore排除)透過git在兩台機器間同步——歷史資料量太大不
# 適合整包用git同步，留在各自機器獨立累積。這裡每次載入dashboard都檢查一次(單純讀本地
# 檔案比對，很便宜，不用像check_and_update那樣節流)，如果偵測到檔案內容(可能是git pull
# 下來的)跟本地資料庫不一樣就套用進去；git add/commit/push都是使用者自己手動做，
# dashboard不會自動碰git。
with connect(config.db_path) as conn:
    import_watchlist_snapshot(conn, watchlist_sync_path(config.db_path))


# 「策略歷史勝率」表格要列的策略——2026-08-15前golden_cross_scaleout是一買配兩賣的
# 分批出場，要用simulate_scaleout_trades另外配對，特別排除在外處理；換成單一停損全出
# 當預設後，所有NOTIFIABLE_STRATEGIES都是一買配一賣，直接用同一份清單、同一套
# simulate_round_trips迴圈處理即可，不用再特殊分流。
TRACK_RECORD_STRATEGIES = sorted(NOTIFIABLE_STRATEGIES)


@st.cache_data(ttl=300, show_spinner=False)
def _compute_track_record_for_symbol(_config, code: str) -> dict | None:
    """單一股票的策略歷史勝率/平均報酬——2026-08-17拆成per-symbol快取(原本是
    _compute_track_records整批用tuple(symbols)當快取鍵)：切換群組(不同子集合)幾乎
    每次都換一個新的tuple，全部cache miss，10年資料量下每次都要重跑7個策略x每支股票
    2000多天的Python迴圈，切換群組變得很慢(全觀察清單22檔約8秒)。拆成per-symbol後，
    同一支股票不管出現在哪個群組都是同一個快取鍵，只有真的沒被任何畫面算過的股票才需要
    重新算，切換群組多半是全部cache hit、瞬間完成。

    ❌前綴對應scripts/recompute_strategy_selection.py寫進symbols.disabled_strategies的
    排除清單——run_live.py/run_batch.py評估這支股票時會跳過這些策略，不會通知/寫進
    signal_events，但這裡的歷史勝率分析不受影響，照樣完整顯示，讓使用者知道「這個策略
    對這支股票表現不好，所以被排除」的理由是什麼。2026-08-16改用❌emoji前綴取代原本
    文字後綴「(已排除)」——st.dataframe的儲存格是純文字(MarkdownColumn也只有點開儲存格
    才會顯示成markdown，不是直接顯示在格子裡)，沒辦法只把「已排除」這幾個字變色，emoji
    本身就是有色圖案字元，不用任何markdown/HTML技巧就能在儲存格裡直接顯示紅色標記，
    使用者確認這樣比純文字後綴更好辨識。"""
    with connect(_config.db_path) as conn:
        name_row = conn.execute("SELECT name FROM symbols WHERE code = ?", (code,)).fetchone()
        bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
        bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
        bars = attach_monthly_revenue_growth(bars, [dict(r) for r in fetch_monthly_revenue(conn, code)])
        if bars.empty:
            return None
        disabled = set(get_disabled_strategies(conn, code))

    def cell_text(name: str, summary: dict | None) -> str:
        if summary:
            pf = summary["profit_factor"]
            pf_text = f"{pf:.1f}" if pf is not None else "∞(無虧損)"
            text = (
                f"{summary['win_rate']:.0f}%勝率 / {summary['avg_return_pct']:+.1f}%平均 / "
                f"{summary['total_return_pct']:+.1f}%加總（{summary['n']}筆）/ "
                f"獲利因子{pf_text} / 最大回撤-{summary['max_drawdown_pct']:.1f}%"
            )
        else:
            text = "尚無完整交易紀錄"
        return f"❌ {text}" if name in disabled else text

    row = {"代號": code, "名稱": (name_row["name"] if name_row else None) or "—"}
    for name in TRACK_RECORD_STRATEGIES:
        strategy_params = _config.strategy_params.get(name, {})
        events = STRATEGY_REGISTRY[name].evaluate(code, bars, strategy_params)
        trades, _ = (
            simulate_scaleout_trades(events) if is_scaleout_strategy(name, strategy_params) else simulate_round_trips(events)
        )
        row[STRATEGY_LABELS[name].split("(")[0]] = cell_text(name, summarize_trades(trades))
    return row


def _compute_track_records(_config, symbols: tuple):
    """組合每支股票各自的快取結果(見_compute_track_record_for_symbol)——這層本身不用
    st.cache_data，因為裡面每一支都已經是各自快取過的，這層只是便宜的list組裝，重算
    也不痛不癢，不用為它另外佔一份快取空間。"""
    rows = [_compute_track_record_for_symbol(_config, code) for code in symbols]
    return [row for row in rows if row is not None]


@st.cache_data(ttl=30, show_spinner=False)
def _cached_overview_row(_config, code: str):
    """build_overview_row_for_symbol本身不帶快取(watchlist_view.py是純商業邏輯模組，
    直接被tests/test_watchlist_view.py單元測試呼叫，不該混進streamlit依賴/快取語意)，
    這裡包一層per-symbol快取給dashboard用。2026-08-17一開始是整個觀察清單一起快取
    (單一cache key)，2026-08-18改成per-symbol：▲▼移動排序/移除/新增股票都只影響那一支，
    跟其他股票的RSI/MACD/KD等指標完全無關，整包快取代表每次移動/刪除都要clear掉重算
    全部股票(10年資料量下這是移動/刪除變慢的主因)；拆成per-symbol後，移動/刪除/切換
    群組完全不用清快取，只有真的還沒算過的股票(新增/還沒過期)才需要重算。快取30秒，
    跟render_watchlist_table的run_every="30s"對齊。"""
    return build_overview_row_for_symbol(_config, code)


def _cached_overview_rows_for(_config, codes: tuple):
    """組合每支股票各自的快取結果(見_cached_overview_row)——這層本身不用st.cache_data，
    理由跟_compute_track_records同一套：裡面每一支都已經各自快取過，這層只是便宜的list
    組裝。"""
    return [_cached_overview_row(_config, code) for code in codes]


@st.cache_data(ttl=60, show_spinner=False)
def _cached_chart_data(_config, symbol: str):
    """K線圖頁籤的資料抓取+畫圖包一層快取——這張圖疊了K線+4條均線+布林通道+成交量+
    法人買賣超+KD+MACD好幾個指標，10年資料量下重算有一定成本；沒有這層快取的話，使用者
    切換群組、點擊觀察清單的▲▼/移除等任何觸發整頁rerun的操作，都會連帶重新抓資料+
    重畫這張圖，即使使用者根本沒有在看這個頁籤。快取60秒，同一支股票短時間內重複顯示
    不用重算。"""
    with connect(_config.db_path) as conn:
        bars = bars_to_dataframe(fetch_bars_daily(conn, symbol), ts_field="date")
        flow_rows = [dict(r) for r in fetch_institutional_flows(conn, symbol)]
        valuation_rows = [dict(r) for r in fetch_valuations(conn, symbol)]
        ex_div_rows = [dict(r) for r in fetch_ex_dividend_schedule(conn, symbol)]

    fig = None
    if not bars.empty:
        flow_df = pd.DataFrame(flow_rows) if flow_rows else None
        fig = price_and_chip_chart(bars, flow_df, ma_windows=[5, 10, 20, 60])

    return {
        "bars_empty": bars.empty,
        "fig": fig,
        "has_flow": bool(flow_rows),
        "valuation_rows": valuation_rows,
        "ex_div_rows": ex_div_rows,
    }


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_today_intraday(_config, symbols: tuple):
    """現場連線Shioaji抓觀察清單今天的分K，供「今日走勢」小圖用，不用等run_live.py
    整天掛著累積。快取30秒，跟render_watchlist_table的run_every="30s"對齊，走勢圖才會
    跟目前價位同一個節奏更新；▲▼/移除按鈕點擊時如果快取還沒過期也會直接沿用，不用
    每次都重新登入Shioaji。呼叫端一定要傳「整個觀察清單」的symbols，不是群組篩選後的
    子集合——2026-08-17發現：如果傳群組篩選後的symbols，切換群組會換一個新的tuple，
    每次都要重新連線Shioaji(現場連線有真實網路延遲)，這是切換群組明顯變慢的主因之一。"""
    client = ShioajiClient(_config)
    client.connect()
    try:
        return client.fetch_today_kbars(list(symbols))
    finally:
        client.disconnect()


def _render_watchlist_table_body(config, all_symbols: tuple, symbols: tuple):
    """總覽表格的實際內容——2026-08-17拆成獨立函式，讓render_watchlist_table_live(開盤
    時段，30秒自動更新)/render_watchlist_table_static(收盤時段，不自動更新)兩個fragment
    共用同一份渲染邏輯，只差有沒有run_every。

    all_symbols是整個觀察清單(不受群組篩選影響，快取鍵穩定)，symbols是目前群組篩選後
    要顯示的子集合——2026-08-18改成per-symbol快取(見_cached_overview_row)後直接用
    symbols就好，不用再抓全部watchlist的資料才篩選，切換群組只會計算/命中這個群組
    實際需要的股票。

    2026-08-18發現：這個函式雖然被包在fragment裡，但fragment不會讓「其他地方觸發的
    整頁rerun」跳過自己執行——切群組(segmented_control在所有頁籤外面，不屬於任何
    fragment)、新增股票等操作一樣會整頁重新執行，這個fragment(連同這裡的Shioaji
    即時連線)照樣會跑一次，不管使用者當下在看哪個頁籤(K線圖/訊號紀錄都一樣)。收盤
    時段股價/籌碼資料根本不會變，即時分K也不會有新資料，之前只讓_render_watchlist_
    table_static不要「自動」輪詢(拿掉run_every)，但沒擋掉「被動」觸發時仍然會真的
    連線Shioaji這件事——這才是切群組/訊號紀錄頁籤偶爾還是覺得慢的殘留原因(30秒快取
    一過期，下一次任何操作觸發的整頁rerun就要重新登入Shioaji，跟使用者在看哪個頁籤
    無關)。收盤時段直接跳過這次連線，不只是不自動排程。"""
    overview_rows = _cached_overview_rows_for(config, symbols)
    if all_symbols and is_market_open_now(config, datetime.now()):
        try:
            intraday_bars = _fetch_today_intraday(config, all_symbols)
        except Exception as exc:
            intraday_bars = {}
            st.warning(f"⚠️ 抓即時盤中資料失敗，今日走勢欄位暫時顯示「尚無盤中資料」：{exc}")
    else:
        intraday_bars = {}

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
                    move_watchlist_symbol(conn, code, direction=-1, visible_codes=set(symbols))
                    export_watchlist_snapshot(conn, watchlist_sync_path(config.db_path))
                # 排序不影響任何股票自己的指標數值，不用clear _cached_overview_row
                # (2026-08-18改成per-symbol快取後這裡不用再重算整個觀察清單)。
                st.rerun()
            if cols[1].button("▼", key=f"down_{code}", disabled=(i == len(overview_rows) - 1)):
                with connect(config.db_path) as conn:
                    move_watchlist_symbol(conn, code, direction=1, visible_codes=set(symbols))
                    export_watchlist_snapshot(conn, watchlist_sync_path(config.db_path))
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
                    export_watchlist_snapshot(conn, watchlist_sync_path(config.db_path))
                # 移除的股票之後不會再被_cached_overview_rows_for請求，不用特地clear它
                # 的快取項目——30秒TTL到期後自然被cache淘汰。
                st.rerun()


@st.fragment(run_every="30s")
def _render_watchlist_table_live(config, all_symbols: tuple, symbols: tuple):
    _render_watchlist_table_body(config, all_symbols, symbols)


@st.fragment()
def _render_watchlist_table_static(config, all_symbols: tuple, symbols: tuple):
    _render_watchlist_table_body(config, all_symbols, symbols)


def render_watchlist_table(config, all_symbols: tuple, symbols: tuple):
    """開盤時段用30秒自動更新(_render_watchlist_table_live)，收盤時段股價/籌碼資料不會變，
    自動輪詢(尤其是Shioaji現場連線)是白工，改用不自動更新的版本(_render_watchlist_table_
    static)——2026-08-17使用者要求。兩個都還是fragment，▲▼/移除按鈕本來就是靠按下去
    st.rerun()才更新，不受這裡差異影響。"""
    if is_market_open_now(config, datetime.now()):
        _render_watchlist_table_live(config, all_symbols, symbols)
    else:
        _render_watchlist_table_static(config, all_symbols, symbols)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_strategy_recommendations_for_symbol(_config, code: str):
    """build_strategy_recommendations_for_symbol本身不帶快取(watchlist_view.py是純
    商業邏輯模組，直接被tests/test_watchlist_view.py單元測試呼叫)，這裡包一層per-symbol
    快取給dashboard用。ttl=30跟render_strategy_recommendations的run_every="30s"對齊，
    不快取更久——這張表的「現價」要反映當下報價，快取太久會重新引入2026-08-13已經修過
    的現價不同步問題。2026-08-17發現整批版本全觀察清單10年資料下要跑4.5秒，切換群組
    都要重付這筆成本；2026-08-18改成per-symbol：切群組只需要那個群組實際涵蓋的股票，
    不用管其他股票的快取有沒有過期，也不會因為某支股票的資料還沒過期就被迫連帶重算。"""
    return build_strategy_recommendations_for_symbol(_config, code)


def _cached_strategy_recommendations_for(_config, codes) -> list[dict]:
    """組合每支股票各自的快取結果——這層本身不用st.cache_data，理由跟
    _cached_overview_rows_for同一套：裡面每一支都已經各自快取過，這層只是便宜的list
    組裝。"""
    rows = []
    for code in codes:
        rows.extend(_cached_strategy_recommendations_for_symbol(_config, code))
    return rows


@st.cache_data(ttl=30, show_spinner=False)
def _cached_paper_trades_for_symbol(_config, code: str, start_date: str):
    """build_paper_trades_for_symbol本身不帶快取(理由同
    _cached_strategy_recommendations_for_symbol)。"持有中"部位的報酬率是用現價估算，
    快取太久會顯示過時的未實現報酬，所以一樣限制ttl=30。2026-08-17發現整批版本全觀察
    清單10年資料下要跑4.8秒，是切群組明顯變慢的主因之一；2026-08-18改成per-symbol，
    理由跟_cached_strategy_recommendations_for_symbol同一套。"""
    return build_paper_trades_for_symbol(_config, code, start_date=start_date)


def _cached_paper_trades_for(_config, codes, start_date: str) -> list[dict]:
    """組合每支股票各自的快取結果，理由同_cached_strategy_recommendations_for。"""
    rows = []
    for code in codes:
        rows.extend(_cached_paper_trades_for_symbol(_config, code, start_date))
    return rows


def _render_strategy_recommendations_body(config, watchlist: list[dict]):
    """跟render_watchlist_table一樣拆成獨立函式(見_render_watchlist_table_body同一個
    2026-08-17改動)，讓開盤/收盤兩個版本的fragment共用同一份內容，只差有沒有自動更新。
    這張表的「現價」之前沒有跟著自動更新，因為build_strategy_recommendations是在
    fragment外面呼叫的，只有整頁重新執行(按鈕點擊/手動重新整理)才會重算，2026-08-13
    使用者發現這裡的現價沒有同步。"""
    filter_col1, filter_col2, _filter_spacer = st.columns([1, 2, 3])
    today_only = filter_col1.checkbox("只顯示今天觸發", key="buy_recommendations_today_only")
    symbol_options = [f"{w['code']} {w['name']}" for w in watchlist]
    selected_symbols = filter_col2.multiselect(
        "只看特定股票", symbol_options, key="buy_recommendations_symbol_filter"
    )

    today_str = date.today().strftime("%Y-%m-%d")
    recommendations = _cached_strategy_recommendations_for(config, [w["code"] for w in watchlist])
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


@st.fragment(run_every="30s")
def _render_strategy_recommendations_live(config, watchlist: list[dict]):
    _render_strategy_recommendations_body(config, watchlist)


@st.fragment()
def _render_strategy_recommendations_static(config, watchlist: list[dict]):
    _render_strategy_recommendations_body(config, watchlist)


def render_strategy_recommendations(config, watchlist: list[dict]):
    """開盤時段30秒自動更新，收盤時段股價不會變，不需要自動輪詢——2026-08-17使用者要求，
    跟render_watchlist_table同一套dispatch邏輯。"""
    if is_market_open_now(config, datetime.now()):
        _render_strategy_recommendations_live(config, watchlist)
    else:
        _render_strategy_recommendations_static(config, watchlist)


@st.fragment()
def _render_chart_tab(config, watchlist: list[dict], symbols: list):
    """K線圖頁籤——2026-08-18包成fragment：原本選股下拉是tab_chart區塊裡的一般widget，
    不在任何fragment裡，切換股票會觸發整頁rerun，連帶重新執行觀察清單/訊號紀錄/策略邏輯
    其他三個頁籤的程式碼(即使使用者根本沒有在看那些頁籤)，是「切頁面/切股票感覺變慢」的
    原因之一。包成fragment後，這裡的選股/圖表互動只會重跑這個fragment，不會拖累其他頁籤。"""
    st.subheader("K線圖 + 均線 + 籌碼面（證交所免費公開資料）")
    if not symbols:
        st.info("觀察清單是空的，沒有資料可以畫圖")
        return

    chart_symbol_names = {w["code"]: w["name"] for w in watchlist}
    selected = st.selectbox(
        "選擇股票",
        symbols,
        format_func=lambda code: f"{code} {chart_symbol_names.get(code, '')}".strip(),
    )
    # 2026-08-15使用者要求把籌碼面資料搬到K線圖下方一起比對，不要分開頁籤來回切換——
    # 三張圖疊在同一個figure裡(shared_xaxes)，拖曳/縮放任一段時間軸，其他段會跟著對齊；
    # 資料抓取+畫圖包在_cached_chart_data裡(見上方定義)，避免切到其他頁籤/按其他按鈕
    # 觸發整頁rerun時被迫重算這張圖。
    chart_data = _cached_chart_data(config, selected)

    if chart_data["bars_empty"]:
        st.warning(f"{selected} 沒有歷史K棒資料")
    else:
        st.plotly_chart(chart_data["fig"], use_container_width=True)
        if not chart_data["has_flow"]:
            st.info("沒有三大法人資料，先跑 `python scripts/fetch_market_data.py`")

    st.markdown("#### 目前估值")
    valuation_rows = chart_data["valuation_rows"]
    if valuation_rows:
        latest = valuation_rows[-1]
        col1, col2, col3 = st.columns(3)
        col1.metric("本益比(PE)", latest["pe_ratio"] if latest["pe_ratio"] is not None else "N/A")
        col2.metric("殖利率(%)", latest["dividend_yield"])
        col3.metric("股價淨值比(PB)", latest["pb_ratio"])
    else:
        st.info("沒有估值資料，先跑 `python scripts/fetch_market_data.py`")

    st.markdown("#### 近期除權息")
    ex_div_rows = chart_data["ex_div_rows"]
    if ex_div_rows:
        ex_div_df = pd.DataFrame(ex_div_rows)
        st.dataframe(
            ex_div_df[["ex_date", "cash_dividend", "stock_dividend_ratio", "detail"]],
            use_container_width=True,
        )
    else:
        st.info("目前沒有排定中的除權息")


@st.fragment()
def _render_history_tab(config, watchlist: list[dict], symbols: list):
    """訊號紀錄頁籤——2026-08-18包成fragment，理由跟_render_chart_tab同一個：篩選股票/
    策略/模擬起始日期這些widget本來會觸發整頁rerun，連帶重跑觀察清單/K線圖/策略邏輯
    其他頁籤，包成fragment後這裡的篩選互動不會拖累其他頁籤。"""
    st.subheader("訊號歷史紀錄")
    symbol_names = {w["code"]: w["name"] for w in watchlist}
    col_symbol, col_strategy = st.columns(2)
    filter_symbol = col_symbol.selectbox(
        "篩選股票（可選）",
        ["全部"] + symbols,
        format_func=lambda k: k if k == "全部" else f"{k} {symbol_names.get(k, '')}",
    )
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
            symbols=symbols,
            limit=200,
        )

    if not rows:
        st.info("目前沒有任何訊號紀錄（backtest.py不會寫入signal_events，要跑live/batch才會有）")
    else:
        df = pd.DataFrame([dict(r) for r in rows])
        df["strategy"] = df["strategy"].apply(strategy_label)
        df["name"] = df["symbol"].map(symbol_names).fillna("—")  # 全市場批次掃描(tier=batch)
        # 的股票不在觀察清單裡，資料庫沒存名字，查不到就是None——顯示"—"跟其他地方缺值
        # 的慣例一致，不要讓使用者看到裸的"None"字串
        st.dataframe(
            df[["ts", "symbol", "name", "strategy", "direction", "price", "detail", "tier"]], use_container_width=True
        )

    st.markdown("#### 模擬交易紀錄（觀察策略是否可行）")
    st.caption(
        "從下面選的日期開始，每個策略每次BUY訊號當作買進、配對到SELL訊號當作賣出，純粹照訊號模擬記錄，"
        "不是真的下單；「持有中」代表還沒配到出場訊號，報酬率用現價估算(未實現)。"
    )
    paper_start_col, paper_symbol_col, paper_strategy_col = st.columns([1, 2, 2])
    paper_start = paper_start_col.date_input("模擬起始日期", value=date(2026, 7, 1), key="paper_trades_start")
    paper_symbol_options = [f"{w['code']} {w['name']}" for w in watchlist]
    paper_selected_symbols = paper_symbol_col.multiselect(
        "只看特定股票", paper_symbol_options, key="paper_trades_symbol_filter"
    )
    paper_strategy_options = sorted(NOTIFIABLE_STRATEGIES)  # build_paper_trades本身只跑
    # NOTIFIABLE_STRATEGIES(見watchlist_view.build_paper_trades docstring)，選項不用列
    # 單一指標，列了也永遠篩不出東西。
    paper_selected_strategies = paper_strategy_col.multiselect(
        "只看特定策略", paper_strategy_options, format_func=strategy_label, key="paper_trades_strategy_filter"
    )

    paper_trades = _cached_paper_trades_for(config, symbols, paper_start.strftime("%Y-%m-%d"))
    if paper_selected_symbols:
        paper_selected_codes = {s.split(" ", 1)[0] for s in paper_selected_symbols}
        paper_trades = [r for r in paper_trades if r["代號"] in paper_selected_codes]
    if paper_selected_strategies:
        paper_trades = [r for r in paper_trades if r["策略"] in paper_selected_strategies]

    if not paper_trades:
        st.info("這段時間沒有任何策略觸發買進訊號")
        return

    paper_trades = [{**r, "策略": strategy_label(r["策略"])} for r in paper_trades]
    by_symbol_df = pd.DataFrame(paper_trades)
    by_symbol = (
        by_symbol_df.groupby(["代號", "名稱"])["報酬率(%)"]
        .agg(交易筆數="count", 平均報酬="mean", **{"加總報酬(含持有中未實現)": "sum"})
        .round(1)
        .reset_index()
        .sort_values("加總報酬(含持有中未實現)", ascending=False)
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

        def _profit_factor(returns: pd.Series):
            gains = returns[returns > 0].sum()
            losses = -returns[returns < 0].sum()
            return gains / losses if losses > 0 else None

        summary = (
            summary_df.groupby("策略")["報酬率(%)"]
            .agg(筆數="count", 勝率=lambda s: (s > 0).mean() * 100, 獲利因子=_profit_factor, 平均報酬="mean", 加總報酬="sum")
            .round(1)
            .reset_index()
        )
        st.caption("「獲利因子」是總獲利/總虧損(絕對值)，None代表這個策略目前完全沒有虧損過的交易。")
        st.dataframe(summary, use_container_width=True, hide_index=True)

    trades_df = pd.DataFrame(paper_trades).sort_values("買進日期", ascending=False)
    trades_df["賣出日期"] = trades_df["賣出日期"].fillna("持有中")
    # 賣出價位維持數字型別(NaN)讓Arrow序列化不會因為跟已平倉的float混在一起而出錯，
    # st.dataframe本身就會把NaN顯示成空白，不需要另外塞"—"字串
    st.dataframe(trades_df, use_container_width=True, hide_index=True)


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

with connect(config.db_path) as conn:
    watchlist_rows = fetch_watchlist(conn)

all_watchlist = [dict(r) for r in watchlist_rows]
for _w in all_watchlist:
    _w["groups"] = json.loads(_w["groups"]) if _w["groups"] else []

all_groups = sorted({g for w in all_watchlist for g in w["groups"]})

# 群組是標籤式(一支股票可以同時屬於多個群組)、自訂名稱——2026-08-17使用者要求，觀察清單
# 股票變多之後太亂，想要「切分類」的感覺。這個選擇器放在所有頁籤上方(2026-08-17使用者
# 指出放在頁籤下面不好用，改到st.tabs()之前，這樣才是整頁最上方)，是全站共用的篩選：
# 選了某個群組之後，下面觀察清單/買進賣出策略訊號/策略歷史勝率參考/訊號紀錄頁籤都只會顯示
# 這個群組裡的股票——靠的是watchlist/symbols這兩個變數被這裡篩選過，後面每個頁籤都是重複
# 使用這兩個變數，不用每個頁籤各自處理篩選邏輯。K線圖/籌碼基本面頁籤的選股下拉也會跟著
# 縮小選項，這是共用同一份symbols變數的自然結果，不是特別去改那兩個頁籤。
selected_group = st.segmented_control("群組", ["全部"] + all_groups, default="全部", key="active_group") or "全部"
if selected_group == "全部":
    watchlist = all_watchlist
else:
    watchlist = [w for w in all_watchlist if selected_group in w["groups"]]
symbols = [w["code"] for w in watchlist]

tab_watchlist, tab_chart, tab_history, tab_strategy_logic = st.tabs(
    ["觀察清單", "K線圖", "訊號紀錄", "策略邏輯"]
)

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
        # 新增流程本身會重新抓這支股票的資料，理論上不會撞到殘留的舊快取，但這裡不是
        # 熱路徑(不像移動/刪除那麼頻繁)，還是clear整個快取比較保險。
        _cached_overview_row.clear()
        st.rerun()

    if all_watchlist:
        with st.expander("🏷️ 管理群組"):
            st.caption("用逗號分開可以同時填多個群組(例如「AI供應鏈, 記憶體」)；留空代表不分類，只會出現在「全部」。")
            with st.form("edit_groups_form"):
                group_inputs = {
                    w["code"]: st.text_input(
                        f"{w['code']} {w['name']}", value=", ".join(w["groups"]), key=f"group_edit_{w['code']}"
                    )
                    for w in all_watchlist
                }
                if st.form_submit_button("儲存群組設定"):
                    with connect(config.db_path) as conn:
                        for code, raw in group_inputs.items():
                            set_symbol_groups(conn, code, [g.strip() for g in raw.split(",") if g.strip()])
                        export_watchlist_snapshot(conn, watchlist_sync_path(config.db_path))
                    # 群組只影響「哪些股票被篩出來顯示」，不影響任何股票自己的指標數值，
                    # 不用clear _cached_overview_row。
                    st.rerun()

    if watchlist:
        strategy_keys = [k for k in STRATEGY_LABELS if k in NOTIFIABLE_STRATEGIES]
        indicator_keys = [k for k in STRATEGY_LABELS if k not in NOTIFIABLE_STRATEGIES]
        st.caption(
            f"每檔股票套用 {len(indicator_keys)} 種指標訊號 + {len(strategy_keys)} 種策略，"
            "策略部分依scripts/recompute_strategy_selection.py的backtest結果各自排除表現不好的（見下方「策略歷史勝率參考」的「❌」標記）："
        )
        st.caption(f"📊 策略（會推播Telegram）：{'、'.join(STRATEGY_LABELS[k] for k in strategy_keys)}")
        st.caption(f"📈 指標訊號（只記錄不推播）：{'、'.join(STRATEGY_LABELS[k] for k in indicator_keys)}")
        _refresh_note = "每30秒自動更新" if is_market_open_now(config, datetime.now()) else "現在是收盤時段，暫停自動更新"
        st.markdown(
            f"#### 總覽（價位/均線/指標，暫用最新收盤價，之後接即時報價會自動換資料源；▲▼可調整順序，{_refresh_note}）"
        )
        render_watchlist_table(config, tuple(w["code"] for w in all_watchlist), tuple(symbols))

        st.markdown(
            f"#### 買進/賣出策略訊號（一列一個策略，標示觸發當天的價格/日期，現價供對照；預設依觸發日期新到舊排序，{_refresh_note}）"
        )
        render_strategy_recommendations(config, watchlist)

        with st.expander("📊 策略歷史勝率參考（不是自動下單依據，只是這個策略在這支股票過去表現如何）"):
            track_records = _compute_track_records(config, tuple(symbols))
            if track_records:
                st.dataframe(pd.DataFrame(track_records), hide_index=True)
            else:
                st.caption("歷史資料不足，算不出任何一次完整的進出場")
    else:
        st.info("觀察清單是空的，用上面欄位新增股票，或先跑 `python scripts/fetch_historical.py` 填範例資料")

with tab_chart:
    _render_chart_tab(config, watchlist, symbols)

with tab_history:
    _render_history_tab(config, watchlist, symbols)

with tab_strategy_logic:
    st.subheader("策略邏輯")
    st.caption(
        "「策略」進場+出場邏輯完整綁在一起，可以直接依據行動，會推播Telegram，每個策略類別本身的"
        "docstring就是這裡的說明文字；「指標訊號」單獨一個不構成完整交易系統，只記錄不推播，"
        "用一行帶過。目前套用的參數(config.json的strategy_params)列在每個策略說明下面。"
    )

    st.markdown("### 🚫 自動排除規則")
    st.caption(
        "每支股票的每個「策略」各自backtest一次，下面任一項沒過就自動排除(存進symbols."
        "disabled_strategies，不會推播Telegram，但這裡跟「策略歷史勝率參考」照樣完整顯示全部策略供參考)。"
        "由scripts/recompute_strategy_selection.py手動執行重跑更新，不是即時計算。"
    )
    st.markdown(
        f"- 交易次數 < **{MIN_TRADES_FOR_RANKING}筆**（樣本不足，含完全沒有完整買賣配對；"
        "所有策略統一用這個門檻，2026-08-16之前曾有per-strategy override，濾網疊加後"
        "重新校準成統一值，見strategy_selection.py說明）\n"
        f"- 平均報酬率 < **{MIN_AVG_RETURN_PCT:+.1f}%**\n"
        f"- 加總報酬 <= **{MIN_TOTAL_RETURN_PCT:+.1f}%**\n"
        f"- 獲利因子 < **{MIN_PROFIT_FACTOR:.1f}**（完全沒有虧損時獲利因子視為∞，不排除）\n\n"
        "不單獨用勝率或最大回撤(MDD)當排除依據——低勝率+高賺賠比是趨勢跟隨策略的正常樣貌，"
        "MDD深但獲利因子夠高代表過程顛簸但賺賠比紮實，都不該被錯殺。"
    )

    st.markdown("### 📊 策略（會推播Telegram）")
    for name in TRACK_RECORD_STRATEGIES:
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
