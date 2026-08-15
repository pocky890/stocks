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
    bars_list_to_dataframe,
    bars_to_dataframe,
    connect,
    export_watchlist_snapshot,
    fetch_bars_daily,
    fetch_ex_dividend_schedule,
    fetch_institutional_flows,
    fetch_margin_balances,
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
    MIN_TRADES_OVERRIDES,
)
from stocks.strategy_stats import simulate_round_trips, summarize_trades
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

    「（已排除）」標記對應scripts/recompute_strategy_selection.py寫進symbols.
    disabled_strategies的排除清單——run_live.py/run_batch.py評估這支股票時會跳過這些
    策略，不會通知/寫進signal_events，但這裡的歷史勝率分析不受影響，照樣完整顯示，
    讓使用者知道「這個策略對這支股票表現不好，所以被排除」的理由是什麼。"""
    with connect(_config.db_path) as conn:
        name_row = conn.execute("SELECT name FROM symbols WHERE code = ?", (code,)).fetchone()
        bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
        bars = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))
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
        return f"{text} (已排除)" if name in disabled else text

    row = {"代號": code, "名稱": (name_row["name"] if name_row else None) or "—"}
    for name in TRACK_RECORD_STRATEGIES:
        events = STRATEGY_REGISTRY[name].evaluate(code, bars, _config.strategy_params.get(name, {}))
        trades, _ = simulate_round_trips(events)
        row[STRATEGY_LABELS[name].split("(")[0]] = cell_text(name, summarize_trades(trades))
    return row


def _compute_track_records(_config, symbols: tuple):
    """組合每支股票各自的快取結果(見_compute_track_record_for_symbol)——這層本身不用
    st.cache_data，因為裡面每一支都已經是各自快取過的，這層只是便宜的list組裝，重算
    也不痛不癢，不用為它另外佔一份快取空間。"""
    rows = [_compute_track_record_for_symbol(_config, code) for code in symbols]
    return [row for row in rows if row is not None]


@st.cache_data(ttl=30, show_spinner=False)
def _cached_overview_rows(_config):
    """build_overview_rows本身不帶快取(watchlist_view.py是純商業邏輯模組，直接被
    tests/test_watchlist_view.py單元測試呼叫，不該混進streamlit依賴/快取語意)，這裡包一層
    快取給dashboard用。永遠處理「整個觀察清單」，不吃symbols參數——2026-08-17發現：
    切換群組時如果沒有這層快取，每次都要重新對全部股票跑RSI/MACD/KD等指標運算，10年
    資料量下一次要將近1秒，每次群組切換/任何按鈕點擊都要重付這筆成本。快取30秒，跟
    render_watchlist_table的run_every="30s"對齊。"""
    return build_overview_rows(_config)


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
    要顯示的子集合——抓資料用all_symbols(才能命中快取)，顯示前再用symbols篩選一次。"""
    overview_rows = [r for r in _cached_overview_rows(config) if r["代號"] in symbols]
    try:
        intraday_bars = _fetch_today_intraday(config, all_symbols) if all_symbols else {}
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
                    export_watchlist_snapshot(conn, watchlist_sync_path(config.db_path))
                st.rerun()
            if cols[1].button("▼", key=f"down_{code}", disabled=(i == len(overview_rows) - 1)):
                with connect(config.db_path) as conn:
                    move_watchlist_symbol(conn, code, direction=1)
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
def _cached_strategy_recommendations(_config):
    """build_strategy_recommendations本身不帶快取(watchlist_view.py是純商業邏輯模組，
    直接被tests/test_watchlist_view.py單元測試呼叫)，這裡包一層快取給dashboard用。
    ttl=30跟render_strategy_recommendations的run_every="30s"對齊，不快取更久——這張表
    的「現價」要反映當下報價，快取太久會重新引入2026-08-13已經修過的現價不同步問題。
    2026-08-17發現：這個函式全觀察清單10年資料下要跑4.5秒，沒有快取的話每次切換群組
    (全頁重新執行，這個fragment也會跟著重跑)都要重付這筆成本。"""
    return build_strategy_recommendations(_config)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_paper_trades(_config, start_date: str):
    """build_paper_trades本身不帶快取(理由同_cached_strategy_recommendations)。這裡
    不在fragment裡(訊號紀錄頁籤沒有自動更新機制)，但一樣要限制TTL(不能無限期快取)——
    "持有中"部位的報酬率是用現價估算，快取太久會顯示過時的未實現報酬。2026-08-17發現：
    這個函式全觀察清單10年資料下要跑4.8秒，沒有快取的話每次切換群組/任何按鈕點擊都要
    重付這筆成本，是切換群組明顯變慢的主因之一。"""
    return build_paper_trades(_config, start_date=start_date)


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
    watchlist_codes = {w["code"] for w in watchlist}
    recommendations = [r for r in _cached_strategy_recommendations(config) if r["代號"] in watchlist_codes]
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
                st.dataframe(pd.DataFrame(track_records), use_container_width=True, hide_index=True)
            else:
                st.caption("歷史資料不足，算不出任何一次完整的進出場")
    else:
        st.info("觀察清單是空的，用上面欄位新增股票，或先跑 `python scripts/fetch_historical.py` 填範例資料")

with tab_chart:
    st.subheader("K線圖 + 均線 + 籌碼面（證交所免費公開資料）")
    if not symbols:
        st.info("觀察清單是空的，沒有資料可以畫圖")
    else:
        selected = st.selectbox("選擇股票", symbols)
        with connect(config.db_path) as conn:
            bars = bars_to_dataframe(fetch_bars_daily(conn, selected), ts_field="date")
            flow_rows = fetch_institutional_flows(conn, selected)
            margin_rows = fetch_margin_balances(conn, selected)
            valuation_rows = fetch_valuations(conn, selected)
            ex_div_rows = fetch_ex_dividend_schedule(conn, selected)

        if bars.empty:
            st.warning(f"{selected} 沒有歷史K棒資料")
        else:
            # 2026-08-15使用者要求把籌碼面資料搬到K線圖下方一起比對，不要分開頁籤來回切換——
            # 三張圖疊在同一個figure裡(shared_xaxes)，拖曳/縮放任一段時間軸，其他段會跟著對齊。
            flow_df = pd.DataFrame([dict(r) for r in flow_rows]) if flow_rows else None
            margin_df = pd.DataFrame([dict(r) for r in margin_rows]) if margin_rows else None
            fig = price_and_chip_chart(bars, flow_df, margin_df, ma_windows=[5, 10, 20, 60])
            st.plotly_chart(fig, use_container_width=True)
            if not flow_rows:
                st.info("沒有三大法人資料，先跑 `python scripts/fetch_market_data.py`")
            if not margin_rows:
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
    paper_start_col, paper_symbol_col, _paper_spacer = st.columns([1, 2, 3])
    paper_start = paper_start_col.date_input("模擬起始日期", value=date(2026, 7, 1), key="paper_trades_start")
    paper_symbol_options = [f"{w['code']} {w['name']}" for w in watchlist]
    paper_selected_symbols = paper_symbol_col.multiselect(
        "只看特定股票", paper_symbol_options, key="paper_trades_symbol_filter"
    )

    paper_trades = [r for r in _cached_paper_trades(config, paper_start.strftime("%Y-%m-%d")) if r["代號"] in symbols]
    if paper_selected_symbols:
        paper_selected_codes = {s.split(" ", 1)[0] for s in paper_selected_symbols}
        paper_trades = [r for r in paper_trades if r["代號"] in paper_selected_codes]

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

            def _profit_factor(returns: pd.Series):
                gains = returns[returns > 0].sum()
                losses = -returns[returns < 0].sum()
                return gains / losses if losses > 0 else None

            def _max_drawdown(group: pd.DataFrame) -> float:
                # 依買進日期排序後累加報酬率畫簡化權益曲線，抓從高點到低點最大跌了多少，
                # 跟strategy_stats._max_drawdown_pct同一套算法，這裡改用pandas寫一份是因為
                # 這張表的資料來源是build_paper_trades攤平後的dict list，不是Trade物件。
                # 2026-08-17程式碼review發現：peak沒有以0(起始基準點)做底，如果權益曲線
                # 從頭到尾沒有回到0以上，回撤會被低估(例如[-10,-5,+3]這裡原本只算出5.0，
                # 正確答案是15.0)——用clip(lower=0.0)補回peak最少是0這個起始基準，
                # 跟strategy_stats._max_drawdown_pct的peak = max(peak, cumulative)(從
                # peak=0.0開始累加)數學上等價。
                ordered = group.sort_values("買進日期")["報酬率(%)"]
                cumulative = ordered.cumsum()
                peak = cumulative.cummax().clip(lower=0.0)
                return (peak - cumulative).max()

            summary = (
                summary_df.groupby("策略")["報酬率(%)"]
                .agg(筆數="count", 勝率=lambda s: (s > 0).mean() * 100, 平均報酬="mean", 加總報酬="sum", 獲利因子=_profit_factor)
                .round(1)
                .reset_index()
            )
            mdd_by_strategy = summary_df.groupby("策略").apply(_max_drawdown)
            summary["最大回撤"] = summary["策略"].map(-mdd_by_strategy).round(1)
            st.caption("「獲利因子」是總獲利/總虧損(絕對值)，None代表這個策略目前完全沒有虧損過的交易；「最大回撤」是簡化權益曲線(每筆報酬率依買進日期直接加總)從高點到低點最大跌了多少，不是真正的資金曲線，只當風險參考。")
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

    st.markdown("### 🚫 自動排除規則")
    st.caption(
        "每支股票的每個「策略」各自backtest一次，下面任一項沒過就自動排除(存進symbols."
        "disabled_strategies，不會推播Telegram，但這裡跟「策略歷史勝率參考」照樣完整顯示全部策略供參考)。"
        "由scripts/recompute_strategy_selection.py手動執行重跑更新，不是即時計算。"
    )
    overrides_text = "、".join(f"{strategy_label(name)}除外(改用{n}筆)" for name, n in MIN_TRADES_OVERRIDES.items())
    st.markdown(
        f"- 交易次數 < **{MIN_TRADES_FOR_RANKING}筆**（樣本不足，含完全沒有完整買賣配對；{overrides_text}）\n"
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
