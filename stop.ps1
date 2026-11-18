<#
.SYNOPSIS
    Stops all running SREMS (Free-Fall-Elevator) subsystems.

.DESCRIPTION
    Terminates running backend (FastAPI/uvicorn on port 8000) and frontend (Vite/Node on port 5173)
    processes, and closes any associated terminal windows.

.PARAMETER Port
    Backend port to release (default: 8000).

.PARAMETER FrontendPort
    Frontend port to release (default: 5173).

.EXAMPLE
    .\stop.ps1
#>

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "SilentlyContinue"

Write-Host "====================================================================" -ForegroundColor Cyan
Write-Host "  Stopping SREMS Subsystems" -ForegroundColor Cyan
Write-Host "====================================================================" -ForegroundColor Cyan

function Stop-ProcessesOnPort {
    param([int]$TargetPort, [string]$Label)

    $stopped = 0
    try {
        $connections = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
        if ($connections) {
            $pids = $connections | Select-Object -ExpandProperty OwningProcess -Unique
            foreach ($procId in $pids) {
                if ($procId -gt 0) {
                    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                    if ($proc) {
                        Write-Host "[*] Stopping $Label process (PID: $procId - $($proc.ProcessName)) ..." -ForegroundColor Yellow
                        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                        $stopped++
                    }
                }
            }
        }
    } catch {}

    if ($stopped -gt 0) {
        Write-Host "[+] Released port $TargetPort ($stopped process(es) stopped)." -ForegroundColor Green
    } else {
        Write-Host "[-] No active process found on port $TargetPort ($Label)." -ForegroundColor Gray
    }
}

# Stop Backend
Stop-ProcessesOnPort -TargetPort $Port -Label "Backend (FastAPI)"

# Stop Frontend
Stop-ProcessesOnPort -TargetPort $FrontendPort -Label "Frontend (Vite)"

# Look for titled powershell windows if still open
$titledWindows = Get-Process powershell -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -match "SREMS Backend" -or $_.MainWindowTitle -match "SREMS Frontend"
}
foreach ($tw in $titledWindows) {
    Write-Host "[*] Closing window '$($tw.MainWindowTitle)' (PID: $($tw.Id)) ..." -ForegroundColor Yellow
    Stop-Process -Id $tw.Id -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "[+] All SREMS subsystems stopped." -ForegroundColor Green
Write-Host ""
