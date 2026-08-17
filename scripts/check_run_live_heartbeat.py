"""檢查run_live.py是不是還活著——盤中定期執行(Windows排程TWStocks-CheckRunLiveHeartbeat，
每10分鐘一次)，讀取run_live.py主迴圈每個5分K bucket寫入的心跳時間戳，太久沒更新就發
Telegram警告。獨立成一支輕量腳本、不是run_live.py自己內部檢查自己——run_live.py如果真的
被中止(Ctrl+C)或卡死，沒辦法自己通知自己已經停了，需要一個生命週期完全獨立的外部檢查。

起因：2026-08-17 run_live.py早上10點左右被中止，一直到使用者13:20自己發現「怎麼沒收到
通知」才注意到，中間3小時多完全沒有監控在跑、也沒有人知道。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stocks.config import load_config
from stocks.daily_update import is_market_open_now
from stocks.db import connect, get_setting, set_setting
from stocks.notifier import RUN_LIVE_HEARTBEAT_KEY, RUN_LIVE_STALL_ALERTED_KEY, notify_connectivity

STALE_AFTER_MINUTES = 10  # run_live.py每5分鐘一個bucket，留1個bucket的緩衝才判定停止，避免單次網路延遲誤報


def evaluate_heartbeat(heartbeat_iso: str | None, now: datetime, already_alerted: bool) -> str:
    """回傳"alert_stalled"/"alert_recovered"/"none"三選一。純函式(不碰DB/Telegram)方便測試。

    already_alerted代表「上一次檢查是不是已經因為停止而發過警告了」——避免run_live.py
    真的停很久時，每10分鐘就再轟炸一次同樣的「已停止」訊息；等心跳恢復新鮮，才用
    alert_recovered告知一次「恢復了」，同時解除這個旗標，下次真的又停止才會再警告一次。"""
    if heartbeat_iso is None:
        return "none" if already_alerted else "alert_stalled"

    minutes_since = (now - datetime.fromisoformat(heartbeat_iso)).total_seconds() / 60
    stale = minutes_since > STALE_AFTER_MINUTES

    if stale and not already_alerted:
        return "alert_stalled"
    if not stale and already_alerted:
        return "alert_recovered"
    return "none"


def main():
    config = load_config()
    now = datetime.now()
    if not is_market_open_now(config, now):
        return  # 收盤時段run_live.py本來就不會有新心跳，不用檢查

    with connect(config.db_path) as conn:
        heartbeat_iso = get_setting(conn, RUN_LIVE_HEARTBEAT_KEY)
        already_alerted = get_setting(conn, RUN_LIVE_STALL_ALERTED_KEY) == "1"

    action = evaluate_heartbeat(heartbeat_iso, now, already_alerted)

    if action == "alert_stalled":
        if heartbeat_iso is None:
            detail = "從未收到過心跳，run_live.py可能沒有啟動"
        else:
            minutes_since = (now - datetime.fromisoformat(heartbeat_iso)).total_seconds() / 60
            detail = f"已經{minutes_since:.0f}分鐘沒有心跳，可能已被中止或卡住"
        notify_connectivity(config, "run_live_stalled", detail)
        with connect(config.db_path) as conn:
            set_setting(conn, RUN_LIVE_STALL_ALERTED_KEY, "1")
    elif action == "alert_recovered":
        notify_connectivity(config, "run_live_recovered")
        with connect(config.db_path) as conn:
            set_setting(conn, RUN_LIVE_STALL_ALERTED_KEY, "")


if __name__ == "__main__":
    main()
