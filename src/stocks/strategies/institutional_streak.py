import numpy as np
import pandas as pd

from stocks.models import Direction, SignalEvent, Tier

_COLUMN_LABELS = [("foreign_net", "外資"), ("trust_net", "投信")]


class InstitutionalStreakStrategy:
    """外資、投信各自獨立判斷連續買超/賣超天數，天數剛好達到門檻(預設3天)時觸發一次
    （不是持續達標每天都發）。需要bars裡有foreign_net/trust_net欄位
    （由db.attach_institutional_flows() join進來），沒有就直接跳過。
    斷路器：不適用(非NOTIFIABLE_STRATEGIES)。"""

    name = "institutional_streak"

    def evaluate(self, symbol: str, bars: pd.DataFrame, params: dict) -> list[SignalEvent]:
        if "foreign_net" not in bars.columns or "trust_net" not in bars.columns:
            return []

        threshold = params.get("threshold_days", 3)
        events = []

        for column, label in _COLUMN_LABELS:
            sign = np.sign(bars[column].fillna(0))
            group_id = (sign != sign.shift()).cumsum()
            streak = sign.groupby(group_id).cumcount() + 1
            just_reached = streak == threshold

            events += [
                SignalEvent(symbol, self.name, Direction.BUY, bars["close"][t], t, f"{label}連續{threshold}日買超")
                for t in bars.index[just_reached & (sign == 1)]
            ]
            events += [
                SignalEvent(symbol, self.name, Direction.SELL, bars["close"][t], t, f"{label}連續{threshold}日賣超")
                for t in bars.index[just_reached & (sign == -1)]
            ]

        return events
