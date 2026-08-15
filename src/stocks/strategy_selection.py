"""每支股票各自backtest一次NOTIFIABLE_STRATEGIES，判斷哪些策略表現明顯不夠好該排除
(存進symbols.disabled_strategies，run_live.py/run_batch.py評估這支股票時會跳過)。
被scripts/recompute_strategy_selection.py(定期全觀察清單重跑)跟daily_update.
add_symbol_to_watchlist(新增股票時立刻跑一次這支)共用，決定邏輯只寫一份。"""
import pandas as pd

from stocks.models import SignalEvent
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import simulate_round_trips, summarize_trades

MIN_TRADES_FOR_RANKING = 15  # 交易次數太少，勝率/報酬率本身就不可信——2026-08-08使用者
# 確認：不可信就該保守排除，不是給「還不能判斷」的寬限期，避免拿1筆的雜訊當依據推播
# 通知。代價是新股票/新啟用的策略剛開始會整組沒有任何通知，要等累積到MIN_TRADES_
# FOR_RANKING筆才會開始有判斷結果——這是使用者接受的取捨(寧可暫時安靜也不要拿雜訊
# 通知)，不是bug。原本回測3年時門檻是5筆，2026-08-17回測拉長到10年(約3.3倍長)後，
# 使用者確認同比例調高到15筆——資料變長理當要求更多筆數才可信，不是資料變多卻還用
# 同一套寬鬆標準。
MIN_TRADES_OVERRIDES = {
    "long_swing": 8,  # 中長波段持倉動輒數月，交易頻率天生遠低於chip_momentum這類策略，
    # 用同一套門檻某些股票要等更久才會達標。2026-08-08使用者確認3年門檻降到3筆，
    # 2026-08-17回測拉長到10年後同比例調高到8筆(沒有完全按3.3倍的15筆，長波段的觸發
    # 頻率天生不會跟著資料長度線性成長)。
    "capitulation_reversal": 5,  # 「單日重挫+爆量」本身是罕見事件，用15筆的標準門檻
    # 全觀察清單22檔沒有一檔達標過，整支策略形同虛設。2026-08-15回測驗證：改成5筆後
    # 22檔裡有10檔通過，合併起來勝率62.7%、獲利因子6.67，是目前所有NOTIFIABLE_STRATEGIES
    # 生效後數字最好的一支，沒通過的12檔要嘛連5筆都不到、要嘛平均/加總報酬本來就不過關
    # (不是被門檻錯殺)，5筆的門檻是合理的。
}
MIN_AVG_RETURN_PCT = 4.0  # 低於這個平均報酬率才排除——門檻不是台股來回交易成本(~0.6%)，
# 是「值不值得花風險做」：只是沒虧錢但賺很少，一樣不該留著繼續佔用通知額度，2026-08-07
# 使用者確認改成5%，2026-08-17再調整成4%(跟MIN_TOTAL_RETURN_PCT一起看，不是單獨這個
# 數字變寬鬆)。
MIN_TOTAL_RETURN_PCT = 50.0  # 2026-08-17使用者新增的第二條門檻：加總報酬(每筆報酬率直接
# 加起來，不是複利)沒超過這個數字也要排除——平均報酬看的是「單筆值不值得做」，加總報酬
# 看的是「這段時間累積下來對整體有沒有實質貢獻」，兩者都要過關才留著；只看平均可能漏掉
# 「單筆賺得不錯但這段時間總共只交易1、2次，整體貢獻很小」的策略。
# 2026-08-08拿掉單獨的勝率門檻：atr_breakout/chip_momentum/trust_momentum這類趨勢跟隨
# 策略本來就是「靠少數幾筆大波段撐報酬」的樣貌，低勝率(甚至<40%)只要賺賠比夠大、平均
# 報酬還是正的，一樣是能用的策略，不該被單獨的勝率門檻誤殺。
# 同一天也試過用avg_return_excluding_best_pct(拿掉單筆最賺的那一筆之後還剩什麼)當
# 第二個自動排除條件，但使用者指出這跟拿掉勝率門檻的理由自相矛盾——「靠少數幾筆大波段
# 撐報酬」本來就是這類策略的設計精神，不該因為報酬集中在少數大波段就自動否決，那正是
# 抓對的訊號。改成只留平均報酬<MIN_AVG_RETURN_PCT這一個自動排除門檻；
# avg_return_excluding_best_pct繼續算出來顯示，當「這筆正報酬有沒有過度依賴單筆」的
# 參考資訊，但不再自動觸發排除，交給使用者自己判斷。
MIN_PROFIT_FACTOR = 2.0  # 2026-08-17使用者拿10年真實回測數字比較後決定：獲利因子<2就排除，
# 不管MDD多深——同樣是重虧損股票，MDD深但獲利因子夠高(例如7.5)代表過程顛簸但賺賠比紮實，
# 不該被單獨的MDD門檻誤殺(套用同一支股票兩個策略實測：MDD-82%/獲利因子2.4 vs
# MDD-28.3%/獲利因子4.6，後者全面更好)；獲利因子<2代表賺的錢沒有明顯蓋過賠的錢，這種
# 才是真正該排除的。完全沒有虧損時profit_factor是None(不是0)，這裡不排除——那是最好的
# 情況，不是資料不足。目前只加獲利因子門檻，MDD本身暫不單獨設門檻(套10年實測資料發現
# MDD>35%會誤殺58%現有保留的策略，且MDD跟策略好壞沒有乾淨的對應關係，不像獲利因子)。


def summarize_strategy(symbol: str, bars: pd.DataFrame, strategy_name: str, params: dict) -> dict | None:
    """所有NOTIFIABLE_STRATEGIES現在都是一買配一賣的形狀，統一用simulate_round_trips
    配對。golden_cross_scaleout 2026-08-15前预设是一買配兩賣(分批出場)，需要另外用
    simulate_scaleout_trades配對；換成單一15%移動停損全出當預設後不再需要特殊處理，
    如果之後又手動把params的stop_mode改回"ma_scaleout"，呼叫端要自己改用
    simulate_scaleout_trades，這裡不會自動偵測。"""
    strategy = STRATEGY_REGISTRY.get(strategy_name)
    if strategy is None:
        return None
    events: list[SignalEvent] = strategy.evaluate(symbol, bars, params)
    trades, _ = simulate_round_trips(events)
    return summarize_trades(trades)


def should_disable(summary: dict | None, strategy_name: str | None = None) -> bool:
    """summary是None(沒有完整買賣配對)或交易數太少都排除——2026-08-08使用者確認：樣本
    不足時那個勝率/報酬率本身就不可信，不可信就該保守排除，不該預設保留、拿雜訊當依據
    推播通知。「太少」的門檻預設是MIN_TRADES_FOR_RANKING，但strategy_name若在
    MIN_TRADES_OVERRIDES裡有個別設定(例如long_swing持倉數月、交易天生比較少)就改用
    該策略自己的門檻。樣本夠的話，平均報酬率跟加總報酬要同時過關才留著(任一個沒過就
    排除)：平均報酬率<MIN_AVG_RETURN_PCT代表單筆不值得做；加總報酬<=MIN_TOTAL_RETURN_PCT
    代表即使單筆看起來不錯，這段時間累積下來對整體貢獻也不夠——2026-08-17使用者新增這第
    二條門檻，避免漏掉「平均報酬過關但整體交易次數/累積貢獻太小」的策略。不單獨用勝率
    當門檻：低勝率+高賺賠比是趨勢跟隨策略的正常樣貌，用勝率否決會錯殺這種類型的策略。
    也不會因為avg_return_excluding_best_pct(拿掉單筆最賺的那一筆之後還剩什麼)轉負就
    排除——那正是這類策略設計上要抓的「靠少數幾筆大波段撐報酬」，不是瑕疵，只當參考
    資訊顯示，不當自動排除依據。獲利因子<MIN_PROFIT_FACTOR也排除(2026-08-17新增)——
    profit_factor是None代表完全沒有虧損，這是最好的情況，不當排除依據。"""
    min_trades = MIN_TRADES_OVERRIDES.get(strategy_name, MIN_TRADES_FOR_RANKING)
    if not summary or summary["n"] < min_trades:
        return True
    if summary["avg_return_pct"] < MIN_AVG_RETURN_PCT:
        return True
    if summary["total_return_pct"] <= MIN_TOTAL_RETURN_PCT:
        return True
    profit_factor = summary.get("profit_factor")
    return profit_factor is not None and profit_factor < MIN_PROFIT_FACTOR


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
