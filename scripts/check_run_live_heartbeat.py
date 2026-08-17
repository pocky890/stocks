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
from stocks.notifier import RUN_LIVE_HEARTBEAT_KEY, RUN_LIVE_STALE_SINCE_KEY, RUN_LIVE_STALL_ALERTED_KEY, notify_connectivity

STALE_AFTER_MINUTES = 10  # run_live.py每5分鐘一個bucket，留1個bucket的緩衝才判定停止，避免單次網路延遲誤報
GRACE_PERIOD_MINUTES = 10  # 見evaluate_heartbeat說明：第一次發現停止不馬上警告，等下一輪還是停止才真的警告


def evaluate_heartbeat(
    heartbeat_iso: str | None, now: datetime, already_alerted: bool, stale_since_iso: str | None
) -> tuple[str, str | None]:
    """回傳(action, new_stale_since_iso)。action是"alert_stalled"/"alert_recovered"/"none"
    三選一，new_stale_since_iso是main()下一輪要寫回DB的狀態(None代表清除)。純函式(不碰
    DB/Telegram)方便測試。

    2026-08-17發現：如果一發現心跳不新鮮就馬上警告，開機比平常晚(例如10點才開機)時會
    誤報——run_live.py本身還在啟動中(載入設定/連線Shioaji/訂閱報價需要一點時間)，這幾
    分鐘的空窗會被誤判成「已經停止」，馬上發一次「心跳中斷」，等run_live.py真正啟動完成
    寫下第一筆心跳，下一次檢查又馬上發一次「心跳恢復」——兩則訊息都是假警報。改成加一段
    緩衝期：第一次發現心跳不新鮮時只記錄「從這時候開始不新鮮」，不馬上警告；只有再過一輪
    檢查(約10分鐘後)還是不新鮮，才真的發警告。開機延遲的空窗通常一兩分鐘就會被run_live.py
    自己補上，撐不過一輪緩衝期就會恢復新鮮，不會誤報；真正停止的話會連續兩輪都不新鮮，
    一樣能在10~20分鐘內抓到，比起沒有這層緩衝也只是多等一輪，跟今天3小時多才發現比起來
    差距很小。

    already_alerted代表「上一次檢查是不是已經因為停止而發過警告了」——避免run_live.py
    真的停很久時，每10分鐘就再轟炸一次同樣的「已停止」訊息；等心跳恢復新鮮，才用
    alert_recovered告知一次「恢復了」，同時解除這個旗標，下次真的又停止才會再警告一次。"""
    if heartbeat_iso is None:
        stale = True
        minutes_since = None
    else:
        minutes_since = (now - datetime.fromisoformat(heartbeat_iso)).total_seconds() / 60
        stale = minutes_since > STALE_AFTER_MINUTES

    if not stale:
        if already_alerted:
            return "alert_recovered", None
        return "none", None

    if stale_since_iso is None:
        return "none", now.isoformat()  # 第一次發現不新鮮，進入緩衝期，先不警告

    grace_elapsed_minutes = (now - datetime.fromisoformat(stale_since_iso)).total_seconds() / 60
    if grace_elapsed_minutes >= GRACE_PERIOD_MINUTES and not already_alerted:
        return "alert_stalled", stale_since_iso
    return "none", stale_since_iso


def main():
    config = load_config()
    now = datetime.now()
    if not is_market_open_now(config, now):
        return  # 收盤時段run_live.py本來就不會有新心跳，不用檢查

    with connect(config.db_path) as conn:
        heartbeat_iso = get_setting(conn, RUN_LIVE_HEARTBEAT_KEY)
        already_alerted = get_setting(conn, RUN_LIVE_STALL_ALERTED_KEY) == "1"
        stale_since_iso = get_setting(conn, RUN_LIVE_STALE_SINCE_KEY) or None  # 空字串(已清除)當None處理

    action, new_stale_since_iso = evaluate_heartbeat(heartbeat_iso, now, already_alerted, stale_since_iso)

    with connect(config.db_path) as conn:
        set_setting(conn, RUN_LIVE_STALE_SINCE_KEY, new_stale_since_iso or "")

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
