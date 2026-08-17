"""全市場同產業寬度斷路器：某產業≥60%的股票跌破自己20日均線時，暫停對「這個產業裡
的股票」發送新的BUY通知(SELL永遠不擋、既有部位一樣可以出場)。

全市場產業寬度用「前一交易日收盤」算(run_batch.py收盤後14:00才更新，見refresh_industry_
states)；個股自己是否跌破月線用「當下即時價格」(run_live.py本來就在追蹤，含今天盤中
partial K)——兩者新鮮度不同，是2026-08-16使用者確認接受的設計(要即時算全市場寬度，
代表盤中每5分鐘要多打一次全市場~2000檔快照，是使用者明確要避免的額外掃描負擔)。

60%/40%的遲滯門檻(進場60%、解除40%，避免臨界值附近反覆開關)是2026-08-15~16全觀察清單
2年回測驗證過的：不設斷路器時2026年7月系統性重挫獲利因子只有0、加總虧損-1099；加這個
「全市場同產業+自己須破月線」版本後，2026 YTD總報酬回升到3984(比純產業寬度版的3327好，
但不如更寬鬆版本)，7月虧損-441.5(比純產業版的-241.3多，但遠比不設斷路器的-1099.4好)——
當時使用者選了這個「比較平衡」的AND版本(還要求自己也跌破均線才擋)，理由是避免同業平均
被拖累而錯殺逆勢股(3711日月光投控2~4月的案例：全市場半導體寬度觸發，但3711自己沒破
月線，純產業寬度斷路器會誤殺它8筆好進場，加上這個AND條件後幾乎完全救回)。

2026-08-16後續發現並修正：使用者檢討「隊長」群組(15檔同產業半導體設備/封測股)2026年
6-7月經歷產業性重挫時，這個AND條件對golden_cross/trend_following/long_swing/
atr_breakout/breakout這5支策略實測擋下率是0%——這幾支策略的進場條件本身就要求「站上」
某條均線(創新高/均線交叉)，跟AND條件要求的「自己也跌破均線」幾乎互斥，等於這道防線對
這5支策略形同虛設(拉長own_ma_period到120日一樣沒用，見scripts/backtest_circuit_
breaker_own_ma.py)。改成拿掉AND條件(config.circuit_breaker_own_ma_period=None，
只看產業寬度)後，隊長組實測擋下率從0%升到38.3%，long_swing從獲利因子0.79(淨虧)翻正
到1.39；全觀察清單10年整體只犧牲3~4%總報酬。代價：3711那筆+30.7%的好單(當初AND條件
要保護的原始案例)在新設定下會被誤擋——使用者看過這個具體代價後仍確認要拿掉AND條件，
是接受「少數逆勢股好單被誤殺」換「同產業系統性重挫時的整體保護」，不是發現AND條件
本身是錯的、也不是否定當時的判斷，是新場景(隊長組的產業集中度)下重新評估的取捨。
"""
import json

import pandas as pd

from stocks.config import Config
from stocks.db import fetch_all_industry_codes, fetch_industry_closes, get_setting, set_setting

STATE_KEY = "circuit_breaker_state"  # app_settings裡存{industry_code: bool}的JSON，
# 用app_settings(不另開一張表)是因為這專案已經有這個key-value機制、也是disabled_
# strategies/groups欄位同樣的JSON-in-column慣例，這裡只是換成JSON-in-app_settings-value。


def load_active_state(conn) -> dict:
    raw = get_setting(conn, STATE_KEY)
    return json.loads(raw) if raw else {}


def _save_active_state(conn, state: dict) -> None:
    set_setting(conn, STATE_KEY, json.dumps(state))


def compute_breadth_pct(conn, industry_code: str, ma_period: int) -> float | None:
    """回傳這個產業代碼「目前資料庫裡最新一天」有幾成股票跌破自己的ma_period日均線。
    資料還不夠(累積不到ma_period天)或這個產業代碼完全沒有資料，回傳None——呼叫端
    (refresh_industry_states)遇到None要跳過，不能當成0%處理(0%會誤觸發「解除」)。"""
    rows = fetch_industry_closes(conn, industry_code)
    if not rows:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    pivot = df.pivot(index="date", columns="symbol", values="close").sort_index()
    if len(pivot) < ma_period:
        return None
    ma = pivot.rolling(ma_period).mean()
    latest_close = pivot.iloc[-1]
    latest_ma = ma.iloc[-1]
    valid = latest_close.notna() & latest_ma.notna()
    if valid.sum() == 0:
        return None
    return float((latest_close[valid] < latest_ma[valid]).sum() / valid.sum())


def compute_breadth_series(conn, industry_code: str, ma_period: int) -> pd.Series:
    """跟compute_breadth_pct同一套算法，但回傳「每一天」的寬度數字(index是pd.Timestamp)，
    不是只回傳資料庫裡最新一天——build_paper_trades要回放「當時」的斷路器狀態來過濾
    歷史BUY訊號，不能只看app_settings存的『現在』狀態。每一天的均線都只用當天(含)以前
    的收盤價算，跟原本逐日rolling的因果性一致，不會有look-ahead。"""
    rows = fetch_industry_closes(conn, industry_code)
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame([dict(r) for r in rows])
    pivot = df.pivot(index="date", columns="symbol", values="close").sort_index()
    pivot.index = pd.to_datetime(pivot.index)
    if len(pivot) < ma_period:
        return pd.Series(dtype=float)
    ma = pivot.rolling(ma_period).mean()
    valid = pivot.notna() & ma.notna()
    valid_count = valid.sum(axis=1).astype(float)
    below_count = (pivot < ma)[valid].sum(axis=1).astype(float)
    pct = (below_count / valid_count)[valid_count > 0]
    return pct


def replay_active_state(breadth_pct: pd.Series, enter_threshold: float, exit_threshold: float) -> pd.Series:
    """依照refresh_industry_states同一套60%/40%遲滯規則，逐日回放斷路器on/off狀態
    (index跟breadth_pct一樣是pd.Timestamp)。回傳的是「當天收盤後」算出的狀態——正式
    環境裡這個狀態要到隔天run_live.py才會讀到(見load_active_state)，所以呼叫端要自己
    shift(1)一天才是「當天盤中」實際生效的狀態，不能直接拿當天的值去擋當天的訊號
    (那會變成用當天收盤資料去擋當天盤中已經發生的訊號，等於look-ahead)。"""
    states = {}
    active = False
    for date, pct in breadth_pct.items():
        if not active and pct >= enter_threshold:
            active = True
        elif active and pct <= exit_threshold:
            active = False
        states[date] = active
    return pd.Series(states)


def refresh_industry_states(conn, config: Config) -> dict:
    """收盤後(run_batch.py儲存完當天的industry_closes後)呼叫一次：對觀察清單目前涵蓋的
    每個產業代碼重新算寬度、套遲滯規則更新on/off狀態並存回app_settings。回傳更新後的
    完整狀態字典，run_live.py隔天啟動時透過load_active_state讀到的就是這裡存的結果。"""
    codes = set(fetch_all_industry_codes(conn).values())
    state = load_active_state(conn)
    for code in codes:
        pct = compute_breadth_pct(conn, code, config.circuit_breaker_ma_period)
        if pct is None:
            continue
        active = state.get(code, False)
        if not active and pct >= config.circuit_breaker_enter_threshold:
            active = True
        elif active and pct <= config.circuit_breaker_exit_threshold:
            active = False
        state[code] = active
    _save_active_state(conn, state)
    return state


CIRCUIT_BREAKER_EXEMPT_STRATEGIES = {"bullish_divergence", "capitulation_reversal"}  # 背離
# 抄底：實測進場訊號被斷路器擋下的比例接近100%——包括大贏家全部一起被擋。原因是這個
# 斷路器「AND自己也跌破月線」這個條件，本來是設計給動能/趨勢類策略用的(避免買進一支連
# 自己都還沒轉強的股票)；但背離抄底這種抄底策略「當下正跌破自己月線」根本是進場的前提
# 條件，不是警訊，兩層過濾疊在一起等於直接把整支策略關掉。
#
# 爆量急殺止穩：實測「同產業≥60%也跌破月線」AND「自己也跌破月線」兩個條件同時成立、
# 真正擋下BUY的比例只有2.2%(不是結構性互斥，遠低於背離抄底的近100%)——但使用者
# 2026-08-16確認：這2.2%剛好是「單一股票恐慌急殺+整個產業同時系統性重挫」同時發生的
# 情況，可能正是最劇烈、最值得抓的恐慌轉折點，寧可不設這道防線也不要錯過，故一併排除。


def is_buy_suppressed(
    symbol: str,
    industry_codes: dict,
    active_state: dict,
    own_bars_with_today: pd.DataFrame,
    own_ma_period: int | None,
) -> bool:
    """純函式，不碰DB——industry_codes/active_state是呼叫端(run_live.py)在迴圈開始前
    讀好一次的{symbol: industry_code}/{industry_code: bool}，避免每個5分K tick、每支
    股票都重新查一次DB(這兩份資料一整天內不會變：industry_codes只在新增股票時變、
    active_state只有run_batch.py收盤後才會更新)。own_bars_with_today是呼叫端已經算好
    的「歷史日K+今天partial K」(build_daily_bars_with_today)，用來判斷這支股票自己
    當下是否跌破own_ma_period日均線。

    own_ma_period是None(2026-08-16後的現行預設)時，只看產業寬度是否觸發，不再額外
    要求「自己也跌破均線」——這道AND條件原本是為了避免同業平均拖累而錯殺逆勢股(3711
    日月光投控2~4月的案例)，但實測對golden_cross/trend_following/long_swing/
    atr_breakout/breakout這5支策略幾乎形同虛設(擋下率0%，這幾支的進場條件本身就要求
    「站上」某條均線，跟「自己也跌破均線」幾乎互斥，拉長own_ma_period到120日一樣沒用，
    見scripts/backtest_circuit_breaker_own_ma.py)。使用者2026-08-16在看過「拿掉AND
    條件會讓3711那筆+30.7%的好單被誤擋」這個具體代價後，權衡隊長組同產業修正的實質
    改善(long_swing獲利因子0.79→1.39翻正)後，確認拿掉這道AND條件。若要恢復舊行為，
    把own_ma_period設成非None的整數(例如20)即可。"""
    industry_code = industry_codes.get(symbol)
    if industry_code is None or not active_state.get(industry_code, False):
        return False

    if own_ma_period is None:
        return True

    if own_bars_with_today.empty or len(own_bars_with_today) < own_ma_period:
        return False
    close = own_bars_with_today["close"]
    latest_ma = close.rolling(own_ma_period).mean().iloc[-1]
    if pd.isna(latest_ma):
        return False
    return bool(close.iloc[-1] < latest_ma)
