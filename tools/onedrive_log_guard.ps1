<#
  onedrive_log_guard.ps1 — stops OneDrive's diagnostic logs from filling C: again.

  BACKGROUND: OneDrive writes .odl diagnostic logs to
      %LOCALAPPDATA%\Microsoft\OneDrive\logs\ListSync
  and has a long-standing bug where it never prunes them. On this PC they grew to
  1,005,327 files / 981 GB between 2026-01-12 and 2026-08-06, filling the C: drive
  and breaking JForex, Chrome and Opera (all failed to write their caches).

  These files are pure diagnostics. They contain no user data and OneDrive recreates
  whatever it needs. Deleting them is safe.

  This script deletes any ListSync log older than $KeepDays and reports what it freed.
  Register it as a daily scheduled task with -Install.

  Usage:
      powershell -ExecutionPolicy Bypass -File onedrive_log_guard.ps1            # clean now
      powershell -ExecutionPolicy Bypass -File onedrive_log_guard.ps1 -Install   # + daily task
#>
param(
    [int]$KeepDays = 2,
    [switch]$Install
)

$ErrorActionPreference = "SilentlyContinue"
$LogRoot = Join-Path $env:LOCALAPPDATA "Microsoft\OneDrive\logs"
$TaskName = "OneDrive log guard"

function Get-FolderGB($path) {
    if (-not (Test-Path $path)) { return 0 }
    $s = (Get-ChildItem $path -Recurse -File -Force | Measure-Object Length -Sum).Sum
    if ($null -eq $s) { return 0 }
    return [math]::Round($s / 1GB, 2)
}

if ($Install) {
    $me = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$me`" -KeepDays $KeepDays"
    $trigger = New-ScheduledTaskTrigger -Daily -At 9am
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Deletes OneDrive .odl diagnostic logs older than $KeepDays days (they are not user data)." -Force | Out-Null
    Write-Output "Installed daily scheduled task: '$TaskName' (09:00, keeps $KeepDays days)"
}

if (-not (Test-Path $LogRoot)) {
    Write-Output "OneDrive log folder not present - nothing to do."
    return
}

$before = Get-FolderGB $LogRoot
$cutoff = (Get-Date).AddDays(-$KeepDays)

$old = Get-ChildItem $LogRoot -Recurse -File -Force |
       Where-Object { $_.Extension -in ".odl", ".odlgz", ".aodl" -and $_.LastWriteTime -lt $cutoff }

$count = 0
foreach ($f in $old) {
    Remove-Item $f.FullName -Force
    $count++
}

# drop any directories left empty
Get-ChildItem $LogRoot -Recurse -Directory -Force | Sort-Object FullName -Descending | ForEach-Object {
    if (-not (Get-ChildItem $_.FullName -Force)) { Remove-Item $_.FullName -Force }
}

$after = Get-FolderGB $LogRoot
Write-Output ("{0}  OneDrive logs: {1} GB -> {2} GB  (deleted {3} files older than {4}d)" -f `
    (Get-Date -Format "yyyy-MM-dd HH:mm"), $before, $after, $count, $KeepDays)
