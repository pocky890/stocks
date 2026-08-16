"""每支股票各自backtest一次NOTIFIABLE_STRATEGIES，判斷哪些策略表現明顯不夠好該排除
(存進symbols.disabled_strategies，run_live.py/run_batch.py評估這支股票時會跳過)。
被scripts/recompute_strategy_selection.py(定期全觀察清單重跑)跟daily_update.
add_symbol_to_watchlist(新增股票時立刻跑一次這支)共用，決定邏輯只寫一份。"""
import pandas as pd

from stocks.models import SignalEvent
from stocks.notifier import NOTIFIABLE_STRATEGIES
from stocks.strategies import STRATEGY_REGISTRY
from stocks.strategy_stats import is_scaleout_strategy, simulate_round_trips, simulate_scaleout_trades, summarize_trades

MIN_TRADES_FOR_RANKING = 5  # 交易次數太少，勝率/報酬率本身就不可信——2026-08-08使用者
# 確認：不可信就該保守排除，不是給「還不能判斷」的寬限期，避免拿1筆的雜訊當依據推播
# 通知。代價是新股票/新啟用的策略剛開始會整組沒有任何通知，要等累積到MIN_TRADES_
# FOR_RANKING筆才會開始有判斷結果——這是使用者接受的取捨(寧可暫時安靜也不要拿雜訊
# 通知)，不是bug。
#
# 這個數字調整過幾次：3年回測時是5筆，2026-08-17回測拉長到10年後同比例調高到15筆，
# 同時針對交易頻率天生較低的策略設個別override(long_swing 8、capitulation_reversal
# 5、atr_breakout/chip_momentum 6~10)。2026-08-16這一批(60/120日regime濾網、240日
# 年線濾網、月營收年增率濾網疊加上去)進一步把chip_momentum/trust_momentum/golden_
# cross_scaleout/atr_breakout/breakout這5支的交易頻率再砍掉30%~46%，導致大量個股
# (例如8299群聯的breakout只剩6筆，比當時breakout的10筆override還低)純粹因為樣本
# 不足被排除，即使數字漂亮(平均+32%、獲利因子50.8)也一樣被擋——使用者認為這樣「筆數
# 被壓得太低」不合理，要求統一放寬。
#
# 查證過完全拿掉這道門檻(只看平均報酬/加總報酬/獲利因子三個數值門檻)的風險：全觀察
# 清單有3組策略/股票組合只有1筆交易就會通過數值門檻(atr_breakout on 7769、
# capitulation_reversal on 6903、trust_momentum on 3552，都是單筆歷史巧合，不是
# 真的驗證過的優勢)，完全拿掉等於讓系統拿丟硬幣等級的證據當推播依據，不是使用者要的
# 效果。使用者2026-08-16確認：統一降到5筆(不分策略，取消per-strategy override，跟
# capitulation_reversal當初驗證過的5筆下限一致)——比只完全拿掉保守，比15筆(或個別
# 6~10筆override)寬鬆，是這次濾網疊加後重新校準的結果，不是每次濾網一改就無限調降
# 下去；之後如果又疊加新濾網、樣本又進一步下降，要重新評估「5筆」這個下限還適不適合，
# 不是理所當然繼續降。
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
    """大多數NOTIFIABLE_STRATEGIES是一買配一賣的形狀，用simulate_round_trips配對；少數
    分批出場的策略(golden_cross_scaleout的stop_mode="ma_scaleout"、bullish_divergence
    的enable_tiered_profit=True)是一買配兩賣，要用simulate_scaleout_trades配對——
    is_scaleout_strategy()統一判斷，不用每個呼叫端各自記得特殊處理。"""
    strategy = STRATEGY_REGISTRY.get(strategy_name)
    if strategy is None:
        return None
    events: list[SignalEvent] = strategy.evaluate(symbol, bars, params)
    trades, _ = simulate_scaleout_trades(events) if is_scaleout_strategy(strategy_name, params) else simulate_round_trips(events)
    return summarize_trades(trades)


def should_disable(summary: dict | None) -> bool:
    """summary是None(沒有完整買賣配對)或交易數太少都排除——2026-08-08使用者確認：樣本
    不足時那個勝率/報酬率本身就不可信，不可信就該保守排除，不該預設保留、拿雜訊當依據
    推播通知。「太少」的門檻是MIN_TRADES_FOR_RANKING(現行:5，統一適用所有策略，見該
    常數註解說明2026-08-16為何從per-strategy override改成統一門檻)。樣本夠的話，
    平均報酬率跟加總報酬要同時過關才留著(任一個沒過就排除)：平均報酬率<MIN_AVG_
    RETURN_PCT代表單筆不值得做；加總報酬<=MIN_TOTAL_RETURN_PCT代表即使單筆看起來
    不錯，這段時間累積下來對整體貢獻也不夠——2026-08-17使用者新增這第二條門檻，避免
    漏掉「平均報酬過關但整體交易次數/累積貢獻太小」的策略。不單獨用勝率當門檻：低勝率
    +高賺賠比是趨勢跟隨策略的正常樣貌，用勝率否決會錯殺這種類型的策略。也不會因為
    avg_return_excluding_best_pct(拿掉單筆最賺的那一筆之後還剩什麼)轉負就排除——那正
    是這類策略設計上要抓的「靠少數幾筆大波段撐報酬」，不是瑕疵，只當參考資訊顯示，不
    當自動排除依據。獲利因子<MIN_PROFIT_FACTOR也排除(2026-08-17新增)——profit_factor
    是None代表完全沒有虧損，這是最好的情況，不當排除依據。"""
    if not summary or summary["n"] < MIN_TRADES_FOR_RANKING:
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
    trust_net欄位才能算出真正的表現)——沒接上這些欄位、或還沒累積到MIN_TRADES_FOR_
    RANKING筆完整交易的策略，一樣會被排除(見should_disable)，新股票/新啟用的策略會
    整組先排除，等資料累積夠了下次重跑才會開始判斷。"""
    disabled = []
    for strategy_name in sorted(NOTIFIABLE_STRATEGIES):
        summary = summarize_strategy(symbol, bars, strategy_name, strategy_params.get(strategy_name, {}))
        if should_disable(summary):
            disabled.append(strategy_name)
    return disabled
