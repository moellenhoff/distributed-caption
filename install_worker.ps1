# install_worker.ps1 — One-time setup on Windows (RTX 4090 / CUDA)
#
# Installs Miniforge3, creates conda env, downloads Molmo weights,
# registers a Windows Task Scheduler job for autostart at login.
#
# Usage (run in PowerShell as Administrator):
#   .\install_worker.ps1 -Coordinator http://10.0.0.1:5000 -WorkerName win-4090
#
# Or with a custom worker dir:
#   .\install_worker.ps1 -Coordinator http://10.0.0.1:5000 -WorkerName win-4090 -WorkerDir C:\caption-worker

param(
    [Parameter(Mandatory=$true)]
    [string]$Coordinator,

    [string]$WorkerName = $env:COMPUTERNAME,

    [string]$WorkerDir = "$env:USERPROFILE\caption-worker",

    [string]$CondaEnv = "caption-worker",

    [string]$TaskName = "CaptionWorker"
)

$ErrorActionPreference = "Stop"

Write-Host "=== Distributed Caption Worker Setup (Windows) ===" -ForegroundColor Cyan
Write-Host "Coordinator : $Coordinator"
Write-Host "Worker name : $WorkerName"
Write-Host "Worker dir  : $WorkerDir"
Write-Host "=================================================="

New-Item -ItemType Directory -Force -Path $WorkerDir | Out-Null
New-Item -ItemType Directory -Force -Path "$WorkerDir\logs" | Out-Null

# ── 1. Miniforge3 ─────────────────────────────────────────────────────────────

$MiniforgePath = "$env:USERPROFILE\miniforge3"
$CondaBin      = "$MiniforgePath\condabin\conda.bat"
$PythonExe     = "$MiniforgePath\envs\$CondaEnv\python.exe"

if (-Not (Test-Path $MiniforgePath)) {
    Write-Host "`nInstalling Miniforge3..." -ForegroundColor Yellow
    $installer = "$env:TEMP\Miniforge3-Windows-x86_64.exe"
    Invoke-WebRequest `
        -Uri "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe" `
        -OutFile $installer
    Start-Process -FilePath $installer `
        -ArgumentList "/S /InstallationType=JustMe /RegisterPython=0 /D=$MiniforgePath" `
        -Wait -NoNewWindow
    Remove-Item $installer
} else {
    Write-Host "Miniforge3 already installed."
}

# ── 2. Conda environment ──────────────────────────────────────────────────────

$envExists = & "$MiniforgePath\Scripts\conda.exe" env list 2>&1 | Select-String "^$CondaEnv "
if (-Not $envExists) {
    Write-Host "`nCreating conda env '$CondaEnv' (Python 3.11)..." -ForegroundColor Yellow
    & "$MiniforgePath\Scripts\conda.exe" create -n $CondaEnv python=3.11 -y
} else {
    Write-Host "Conda env '$CondaEnv' already exists."
}

# ── 3. Python dependencies ────────────────────────────────────────────────────

Write-Host "`nInstalling Python dependencies..." -ForegroundColor Yellow

# PyTorch with CUDA 12.1 (RTX 4090 needs CUDA >= 11.8)
& $PythonExe -m pip install --quiet `
    torch torchvision `
    --index-url https://download.pytorch.org/whl/cu121

# Remaining deps (transformers pinned for Molmo compatibility)
& $PythonExe -m pip install --quiet `
    "transformers>=4.40.0,<4.46.0" `
    accelerate `
    einops `
    requests `
    tqdm `
    pyarrow `
    pillow `
    pystray `
    tensorflow

Write-Host "Dependencies installed."

# ── 4. Copy worker scripts ────────────────────────────────────────────────────

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Copy-Item "$scriptDir\worker.py"   "$WorkerDir\worker.py"   -Force
Copy-Item "$scriptDir\tray_app.py" "$WorkerDir\tray_app.py" -Force
Write-Host "worker.py + tray_app.py copied to $WorkerDir\"

# ── 5. Pre-download Molmo model ───────────────────────────────────────────────

Write-Host "`nPre-downloading Molmo-7B-D-0924 model weights (~14 GB)..." -ForegroundColor Yellow
Write-Host "(Press Ctrl+C to skip — worker will download on first task.)"

$downloadScript = @"
from huggingface_hub import snapshot_download
print('Downloading Molmo-7B-D-0924 ...')
snapshot_download('allenai/Molmo-7B-D-0924')
print('Model files cached.')
"@
$downloadScript | & $PythonExe -

# ── 6. Task Scheduler (autostart at login) ────────────────────────────────────

Write-Host "`nRegistering Windows Task Scheduler job '$TaskName'..." -ForegroundColor Yellow

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$logFile = "$WorkerDir\logs\worker.log"

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "$WorkerDir\worker.py --coordinator $Coordinator --worker-name $WorkerName" `
    -WorkingDirectory $WorkerDir

$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Seconds 30) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

# Start worker immediately
Start-ScheduledTask -TaskName $TaskName

# ── 7. Tray App Task Scheduler (autostart at login) ──────────────────────────

$TrayTaskName = "CaptionWorkerTray"

Write-Host "Registering tray app '$TrayTaskName'..." -ForegroundColor Yellow

Unregister-ScheduledTask -TaskName $TrayTaskName -Confirm:$false -ErrorAction SilentlyContinue

$trayAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "$WorkerDir\tray_app.py --coordinator $Coordinator --worker-name $WorkerName" `
    -WorkingDirectory $WorkerDir

$traySettings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Seconds 10) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName  $TrayTaskName `
    -Action    $trayAction `
    -Trigger   $trigger `
    -Settings  $traySettings `
    -Principal $principal `
    -Force | Out-Null

# Start tray app immediately
Start-ScheduledTask -TaskName $TrayTaskName

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "Worker '$WorkerName' is running. Tray icon appears in the system tray (bottom right)."
Write-Host "Coordinator : $Coordinator"
Write-Host "Log file    : $logFile"
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
Write-Host "  Stop worker  : Stop-ScheduledTask  -TaskName $TaskName"
Write-Host "  Start worker : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Watch log    : Get-Content $logFile -Wait"
Write-Host "  Task status  : Get-ScheduledTask   -TaskName $TaskName | Select-Object State"

