from datetime import datetime, timedelta

from check_run_live_heartbeat import evaluate_heartbeat


def test_no_heartbeat_first_time_enters_grace_period_without_alerting():
    # 2026-08-17發現：開機比平常晚時run_live.py還在啟動中，這幾分鐘的空窗不該馬上警告，
    # 第一次發現心跳不新鮮只記錄「從現在開始不新鮮」，等下一輪還是不新鮮才真的警告。
    now = datetime.now()
    action, stale_since = evaluate_heartbeat(None, now, already_alerted=False, stale_since_iso=None)
    assert action == "none"
    assert stale_since == now.isoformat()


def test_no_heartbeat_still_stale_after_grace_period_triggers_stalled_alert():
    now = datetime.now()
    first_noticed = (now - timedelta(minutes=10)).isoformat()
    action, stale_since = evaluate_heartbeat(None, now, already_alerted=False, stale_since_iso=first_noticed)
    assert action == "alert_stalled"


def test_no_heartbeat_still_within_grace_period_does_not_alert_yet():
    now = datetime.now()
    first_noticed = (now - timedelta(minutes=5)).isoformat()
    action, stale_since = evaluate_heartbeat(None, now, already_alerted=False, stale_since_iso=first_noticed)
    assert action == "none"
    assert stale_since == first_noticed  # 緩衝期還沒過，維持原本記錄的時間點不變


def test_delayed_boot_scenario_never_alerts_if_heartbeat_recovers_within_grace_period():
    # 模擬10點才開機：第一次檢查心跳是None，進入緩衝期；run_live.py幾分鐘後啟動完成、
    # 心跳恢復新鮮，下一輪檢查應該直接判定正常，完全不觸發任何警告(不是alert再alert_recovered)。
    now = datetime.now()
    action1, stale_since1 = evaluate_heartbeat(None, now, already_alerted=False, stale_since_iso=None)
    assert action1 == "none"

    later = now + timedelta(minutes=3)
    fresh_heartbeat = (later - timedelta(minutes=1)).isoformat()
    action2, stale_since2 = evaluate_heartbeat(fresh_heartbeat, later, already_alerted=False, stale_since_iso=stale_since1)
    assert action2 == "none"
    assert stale_since2 is None


def test_stale_heartbeat_already_alerted_does_not_repeat_every_check():
    # 已經警告過「停止了」，同一次停止期間(還沒恢復)不要每10分鐘再轟炸一次同樣的訊息。
    now = datetime.now()
    first_noticed = (now - timedelta(minutes=40)).isoformat()
    heartbeat = (now - timedelta(minutes=40)).isoformat()
    action, _ = evaluate_heartbeat(heartbeat, now, already_alerted=True, stale_since_iso=first_noticed)
    assert action == "none"


def test_fresh_heartbeat_after_previously_alerted_triggers_recovered_alert_and_clears_state():
    # 剛才還是停止狀態、現在心跳又新鮮了，代表process重新開始跑了，通知一次「恢復」並
    # 解除旗標，stale_since也要清掉，不然下次真的又停止時緩衝期會用到過期的時間點。
    now = datetime.now()
    heartbeat = (now - timedelta(minutes=2)).isoformat()
    action, stale_since = evaluate_heartbeat(heartbeat, now, already_alerted=True, stale_since_iso="irrelevant")
    assert action == "alert_recovered"
    assert stale_since is None


def test_fresh_heartbeat_and_not_alerted_is_normal_no_op():
    now = datetime.now()
    heartbeat = (now - timedelta(minutes=3)).isoformat()
    action, stale_since = evaluate_heartbeat(heartbeat, now, already_alerted=False, stale_since_iso=None)
    assert action == "none"
    assert stale_since is None


def test_heartbeat_exactly_at_stale_threshold_boundary_is_not_yet_stale():
    now = datetime.now()
    heartbeat = (now - timedelta(minutes=10)).isoformat()
    action, stale_since = evaluate_heartbeat(heartbeat, now, already_alerted=False, stale_since_iso=None)
    assert action == "none"
    assert stale_since is None
