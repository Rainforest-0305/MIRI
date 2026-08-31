# ============================================================================
#  MIRI TOSS bridge - Windows Task Scheduler registration (ASCII only)
#
#  *** THIS SCRIPT DOES NOTHING BY DEFAULT. ***
#  Default = print the exact schtasks commands (dry run). Registration is a
#  Partner gate: it changes the machine's scheduled workload. Pass -Execute
#  ONLY after Partner approval.
#
#  Usage
#    powershell -ExecutionPolicy Bypass -File register_toss_bridge_task.ps1
#    powershell -ExecutionPolicy Bypass -File register_toss_bridge_task.ps1 -Execute
#    ... -BridgeDir "C:\Users\urimk\miri-bridge" -PythonExe "C:\Program Files\Python312\python.exe"
#    ... -Remove            (unregister the three tasks)
#
#  Tasks created (run as SYSTEM = session 0 = NO visible window, ever):
#    MIRI_TossBridge_Prices     every 10 min, 08:50 - 16:20 (daily)
#    MIRI_TossBridge_Sched_AM   daily 07:40
#    MIRI_TossBridge_Sched_PM   daily 18:40
#
#  Why these periods
#    prices    : KRX regular session is 09:00-15:30 KST. 10 min keeps the
#                snapshot age well under the ranking cache TTL, so the app
#                shows a near-live change_pct. ~45 runs/day x ~35 codes
#                ~= 1,600 candle GETs/day (TOSS limit is 5 req/s; the
#                collector spaces calls 0.22s apart).
#                It runs on weekends too - on purpose. Weekend runs re-read
#                Friday's close (correct value) and keep the snapshot young,
#                so the staleness alarm does not cry every Monday morning.
#    scheduled : IPO/ETF listing dates change at day resolution. Twice a day
#                (before open, after close) is plenty. 3 stocks/all calls
#                + 1 stocks call per run - negligible.
# ============================================================================
param(
  [string]$BridgeDir = "C:\Users\urimk\miri-bridge",
  [string]$PythonExe = "C:\Program Files\Python312\python.exe",
  [switch]$Execute,
  [switch]$Remove
)

$ErrorActionPreference = "Stop"
$wrapper = Join-Path $BridgeDir "toss_bridge_run.cmd"

$tasks = @(
  @{ Name = "MIRI_TossBridge_Prices";   Args = "prices";
     Sched = @("/sc","MINUTE","/mo","10","/st","08:50","/et","16:20","/k") },
  @{ Name = "MIRI_TossBridge_Sched_AM"; Args = "scheduled";
     Sched = @("/sc","DAILY","/st","07:40") },
  @{ Name = "MIRI_TossBridge_Sched_PM"; Args = "scheduled";
     Sched = @("/sc","DAILY","/st","18:40") }
)

Write-Host "BridgeDir : $BridgeDir"
Write-Host "PythonExe : $PythonExe"
Write-Host "Wrapper   : $wrapper"
Write-Host ""

if (-not $Remove) {
  foreach ($p in @($BridgeDir, $wrapper, (Join-Path $BridgeDir "toss_bridge.py"), (Join-Path $BridgeDir ".env"))) {
    if (-not (Test-Path $p)) { Write-Host "MISSING: $p" -ForegroundColor Yellow }
  }
  if (-not (Test-Path $PythonExe)) { Write-Host "MISSING: $PythonExe" -ForegroundColor Yellow }
  Write-Host ""
}

# Build argument ARRAYS, not a single string. PowerShell then hands each argument
# to schtasks.exe verbatim - no nested-quote guessing through cmd /c, which is
# where "/tr" registrations usually break silently (task registers, then the
# action is garbage and every run fails with 0x1).
# The wrapper is a .cmd, which Task Scheduler can launch directly (no cmd /c).
$argSets = @()
foreach ($t in $tasks) {
  if ($Remove) {
    $argSets += ,@("/delete", "/tn", $t.Name, "/f")
  } else {
    $tr = "`"$wrapper`" $($t.Args)"
    $argSets += ,(@("/create", "/tn", $t.Name, "/tr", $tr) + $t.Sched +
                  @("/ru", "SYSTEM", "/rl", "HIGHEST", "/f"))
  }
}

function Show-Args($a) {
  ($a | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
}

if (-not $Execute) {
  Write-Host "=== DRY RUN (nothing was changed). Commands that WOULD run: ===" -ForegroundColor Cyan
  $argSets | ForEach-Object { Write-Host "  schtasks $(Show-Args $_)" }
  Write-Host ""
  Write-Host "Re-run with -Execute to apply (Partner gate)." -ForegroundColor Cyan
  exit 0
}

Write-Host "=== EXECUTING ===" -ForegroundColor Green
foreach ($a in $argSets) {
  Write-Host "  schtasks $(Show-Args $a)"
  & schtasks.exe @a
  if ($LASTEXITCODE -ne 0) { Write-Host "  -> FAILED ($LASTEXITCODE)" -ForegroundColor Red }
}
Write-Host ""
Write-Host "Verify: schtasks /query /tn MIRI_TossBridge_Prices /v /fo LIST | findstr /i `"Status Last Next`""
