# Scout Live Status Dashboard
# 纯文字版, 30秒刷新, 不闪屏

$Host.UI.RawUI.WindowTitle = "Scout Live Status"

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

while ($true) {
    Clear-Host
    Write-Host "=== Scout Live Status ===" -ForegroundColor Cyan
    Write-Host "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    Write-Host "Press Ctrl+C to exit" -ForegroundColor DarkGray
    Write-Host ""

    Write-Host "--- Watchdog ---" -ForegroundColor Yellow
    if (Test-Path "C:\Tools\scout-watchdog.log") {
        Get-Content "C:\Tools\scout-watchdog.log" -Tail 2
    } else {
        Write-Host "Watchdog log not found" -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "--- Agent Errors (last 3 in 24h) ---" -ForegroundColor Yellow
    try {
        sqlite3 "D:\13700F\Scout\data\knowledge.db" "SELECT occurred_at, agent_name, substr(error_message, 1, 60) FROM agent_errors WHERE occurred_at > datetime('now', '-24 hours') ORDER BY occurred_at DESC LIMIT 3;"
    } catch {
        Write-Host "Cannot read knowledge.db" -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "--- Top Recommendations (5) ---" -ForegroundColor Yellow
    try {
        sqlite3 "D:\13700F\Scout\data\knowledge.db" "SELECT stock, recommend_level, total_score FROM recommendations WHERE recommend_level IN ('A','B') GROUP BY stock ORDER BY MAX(total_score) DESC LIMIT 5;"
    } catch {
        Write-Host "Cannot read recommendations" -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "--- Message Queue ---" -ForegroundColor Yellow
    try {
        $pending = sqlite3 "D:\13700F\Scout\data\queue.db" "SELECT COUNT(*) FROM message_queue WHERE status='pending';"
        $done = sqlite3 "D:\13700F\Scout\data\queue.db" "SELECT COUNT(*) FROM message_queue WHERE status='done' AND created_at > datetime('now', '-24 hours');"
        Write-Host "Pending: $pending"
        Write-Host "Done 24h: $done"
    } catch {
        Write-Host "Cannot read queue.db" -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "Next refresh in 30s..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 30
}
