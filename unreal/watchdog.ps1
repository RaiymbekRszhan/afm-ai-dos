# watchdog.ps1 — сторож рендер-ноды Ai-dos (Windows, Unreal Editor 24/7).
#
# Что делает (проверка раз в $CheckSec секунд):
#   1. Редактор не запущен            -> запускает UnrealEditor с проектом.
#   2. Редактор завис / скрипт умер   -> видит по heartbeat бэкенда
#      (GET /health -> node.last_poll_ago_sec: нода в watch() опрашивает бэкенд
#      каждые <=30 с; тишина дольше $StaleSec = зависание) -> убивает и запускает.
#   3. Ночной профилактический рестарт в $NightlyHour ч. — редактор за сутки
#      копит память и ассеты ответов (секвенции не удаляются из-за модалки UE).
#   4. Чистит WAV-ответы старше $WavKeepDays дней в <проект>\Saved\Aidos.
#
# После рестарта редактор сам включает авторежим: в Content\Python лежит
# init_unreal.py (см. unreal/README.md, раздел «Работа 24/7»).
#
# Установка (один раз, PowerShell от администратора):
#   schtasks /Create /TN "AidosWatchdog" /SC ONLOGON /RL HIGHEST /F ^
#     /TR "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Aidos\watchdog.ps1"
# и включить автологон Windows (netplwiz), чтобы задача стартовала после ребута.

# ---------------- конфиг (поправить под ноду) ----------------
$Backend      = "http://192.168.23.120:8000"   # оркестратор (IP Мака/сервера)
$Token        = ""                              # X-Aidos-Token, если включён LAST_ANSWER_TOKEN
$UProject     = "C:\Aidos\AidosAvatar\AidosAvatar.uproject"
$EditorExe    = "C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\Win64\UnrealEditor.exe"
# -noepicportal и выключенный AutoSave — от фризов (см. README); лишние флаги не мешают.
$EditorArgs   = "`"$UProject`" -noepicportal"
$CheckSec     = 60      # период проверки
$StaleSec     = 180     # нет heartbeat дольше -> считаем редактор зависшим
$GraceSec     = 600     # после запуска редактора не трогаем его столько секунд
                        # (загрузка проекта + задержка init_unreal)
$NightlyHour  = 4       # час ночного рестарта (локальное время ноды)
$WavKeepDays  = 7       # сколько дней хранить WAV-ответы на диске
$LogFile      = Join-Path $PSScriptRoot "watchdog.log"

# ---------------- служебное ----------------
$SaveDir = Join-Path (Split-Path $UProject) "Saved\Aidos"
$script:lastStart   = Get-Date "2000-01-01"
$script:lastNightly = (Get-Date).Date  # сегодняшний ночной рестарт считаем сделанным

function Log($msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    # не даём логу расти бесконечно
    if ((Get-Item $LogFile -ErrorAction SilentlyContinue).Length -gt 5MB) {
        Get-Content $LogFile -Tail 2000 | Set-Content $LogFile -Encoding UTF8
    }
}

function Get-Editor { Get-Process "UnrealEditor" -ErrorAction SilentlyContinue }

function Start-Editor {
    Log "запускаю редактор: $EditorExe $EditorArgs"
    Start-Process -FilePath $EditorExe -ArgumentList $EditorArgs
    $script:lastStart = Get-Date
}

function Stop-Editor($reason) {
    Log "останавливаю редактор ($reason)"
    Get-Editor | Stop-Process -Force -ErrorAction SilentlyContinue
    # CrashReportClient, застрявший после падения, блокировал бы следующий цикл
    Get-Process "CrashReportClient" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 10
}

function Get-NodeHeartbeat {
    # Возвращает last_poll_ago_sec с бэкенда, $null = "нода ещё не приходила",
    # строку "backend_down" — бэкенд недоступен (нода не виновата, не рестартим).
    try {
        $headers = @{}
        if ($Token) { $headers["X-Aidos-Token"] = $Token }
        $h = Invoke-RestMethod -Uri "$Backend/health" -TimeoutSec 15 -Headers $headers
        return $h.node.last_poll_ago_sec
    } catch {
        return "backend_down"
    }
}

function Prune-Wavs {
    if (-not (Test-Path $SaveDir)) { return }
    $old = Get-ChildItem $SaveDir -Filter "*.wav" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$WavKeepDays) }
    if ($old) {
        $old | Remove-Item -Force -ErrorAction SilentlyContinue
        Log ("удалил старых WAV: {0}" -f $old.Count)
    }
}

# ---------------- главный цикл ----------------
Log "watchdog запущен (backend=$Backend, stale=$StaleSec c, ночной рестарт в $NightlyHour ч.)"
while ($true) {
    try {
        $now = Get-Date

        # ночной профилактический рестарт + чистка диска
        if ($now.Hour -eq $NightlyHour -and $script:lastNightly -lt $now.Date) {
            $script:lastNightly = $now.Date
            Stop-Editor "ночной профилактический рестарт"
            Prune-Wavs
            Start-Editor
        }
        elseif (-not (Get-Editor)) {
            Log "редактор не запущен (упал?)"
            Start-Editor
        }
        elseif (($now - $script:lastStart).TotalSeconds -gt $GraceSec) {
            $ago = Get-NodeHeartbeat
            if ("$ago" -eq "backend_down") {
                # бэкенд лежит — редактор ни при чём, ждём
            }
            elseif ($null -eq $ago -or [double]$ago -gt $StaleSec) {
                Stop-Editor "нода молчит (last_poll_ago_sec=$ago > $StaleSec)"
                Start-Editor
            }
        }
    } catch {
        Log "ошибка цикла: $_"
    }
    Start-Sleep -Seconds $CheckSec
}
