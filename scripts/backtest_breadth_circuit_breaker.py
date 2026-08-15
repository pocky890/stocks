"""研究用一次性腳本：2026年7月系統性重挫期間，NOTIFIABLE_STRATEGIES幾乎在同一時間對
十幾檔不同股票同時觸發進場，個別策略/個股層級的濾網(MA60/隔天確認/MACD/KD)都測過，
效果有限或有取捨。這裡改測「投資組合層級斷路器」：算全觀察清單每天有多少比例的股票
收盤價跌破自己的20日均線(市場寬度)，寬度過高時暫停「全部」策略的新進場(不影響已有
部位的出場)，等寬度回落才恢復——概念上跟公司在系統性風險升高時全面收手是同一件事，
不是針對單一策略調整。

已知簡化/近似：這裡是把策略原本產生的BUY事件事後濾掉「斷路器生效當天」的那些，不是
真的讓策略在計算當下就跳過(策略內部的in_position狀態機不知道那筆BUY被濾掉了)——如果
某次進場被斷路器擋掉，策略內部還是會以為自己已經進場，直到原本的出場條件觸發(那個SELL
事件也會因為配不到對應BUY而被simulate_round_trips自動忽略)才會重新考慮下一次進場。
這代表斷路器解除後、策略真正恢復偵測新機會可能會有落後，是保守估計(可能低估濾網的
效益，因為錯過的不只斷路器生效那幾天，還有策略內部誤以為在場內的那段空窗期)，但方向性
的結論(斷路器有沒有用、代價多大)仍然成立。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from stocks.config import load_config
from stocks.db import (
    attach_institutional_flows,
    bars_to_dataframe,
    connect,
    fetch_bars_daily,
    fetch_institutional_flows,
    fetch_watchlist,
)
from stocks.models import Direction
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import simulate_round_trips, summarize_trades

BREADTH_MA_PERIOD = 20
ENTER_THRESHOLD = 0.60  # 觀察清單裡>=60%的股票跌破自己的20日均線 -> 觸發斷路器
EXIT_THRESHOLD = 0.40  # 回落到<=40% -> 解除(用不同進出閾值避免在臨界值附近反覆開關)

YTD_START = pd.Timestamp("2026-01-01")
JULY_START = pd.Timestamp("2026-07-01")
JULY_END = pd.Timestamp("2026-07-31")


def compute_breadth_active_mask(bars_by_symbol: dict) -> pd.Series:
    closes = {code: b["close"] for code, b in bars_by_symbol.items() if not b.empty}
    df = pd.DataFrame(closes).sort_index()
    ma20 = df.rolling(BREADTH_MA_PERIOD).mean()
    pct_below = (df < ma20).sum(axis=1) / df.notna().sum(axis=1)

    active = pd.Series(False, index=pct_below.index)
    state = False
    for t, v in pct_below.items():
        if not pd.isna(v):
            if not state and v >= ENTER_THRESHOLD:
                state = True
            elif state and v <= EXIT_THRESHOLD:
                state = False
        active[t] = state
    return active


def summarize(trades):
    summary = summarize_trades(trades)
    if summary is None:
        return {"筆數": 0}
    return {
        "筆數": summary["n"],
        "勝率": round(summary["win_rate"], 1),
        "平均報酬": round(summary["avg_return_pct"], 1),
        "加總報酬": round(summary["total_return_pct"], 1),
        "獲利因子": round(summary["profit_factor"], 2) if summary["profit_factor"] is not None else None,
        "最大回撤": round(-summary["max_drawdown_pct"], 1),
    }


def main():
    config = load_config()

    with connect(config.db_path) as conn:
        symbols = [(row["code"], row["name"]) for row in fetch_watchlist(conn)]
        bars_by_symbol = {}
        for code, name in symbols:
            bars = bars_to_dataframe(fetch_bars_daily(conn, code), ts_field="date")
            bars_by_symbol[code] = attach_institutional_flows(bars, fetch_institutional_flows(conn, code))

    active = compute_breadth_active_mask(bars_by_symbol)
    active_days = active[active].index
    print(f"斷路器歷史上總共生效 {len(active_days)} 個交易日")
    july_active = active.loc[JULY_START:JULY_END]
    print(f"2026-07 生效天數：{july_active.sum()} / {len(july_active)}\n")

    strategy_names = sorted(NOTIFIABLE_STRATEGIES)

    for scope_label, start, end in [("全觀察清單10年", None, None), ("2026 YTD", YTD_START, None), ("2026-07單月", JULY_START, JULY_END)]:
        rows = []
        for cb_label, apply_cb in [("原本(無斷路器)", False), ("加斷路器", True)]:
            all_trades = []
            for strategy_name in strategy_names:
                strategy = STRATEGY_REGISTRY[strategy_name]
                for code, name in symbols:
                    bars = bars_by_symbol[code]
                    if bars.empty:
                        continue
                    events = strategy.evaluate(code, bars, config.strategy_params.get(strategy_name, {}))
                    if apply_cb:
                        events = [e for e in events if not (e.direction == Direction.BUY and active.get(e.ts, False))]
                    if start is not None:
                        events = [e for e in events if e.ts >= start]
                    if end is not None:
                        events = [e for e in events if e.ts <= end]
                    trades, _ = simulate_round_trips(events)
                    all_trades.extend(trades)
            rows.append({"設定": cb_label, **summarize(all_trades)})
        print(f"=== {scope_label}：全部NOTIFIABLE_STRATEGIES併在一起算 ===")
        print(pd.DataFrame(rows).to_string(index=False))
        print()


if __name__ == "__main__":
    main()
