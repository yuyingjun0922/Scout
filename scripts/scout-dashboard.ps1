# Scout Live Status Dashboard
# 状态响应小猫 + Banner风格
# 双击BAT启动, Ctrl+C退出

$Host.UI.RawUI.WindowTitle = "Scout Live Status"

# UTF-8支持
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ========== Helper Functions ==========

function Get-WatchdogStatus {
    if (-not (Test-Path "C:\Tools\scout-watchdog.log")) {
        return @{ollama="?"; scout="?"; gateway="?"; time="log missing"}
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
    return @{ollama="?"; scout="?"; gateway="?"; time="parse err"}
}

function Invoke-SqliteQuery($db, $query) {
    try {
        if (-not (Test-Path $db)) { return $null }
        $result = sqlite3 $db $query 2>$null
        return $result
    } catch {
        return $null
    }
}

function Get-StatusIcon($value) {
    if ($value -eq "True") { return "🟢" }
    elseif ($value -eq "False") { return "🔴" }
    else { return "⚪" }
}

# ========== Cat Mood Detection ==========

function Get-CatMood() {
    $wd = Get-WatchdogStatus

    if ($wd.ollama -eq "False" -or $wd.scout -eq "False" -or $wd.gateway -eq "False") {
        return "panic"
    }

    $redCount = Invoke-SqliteQuery "D:\13700F\Scout\data\knowledge.db" "SELECT COUNT(*) FROM agent_errors WHERE occurred_at > datetime('now', '-1 hour');"
    if ([int]$redCount -gt 5) {
        return "alert"
    }

    if ([int]$redCount -gt 0) {
        return "watchful"
    }

    return "happy"
}

# ========== Cat ASCII (每种心情3帧动画) ==========

$CatFrames = @{
    happy = @(
@"
    /\_/\
   ( ^.^ )
    > u <
     W W
"@,
@"
    /\_/\
   ( ^o^ )
    > u <
    /U U\
"@,
@"
     /\_/\
    ( -.- )
     > u <
     W W
"@
    )

    watchful = @(
@"
    /\_/\
   ( o.o )
    > . <
     W W
"@,
@"
    /\_/\
   ( O.O )
    > . <
     W W
"@,
@"
    /\_/\
   ( o.o )
    > _ <
     W W
"@
    )

    alert = @(
@"
    /\_/\
   ( O_O )!
    > ! <
     W W
"@,
@"
    /ΞΞΞ\
   ( O_O )!
    > ! <
    /U U\
"@,
@"
    /\_/\
   ( O_O );
    > ! <
     W W
"@
    )

    panic = @(
@"
    /ΞΞΞ\
   (X_X!!)
    >ΞoΞ<
    /U U\
"@,
@"
    /ΞΞΞ\
   ( X@X )!
    >!!!<
    \U U/
"@,
@"
   /ΞΞΞΞ\
  ( X_X!!!)
   >Ξ!oΞ!
    /U U\
"@
    )
}

$CatColors = @{
    happy = "Green"
    watchful = "Yellow"
    alert = "Magenta"
    panic = "Red"
}

$CatMoodText = @{
    happy = "✨ Everything purrs ✨"
    watchful = "👀 Something's up..."
    alert = "⚠️  Pay attention!"
    panic = "🚨 SYSTEM DOWN 🚨"
}

# ========== Main Loop ==========

$frameIdx = 0

while ($true) {
    Clear-Host

    $mood = Get-CatMood
    $frame = $CatFrames[$mood][$frameIdx % 3]
    $color = $CatColors[$mood]
    $moodText = $CatMoodText[$mood]

    $wd = Get-WatchdogStatus

    # Top Banner
    Write-Host ""
    Write-Host "  ╭─── " -ForegroundColor DarkCyan -NoNewline
    Write-Host "🛰️  Scout Live Status" -ForegroundColor Cyan -NoNewline
    Write-Host " · v1.15-suppress " -ForegroundColor DarkGray -NoNewline
    Write-Host ("─" * 30) -ForegroundColor DarkCyan -NoNewline
    Write-Host "╮" -ForegroundColor DarkCyan

    # Cat + Status Split
    $catLines = $frame -split "`r?`n"

    $errs = Invoke-SqliteQuery "D:\13700F\Scout\data\knowledge.db" "SELECT COUNT(*) FROM agent_errors WHERE occurred_at > datetime('now', '-24 hours');"
    $pending = Invoke-SqliteQuery "D:\13700F\Scout\data\queue.db" "SELECT COUNT(*) FROM message_queue WHERE status='pending';"
    $topStock = Invoke-SqliteQuery "D:\13700F\Scout\data\knowledge.db" "SELECT stock FROM recommendations WHERE recommend_level IN ('A','B') GROUP BY stock ORDER BY MAX(total_score) DESC LIMIT 1;"
    if ($topStock) { $topStock = $topStock.Trim("'") } else { $topStock = "(none)" }

    $statusLines = @(
        "",
        "Services",
        "  $(Get-StatusIcon $wd.ollama)  Ollama",
        "  $(Get-StatusIcon $wd.scout)  Scout",
        "  $(Get-StatusIcon $wd.gateway)  Gateway",
        "",
        "Activity",
        "  📊 Errors 24h: $errs",
        "  📬 Pending: $pending",
        "  🏆 Top Pick: $topStock"
    )

    $maxLines = [Math]::Max($catLines.Count, $statusLines.Count)
    for ($i = 0; $i -lt $maxLines; $i++) {
        Write-Host "  │  " -ForegroundColor DarkCyan -NoNewline

        if ($i -lt $catLines.Count) {
            $catLine = $catLines[$i]
            $catPadded = $catLine.PadRight(14)
            Write-Host $catPadded -ForegroundColor $color -NoNewline
        } else {
            Write-Host (" " * 14) -NoNewline
        }

        Write-Host "     │  " -ForegroundColor DarkCyan -NoNewline

        if ($i -lt $statusLines.Count) {
            $statusLine = $statusLines[$i]
            if ($statusLine -match "^(Services|Activity)$") {
                Write-Host $statusLine -ForegroundColor Yellow -NoNewline
            } else {
                Write-Host $statusLine -NoNewline
            }
            $padding = 35 - $statusLine.Length
            if ($padding -gt 0) { Write-Host (" " * $padding) -NoNewline }
        } else {
            Write-Host (" " * 35) -NoNewline
        }

        Write-Host "│" -ForegroundColor DarkCyan
    }

    # Cat Mood Line
    Write-Host "  │  " -ForegroundColor DarkCyan -NoNewline
    $moodPadded = $moodText.PadRight(58)
    Write-Host "  $moodPadded" -ForegroundColor $color -NoNewline
    Write-Host "│" -ForegroundColor DarkCyan

    # Bottom Banner
    Write-Host "  ╰" -ForegroundColor DarkCyan -NoNewline
    Write-Host ("─" * 62) -ForegroundColor DarkCyan -NoNewline
    Write-Host "╯" -ForegroundColor DarkCyan

    # Recent Top 3
    Write-Host ""
    Write-Host "  Top Recommendations:" -ForegroundColor DarkYellow
    $recs = Invoke-SqliteQuery "D:\13700F\Scout\data\knowledge.db" "SELECT stock, recommend_level, total_score FROM recommendations WHERE recommend_level IN ('A','B') GROUP BY stock ORDER BY MAX(total_score) DESC LIMIT 3;"
    if ($recs) {
        $rank = 1
        foreach ($line in $recs) {
            $parts = $line -split '\|'
            if ($parts.Length -ge 3) {
                $stock = $parts[0].Trim("'")
                $level = $parts[1]
                $score = $parts[2]

                $medal = switch ($rank) {
                    1 { "🥇" }
                    2 { "🥈" }
                    3 { "🥉" }
                }

                $levelColor = if ($level -eq "A") { "Red" } else { "Yellow" }

                Write-Host "    $medal  " -NoNewline
                Write-Host "$stock " -NoNewline -ForegroundColor White
                Write-Host "[$level] " -NoNewline -ForegroundColor $levelColor
                Write-Host "$score" -ForegroundColor Cyan
            }
            $rank++
        }
    }

    # Footer
    Write-Host ""
    Write-Host "  " -NoNewline
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray -NoNewline
    Write-Host "  ·  " -ForegroundColor DarkGray -NoNewline
    Write-Host "Watchdog: $($wd.time)" -ForegroundColor DarkGray -NoNewline
    Write-Host "  ·  " -ForegroundColor DarkGray -NoNewline
    Write-Host "Ctrl+C to exit" -ForegroundColor DarkGray
    Write-Host ""

    Start-Sleep -Seconds 1
    $frameIdx++
}
