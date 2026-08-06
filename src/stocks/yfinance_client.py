"""暫用：Shioaji金鑰到手前，用yfinance抓範例歷史日K資料。之後直接替換成
shioaji_client.fetch_kbars()，其他呼叫端（signal_engine, backtest, dashboard）都不需要改。
"""
import pandas as pd
import yfinance as yf

from stocks.models import Bar

SUFFIX_TO_MARKET = {".TW": "TWSE", ".TWO": "TPEx"}


def _bars_from_dataframe(symbol: str, df: pd.DataFrame) -> list[Bar]:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    bars = []
    for ts, row in df.iterrows():
        if pd.isna(row["Open"]) or pd.isna(row["High"]) or pd.isna(row["Low"]) or pd.isna(row["Close"]):
            continue  # 當天還在盤中/尚未收盤，yfinance會回傳缺值OHLC(但Volume可能已經有部分值)，
            # 不是真正的K棒——跟shioaji_client.fetch_daily_quotes()的同一個防護一致。
        bars.append(
            Bar(
                symbol=symbol,
                ts=ts.to_pydatetime(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=int(row["Volume"]),
            )
        )
    return bars


def fetch_symbol_bars(symbol: str, period: str = "1y") -> list[Bar]:
    """上市(.TW)優先，抓不到就當上櫃試 .TWO。不知道市場歸屬時用這個，
    需要同時知道是上市/上櫃的呼叫端請用 detect_market_and_fetch_bars()。"""
    bars, _ = detect_market_and_fetch_bars(symbol, period)
    return bars


def detect_market_and_fetch_bars(symbol: str, period: str = "1y") -> tuple[list[Bar], str]:
    """回傳 (bars, market)，market 是 'TWSE' 或 'TPEx'（都抓不到則market=""）。"""
    for suffix, market in SUFFIX_TO_MARKET.items():
        df = yf.download(f"{symbol}{suffix}", period=period, progress=False)
        if not df.empty:
            return _bars_from_dataframe(symbol, df), market
    return [], ""
