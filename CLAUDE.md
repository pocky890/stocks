# 專案筆記

台股訊號監控/模擬交易系統。Streamlit dashboard(`dashboard/app.py`) + 策略引擎
(`src/stocks/strategies/`) + 排程腳本(`scripts/run_live.py`盤中、`scripts/run_batch.py`
收盤後)。commit message已經寫得很詳細(這個專案的慣例)，想知道某個決定的來龍去脈，
`git log`本身就是最完整的文件，這裡只記「git本身看不出來、容易漏掉」的事。

## 每台電腦拉新commit後要做的事

**`data/*.db`(SQLite資料庫)是`.gitignore`排除的，每台電腦各自獨立、不會透過git同步。**
這代表：
- 觀察清單股票、K線/籌碼歷史資料、`signal_events`歷史訊號——各機器本來就該有自己的，
  正常跑`run_live.py`/`run_batch.py`/`daily_update`就會累積起來，不用特別處理。
- 但`symbols.disabled_strategies`(每支股票每個策略要不要推播的排除清單)是用
  **當時的策略參數**backtest算出來、寫進這台電腦的DB——如果pull到的commit改了策略的
  預設參數(例如調整RSI門檻、停損%、連續天數這類)，這台電腦的排除清單並不會自動跟著變，
  因為那是`scripts/recompute_strategy_selection.py`手動跑出來的結果，不是git同步的內容。

**所以：只要pull到的commit改了`config.json`裡任何策略的`strategy_params`(或
`strategy_selection.py`的門檻)，都要在這台電腦上重跑一次：**

```bash
python scripts/recompute_strategy_selection.py
```

不跑的話，這台電腦的通知/模擬交易紀錄會繼續用「舊參數當時算出的排除清單」在跑，
即使程式碼裡的策略邏輯已經是新的了。

2026-08-15這批commit(`521d60b`..`2989fd1`)全部都動到了策略參數或排除門檻(背離抄底
RSI/確認機制、外資買超動能連續天數、投信買超動能視窗、投信轉買超搶進整支移除)，pull完
一定要重跑一次上面那行指令。

## 其他這批commit裡值得注意的變化

- `chip_reversal_fast`(投信轉買超搶進)整支被移除——程式碼、STRATEGY_REGISTRY、
  config.json都拿掉了。如果這台電腦的DB裡有股票的`disabled_strategies`還留著這個
  字串殘留，重跑上面那行recompute就會順便清乾淨(不影響任何行為，NOTIFIABLE_STRATEGIES
  已經不含它)。
- 新增了`CIRCUIT_BREAKER_EXEMPT_STRATEGIES`(目前只有`bullish_divergence`)——這支策略
  進場條件本身就是要在股票跌破自己月線時買，跟斷路器的「自己也跌破月線」擋單條件天生
  衝突，已經讓它完全跳過斷路器檢查(`src/stocks/circuit_breaker.py`)。

## 2026-08-16這批commit：bullish_divergence/capitulation_reversal改用兩三階段出場

**這批動到`bullish_divergence`跟`capitulation_reversal`的`strategy_params`(rsi_ceiling/
stop_mode等)，pull完一定要重跑一次`python scripts/recompute_strategy_selection.py`。**

- `bullish_divergence`(背離抄底)正式改用：`rsi_ceiling`30→35(全觀察清單10年回測驗證：
  加總報酬+22%，勝率/平均報酬幾乎不變)；出場從固定15%移動停損改成
  `stop_mode="structural"` + `enable_tiered_profit`三階段架構(進場K棒低點-5%緩衝防
  接刀→獲利12%或觸及季線先賣半倉、剩餘部位停損上移保本→剩餘部位改15%寬幅移動停損)。
  這是使用者明確要的取捨：勝率45.3%→56.9%、最大回撤大幅收斂，代價是總報酬跟獲利因子
  都比固定15%版本差不少(這個codebase其他策略平常是獲利因子+總報酬優先，這支是使用者
  特意選的例外，不是判斷標準本身變了)。
- `capitulation_reversal`(爆量急殺止穩)正式改用：`stop_mode="structural"`(停損=爆量
  急殺當天最低點-5%緩衝，不是進場當天) + `enable_tiered_profit`兩階段架構(反彈觸及
  20日均線先賣半倉、剩餘部位停損上移保本後改15%寬幅移動停損)，同時加入
  `CIRCUIT_BREAKER_EXEMPT_STRATEGIES`。這支策略啟用兩階段出場後獲利因子不降反升
  (5.82→7.62)、MDD大幅收斂(-110.1→-68.6)，跟其他策略「提早鎖利就系統性犧牲總報酬」
  的結論不同(推測是這支策略的優勢本來就是快狠準吃反彈、不是抱大波段)，代價是總報酬
  下滑、且有幾檔個股從正報酬變負報酬——使用者確認接受。斷路器部分：實測「同產業
  ≥60%也跌破月線」AND「自己也跌破月線」同時成立、真正擋下BUY的比例只有2.2%(不是
  結構性衝突，遠低於bullish_divergence的近100%)，但使用者認為這2.2%剛好是最劇烈的
  恐慌轉折點，寧可不設這道防線，故仍加入豁免清單。
- 兩支策略現在都是一買配兩賣的scaleout形狀，新增了共用的`is_scaleout_strategy()`
  (`src/stocks/strategy_stats.py`)判斷函式——`strategy_selection.py`(排除清單邏輯)、
  `dashboard/app.py`的策略歷史勝率表格、`watchlist_view.py`的模擬交易紀錄表格三處
  都已經改用它動態決定要用`simulate_round_trips`還是`simulate_scaleout_trades`配對，
  不是只在backtest腳本裡驗證過而已。模擬交易紀錄表格會把一筆scaleout交易拆成
  「(半倉)」「(剩餘半倉)」兩列各自的真實買賣價位顯示，不是合併成一個平均數字。
  如果之後新增第三支scaleout策略，記得也要在`is_scaleout_strategy()`裡加判斷。
- 這批commit同時把所有策略檔案(`src/stocks/strategies/*.py`)的docstring/inline註解
  大幅簡化，只保留「現行規則是什麼」的條列說明+斷路器適用與否，原本落落長的backtest
  历史/使用者建議討論過程都拿掉了(那些內容`git log`本身找得到，不需要留在程式碼裡)。
  如果之後想查某個參數當初為什麼改，去查這批commit附近的對話記錄或問使用者，不是
  在程式碼註解裡找。
- `trend_following`/`breakout`/`atr_breakout`裡新增了幾個目前config.json**沒有**
  啟用的研究參數(`entry_trigger`、`stop_mode="trailing_atr"`/`"two_stage"`、
  `volume_multiplier`、`ma_break_confirm_days`等)——這些是backtest過但證實沒有比
  現行設定好(反覆驗證出同一個結論：這幾支策略靠少數幾筆抱到滿的大波段撐報酬，任何
  提早鎖利/收緊停損的機制幾乎都會系統性砍掉這個獲利來源)，只是保留起來方便以後測試用，
  不是待完成的功能，不用理它們。
- **`chip_momentum`(外資買超動能)正式改用`entry_mode="ratio"`(`ratio_window_days=5`、
  `ratio_threshold=0.10`)**：近5日外資淨買超加總為正、且加總/近5日總成交量加總>10%，
  用集中度取代原本「連續剛好N天買超」的頻率判斷。這是`config.json`的
  `strategy_params`變更，**pull到這個commit要在這台電腦重跑一次
  `python scripts/recompute_strategy_selection.py`**(已在做這個決定的那台電腦上跑過)。
  全觀察清單10年回測(`scripts/backtest_chip_momentum_ratio_entry.py`，baseline要用
  `config.strategy_params["chip_momentum"]`而不是程式碼裡的hardcoded預設值——兩者
  不一樣，第一次跑這支腳本時baseline誤用了空dict、讓chip_streak_days掉回程式碼裡的
  預設值3而非config.json實際的5，算出來的比較是錯的，後來修正過)結果：原本(連續
  剛好5天)410筆、加總報酬6758.6、獲利因子3.78；ratio 10%門檻530筆、加總報酬7061.7、
  獲利因子3.25——10年獲利因子略輸原本版本(原本優勢部分來自少數幾檔股票的超高獲利
  因子，如2330台積電10.13、2454聯發科13.97、3661世芯-KY 12.03，跟這codebase其他
  策略「靠少數大波段撐報酬」的既有模式一致)，但2026年較近期區間(YTD/7月)ratio 10%
  全面優於原本版本(YTD勝率48.3% vs 38.7%、獲利因子2.92 vs 1.70、最大回撤-138.3 vs
  -138.4打平；7月最大回撤-37.2 vs -44.6)。在ratio 6/8/10/12%四個門檻裡，10%在三個
  時間窗口的綜合表現(獲利因子、最大回撤、近期表現)都優於8%——8%雖然10年加總報酬
  略高，但獲利因子更低、回撤更大，近期表現也輸10%。使用者確認採用10%，是接受10年
  獲利因子小幅下降、換取近期表現與風控改善的取捨。原本的`entry_mode="streak"`(連續
  剛好`chip_streak_days`天買超)保留在程式碼裡當備援，`config.json`的`chip_streak_days`
  也還留著(現在entry_mode="ratio"不會讀到它，純參考值，之後要切回streak模式可以用)。
- `trust_momentum`(投信買超動能)新增`cooldown_days`研究參數(停損出場後N天內不重新
  進場)，**config.json沒有啟用(預設0，即現行:level-triggered，條件仍成立就立刻
  重新進場)**。起因是使用者質疑投信「左側攤平」+ level-triggered進場可能造成連續
  停損的死亡螺旋，提議加冷卻期或價格確認(收盤價>5MA/20MA，這個用現有的
  `require_uptrend`+`trend_ma_period`參數就能測，不用新增程式碼)。全觀察清單10年
  回測(`scripts/backtest_trust_momentum_confirm_cooldown.py`)結果：兩個方向都沒有
  幫助——冷卻5/10天、5MA/20MA確認、以及兩兩組合，7種變體在10年、2026 YTD、甚至
  2026-07這種全月虧損的區間，加總報酬跟獲利因子全部都比現行設定差(10年現行7090.5/
  3.08 vs 最好的變體也只有6453.7/3.04；連理論上該幫上忙的極端虧損月份，現行的
  -254.7也優於5MA確認的-305.8)。判斷是投信連續買超的訊號雖然理論上有「左側攤平」
  風險，但實際上continuation(而非死亡螺旋)才是常態，過濾掉重新進場反而系統性
  砍掉這個獲利來源，跟`trend_following`那組研究參數的結論同一個道理。維持現行
  `cooldown_days=0`、`require_uptrend=False`，這個研究參數保留起來但不用理它。
  另外提醒：config.json裡`trust_momentum`的`chip_window_days`/`chip_min_buy_days`
  是舊`entry_mode="default"`用的參數，現在active的是`entry_mode="window10_3"`
  (用`cum_window_days`/`recent_window_days`/`recent_min_buy_days`)，前兩個key
  目前完全沒被讀到——不是bug(不影響行為)，但容易讓人誤以為那是現行邏輯，之後如果
  要切回`entry_mode="default"`要記得這兩個key還在。
