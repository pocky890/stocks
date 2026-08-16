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

## 2026-08-16：K線圖拿掉融資融券子圖、改成成交量子圖，整支融資融券資料來源移除

使用者確認融資融券餘額圖表沒在用，整條資料管線都拿掉了，不是只藏起來不顯示：

- `dashboard/charts.py`的`price_and_chip_chart()`拿掉`margin_df`參數跟融資/融券
  雙y軸子圖，改成K線正下方(row 2)加一個成交量子圖(紅漲綠跌配色跟K棒一致)。
- 資料抓取全部拿掉：`src/stocks/db.py`的`margin_balances`表(CREATE TABLE)/
  `insert_margin_balances`/`fetch_margin_balances`、`daily_update.py`(TWSE逐日
  路徑+TPEx FinMind路徑+新增股票時的10年回補，三處都有)、
  `scripts/fetch_market_data.py`的歷史回補、`finmind_client.py`的
  `fetch_margin_balances_for_range`/`MARGIN_DATASET`、`twse_client.py`的
  `fetch_margin_balances_for_date`。`tpex_client.py`的`fetch_margin_balances_latest`
  原本就已經是死碼(2026-08-13 TPEx路徑改用FinMind後就沒人呼叫了)，這次一併清掉。
- **`data/*.db`裡舊有的`margin_balances`表不會被砍掉**(這裡只拿掉`CREATE TABLE IF
  NOT EXISTS`那行schema，不是主動DROP TABLE)——沒有殺傷力，就是留著不再寫入，之後
  真的要清也可以手動`DROP TABLE margin_balances`，不影響任何現行邏輯。
- 這份資料从來沒被任何策略讀過(只有dashboard顯示用)，所以這次移除對策略邏輯/
  `recompute_strategy_selection.py`完全沒有影響，不用重跑。

## 2026-08-16：發現yfinance會在真的休市那天塞一根假K棒(量能掛零)，已修正+清過本機DB

加成交量子圖之後意外曝光的bug：使用者發現K線圖最右邊那根K棒被裁到一半、某天成交量
異常掛零。兩個各自獨立的問題：

- **最右邊K棒被裁到**：`dashboard/charts.py`的x軸range右端點原本剛好卡在`last_date`，
  但candlestick是以K棒為中心往兩側展開寬度，最後一根的右半邊會超出繪圖區被裁掉。
  已修正：range右端點多留1天緩衝(`last_date + 1天`，那天本來就沒有K棒，不影響
  rangebreaks)。
- **2026-07-10全觀察清單每一檔都出現open=high=low=close+volume=0的假K棒**：查證
  是市場真的休市(可能是颱風假)那天，yfinance沒有直接跳過，而是回傳前一天收盤價
  當佔位K棒。`src/stocks/yfinance_client.py`的`_bars_from_dataframe()`已經加上
  `volume==0`就跳過不插入的防護(真實交易日成交量幾乎不可能剛好是0，這個判斷很安全)——
  這樣以後(不管是`_refresh_price_data`每日增量、還是新增股票時的10年回補)遇到同樣
  情況都不會再把假K棒寫進`bars_daily`。
  **但這只防得住「以後」，這台電腦`bars_daily`裡已經存在的舊假K棒不會自動消失**——
  已經在這台電腦手動跑過`DELETE FROM bars_daily WHERE volume = 0`清掉1208筆(跨774個
  不同日期，不是只有2026-07-10這天，過去10年偶爾都會有零星1、2檔股票單獨中招)。
  **`data/*.db`是每台電腦各自獨立的，這個DELETE只清了這台電腦——如果另一台電腦的
  K線圖也看到同樣詭異的平盤零成交量K棒，要在那台電腦上執行同樣的SQL清一次**：
  ```sql
  DELETE FROM bars_daily WHERE volume = 0;
  ```
  清掉之後，dashboard的rangebreaks邏輯會自動把這些日期當成缺資料的假日整批跳過
  (不需要额外處理)。這個bug也代表過去的回測/`chip_momentum`新的ratio進場模式(除以
  滾動成交量加總)如果剛好把這種假K棒算進rolling window，分母會被輕微低估——影響
  應該很小(1208筆對全部76572筆bars_daily只占1.6%，且大多是單一股票單一天零星
  出現)，沒有重新驗證過所有backtest數字，如果之後重新回測發現數字有小幅變動，
  這就是原因。

  使用者要求確認「以後真的遇到平日休市，圖表要正確跳過、不留空白」這件事有被驗證到，
  不是只靠「應該會生效」的推論——查過`dashboard/charts.py`原本就有的rangebreaks邏輯
  (`missing_days`/`holiday_days`那段)本來就是設計來處理這個情境的：只要某個交易日
  在`bars_daily`裡完全沒有那一列資料(不是像yfinance假K棒那樣有資料但是假的)，就會被
  歸進`missing_days`，過濾出平日(`weekday < 5`)後加進`rangebreaks`的`values`，
  plotly就會把那天完全壓縮掉，不會留空白也不會畫K棒——這段邏輯本來就存在、不是這次
  新寫的，這次只是驗證它跟「跳過假K棒」的修正接起來後仍然正確運作。已經補上
  `tests/test_charts.py`鎖住這個行為(模擬拿掉一個平日當作holiday，驗證確實被抓進
  rangebreaks)，避免以後改壞。

## 2026-08-16：chip_momentum/trust_momentum新增出場量能濾網(volume_alert_scaleout)

起因是使用者檢討8299(群聯)/2313(華通)今年虧損時，發現多支策略完全沒有量能濾網，找了
Gemini一起提案。總共測了4組量能濾網提案(2支AI各提2組)，全觀察清單10年+2026 YTD
回測後**只有1組確認有效並採用為正式預設**，其他3組都測過但沒有採用，程式碼留著當研究
參數：

- **採用(已寫進config.json，pull到這個commit要重跑`recompute_strategy_selection.py`)**：
  `chip_momentum`/`trust_momentum`新增`stop_mode="volume_alert_scaleout"`——高檔跌破
  `alert_ma_period`(現行:10)日均線且成交量>`alert_volume_multiplier`(現行:1.5)倍
  `alert_volume_avg_period`(現行:20)日均量時，觸發賣出一半("爆量出貨警示")，剩餘半倉
  改用`stop_pct`(15%)移動停損出場，不用傻等單一停損才反應。這是一買配兩賣的scaleout
  形狀，`strategy_stats.is_scaleout_strategy()`已經加入判斷(`stop_mode=="volume_alert_scaleout"`
  時回傳True，跟`golden_cross_scaleout`的`ma_scaleout`同一個機制)，不用另外處理三個呼叫端
  (`strategy_selection.py`/`dashboard/app.py`策略歷史表格/`watchlist_view.py`模擬交易
  紀錄表格都共用同一個判斷函式)。回測結果(vs 現行單一15%移動停損)：trust_momentum
  10年獲利因子3.09→5.19、加總報酬還小贏(7421.0→8085.7)、最大回撤砍到-352.6(原
  -571.9)；chip_momentum 10年獲利因子3.03→4.24、回撤-553.8→-292.0，總報酬小輸12%
  換回撤砍半，全面比現行好。5MA vs 10MA兩個版本都測過：10年樣本10MA全面較好，
  2026 YTD單獨看5MA較好但樣本小(trust_momentum YTD只有23~28筆)，使用者最後選10MA
  (10年樣本較穩健，不是單看近期表現)。
- **測過沒採用**：
  - `chip_momentum`/`trust_momentum`進場加量能濾網(`require_entry_volume`，"帶量點火"，
    要求進場當天量>1.3~1.5倍均量)——10年跟YTD筆數都腰斬(568→335~377)，總報酬明顯下滑，
    獲利因子沒有穩定變好。跟`trust_momentum`之前測過的「進場加5MA/20MA價格確認」
    (2026-08-15那次研究)結論一樣：這類策略靠緩慢累積的法人買超訊號，額外疊加稀有事件
    門檻(不管是價格確認還是量能點火)都會系統性砍掉延續性訊號，不是單一次巧合。
  - `bullish_divergence`加爆量確認(`require_capitulation_volume`：創新低當天要求爆量；
    `require_reversal_volume`：確認訊號當天要求爆量，兩者都已寫進程式碼但預設False)——
    兩者都是用大砍筆數(622→225或607)換一點點勝率/獲利因子，總報酬腰斬，沒有乾淨的贏；
    且`require_capitulation_volume`會把8299/2313今年的訊號全部濾掉(不是把虧的那筆變賺，
    是整支策略今年對這兩支股票完全不出手)。跟`capitulation_reversal`訊號重疊率不高
    (<5%)，這點原本的疑慮不成立，但不影響「總報酬掉太多」這個結論。
  - `long_swing`重新進場加量能濾網(`require_reentry_volume`：回踩期間量縮+重新站回
    20MA當天微幅放量，已寫進程式碼但預設False)——10年加總報酬9148.5→7879.9(-14%)、
    獲利因子4.22→4.08，全部指標都變差；8299今年這3筆交易筆數沒變少，但因為濾網延後
    進場日、買在更差價位，虧損從-17.4惡化成-43.4。全面劣化，不是取捨。
  - `atr_breakout`進場加量能濾網(`require_entry_volume`+`volume_multiplier`，跟姊妹
    策略`breakout`同一套命名，已寫進程式碼但預設False)——幾乎是no-op：10年筆數只少
    3~5%，獲利因子/回撤幾乎沒變化。推測是現行已啟用的`require_weekly_trend`(週線趨勢
    確認)已經先把「量縮盤整後假突破」濾掉大半，量能濾網疊上去篩不出額外雜訊。而且
    8299/2313今年完全沒有觸發過這支策略的訊號(0筆)，跟這兩支個股今年的問題無關。

以上4支策略檔案(`chip_momentum.py`/`trust_momentum.py`/`bullish_divergence.py`/
`long_swing.py`)裡沒被採用的研究參數，全部保留在程式碼裡但預設False/`stop_mode`
維持原值，不影響現行行為；對應的回測腳本(`scripts/backtest_chip_trust_momentum_volume_filter.py`/
`backtest_bullish_divergence_volume_confirm.py`/`backtest_long_swing_reentry_volume.py`/
`backtest_atr_breakout_volume_filter.py`)也留著，之後要重新驗證或調整門檻可以直接跑。

## 2026-08-16：chip_momentum/trust_momentum/golden_cross_scaleout/atr_breakout/breakout
新增長期regime濾網(require_long_regime)

起因：使用者新加了幾支這幾年一路下跌的股票(2314台揚2021年見頂後跌95%、4763材料*-KY
2023年見頂後腰斬)進觀察清單，質疑「這幾支策略是不是設計有問題，見頂反轉後會一路撿一路
停損」。查證後確認**是策略設計本身的缺口，不只是排除清單反應太慢的問題**：這5支策略的
進場條件全部是5~20天級別的短線技術型態(均線交叉、法人近5~15日買超、20日新高突破)，
完全沒有檢查「這支股票現在是不是已經進入長期空頭」，空頭市場裡的假反彈一樣容易觸發這些
短線條件。反證：`long_swing`是全部NOTIFIABLE_STRATEGIES裡唯一原本就有長期regime濾網
(`regime_active = MA60 > MA120`)的策略，在2314/4763見頂後撐得明顯比其他策略好(4763見頂後
獲利因子37.37，其他策略只有golden_cross_scaleout以外全部轉負)——`trend_following`用
20/60日這個較短的regime窗口，效果就明顯不如long_swing的60/120日，證實「regime確認窗口
越長，抵抗空頭假反彈的能力越強」這個機制推論。

**已採用(config.json這5支策略都加上`require_long_regime: true`、
`regime_fast_period: 60`、`regime_slow_period: 120`，pull到這個commit要重跑
`recompute_strategy_selection.py`)**：`scripts/backtest_long_regime_filter.py`全觀察
清單10年回測，5支策略全部同一個方向——獲利因子/最大回撤全面大幅改善(獲利因子普遍
+20%~+65%，最大回撤普遍砍半)，總報酬多數只小輸10%~21%(`chip_momentum`幾乎沒差：
6205.1→6154.2)，不是像某些量能濾網那種腰斬式犧牲。2314/4763見頂後窗口驗證：
**`golden_cross_scaleout`在4763上直接從獲利因子0.90(淨虧-7.8%)翻正變成獲利因子2.57
(淨賺+37.4%)**，這正是「10年整體數字還很漂亮、排除清單還沒抓到、但這支股票已經在
賠錢」的活案例，加濾網直接解決。2314因為崩跌幅度太極端(高點跌95%)，濾網沒辦法完全
消除虧損(空頭市場偶爾60日均線還是會短暫爬回120日均線之上)，但5支策略的虧損全部明顯
縮小(例如golden_cross_scaleout -130.9%→-51.0%、atr_breakout -68.4%→-17.4%)，
`chip_momentum`是效果最弱的一支(2314上總損失只小幅改善，但在4763上濾網讓策略整段
見頂期間完全不出手，避開原本的虧損)。

重跑`recompute_strategy_selection.py`後，**很多股票的`trust_momentum`新被排除**，
主因不是表現變差，是這個濾網讓交易頻率下降(全觀察清單10年406→290筆，-29%)，不少股票
的交易筆數因此掉到`MIN_TRADES_FOR_RANKING`(15筆)門檻以下——這是預期中的副作用，跟
`atr_breakout`(2026-08-16加`require_weekly_trend`後降到8筆)、`chip_momentum`(連續
買超天數3→5後降到10筆)當初交易頻率下降時的處理是同一個模式，之後累積更多交易後
會自然解除排除，不代表這些股票的訊號品質變差了。目前`trust_momentum`還沒有像
`atr_breakout`/`chip_momentum`一樣調整`MIN_TRADES_OVERRIDES`，如果之後覺得太多股票
長期安靜可以考慮調降門檻，暫時先觀察。

`long_swing`本身已經有這套regime濾網(不用重複加)，`bullish_divergence`/
`capitulation_reversal`的進場前提是「自己正在破底/急殺」，跟長期多頭regime濾網天生
衝突(這兩支本來就是要在股票走弱時進場)，故沒有加這個濾網。

## 2026-08-16：新增18支近年跌很兇的股票當基本面濾網研究樣本

為了驗證regime濾網、以後的基本面濾網有沒有真的抓對「見頂反轉後避免一路接刀」這件事，
樣本只有2314/4763兩支太少，容易overfitting到這兩支個案的巧合。使用者從一份10年累計
跌幅-85%~-97%的股票清單裡挑了18支加進觀察清單：8444/2929/4426/8437/4174/8044/1340/
2239/3552/4529/8429/2726/1338/1565/4552/4416/8450(公司)+00664R(國泰臺灣加權反1，
反向ETF)。加入後跑過一次`recompute_strategy_selection.py`，這18支幾乎都是9支
NOTIFIABLE_STRATEGIES全部被排除的狀態，符合預期(10年報酬這麼差，動能/突破類策略的
門檻本來就該全滅)。

**00664R是反向ETF，不是公司**：它的漲跌方向刻意跟大盤相反、還有正反向ETF每日結算的
複利耗損效應，這是結構性特性、不是「基本面轉弱」，之後做月營收/EPS類的基本面濾網
它不會有任何資料(FinMind的財報類dataset只涵蓋公司)——已經實測確認：`fetch_market_data.py`
補資料時00664R的估值/月營收都是0筆，這是預期中的正常結果，不是抓取失敗，這支股票留著
測技術面策略但基本面研究樣本要自動排除它。

## 2026-08-16：新增monthly_revenue月營收資料管線(基本面濾網研究第一步)

使用者想在現有策略上疊加基本面判斷(避開「基本面開始走弱」的公司)，經討論決定先做
月營收(比EPS更即時：FinMind的`TaiwanStockMonthRevenue`逐月更新，EPS的
`TaiwanStockFinancialStatements`是季報、且17種財報科目混在同一個dataset裡要另外篩
`type=='EPS'`，時效性跟資料乾淨度都不如月營收，先不做)。**目前這份資料完全沒有被
任何策略讀取**，純粹是資料管線+歷史回補，濾網規則設計跟回測是下一步。

- 新表`monthly_revenue`(symbol, date, revenue_year, revenue_month, revenue)，
  primary key是(symbol, revenue_year, revenue_month)而不是(symbol, date)——`date`
  是FinMind回傳的「營收所屬月份的下個月1號」，不是公司實際公告日期(公司法規公告期限
  是次月10日前)，拿它當實際可用日期會有大約9天內的look-ahead，之後設計進場濾網時
  要處理這個時間差，不能假設`date`當天就查得到這筆資料。
- `finmind_client.fetch_monthly_revenue_for_range()`不分上市/上櫃，兩個市場共用同一個
  dataset，不像三大法人/估值要分TWSE官方API/TPEx FinMind兩條路。
- `add_symbol_to_watchlist()`新增股票時，三大法人/估值/月營收現在是三個各自獨立的
  try/except，任一個失敗不影響其他兩個(也不影響新增流程本身)。
- `daily_update.check_and_update()`新增`_refresh_monthly_revenue()`，對整個觀察清單
  (不分市場)用跟TPEx增量更新同一套`_fetch_range_per_symbol`——這個函式本來就是「查
  上次抓到的日期+1~今天」，同一個月內重複執行、還沒有新月份資料時FinMind回傳空結果，
  沒有额外做「這個月是否已經抓過」的節流，多跑幾次沒有副作用。
- `scripts/fetch_market_data.py`(既有股票的批次回補腳本)一併加上月營收，已經對全部
  51檔觀察清單股票(含上面新加的18支)跑過一次，大多數股票拿到120筆(10年×12個月)，
  上市較晚的股票筆數較少符合預期，00664R(ETF)是0筆(見上一則說明)。

## 2026-08-16：月營收年增率濾網回測結果——4/6策略有效，2支沒用/變差

用`db.attach_monthly_revenue_growth()`(已處理FinMind公告日+10天緩衝的look-ahead)對
`chip_momentum`/`trust_momentum`/`golden_cross_scaleout`/`atr_breakout`/`breakout`/
`long_swing`這6支「事後過濾」BUY訊號(月營收年增率<0%就擋掉，不修改策略檔案，見
`scripts/backtest_revenue_growth_filter.py`)，全觀察清單10年+20支已知近年下跌很兇的
股票(2314/4763+2026-08-16新加的18支，00664R反向ETF排除，理由是它沒有月營收/財報
資料)兩個範圍回測：

- **trust_momentum/golden_cross_scaleout/atr_breakout/breakout明確變好**(20支下跌股上
  獲利因子：2.22→3.12、0.79→0.96、0.89→1.11轉正、1.11→1.53)
- **chip_momentum幾乎沒用**(0.56→0.46，還變差一點)
- **long_swing明確變差**(獲利因子打平但加總報酬507.7→289.4，跌43%)——推測是long_swing
  本身已經有60/120日regime濾網，疊加營收濾網等於兩道獨立趨勢確認互相打架，濾掉了
  regime濾網已經確認沒問題、但營收剛好短期波動(例如認列一次性費用)的正常交易。

門檻測試：「最近月營收年增率<0%」和「近3月年增率均值<0%」效果幾乎一樣，都明顯優於
寬鬆的「<-10%」——真正有效的訊號在「營收由正轉負」這條線附近，不用等營收重摔兩位數。

## 2026-08-16：3個Macro Regime Filter(Gemini建議)回測結果

使用者質疑「前面幾年大漲、後面幾年連續崩跌超過80%」的股票(產業結構性衰退/主力出貨
完畢的超級大循環結束，例如台股常見的宏達電、被動元件、末代面板股型態)，每次短線反彈
都會誘發策略買進然後迅速破底(Value Trap/千刀萬剮)，轉述Gemini提了3個總體位階濾網，
用`scripts/backtest_macro_regime_filters.py`驗證：

- **120日均線斜率向上，套用在抄底類(bullish_divergence/capitulation_reversal)**：
  `bullish_divergence`在20支下跌股上沒有明確幫助(獲利因子0.79→0.54，交易砍到剩1/3但
  品質沒變好)，不採用。`capitulation_reversal`在20支下跌股上明確有效(獲利因子
  0.65→0.98，接近打平)，但代價是全觀察清單總報酬砍71%(1975.2→567.7)，比
  `enable_tiered_profit`當初的取捨還激烈，使用者質疑代價太大、要求找更精準的替代方案，
  **後來改用`loss_cooldown_days`解決(見下一則)，這個120MA斜率濾網最終沒有採用**，
  程式碼裡`require_long_uptrend_intact`還留著但`config.json`沒有開啟。

## 2026-08-16：capitulation_reversal改用loss_cooldown_days(失敗冷卻期)取代120MA斜率濾網

上一則的120MA斜率濾網(全面性擋掉「120MA沒有上揚」的股票)雖然能讓`capitulation_
reversal`在20支已知下跌股上獲利因子從0.65拉到0.98，但因為是不分青紅皂白地擋掉所有
120MA走平的股票(連正常盤整的健康股票也一起濾掉)，全觀察清單總報酬砍71%，使用者
認為代價太大，要求找更精準的替代方案——核心原則(已存進memory：大賺小賠優先於總報酬
最大化，但重點是「可以賠、不能在同一支股票上一路撿一路賠」)。

改用`loss_cooldown_days`：不看均線位階，只看**這支股票自己**上一次進場是否觸發
「跌破結構停損(恐慌未止穩，全部出場)」——如果有，代表這支股票的恐慌沒有真的止穩，
對這支股票暫停loss_cooldown_days天再重新進場；沒觸發全部出場(正常止穩獲利出場)的話
完全不受影響。這是stock-specific的懲罰機制，不是全面性的regime判斷，不會誤傷正常
運作的其他股票、也不影響同一支股票下一次表現正常的訊號。

用`scripts/backtest_capitulation_loss_cooldown.py`測過30/60/90/180/365天，**180天是
甜蜜點**：20支下跌股上獲利因子0.65→0.94(幾乎跟120MA斜率濾網的0.98一樣好)，但全觀察
清單總報酬只掉7%(1975.2→1839.4)，獲利因子甚至還小幅變好(3.62→3.66)——效果差不多、
代價只有120MA斜率濾網的十分之一。365天反而變差(可能冷卻太久錯過真正的轉機反彈)；
60天冷卻疊加120MA斜率的組合數字幾乎跟純120MA斜率一樣，代表兩者疊加沒有增量效益，
不需要同時使用。

**已採用：config.json的`capitulation_reversal`加上`loss_cooldown_days: 180`**，
pull到這個commit要重跑`recompute_strategy_selection.py`。`trust_momentum`之前研究過
的`cooldown_days`(2026-08-15，效果不好、未採用)是不分青紅皂白對所有停損都冷卻，跟這裡
「只在真正的結構性失敗(全部出場，不是正常止穩)才冷卻」不是同一回事，兩支策略的
cooldown設計精神不同，不要混淆。
- **52週高點回撤>40%擋新BUY，套用在chip_momentum/long_swing**：兩支都沒有明顯幫助——
  `long_swing`加了以後數字幾乎沒變(跟MA240疊加後數字完全一樣，代表這道濾網對
  long_swing是no-op，本身的60/120regime已經先把該擋的都擋掉)；`chip_momentum`在
  20支下跌股上獲利因子維持在0.42~0.61打轉，**這是這一輪測過的第5種濾網(量能/regime/
  drawdown/MA240/營收)都沒能讓chip_momentum在已知下跌股上轉正**，判斷是這支策略
  (外資短線買超訊號)在結構性空頭股票上本質上就是弱勢，不是缺一道濾網的問題，之後
  不再繼續為它找新濾網，交給`recompute_strategy_selection.py`的排除清單機制處理。
- **收盤價>240日均線(年線)的絕對位階濾網，跟既有60/120日regime濾網比較**：MA240單獨
  取代60/120regime全面略差，但**兩者疊加(都要通過)在3支上有明確增量效益**：
  `golden_cross_scaleout`(獲利因子0.79→0.92，虧損收斂74%)、`atr_breakout`(0.89→
  1.01轉正)、`breakout`(1.11→1.24)。`trust_momentum`疊加後打平(2.22→2.24，沒有
  明顯增量，這支已經靠營收濾網達到明確改善，不需要再加MA240)。

**已採用(config.json的`golden_cross_scaleout`/`atr_breakout`/`breakout`都加上
`require_above_long_ma: true`、`long_ma_period: 240`，疊加在既有的`require_long_
regime`之上、不是取代，pull到這個commit要重跑`recompute_strategy_selection.py`)**。
`chip_momentum`/`long_swing`維持現狀不加任何新濾網，`bullish_divergence`不採用120MA
斜率濾網，`capitulation_reversal`的120MA斜率濾網取捨還沒決定，程式碼已經加好(`require_
long_uptrend_intact`)但`config.json`尚未啟用。

**更新(見下方2026-08-16「月營收年增率濾網正式接進策略」)：這一則寫的時候營收濾網還
只是事後過濾的驗證，後來已經正式寫進策略程式碼+config.json生效，不要被這段舊敘述
誤導。**

## 2026-08-16：驗證這一輪濾網對「原始觀察清單」(這次對話開始前的28支)有沒有意外傷害

使用者質疑：今天這一輪所有回測都是用「全觀察清單」(混雜了2026-08-16新加的18支+使用者
自己在這次對話前就加的6806/2314/4763，共21支「已知近年很爛」的股票)或「20支已知下跌
很兇的股票」，從來沒有單獨驗證過這次對話開始前git已經commit的原始28支(1303/2308/2313/
2330/2337/2383/2408/2454/3037/3141/3189/3363/3443/3450/3526/3595/3661/3680/3711/
6187/6239/6491/6531/6640/6903/7769/8046/8299)有沒有被今天這些濾網意外傷到。用
`scripts/backtest_original_watchlist_impact.py`補測(改動前config vs 現行config，
只跑這28支)，結果整體正面，跟全觀察清單看到的模式一致：

| 策略 | 10年獲利因子(改動前→後) | 10年總報酬變化 |
|---|---|---|
| atr_breakout | 4.52→4.95 | -22% |
| chip_momentum | 3.26→6.71 | -13% |
| trust_momentum | 3.07→6.30 | -4% |
| golden_cross_scaleout | 3.34→3.49 | -31% |
| breakout | 3.56→3.79 | -16% |

全部獲利因子提升、回撤大幅收斂(這裡沒列，普遍-30%~-73%)，總報酬取捨幅度合理。

**唯一要注意的例外：`trust_momentum`在原始28支股票上的2026 YTD單獨看不太妙**——交易
筆數75→16(砍掉79%)，加總報酬487.5→84.0(掉83%)。這不代表策略壞了(10年數字證明長期
是賺的、且總報酬只小輸4%)，是regime濾網(60/120日均線)讓今年市場狀態下這些原始股票
符合條件的訊號大幅減少，是濾網生效的直接後果，不是bug——但代表2026年這一年`trust_
momentum`在原始清單上會變得比較安靜，通知頻率會明顯下降，不要以為裝了濾網後今年活躍度
會跟以前一樣。其他4支策略的YTD數字都還算穩定(atr_breakout/breakout幾乎沒變，
golden_cross_scaleout今年甚至更好)。

## 2026-08-16：regime/MA240濾網讓交易頻率大減，同步調降MIN_TRADES_OVERRIDES

使用者發現套用今天這一輪regime/MA240濾網後，很多策略的個股交易筆數掉到個位數，質疑
「濾網是不是設太嚴了」。查證後確認是真的：用現行config vs 「今天regime/MA240濾網
之前」的params在全觀察清單(51檔)10年上比較，交易筆數縮減30%~46%：

| 策略 | regime/MA240之前 | 現行 | 縮減比例 |
|---|---|---|---|
| chip_momentum | 711筆 | 396筆 | -44% |
| trust_momentum | 479筆 | 320筆 | -33% |
| golden_cross_scaleout | 1358筆 | 738筆 | -46% |
| atr_breakout | 508筆 | 363筆 | -29% |
| breakout | 734筆 | 498筆 | -32% |

`strategy_selection.py`的`MIN_TRADES_OVERRIDES`(判斷「樣本夠不夠可信」的門檻，不是
策略參數本身)沒有跟著同步調整，導致大量個股(尤其`trust_momentum`51檔裡41檔、
`breakout`35檔)純粹因為樣本跌到門檻以下被排除，不是表現變差——這是機制上的假警報。
按上面的縮減比例，對現有門檻同比例調降：`chip_momentum`10→6、`atr_breakout`8→6、
`trust_momentum`新增override 15→10、`golden_cross_scaleout`新增override 15→8、
`breakout`新增override 15→10。跟`atr_breakout`(2026-08-16加`require_weekly_trend`後
461→274筆調降15→8)、`chip_momentum`(連續買超天數3→5後567→331筆調降15→10)當初的調整
邏輯完全一樣，只是這次是疊加在既有override之上再進一步調降。**已採用**，pull到這個
commit要重跑`recompute_strategy_selection.py`。

## 2026-08-16：驗證regime/MA240濾網移除的交易，是不是多殺賠錢單、少殺賺錢單

使用者追問：交易筆數減少，是賠錢的筆數減少比較多，還是賺錢的？用「before參數的每一筆
交易」vs「after參數還剩下哪些」，用(股票代號,進場日期)當key找出「被移除的那些交易」，
直接統計這些被移除交易本身的輸贏(不是看整體統計數字)：

| 策略 | 移除筆數 | 賺錢(筆數/合計%) | 賠錢(筆數/合計%) |
|---|---|---|---|
| chip_momentum | 476 | 191筆 / +5409.8% | 285筆(60%) / -2678.4% |
| trust_momentum | 281 | 115筆 / +4605.6% | 166筆(59%) / -1633.7% |
| golden_cross_scaleout | 887 | 316筆 / +11474.0% | 571筆(64%) / -6568.0% |
| atr_breakout | 241 | 94筆 / +3584.0% | 147筆(61%) / -1672.8% |
| breakout | 290 | 101筆 / +1598.6% | 189筆(65%) / -1019.4% |

**按筆數看，濾網判斷方向是對的**——5支策略被移除的交易裡59%~65%都是賠錢的，不是隨機
亂砍，代表regime/MA240濾網真的在辨識「這筆該不該進場」。**但按總報酬金額看，移除的
賺錢交易合計金額反而比賠錢交易還大**(例如chip_momentum移除的191筆賺錢交易加起來
+5409.8%，比285筆賠錢交易的-2678.4%還多超過一倍)——原因是這幾支策略「靠少數幾筆大
波段撐報酬」(CLAUDE.md裡反覆記錄過的既有模式)，濾網雖然多殺賠錢單(筆數上)，但也不
可避免會誤殺少數幾筆貢獻巨大的win(金額上)，這就是為什麼獲利因子/回撤變好、但總報酬
會往下掉的真正原因——不是濾網判斷錯了，是頻率換品質的必然取捨。

## 2026-08-16：斷路器拿掉「自己也跌破均線」的AND條件(config.circuit_breaker_own_ma_period=None)

起因是使用者發現「隊長」群組(2454聯發科/2408南亞科/3450聯鈞/3189景碩/3037欣興/3443創意/
6239力成/6531愛普/7769鴻勁/8046南電/3680家登/6187萬潤/6640均華/6903巨漢/3595山太士這
15檔，多數同屬產業代碼24半導體設備/封測/基板)2026年5月起幾乎每支策略都在賠錢。查證後
發現這15檔集中在同一個產業，2026年6-7月這個產業經歷了同步性劇烈修正(產業寬度從5月的
30%飆升到7月底97%~98%，斷路器持續啟動6/8~6/17跟7/9~8/7)，多數股票其實5月至今是大漲的
(南亞科+117%、景碩+63%等)，8月已經回升——是主升段中的一次同步性中繼修正，不是結構性
反轉。

**量化斷路器實際擋下率發現了一個結構性問題**：`golden_cross_scaleout`/`trend_
following`/`long_swing`/`atr_breakout`/`breakout`這5支策略在隊長組上的斷路器擋下率
是**0%**(146筆BUY訊號只擋掉8筆，且那8筆全部是chip_momentum/trust_momentum貢獻的)。
原因是斷路器原本的AND條件("全市場同產業≥60%跌破月線" AND "這支股票自己也跌破月線")
要求股票自己也跌破均線，但這5支策略的進場條件本身就要求「站上」某條均線(創新高/均線
交叉)才會觸發BUY——等到訊號真的觸發時，股價通常已經站回去了，兩個條件幾乎互斥。這
跟`atr_breakout`舊docstring早就記錄過的個別案例(10年355次BUY、擋下率0%)是同一個問題，
只是這次發現不只atr_breakout一支，是整組價格/均線類策略共通的結構性盲點。

**測過的兩個方案**：
- **拉長own_ma_period(例如60/120日)**：沒用，擋下率只從0%爬到0.2%~3.5%，見
  `scripts/backtest_circuit_breaker_own_ma.py`。原因同上——不管均線拉多長，這幾支策略
  「動能確認」型的進場條件觸發時，股價幾乎都已經站上均線了。
- **乾脆拿掉own MA這道AND條件，純看產業寬度(own_ma_period=None)**：有效——隊長組
  擋下率從0%升到38.3%，`long_swing`獲利因子從0.79(淨虧)翻正到1.39，`trend_following`
  虧損減半；全觀察清單10年整體只犧牲3~4%總報酬，獲利因子幾乎沒變。**但代價真實存在**：
  查了3711日月光投控2026-04-02的trend_following訊號(斷路器加AND條件時最初要保護的
  原始案例：全市場半導體寬度觸發但3711自己沒破月線)，在純寬度版本下會被誤擋，而這筆
  實際賺了**+30.7%**——使用者看過這個具體代價後，仍確認接受(隊產業性系統重挫的整體
  保護 > 少數逆勢股好單被誤殺)。

**已採用**：`config.json`的`circuit_breaker.own_ma_period`改成`null`(對應`config.
circuit_breaker_own_ma_period: int | None`，None代表拿掉AND條件)，`config.py`/
`circuit_breaker.py`(`is_buy_suppressed`)/`watchlist_view.py`(`build_paper_trades_
for_symbol`)都已經改成讀這個獨立參數(原本跟`breadth_ma_period`共用同一個20日均線，
2026-08-16拆開)。`run_live.py`兩處呼叫`is_buy_suppressed`的地方也已經改用新參數。
`bullish_divergence`/`capitulation_reversal`本來就在`CIRCUIT_BREAKER_EXEMPT_
STRATEGIES`裡完全跳過檢查，不受這次改動影響。

**這次也順便補了一個發現的文件缺口**：`atr_breakout`/`breakout`/`golden_cross_
scaleout`/`chip_momentum`/`trust_momentum`/`long_swing`這6支策略的class docstring
(dashboard「策略邏輯」頁籤顯示的說明文字)只有inline參數註解提到`require_long_regime`/
`require_above_long_ma`/`volume_alert_scaleout`，docstring本身完全沒提到——而且docstring
裡的「斷路器」段落全部還在描述舊的AND條件行為，這次AND條件拿掉後這幾段描述變成錯的，
不只是不完整。已經一併更新這6支的docstring，補上regime/MA240/volume_alert_scaleout
的說明，並修正斷路器描述成現行的「純看產業寬度」。`capitulation_reversal`補上`loss_
cooldown_days`說明。之後每次改動策略邏輯或斷路器機制，記得同步檢查class docstring
(不是只改inline註解)，這是使用者這次發現的一個容易漏掉的地方。

## 2026-08-16：月營收年增率濾網正式接進策略(require_revenue_growth)

前面幾則記錄的營收年增率濾網一直只是`scripts/backtest_revenue_growth_filter.py`裡
「事後過濾BUY事件」的驗證方式，沒有真正寫進策略程式碼。使用者用全觀察清單(51檔，
不是挑過的20支下跌股樣本)重新確認效果後，正式接進來了：

- **`chip_momentum`/`trust_momentum`/`golden_cross_scaleout`/`atr_breakout`/
  `breakout`這5支策略新增`require_revenue_growth`參數**(現行config.json:True)+
  `revenue_growth_min_pct`(現行:0.0)——額外要求月營收年增率(`revenue_yoy_growth`)
  >=這個門檻才准進場，缺資料時當NaN一律「未知不擋」。`long_swing`維持不加(理由：
  20支已知下跌股樣本上這個濾網反而讓它變差，它已經有自己的regime濾網處理這個場景，
  詳見前面「2026-08-16：月營收年增率濾網回測結果」那則)。
- **全觀察清單10年重新驗證(config現行參數，即疊加了regime/MA240/volume_alert_
  scaleout之後再加營收濾網)，5支全部獲利因子提升**：chip_momentum 3.47→4.06、
  trust_momentum 5.37→5.49(總報酬只小輸24%)、golden_cross_scaleout 2.59→2.81、
  atr_breakout 3.29→3.65、breakout 3.00→3.69(總報酬幾乎沒掉，-9%)、long_swing
  3.19→3.73(但這支決定不採用，見上)。全觀察清單數字比只看20支下跌股樣本更有說服力，
  是這次改變推薦範圍的原因(原本只推薦4支，現在5支都採用)。
- **資料管線**：新增`db.attach_monthly_revenue_growth()`(月營收年增率接到bars上，
  已處理FinMind公告日+10天緩衝的look-ahead，2026-08-16稍早那批已經寫好)這次接進所有
  會實際跑`STRATEGY_REGISTRY[name].evaluate()`的production呼叫點：`daily_update.
  add_symbol_to_watchlist`(新股票立刻算排除清單)、`watchlist_view.
  build_strategy_recommendations_for_symbol`(買進/賣出策略訊號表格)、`watchlist_
  view.build_paper_trades_for_symbol`(模擬交易紀錄)、`scripts/recompute_strategy_
  selection.py`、`scripts/backtest.py`、`scripts/run_live.py`的`build_daily_bars_
  with_today`(盤中即時通知，`run_batch.py`因為本來就跳過全部`NOTIFIABLE_STRATEGIES`
  不需要接)、`dashboard/app.py`的`_compute_track_record_for_symbol`(策略歷史勝率
  參考表格)。`build_overview_row_for_symbol`(總覽表格的RSI/MACD/KD等指標)不需要，
  它不呼叫任何策略的`evaluate()`。
- 已重跑`recompute_strategy_selection.py`，17檔股票的排除清單有變化(疊加營收濾網後
  交易頻率進一步下降，跟regime/MA240那次同樣的「樣本掉到門檻以下」副作用，不代表
  這17檔表現突然變差)——後續處理見下一則。

## 2026-08-16：MIN_TRADES_OVERRIDES拿掉，統一改用MIN_TRADES_FOR_RANKING=5

使用者看到8299群聯的`breakout`明明89%勝率、獲利因子52.3、幾乎不虧損，10年只有9筆
(後來因為加了營收濾網掉到6筆)卻被排除，質疑「樣本筆數被壓得太低，十年才六筆有點扯」，
要求乾脆拿掉樣本門檻。

**查證後發現完全拿掉有具體風險**：只看平均報酬/加總報酬/獲利因子三個數值門檻、不管
筆數的話，全觀察清單有3組策略/股票組合**只有1筆交易**就會通過數值門檻(atr_breakout
on 7769鴻勁+66.2%、capitulation_reversal on 6903巨漢+90.3%、trust_momentum on
3552同致+96.0%)——這些是單筆歷史巧合，不是驗證過的優勢，完全拿掉門檻等於讓系統拿
丟硬幣等級的證據當推播依據，這正是當初設樣本門檻要避免的事。另外還有10組是2~5筆的
邊緣案例。

使用者看過這個風險後，選擇**統一降到5筆**(不是完全拿掉)——理由：這是`capitulation_
reversal`當初就驗證過合理的下限(「單日重挫+爆量」是罕見事件，5筆門檻讓22檔裡有10檔
通過、合併勝率62.7%/獲利因子6.67，是目前所有策略生效後數字最好的一支)，比15筆(或
2026-08-16之前累積出來的個別6~10筆override)寬鬆很多，但仍保留最基本的統計防線，
不會讓1~2筆巧合的案例通過。

**已採用**：`strategy_selection.py`的`MIN_TRADES_FOR_RANKING`從15改成5，**拿掉整個
`MIN_TRADES_OVERRIDES`字典**(原本long_swing 8、capitulation_reversal 5、
atr_breakout/chip_momentum 6、trust_momentum/breakout 10、golden_cross_scaleout
8這些per-strategy設定全部刪除，統一用5)。`should_disable()`的簽名也跟著簡化，不再
接受`strategy_name`參數(這個參數存在的唯一目的就是查per-strategy override，拿掉
override後這個參數變成死碼，一併移除，呼叫端`compute_disabled_strategies()`/
`scripts/recompute_strategy_selection.py`都已同步更新)。已重跑`recompute_strategy_
selection.py`，18檔股票的排除清單有變化(多數是原本因樣本不足被排除的策略現在重新
被納入評估，8299的breakout確認已經不再排除)。

**這是這次濾網疊加後重新校準的結果，不是「以後每次濾網一改就無限調降下去」的先例**——
下次如果又疊加新濾網、交易頻率又進一步下降，要重新評估「5筆」這個下限還適不適合，
不是理所當然繼續往下降；如果哪次評估發現5筆已經開始出現不可靠的雜訊案例，也要有
心理準備往上調，不是只會單向放寬。
