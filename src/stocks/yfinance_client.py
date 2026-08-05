"""暫用：Shioaji金鑰到手前，用yfinance抓範例歷史日K資料。之後直接替換成
shioaji_client.fetch_kbars()，其他呼叫端（signal_engine, backtest, dashboard）都不需要改。
"""
import pandas as pd
import yfinance as yf

from stocks.models import Bar


def fetch_symbol_bars(symbol: str, period: str = "1y") -> list[Bar]:
    ticker = f"{symbol}.TW"
    df = yf.download(ticker, period=period, progress=False)
    if df.empty:
        return []

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    bars = []
    for ts, row in df.iterrows():
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
