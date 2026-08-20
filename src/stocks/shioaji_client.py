"""包裝Shioaji SDK，隔離所有shioaji特定的物件/常數，讓其他模組(bar_aggregator,
run_live, run_batch)只看得到我們自己的Bar型別跟純量callback參數。永久用
simulation=True——已查證模擬模式的報價/K棒資料是真實市場資料，只有下單/成交走模擬
帳本，這系統本來就不下單，鎖模擬模式多一層保險。"""
import concurrent.futures
import time
from datetime import datetime

import pandas as pd
import shioaji as sj

from stocks.config import Config
from stocks.models import Bar

RECONNECT_MAX_RETRIES = 5
RECONNECT_BASE_DELAY_SECONDS = 1
RECONNECT_MAX_DELAY_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 15  # sj.Shioaji.login()本身沒有timeout參數，2026-08-17發現
# 網路異常時可能無限期卡住不回傳也不拋例外——這個呼叫是dashboard/run_live.py主執行緒
# 同步呼叫的，卡住會讓整個程式看起來完全停住，沒有任何錯誤訊息，重新整理網頁/開新
# session也沒用(新session一樣會卡在同一次連線)。用背景執行緒包一層逾時，逾時就拋
# ConnectionError，讓既有的try/except(ensure_connected的重試迴圈、dashboard的
# except Exception)能正常介入，不會讓整個process卡死。
KBARS_TIMEOUT_SECONDS = 20  # sj.Shioaji.kbars()文件上寫timeout=30000ms有內建逾時，但
# 2026-08-17修完login()逾時後dashboard仍然完全卡死，查證是卡在fetch_today_kbars()逐檔
# 呼叫kbars()這裡——證實跟login()同一種SDK缺陷：文件宣稱的timeout在網路異常時不保證
# 遵守。dashboard的_fetch_today_intraday每30秒盤中就會對整個觀察清單(50+檔)逐檔呼叫
# 一次，只要其中一檔卡住不回應，原本只有except Exception接不到「根本沒有拋例外」的
# 無限期卡住，會讓整個session(進而讓看到同一個process的所有使用者)卡死。同樣用背景
# 執行緒包一層逾時當保險，不能只信任SDK自己回報的timeout數字。


class ShioajiClient:
    def __init__(self, config: Config):
        self.config = config
        self.api: sj.Shioaji | None = None
        self._connected = False

    def connect(self) -> None:
        self.api = sj.Shioaji(simulation=True)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                self.api.login, api_key=self.config.shioaji_api_key, secret_key=self.config.shioaji_secret_key
            )
            future.result(timeout=CONNECT_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            raise ConnectionError(f"Shioaji登入逾時({CONNECT_TIMEOUT_SECONDS}秒未回應)，可能是網路或伺服器問題") from exc
        finally:
            executor.shutdown(wait=False)  # 不等卡住的背景執行緒結束，避免這裡也跟著卡死
        self.api.on_session_down(callback=self._on_session_down)
        self._connected = True

    def _on_session_down(self) -> None:
        self._connected = False

    def disconnect(self) -> None:
        if self.api:
            self.api.logout()
        self._connected = False

    def ensure_connected(
        self,
        max_retries: int = RECONNECT_MAX_RETRIES,
        base_delay: int = RECONNECT_BASE_DELAY_SECONDS,
        max_delay: int = RECONNECT_MAX_DELAY_SECONDS,
    ) -> bool:
        """回傳目前是不是連線中。已連線就直接回True；斷線的話在這一次呼叫裡
        用exponential backoff重試最多max_retries次，全部失敗才回False
        （呼叫端下一輪還會再呼叫一次，等於跨輪也會繼續重試）。"""
        if self._connected:
            return True

        for attempt in range(max_retries):
            delay = min(base_delay * (2**attempt), max_delay)
            time.sleep(delay)
            try:
                self.connect()
                return True
            except Exception:
                continue
        return False

    def fetch_kbars(self, symbol: str, start: str, end: str) -> list[Bar]:
        """start/end格式'YYYY-MM-DD'。回傳日K或分K依Shioaji的kbars()本身行為，
        呼叫端決定要抓多細的區間。用背景執行緒包逾時，見KBARS_TIMEOUT_SECONDS說明——
        不能只靠kbars()自己宣稱的timeout參數。"""
        contract = self.api.Contracts.Stocks[symbol]
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(self.api.kbars, contract=contract, start=start, end=end)
            kbars = future.result(timeout=KBARS_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            raise ConnectionError(f"{symbol} kbars逾時({KBARS_TIMEOUT_SECONDS}秒未回應)，可能是網路或伺服器問題") from exc
        finally:
            executor.shutdown(wait=False)  # 不等卡住的背景執行緒結束，避免這裡也跟著卡死
        df = pd.DataFrame({**kbars})
        if df.empty:
            return []

        bars = []
        for ts, row in zip(pd.to_datetime(df["ts"]), df.itertuples(index=False)):
            bars.append(
                Bar(
                    symbol=symbol,
                    ts=ts.to_pydatetime(),
                    open=float(row.Open),
                    high=float(row.High),
                    low=float(row.Low),
                    close=float(row.Close),
                    volume=int(row.Volume),
                )
            )
        return bars

    def fetch_daily_quotes(self, quote_date) -> list[Bar]:
        """全市場一次呼叫拿當天所有股票的日OHLCV(~2000檔)，供run_batch.py用，
        不用像上市/上櫃籌碼資料那樣逐檔呼叫。quote_date是datetime.date。"""
        quotes = self.api.daily_quotes(date=quote_date)
        df = pd.DataFrame({**quotes})
        if df.empty:
            return []

        ts = datetime.combine(quote_date, datetime.min.time())
        bars = []
        for row in df.itertuples(index=False):
            if pd.isna(row.Open) or pd.isna(row.High) or pd.isna(row.Low) or pd.isna(row.Close):
                continue  # 當天沒有實際成交(例如全天暫停交易)，Shioaji回傳缺值OHLC，不是真正的K棒
            bars.append(
                Bar(
                    symbol=row.Code,
                    ts=ts,
                    open=float(row.Open),
                    high=float(row.High),
                    low=float(row.Low),
                    close=float(row.Close),
                    volume=int(row.Volume),
                )
            )
        return bars

    def fetch_today_kbars(self, symbols: list[str]) -> dict[str, list[Bar]]:
        """給dashboard「今日走勢」小圖用：現場抓觀察清單這幾檔股票當天的分K，不用等
        run_live.py整天掛著累積。symbols是觀察清單(個位數)，不是全市場，每次dashboard
        載入現場抓可以接受。單一symbol抓失敗(例如代號打錯)跳過，不影響其他symbol。"""
        today = datetime.now().strftime("%Y-%m-%d")
        result = {}
        for symbol in symbols:
            try:
                bars = self.fetch_kbars(symbol, start=today, end=today)
            except Exception:
                continue
            if bars:
                result[symbol] = bars
        return result

    def subscribe_ticks(self, symbols: list[str], on_tick) -> None:
        """on_tick(symbol: str, ts: datetime, price: float, volume: int) -- 呼叫端
        (bar_aggregator.BarAggregator.on_tick就是這個形狀)不需要知道shioaji的
        TickSTKv1物件長什麼樣子。

        單一symbol訂閱失敗(例如Shioaji合約資料還沒載入完成、或這支股票當天暫停交易)
        跳過繼續訂閱下一檔——2026-08-20發現：原本任何一檔查不到合約就會讓KeyError直接
        往main()外拋，run_live.py整支process在啟動階段就crash，導致當天29檔股票的
        即時監控全部停擺，只因為其中一檔訂閱失敗。跟fetch_today_kbars()同一種容錯
        慣例(單一symbol失敗不影響其他symbol)。"""

        @self.api.on_tick_stk_v1()
        def _callback(tick):
            on_tick(tick.code, tick.datetime, float(tick.close), int(tick.volume))

        for symbol in symbols:
            try:
                contract = self.api.Contracts.Stocks[symbol]
                self.api.subscribe(contract, quote_type=sj.constant.QUOTE_TYPE_TICK)
            except Exception as exc:
                print(f"訂閱{symbol}失敗，跳過這一檔：{exc}")
