import pandas as pd
import plotly.graph_objects as go

from stocks.indicators import sma


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
        )
    )
    for window in ma_windows:
        fig.add_trace(
            go.Scatter(
                x=df.index,
                y=sma(df["close"], window),
                mode="lines",
                name=f"{window}日均線",
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
