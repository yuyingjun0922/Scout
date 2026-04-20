# Scout Live Status Dashboard
# 双击运行的BAT会调用这个脚本
# Ctrl+C退出

$Host.UI.RawUI.WindowTitle = "Scout Dashboard"

# 检查是否支持Unicode
chcp 65001 | Out-Null

function Get-StatusEmoji($value) {
    if ($value -eq $true -or $value -eq "True") { return "🟢" }
    else { return "🔴" }
}

function Write-Separator {
    Write-Host "═══════════════════════════════════════════════════════════" -ForegroundColor DarkCyan
}

function Write-Box-Top($title) {
    $padding = 60 - $title.Length - 4
    Write-Host ("╔══ " + $title + " " + ("═" * $padding) + "╗") -ForegroundColor Cyan
}

function Write-Box-Bottom {
    Write-Host ("╚" + ("═" * 60) + "╝") -ForegroundColor Cyan
}

function Get-WatchdogStatus {
    if (-not (Test-Path "C:\Tools\scout-watchdog.log")) {
        return @{ollama=$null; scout=$null; gateway=$null; time=$null}
    }
    $last = Get-Content "C:\Tools\scout-watchdog.log" -Tail 1
    if ($last -match "(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*ollama=(\w+).*scout=(\w+).*gateway=(\w+)") {
        return @{
            time = $Matches[1]
            ollama = $Matches[2]
            scout = $Matches[3]
            gateway = $Matches[4]
        }
    }
    return @{ollama=$null; scout=$null; gateway=$null; time=$last}
}

function Invoke-SqliteQuery($db, $query) {
    try {
        $result = sqlite3 $db $query 2>$null
        return $result
    } catch {
        return $null
    }
}

while ($true) {
    Clear-Host

    # Header
    Write-Host ""
    Write-Host "  🛰️  " -NoNewline
    Write-Host "SCOUT " -ForegroundColor Cyan -NoNewline
    Write-Host "LIVE STATUS DASHBOARD" -ForegroundColor White
    Write-Host "  ⏰  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray
    Write-Host "  Press " -NoNewline -ForegroundColor DarkGray
    Write-Host "Ctrl+C" -NoNewline -ForegroundColor Yellow
    Write-Host " to exit. Refresh every 30s." -ForegroundColor DarkGray
    Write-Host ""

    # Box 1: Services
    Write-Box-Top "SERVICES (Watchdog last check)"
    $wd = Get-WatchdogStatus
    if ($wd.ollama -ne $null) {
        Write-Host "  $(Get-StatusEmoji $wd.ollama)  Ollama          " -NoNewline
        Write-Host "Port 11434" -ForegroundColor DarkGray

        Write-Host "  $(Get-StatusEmoji $wd.scout)  Scout           " -NoNewline
        Write-Host "python main.py serve" -ForegroundColor DarkGray

        Write-Host "  $(Get-StatusEmoji $wd.gateway)  Gateway         " -NoNewline
        Write-Host "Port 18789" -ForegroundColor DarkGray

        Write-Host "  ⏱️  Last check: $($wd.time)" -ForegroundColor DarkGray
    } else {
        Write-Host "  ⚠️  Watchdog log missing or unparseable" -ForegroundColor Red
    }
    Write-Box-Bottom
    Write-Host ""

    # Box 2: Recent Errors
    Write-Box-Top "AGENT ERRORS (last 3 in 24h)"
    $errs = Invoke-SqliteQuery "D:\13700F\Scout\data\knowledge.db" "SELECT occurred_at, agent_name, substr(error_message, 1, 55) FROM agent_errors WHERE occurred_at > datetime('now', '-24 hours') ORDER BY occurred_at DESC LIMIT 3;"

    if ($errs) {
        foreach ($line in $errs) {
            $parts = $line -split '\|'
            if ($parts.Length -ge 3) {
                $time = $parts[0].Substring(11, 8)
                $agent = $parts[1]
                $msg = $parts[2]
                Write-Host "  ⚠️  " -NoNewline -ForegroundColor Yellow
                Write-Host "$time " -NoNewline -ForegroundColor DarkGray
                Write-Host "$agent " -NoNewline -ForegroundColor Magenta
                Write-Host "$msg" -ForegroundColor White
            }
        }
    } else {
        Write-Host "  ✨  No errors in last 24h (or DB not reachable)" -ForegroundColor Green
    }
    Write-Box-Bottom
    Write-Host ""

    # Box 3: Top Picks
    Write-Box-Top "TOP RECOMMENDATIONS (latest 5)"
    $recs = Invoke-SqliteQuery "D:\13700F\Scout\data\knowledge.db" "SELECT stock, recommend_level, total_score, substr(recommended_at, 1, 16) FROM recommendations WHERE recommend_level IN ('A','B') GROUP BY stock ORDER BY MAX(total_score) DESC LIMIT 5;"

    if ($recs) {
        $rank = 1
        foreach ($line in $recs) {
            $parts = $line -split '\|'
            if ($parts.Length -ge 4) {
                $stock = $parts[0].Trim("'")
                $level = $parts[1]
                $score = $parts[2]

                $medal = switch ($rank) {
                    1 { "🥇" }
                    2 { "🥈" }
                    3 { "🥉" }
                    default { "  " }
                }

                $levelColor = if ($level -eq "A") { "Red" } else { "Yellow" }

                Write-Host "  $medal  " -NoNewline
                Write-Host "$stock " -NoNewline -ForegroundColor White
                Write-Host "[$level] " -NoNewline -ForegroundColor $levelColor
                Write-Host "$score" -ForegroundColor Cyan
            }
            $rank++
        }
    } else {
        Write-Host "  📭  No recommendations yet" -ForegroundColor DarkGray
    }
    Write-Box-Bottom
    Write-Host ""

    # Box 4: Queue
    Write-Box-Top "MESSAGE QUEUE"
    $pending = Invoke-SqliteQuery "D:\13700F\Scout\data\queue.db" "SELECT COUNT(*) FROM message_queue WHERE status='pending';"
    $done = Invoke-SqliteQuery "D:\13700F\Scout\data\queue.db" "SELECT COUNT(*) FROM message_queue WHERE status='done' AND created_at > datetime('now', '-24 hours');"
    $failed = Invoke-SqliteQuery "D:\13700F\Scout\data\queue.db" "SELECT COUNT(*) FROM message_queue WHERE status='failed';"

    Write-Host "  📬  Pending:   " -NoNewline
    Write-Host "$pending" -ForegroundColor $(if ([int]$pending -gt 10) { "Red" } else { "Yellow" })

    Write-Host "  ✅  Done 24h:  " -NoNewline
    Write-Host "$done" -ForegroundColor Green

    Write-Host "  ❌  Failed:    " -NoNewline
    Write-Host "$failed" -ForegroundColor $(if ([int]$failed -gt 0) { "Red" } else { "DarkGray" })
    Write-Box-Bottom
    Write-Host ""

    # Footer spinner
    $spinner = @('⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏')
    for ($i = 0; $i -lt 30; $i++) {
        Write-Host "`r  $($spinner[$i % 10])  Refreshing in $(30 - $i)s..." -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Seconds 1
    }
}
