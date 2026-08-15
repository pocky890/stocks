import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from stocks.indicators import sma
from stocks.watchlist_view import MA_NAMES


def _ma_label(window: int) -> str:
    return f"{MA_NAMES[window]}線" if window in MA_NAMES else f"{window}日均線"


def price_and_chip_chart(
    bars: pd.DataFrame,
    flow_df: pd.DataFrame | None = None,
    margin_df: pd.DataFrame | None = None,
    ma_windows: list[int] = (5, 10, 20, 60),
) -> go.Figure:
    """K線圖+均線，下面疊三大法人買賣超、融資融券餘額——2026-08-15使用者要求把籌碼面
    資料搬到K線圖下方一起比對，不要分開頁籤來回切換。用make_subplots(shared_xaxes=True)
    疊成一張圖而不是三張獨立的圖，這樣拖曳/縮放任一段時間軸，其他段會跟著對齊，才能
    直接對照「這段價格走勢的當下，籌碼在做什麼」。flow_df/margin_df是可選的(可能沒資料)，
    沒給或是空的就不畫那一段，只留K線圖。

    x軸預設只框住最近3個月，不是拉滿整段歷史——資料本身還是完整的(季線等長週期均線才算得準)，
    使用者可以直接在圖上拖曳/縮放看更長的區間。K線那一段的y軸價位範圍也跟著這個可視窗口算，
    不是套用plotly對「整段歷史資料」的預設autorange(那樣會被10年前的低價股價拉爆比例，
    近3個月的K棒擠成一條線)——只用可視窗口內的高低點(K棒本身+均線，均線可能比K棒更極端)
    抓範圍，上下各留8%當緩衝。"""
    has_flow = flow_df is not None and not flow_df.empty
    has_margin = margin_df is not None and not margin_df.empty

    row_specs = [{"secondary_y": False}]  # row 1: K線+均線
    row_heights = [0.5]
    if has_flow:
        row_specs.append({"secondary_y": False})
        row_heights.append(0.25)
    if has_margin:
        row_specs.append({"secondary_y": True})  # 融資/融券左右各一個y軸
        row_heights.append(0.25)
    rows = len(row_specs)
    # 只有K線圖一段時row_heights=[0.5]會被plotly正規化成整段都是K線，效果等同[1.0]，不用特判

    fig = make_subplots(
        rows=rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
        specs=[[spec] for spec in row_specs],
    )

    fig.add_trace(
        go.Candlestick(
            x=bars.index,
            open=bars["open"],
            high=bars["high"],
            low=bars["low"],
            close=bars["close"],
            name="K線",
            # 台股慣例紅漲綠跌，跟plotly預設的西方配色(綠漲紅跌)相反
            increasing_line_color="red",
            increasing_fillcolor="red",
            decreasing_line_color="green",
            decreasing_fillcolor="green",
        ),
        row=1,
        col=1,
    )
    for window in ma_windows:
        fig.add_trace(
            go.Scatter(x=bars.index, y=sma(bars["close"], window), mode="lines", name=_ma_label(window), line=dict(width=1.5)),
            row=1,
            col=1,
        )

    flow_row = margin_row = None
    next_row = 2
    if has_flow:
        for col, label in [("foreign_net", "外資"), ("trust_net", "投信"), ("dealer_net", "自營商")]:
            fig.add_trace(go.Bar(x=flow_df["date"], y=flow_df[col], name=label), row=next_row, col=1)
        fig.update_yaxes(title_text="買賣超(股)", row=next_row, col=1)
        flow_row = next_row
        next_row += 1
    if has_margin:
        fig.add_trace(go.Scatter(x=margin_df["date"], y=margin_df["margin_balance"], mode="lines", name="融資餘額"), row=next_row, col=1)
        fig.add_trace(
            go.Scatter(x=margin_df["date"], y=margin_df["short_balance"], mode="lines", name="融券餘額"),
            row=next_row,
            col=1,
            secondary_y=True,
        )
        fig.update_yaxes(title_text="融資餘額(張)", row=next_row, col=1, secondary_y=False)
        fig.update_yaxes(title_text="融券餘額(張)", row=next_row, col=1, secondary_y=True)
        margin_row = next_row

    fig.update_layout(
        xaxis_rangeslider_visible=False,
        # barmode="relative"：外資/投信/自營商三根bar疊在同一個x位置(正的疊上面、負的疊
        # 下面)，不是plotly預設的"group"(會把三根bar在同一天並排開，跟K棒對不上、看起來
        # 一天有三根柱子)——2026-08-15使用者發現這個問題，這是原本institutional_flow_chart
        # 就有設的參數，合併成一張圖時漏加了。
        barmode="relative",
        height=300 + 220 * (rows - 1),
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )

    if not bars.empty:
        last_date = bars.index.max()
        x_start = last_date - pd.DateOffset(months=3)
        fig.update_xaxes(range=[x_start, last_date])  # shared_xaxes讓這個範圍套用到全部子圖

        visible = bars.loc[bars.index >= x_start]
        if not visible.empty:
            y_low = visible["low"].min()
            y_high = visible["high"].max()
            for window in ma_windows:
                visible_ma = sma(bars["close"], window).loc[bars.index >= x_start].dropna()
                if not visible_ma.empty:
                    y_low = min(y_low, visible_ma.min())
                    y_high = max(y_high, visible_ma.max())
            pad = (y_high - y_low) * 0.08
            fig.update_yaxes(range=[max(0, y_low - pad), y_high + pad], row=1, col=1)

        # 籌碼/融資融券的y軸範圍也比照K線那段，只用可視窗口內的資料抓高低點——不然這兩段
        # 會套用plotly對「整段10年歷史」的預設autorange，近3個月的波動會被十年份的極端值
        # 壓成一條線，看不出細節。
        if has_flow:
            flow_dates = pd.to_datetime(flow_df["date"])
            visible_flow = flow_df.loc[flow_dates >= x_start, ["foreign_net", "trust_net", "dealer_net"]]
            if not visible_flow.empty:
                y_high = visible_flow.clip(lower=0).sum(axis=1).max()
                y_low = visible_flow.clip(upper=0).sum(axis=1).min()
                if y_high > y_low:
                    pad = (y_high - y_low) * 0.08
                    fig.update_yaxes(range=[y_low - pad, y_high + pad], row=flow_row, col=1)
        if has_margin:
            margin_dates = pd.to_datetime(margin_df["date"])
            visible_margin = margin_df.loc[margin_dates >= x_start]
            if not visible_margin.empty:
                for value_col, secondary in [("margin_balance", False), ("short_balance", True)]:
                    series = visible_margin[value_col]
                    y_low, y_high = series.min(), series.max()
                    if y_high > y_low:
                        pad = (y_high - y_low) * 0.08
                        fig.update_yaxes(
                            range=[max(0, y_low - pad), y_high + pad], row=margin_row, col=1, secondary_y=secondary
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


