import pandas as pd
import plotly.graph_objects as go

from stocks.indicators import sma
from stocks.watchlist_view import MA_NAMES


def _ma_label(window: int) -> str:
    return f"{MA_NAMES[window]}線" if window in MA_NAMES else f"{window}日均線"


def candlestick_with_ma(df: pd.DataFrame, ma_windows: list[int] = (5, 10, 20, 60)) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="K線",
            # 台股慣例紅漲綠跌，跟plotly預設的西方配色(綠漲紅跌)相反
            increasing_line_color="red",
            increasing_fillcolor="red",
            decreasing_line_color="green",
            decreasing_fillcolor="green",
        )
    )
    for window in ma_windows:
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=sma(df["close"], window),
                mode="lines",
                name=_ma_label(window),
                line=dict(width=1.5),
            )
        )
    fig.update_layout(
        xaxis_rangeslider_visible=False,
        height=450,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return fig


def intraday_line_chart(
    df: pd.DataFrame, prev_close: float, height: int = 90, market_open: str = "09:00", market_close: str = "13:30"
) -> go.Figure:
    """總覽表格「今日走勢」欄位用的分時走勢圖，仿券商APP的分時圖：價格線以昨收為基準紅漲綠跌
    （用兩條trace各自遮蓋掉基準線另一側的區段，接起來就是隨走勢變色的單一條線），
    下方疊一段淡黃色成交量長條，中間畫一條昨收虛線當參考。來源是bars_5min今天的資料
    （run_live.py沒開的日子這裡不會有資料，呼叫端要自己檢查df是否為空）。

    x軸固定拉滿整個交易時段(09:00-13:30)，不是只框住現有的幾筆資料——盤中資料還很少時
    (例如只有1-2筆)，線才不會被硬撐滿整個小圖寬度。"""
    close = df["close"]
    above = close.where(close >= prev_close)
    below = close.where(close <= prev_close)

    fig = go.Figure()
    fig.add_trace(
        go.Bar(x=df.index, y=df["volume"], yaxis="y2", marker_color="rgba(255,215,0,0.6)", showlegend=False)
    )
    fig.add_trace(go.Scatter(x=df.index, y=above, mode="lines", line=dict(color="red", width=1.5), showlegend=False))
    fig.add_trace(go.Scatter(x=df.index, y=below, mode="lines", line=dict(color="green", width=1.5), showlegend=False))
    fig.add_hline(y=prev_close, line=dict(color="gray", width=1, dash="dot"))

    day = df.index[0].normalize()
    open_h, open_m = (int(x) for x in market_open.split(":"))
    close_h, close_m = (int(x) for x in market_close.split(":"))
    x_range = [day + pd.Timedelta(hours=open_h, minutes=open_m), day + pd.Timedelta(hours=close_h, minutes=close_m)]

    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(visible=False, range=x_range),
        yaxis=dict(visible=False, domain=[0.3, 1]),
        yaxis2=dict(visible=False, domain=[0, 0.25]),
        showlegend=False,
        bargap=0,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def kd_chart(df: pd.DataFrame, height: int = 70, oversold: int = 20, overbought: int = 80) -> go.Figure:
    """觀察清單「KD」欄位用的迷你線圖，df有k/d兩欄(indicators.stochastic_kd()算出來的序列，
    只留最近一段)。KD要看線的走勢跟交叉位置，不是看單一數字，所以畫線圖不是文字。
    畫超買超賣參考線(20/80)方便一眼看黃金/死亡交叉發生在哪個區間。"""
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df.index, y=df["k"], mode="lines", name="K", line=dict(color="orange", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=df["d"], mode="lines", name="D", line=dict(color="dodgerblue", width=1.5)))
    fig.add_hline(y=oversold, line=dict(color="gray", width=1, dash="dot"))
    fig.add_hline(y=overbought, line=dict(color="gray", width=1, dash="dot"))
    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False, range=[0, 100]),
        showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def institutional_flow_chart(df: pd.DataFrame) -> go.Figure:
    """df has columns: date, foreign_net, trust_net, dealer_net (shares/day)."""
    fig = go.Figure()
    for col, label in [("foreign_net", "外資"), ("trust_net", "投信"), ("dealer_net", "自營商")]:
        fig.add_trace(go.Bar(x=df["date"], y=df[col], name=label))
    fig.update_layout(
        barmode="relative",
        height=320,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        yaxis_title="買賣超(股)",
    )
    return fig


def margin_balance_chart(df: pd.DataFrame) -> go.Figure:
    """df has columns: date, margin_balance, short_balance (單位: 張)."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["date"], y=df["margin_balance"], mode="lines", name="融資餘額"))
    fig.add_trace(go.Scatter(x=df["date"], y=df["short_balance"], mode="lines", name="融券餘額", yaxis="y2"))
    fig.update_layout(
        height=320,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        yaxis=dict(title="融資餘額(張)"),
        yaxis2=dict(title="融券餘額(張)", overlaying="y", side="right"),
    )
    return fig
