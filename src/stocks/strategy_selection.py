"""每支股票各自backtest一次NOTIFIABLE_STRATEGIES，判斷哪些策略表現明顯不夠好該排除
(存進symbols.disabled_strategies，run_live.py/run_batch.py評估這支股票時會跳過)。
被scripts/recompute_strategy_selection.py(定期全觀察清單重跑)跟daily_update.
add_symbol_to_watchlist(新增股票時立刻跑一次這支)共用，決定邏輯只寫一份。"""
import pandas as pd

from stocks.models import SignalEvent
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades, summarize_trades

MIN_TRADES_FOR_RANKING = 5  # 交易次數太少，勝率/報酬率本身就不可信，不該拿來當排除依據
WIN_RATE_THRESHOLD = 45.0  # 低於這個勝率才排除——接近或低於雜訊線
MIN_AVG_RETURN_PCT = 5.0  # 低於這個平均報酬率才排除——門檻不是台股來回交易成本(~0.6%)，
# 是「值不值得花風險做」：只是沒虧錢但賺很少，一樣不該留著繼續佔用通知額度，2026-08-07
# 使用者確認改成這個門檻(原本0.6%只濾掉會虧錢的，太寬鬆)。這條跟勝率門檻是「兩個條件
# 任一觸發就排除」，不是都要達標才排除。
SCALEOUT_STRATEGY = "golden_cross_scaleout"


def summarize_strategy(symbol: str, bars: pd.DataFrame, strategy_name: str, params: dict) -> dict | None:
    strategy = STRATEGY_REGISTRY.get(strategy_name)
    if strategy is None:
        return None
    events: list[SignalEvent] = strategy.evaluate(symbol, bars, params)
    if strategy_name == SCALEOUT_STRATEGY:
        trades, _ = simulate_scaleout_trades(events)
    else:
        trades, _ = simulate_round_trips(events)
    return summarize_trades(trades)


def should_disable(summary: dict | None) -> bool:
    """summary是None(沒有完整買賣配對)或交易數太少(<MIN_TRADES_FOR_RANKING)都不排除——
    樣本不足時那個勝率/報酬率本身就不可信，排除等於憑噪音做決定。樣本夠的話，勝率明顯
    偏弱(<WIN_RATE_THRESHOLD)或平均報酬率沒蓋過交易成本(<MIN_AVG_RETURN_PCT)，任一觸發
    就排除——勝率高但平均賺太少(或實際上是賠的)一樣不該留著，不是只看勝率單一指標。"""
    if not summary or summary["n"] < MIN_TRADES_FOR_RANKING:
        return False
    return summary["win_rate"] < WIN_RATE_THRESHOLD or summary["avg_return_pct"] < MIN_AVG_RETURN_PCT


def compute_disabled_strategies(symbol: str, bars: pd.DataFrame, strategy_params: dict) -> list[str]:
    """回傳這支股票應該排除的策略清單(NOTIFIABLE_STRATEGIES的子集)。bars要先接上
    attach_institutional_flows(chip_momentum/golden_cross_scaleout需要foreign_net/
    trust_net欄位才能算出真正的表現，不是缺欄位就自動排除——缺欄位時那些策略本身就會
    優雅降級，回傳的summary可能因為樣本不足而不排除，這是正確的「還不能判斷」而不是
    「判斷後排除」)。"""
    disabled = []
    for strategy_name in sorted(NOTIFIABLE_STRATEGIES):
        summary = summarize_strategy(symbol, bars, strategy_name, strategy_params.get(strategy_name, {}))
        if should_disable(summary):
            disabled.append(strategy_name)
    return disabled
