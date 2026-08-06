# 註冊「台股訊號監控」需要的兩個Windows工作排程器任務。只需要跑一次（或電腦重灌/搬機器時再跑一次）。
# 前提：電腦本身要開機、使用者要登入(沒有用「使用者是否登入都執行」，避免要存密碼)。
$projectRoot = Split-Path -Parent $PSScriptRoot

$action1 = New-ScheduledTaskAction -Execute (Join-Path $projectRoot "run_live.bat") -WorkingDirectory $projectRoot
$trigger1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "08:55"
$settings1 = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName "TWStocks-RunLive" -Action $action1 -Trigger $trigger1 -Settings $settings1 `
    -Description "台股訊號監控：盤中即時5分K迴圈(09:00-13:30)，08:55先啟動留緩衝" -Force

$action2 = New-ScheduledTaskAction -Execute (Join-Path $projectRoot "run_batch.bat") -WorkingDirectory $projectRoot
$trigger2 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "14:00"
$settings2 = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "TWStocks-RunBatch" -Action $action2 -Trigger $trigger2 -Settings $settings2 `
    -Description "台股訊號監控：收盤後全市場批次掃描+Telegram通知" -Force

Get-ScheduledTask -TaskName "TWStocks-*" | Select-Object TaskName, State
