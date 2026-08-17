from datetime import datetime, timedelta

from check_run_live_heartbeat import evaluate_heartbeat


def test_no_heartbeat_ever_and_not_previously_alerted_triggers_stalled_alert():
    # run_live.py從沒寫過心跳(例如排程任務本身沒有成功啟動)，這是第一次發現，要警告一次。
    assert evaluate_heartbeat(None, datetime.now(), already_alerted=False) == "alert_stalled"


def test_no_heartbeat_ever_but_already_alerted_does_not_repeat():
    # 已經因為「從沒收到心跳」警告過一次，還沒解除前不用每次檢查都再警告一次。
    assert evaluate_heartbeat(None, datetime.now(), already_alerted=True) == "none"


def test_fresh_heartbeat_and_not_alerted_is_normal_no_op():
    now = datetime.now()
    heartbeat = (now - timedelta(minutes=3)).isoformat()
    assert evaluate_heartbeat(heartbeat, now, already_alerted=False) == "none"


def test_stale_heartbeat_and_not_previously_alerted_triggers_stalled_alert():
    # 2026-08-17實際案例：run_live.py被中止後心跳停止更新，超過緩衝時間就代表process
    # 已經不在跑了(不管是被中止還是卡死)，要警告一次。
    now = datetime.now()
    heartbeat = (now - timedelta(minutes=25)).isoformat()
    assert evaluate_heartbeat(heartbeat, now, already_alerted=False) == "alert_stalled"


def test_stale_heartbeat_already_alerted_does_not_repeat_every_check():
    # 已經警告過「停止了」，同一次停止期間(還沒恢復)不要每10分鐘再轟炸一次同樣的訊息。
    now = datetime.now()
    heartbeat = (now - timedelta(minutes=40)).isoformat()
    assert evaluate_heartbeat(heartbeat, now, already_alerted=True) == "none"


def test_fresh_heartbeat_after_previously_alerted_triggers_recovered_alert():
    # 剛才還是停止狀態、現在心跳又新鮮了，代表process重新開始跑了，通知一次「恢復」並解除旗標。
    now = datetime.now()
    heartbeat = (now - timedelta(minutes=2)).isoformat()
    assert evaluate_heartbeat(heartbeat, now, already_alerted=True) == "alert_recovered"


def test_heartbeat_exactly_at_threshold_boundary_is_not_yet_stale():
    now = datetime.now()
    heartbeat = (now - timedelta(minutes=10)).isoformat()
    assert evaluate_heartbeat(heartbeat, now, already_alerted=False) == "none"
