"""包裝Shioaji SDK，隔離所有shioaji特定的物件/常數，讓其他模組(bar_aggregator,
run_live, run_batch)只看得到我們自己的Bar型別跟純量callback參數。永久用
simulation=True——已查證模擬模式的報價/K棒資料是真實市場資料，只有下單/成交走模擬
帳本，這系統本來就不下單，鎖模擬模式多一層保險。"""
import time
from datetime import datetime

import pandas as pd
import shioaji as sj

from stocks.config import Config
from stocks.models import Bar

RECONNECT_MAX_RETRIES = 5
RECONNECT_BASE_DELAY_SECONDS = 1
RECONNECT_MAX_DELAY_SECONDS = 60


class ShioajiClient:
    def __init__(self, config: Config):
        self.config = config
        self.api: sj.Shioaji | None = None
        self._connected = False

    def connect(self) -> None:
        self.api = sj.Shioaji(simulation=True)
        self.api.login(api_key=self.config.shioaji_api_key, secret_key=self.config.shioaji_secret_key)
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
        呼叫端決定要抓多細的區間。"""
        contract = self.api.Contracts.Stocks[symbol]
        kbars = self.api.kbars(contract=contract, start=start, end=end)
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
        TickSTKv1物件長什麼樣子。"""

        @self.api.on_tick_stk_v1()
        def _callback(tick):
            on_tick(tick.code, tick.datetime, float(tick.close), int(tick.volume))

        for symbol in symbols:
            contract = self.api.Contracts.Stocks[symbol]
            self.api.subscribe(contract, quote_type=sj.constant.QUOTE_TYPE_TICK)
