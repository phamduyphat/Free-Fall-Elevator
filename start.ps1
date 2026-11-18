<#
.SYNOPSIS
    Startup script for SREMS (Free-Fall-Elevator) subsystems.

.DESCRIPTION
    Launches and coordinates all SREMS subsystems:
    1. Checks PostgreSQL availability (falls back gracefully to memory mode if offline).
    2. Starts the FastAPI backend (SimulationClock, ACS, Analytics, Operational stub).
    3. Verifies backend health check endpoint (/api/v1/health).
    4. Starts the Vite/React frontend dashboard.
    5. Opens the browser to the SREMS web UI.

.PARAMETER Mode
    Execution mode: "Window" (default, launches dedicated titled terminal windows)
    or "Background" (monitors jobs in current terminal until Ctrl+C is pressed).

.PARAMETER SkipFrontend
    Starts only the FastAPI backend subsystem.

.PARAMETER SkipBackend
    Starts only the React frontend subsystem.

.PARAMETER Port
    Port for the FastAPI backend (default: 8000).

.PARAMETER FrontendPort
    Port for the Vite development server (default: 5173).

.PARAMETER HostAddress
    Host address for backend binding (default: 127.0.0.1).

.PARAMETER NoBrowser
    Prevents automatically opening the web browser on launch.

.PARAMETER PythonPath
    Explicit path to a Python 3.11+ executable.

.PARAMETER DatabaseUrl
    Explicit PostgreSQL DATABASE_URL connection string override.

.EXAMPLE
    .\start.ps1
    .\start.ps1 -Mode Background
    .\start.ps1 -SkipFrontend
#>

[CmdletBinding()]
param(
    [ValidateSet("Window", "Background")]
    [string]$Mode = "Window",
    [switch]$SkipFrontend,
    [switch]$SkipBackend,
    [int]$Port = 8000,
    [int]$FrontendPort = 5173,
    [string]$HostAddress = "127.0.0.1",
    [switch]$NoBrowser,
    [string]$PythonPath = "",
    [string]$DatabaseUrl = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "====================================================================" -ForegroundColor Cyan
    Write-Host "  $Title" -ForegroundColor Cyan
    Write-Host "====================================================================" -ForegroundColor Cyan
}

function Write-Step {
    param([string]$Text)
    Write-Host "[*] $Text" -ForegroundColor Yellow
}

function Write-Success {
    param([string]$Text)
    Write-Host "[+] $Text" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Text)
    Write-Host "[!] $Text" -ForegroundColor DarkYellow
}

function Write-Failure {
    param([string]$Text)
    Write-Host "[-] $Text" -ForegroundColor Red
}

function Resolve-PythonPath {
    param([string]$CustomPath)

    if ($CustomPath -and (Test-Path $CustomPath)) {
        return (Resolve-Path $CustomPath).Path
    }

    $venvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        return $venvPy
    }

    $localAppPys = Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python" -Filter "python.exe" -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                   Where-Object { $_.FullName -match "Python31[1-9]" -and $_.FullName -notmatch "Scripts" } |
                   Sort-Object FullName -Descending
    foreach ($cand in $localAppPys) {
        if (Test-Path $cand.FullName) {
            return $cand.FullName
        }
    }

    $progPys = Get-ChildItem -Path "C:\Program Files\Python*" -Filter "python.exe" -ErrorAction SilentlyContinue |
               Where-Object { $_.FullName -notmatch "Scripts" } |
               Sort-Object FullName -Descending
    foreach ($cand in $progPys) {
        if (Test-Path $cand.FullName) {
            return $cand.FullName
        }
    }

    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        try {
            $ver = & py -3 -V 2>&1
            if ($ver -match "Python 3\.(1[1-9]|[2-9][0-9])") {
                return "py"
            }
        } catch {}
    }

    $pyCmd2 = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd2 -and $pyCmd2.Source -notmatch "WindowsApps") {
        try {
            $ver = & python -V 2>&1
            if ($ver -match "Python 3\.(1[1-9]|[2-9][0-9])") {
                return "python"
            }
        } catch {}
    }

    return $null
}

function Test-PortListening {
    param([int]$CheckPort)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", $CheckPort, $null, $null)
        $wh = $iar.AsyncWaitHandle.WaitOne(800, $false)
        if ($wh -and $client.Connected) {
            $client.EndConnect($iar)
            $client.Close()
            return $true
        }
        $client.Close()
    } catch {}
    return $false
}

function Find-ProcessOnPort {
    param([int]$TargetPort)
    try {
        $connections = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
        if ($connections) {
            return ($connections | Select-Object -ExpandProperty OwningProcess -Unique)
        }
    } catch {}
    return @()
}

Write-Section "SREMS (Free-Fall-Elevator) Subsystem Launcher"
Write-Host "Directory: $ScriptDir" -ForegroundColor Gray

# -----------------------------------------------------------------------------
# 1. Environment & Subsystem Checks
# -----------------------------------------------------------------------------
$detectedPy = $null
if (-not $SkipBackend) {
    $detectedPy = Resolve-PythonPath $PythonPath
    if (-not $detectedPy) {
        Write-Failure "Python 3.11+ was not found. Please run .\setup.ps1 first."
        exit 1
    }
    Write-Success "Python runtime resolved: $detectedPy"

    # PostgreSQL check
    $pgListening = Test-PortListening -CheckPort 5432
    if ($pgListening) {
        Write-Success "PostgreSQL service is responsive on port 5432."
    } else {
        # Check service status if available
        $pgService = Get-Service -Name *postgres* -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pgService -and $pgService.Status -ne "Running") {
            Write-Warn "PostgreSQL service ($($pgService.Name)) is stopped. Attempting to start ..."
            try {
                Start-Service -Name $pgService.Name -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 2
                $pgListening = Test-PortListening -CheckPort 5432
            } catch {}
        }

        if ($pgListening) {
            Write-Success "PostgreSQL started successfully on port 5432."
        } else {
            Write-Warn "PostgreSQL is NOT listening on port 5432."
            Write-Host "  -> SREMS backend will start gracefully in in-memory simulation mode." -ForegroundColor DarkYellow
            Write-Host "  -> To enable database persistence, start your PostgreSQL service and re-run." -ForegroundColor Gray
        }
    }
}

if (-not $SkipFrontend) {
    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCmd) {
        Write-Failure "Node.js was not found in PATH. Please run .\setup.ps1 first."
        exit 1
    }

    $frontendDir = Join-Path $ScriptDir "frontend"
    $nodeModulesDir = Join-Path $frontendDir "node_modules"
    if (-not (Test-Path $nodeModulesDir)) {
        Write-Warn "frontend/node_modules not found. Running setup for frontend ..."
        Push-Location $frontendDir
        try {
            & npm install
        } finally {
            Pop-Location
        }
    }
}

# -----------------------------------------------------------------------------
# 2. Port Conflict Checks
# -----------------------------------------------------------------------------
if (-not $SkipBackend) {
    $backendPids = Find-ProcessOnPort -TargetPort $Port
    if ($backendPids.Count -gt 0) {
        Write-Warn "Port $Port is already in use by PID(s): $($backendPids -join ', ')."
        Write-Host "If this is a previous SREMS backend instance, run .\stop.ps1 to terminate it." -ForegroundColor Yellow
    }
}

if (-not $SkipFrontend) {
    $frontendPids = Find-ProcessOnPort -TargetPort $FrontendPort
    if ($frontendPids.Count -gt 0) {
        Write-Warn "Port $FrontendPort is already in use by PID(s): $($frontendPids -join ', ')."
        Write-Host "If this is a previous SREMS frontend instance, run .\stop.ps1 to terminate it." -ForegroundColor Yellow
    }
}

# -----------------------------------------------------------------------------
# 3. Launch Backend Subsystem
# -----------------------------------------------------------------------------
$backendProcess = $null
if (-not $SkipBackend) {
    Write-Section "Starting SREMS Backend (FastAPI)"
    Write-Step "Binding to http://$($HostAddress):$Port ..."

    $envScript = ""
    if ($DatabaseUrl) {
        $envScript = "`$env:DATABASE_URL = '$DatabaseUrl'; "
    }

    $backendCmd = if ($detectedPy -eq "py") {
        "$envScript py -3 -m uvicorn api:app --reload --host $HostAddress --port $Port"
    } else {
        "$envScript & '$detectedPy' -m uvicorn api:app --reload --host $HostAddress --port $Port"
    }

    if ($Mode -eq "Window") {
        $backendProcess = Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle = 'SREMS Backend - FastAPI'; Set-Location '$ScriptDir'; $backendCmd" -PassThru
        Write-Success "Backend launched in dedicated window (PID: $($backendProcess.Id))."
    } else {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = "powershell.exe"
        $startInfo.Arguments = "-NoProfile -Command `"Set-Location '$ScriptDir'; $backendCmd`""
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $false
        $startInfo.RedirectStandardError = $false
        $backendProcess = [System.Diagnostics.Process]::Start($startInfo)
        Write-Success "Backend launched in background process (PID: $($backendProcess.Id))."
    }

    # Verify Backend Health Endpoint
    Write-Step "Waiting for backend health check at http://127.0.0.1:$Port/api/v1/health ..."
    $healthOk = $false
    $timeoutSeconds = 15
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSeconds) {
        Start-Sleep -Milliseconds 800
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -Method Get -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($resp -and $resp.status -eq "ok") {
                $healthOk = $true
                break
            }
        } catch {}
    }

    if ($healthOk) {
        Write-Success "Backend is healthy and serving requests!"
    } else {
        Write-Warn "Backend did not respond to /api/v1/health within $timeoutSeconds seconds. It may still be starting."
    }
}

# -----------------------------------------------------------------------------
# 4. Launch Frontend Subsystem
# -----------------------------------------------------------------------------
$frontendProcess = $null
if (-not $SkipFrontend) {
    Write-Section "Starting SREMS Frontend (Vite/React)"
    $frontendDir = Join-Path $ScriptDir "frontend"
    Write-Step "Starting Vite development server on port $FrontendPort ..."

    $pnpmCmd = Get-Command pnpm -ErrorAction SilentlyContinue
    $pmName = if ($pnpmCmd) { "pnpm" } else { "npm" }
    $frontendCmd = "$pmName run dev -- --port $FrontendPort"

    if ($Mode -eq "Window") {
        $frontendProcess = Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle = 'SREMS Frontend - Vite'; Set-Location '$frontendDir'; $frontendCmd" -PassThru
        Write-Success "Frontend launched in dedicated window (PID: $($frontendProcess.Id))."
    } else {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = "powershell.exe"
        $startInfo.Arguments = "-NoProfile -Command `"Set-Location '$frontendDir'; $frontendCmd`""
        $startInfo.UseShellExecute = $false
        $frontendProcess = [System.Diagnostics.Process]::Start($startInfo)
        Write-Success "Frontend launched in background process (PID: $($frontendProcess.Id))."
    }
}

# -----------------------------------------------------------------------------
# 5. Open Web Browser
# -----------------------------------------------------------------------------
if (-not $NoBrowser -and -not $SkipFrontend) {
    Start-Sleep -Seconds 1
    $uiUrl = "http://localhost:$FrontendPort"
    Write-Step "Opening browser to $uiUrl ..."
    try {
        Start-Process $uiUrl
    } catch {}
}

# -----------------------------------------------------------------------------
# 6. Status Overview
# -----------------------------------------------------------------------------
Write-Section "SREMS Subsystems Live Status"
Write-Host ""
Write-Host "  * Frontend Dashboard:   http://localhost:$FrontendPort" -ForegroundColor Green
Write-Host "  * Visual Shaft / Sim:   http://localhost:$FrontendPort  (Select 'Simulator' in sidebar)" -ForegroundColor Cyan
Write-Host "  * Backend REST API:     http://$($HostAddress):$Port" -ForegroundColor Green
Write-Host "  * Swagger API Docs:     http://$($HostAddress):$Port/docs" -ForegroundColor Cyan
Write-Host "  * Health Check:         http://$($HostAddress):$Port/api/v1/health" -ForegroundColor Gray
Write-Host "  * Database Health:      http://$($HostAddress):$Port/api/v1/health/db" -ForegroundColor Gray
Write-Host ""
Write-Host "====================================================================" -ForegroundColor DarkGray

if ($Mode -eq "Background") {
    Write-Host "Press Ctrl+C or Enter to stop all subsystems ..." -ForegroundColor Yellow
    try {
        [Console]::ReadLine() | Out-Null
    } finally {
        Write-Step "Shutting down SREMS subsystems ..."
        if ($frontendProcess -and -not $frontendProcess.HasExited) {
            Stop-Process -Id $frontendProcess.Id -Force -ErrorAction SilentlyContinue
        }
        if ($backendProcess -and -not $backendProcess.HasExited) {
            Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
        }
        Write-Success "All subsystems stopped."
    }
} else {
    Write-Host "Subsystems running in separate terminal windows." -ForegroundColor White
    Write-Host "To stop all subsystems at any time, run:" -ForegroundColor Yellow
    Write-Host "  .\stop.ps1" -ForegroundColor White
    Write-Host "Or run .\stop.bat from Command Prompt / Explorer." -ForegroundColor Gray
    Write-Host ""
}
