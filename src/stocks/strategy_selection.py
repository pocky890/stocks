"""每支股票各自backtest一次NOTIFIABLE_STRATEGIES，判斷哪些策略表現明顯不夠好該排除
(存進symbols.disabled_strategies，run_live.py/run_batch.py評估這支股票時會跳過)。
被scripts/recompute_strategy_selection.py(定期全觀察清單重跑)跟daily_update.
add_symbol_to_watchlist(新增股票時立刻跑一次這支)共用，決定邏輯只寫一份。"""
import pandas as pd

from stocks.models import SignalEvent
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import simulate_round_trips, simulate_scaleout_trades, summarize_trades

MIN_TRADES_FOR_RANKING = 5  # 交易次數太少，勝率/報酬率本身就不可信——2026-08-08使用者
# 確認：不可信就該保守排除，不是給「還不能判斷」的寬限期，避免拿1筆的雜訊當依據推播
# 通知。代價是新股票/新啟用的策略剛開始會整組沒有任何通知，要等累積到MIN_TRADES_
# FOR_RANKING筆才會開始有判斷結果——這是使用者接受的取捨(寧可暫時安靜也不要拿雜訊
# 通知)，不是bug。
MIN_TRADES_OVERRIDES = {
    "long_swing": 3,  # 中長波段持倉動輒數月，交易頻率天生遠低於chip_momentum這類策略，
    # 用同一套5筆門檻某些股票(如3189)要再等3-4年才會達標。2026-08-08使用者確認降到3筆——
    # 3105(4筆,+95.3%平均)這種因為樣本不足被誤殺的個股可以被啟用，3189(2筆)還是繼續
    # 排除，不是完全不設門檻，仍要至少3筆才信。
}
MIN_AVG_RETURN_PCT = 5.0  # 低於這個平均報酬率才排除——門檻不是台股來回交易成本(~0.6%)，
# 是「值不值得花風險做」：只是沒虧錢但賺很少，一樣不該留著繼續佔用通知額度，2026-08-07
# 使用者確認改成這個門檻(原本0.6%只濾掉會虧錢的，太寬鬆)。
# 2026-08-08拿掉單獨的勝率門檻：atr_breakout/chip_momentum/trust_momentum這類趨勢跟隨
# 策略本來就是「靠少數幾筆大波段撐報酬」的樣貌，低勝率(甚至<40%)只要賺賠比夠大、平均
# 報酬還是正的，一樣是能用的策略，不該被單獨的勝率門檻誤殺。
# 同一天也試過用avg_return_excluding_best_pct(拿掉單筆最賺的那一筆之後還剩什麼)當
# 第二個自動排除條件，但使用者指出這跟拿掉勝率門檻的理由自相矛盾——「靠少數幾筆大波段
# 撐報酬」本來就是這類策略的設計精神，不該因為報酬集中在少數大波段就自動否決，那正是
# 抓對的訊號。改成只留平均報酬<MIN_AVG_RETURN_PCT這一個自動排除門檻；
# avg_return_excluding_best_pct繼續算出來顯示，當「這筆正報酬有沒有過度依賴單筆」的
# 參考資訊，但不再自動觸發排除，交給使用者自己判斷。
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


def should_disable(summary: dict | None, strategy_name: str | None = None) -> bool:
    """summary是None(沒有完整買賣配對)或交易數太少都排除——2026-08-08使用者確認：樣本
    不足時那個勝率/報酬率本身就不可信，不可信就該保守排除，不該預設保留、拿雜訊當依據
    推播通知。「太少」的門檻預設是MIN_TRADES_FOR_RANKING，但strategy_name若在
    MIN_TRADES_OVERRIDES裡有個別設定(例如long_swing持倉數月、交易天生比較少)就改用
    該策略自己的門檻。樣本夠的話，只看平均報酬率是否蓋過門檻(<MIN_AVG_RETURN_PCT就
    排除)。不單獨用勝率當門檻：低勝率+高賺賠比是趨勢跟隨策略的正常樣貌，用勝率否決會
    錯殺這種類型的策略。也不會因為avg_return_excluding_best_pct(拿掉單筆最賺的那一筆
    之後還剩什麼)轉負就排除——那正是這類策略設計上要抓的「靠少數幾筆大波段撐報酬」，
    不是瑕疵，只當參考資訊顯示，不當自動排除依據。"""
    min_trades = MIN_TRADES_OVERRIDES.get(strategy_name, MIN_TRADES_FOR_RANKING)
    if not summary or summary["n"] < min_trades:
        return True
    return summary["avg_return_pct"] < MIN_AVG_RETURN_PCT


def compute_disabled_strategies(symbol: str, bars: pd.DataFrame, strategy_params: dict) -> list[str]:
    """回傳這支股票應該排除的策略清單(NOTIFIABLE_STRATEGIES的子集)。bars要先接上
    attach_institutional_flows(chip_momentum/golden_cross_scaleout需要foreign_net/
    trust_net欄位才能算出真正的表現)——沒接上這些欄位、或還沒累積到該策略門檻筆數
    (見MIN_TRADES_FOR_RANKING/MIN_TRADES_OVERRIDES)完整交易的策略，一樣會被排除
    (見should_disable)，新股票/新啟用的策略會整組先排除，等資料累積夠了下次重跑才會
    開始判斷。"""
    disabled = []
    for strategy_name in sorted(NOTIFIABLE_STRATEGIES):
        summary = summarize_strategy(symbol, bars, strategy_name, strategy_params.get(strategy_name, {}))
        if should_disable(summary, strategy_name):
            disabled.append(strategy_name)
    return disabled
