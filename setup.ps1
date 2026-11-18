<#
.SYNOPSIS
    One-time setup script for SREMS (Free-Fall-Elevator) subsystems.

.DESCRIPTION
    Configures and initializes all required dependencies and subsystems:
    1. Python environment and runtime dependencies (requirements.txt).
    2. Node.js and frontend dependencies (npm / pnpm install).
    3. PostgreSQL schema and development seed data (seed.sql).

.PARAMETER CreateVenv
    Creates a dedicated virtual environment (.venv) if one does not exist.

.PARAMETER WithTests
    Also installs test runner dependencies (pytest, pytest-asyncio).

.PARAMETER SkipDb
    Bypasses PostgreSQL database seeding.

.PARAMETER SkipFrontend
    Bypasses frontend npm/pnpm dependency installation.

.PARAMETER SkipPython
    Bypasses Python dependency installation.

.PARAMETER PythonPath
    Explicit path to a specific Python 3.11+ executable.

.PARAMETER PostgresUser
    PostgreSQL user for executing seed.sql (default: postgres).

.PARAMETER PostgresHost
    PostgreSQL host (default: localhost).

.PARAMETER PostgresPort
    PostgreSQL port (default: 5432).

.PARAMETER PostgresPassword
    PostgreSQL superuser password (optional; psql will prompt if omitted).

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -CreateVenv -WithTests
    .\setup.ps1 -SkipDb
#>

[CmdletBinding()]
param(
    [switch]$CreateVenv,
    [switch]$WithTests,
    [switch]$SkipDb,
    [switch]$SkipFrontend,
    [switch]$SkipPython,
    [string]$PythonPath = "",
    [string]$PostgresUser = "postgres",
    [string]$PostgresHost = "localhost",
    [int]$PostgresPort = 5432,
    [string]$PostgresPassword = ""
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

    # 1. Check existing .venv in repo root
    $venvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        return $venvPy
    }

    # 2. Check standard Windows LocalAppData Python installations (e.g., Python314, Python312, Python311)
    $localAppPys = Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python" -Filter "python.exe" -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                   Where-Object { $_.FullName -match "Python31[1-9]" -and $_.FullName -notmatch "Scripts" } |
                   Sort-Object FullName -Descending
    foreach ($cand in $localAppPys) {
        if (Test-Path $cand.FullName) {
            return $cand.FullName
        }
    }

    # 3. Check Program Files Python
    $progPys = Get-ChildItem -Path "C:\Program Files\Python*" -Filter "python.exe" -ErrorAction SilentlyContinue |
               Where-Object { $_.FullName -notmatch "Scripts" } |
               Sort-Object FullName -Descending
    foreach ($cand in $progPys) {
        if (Test-Path $cand.FullName) {
            return $cand.FullName
        }
    }

    # 4. Check Windows py launcher
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        try {
            $ver = & py -3 -V 2>&1
            if ($ver -match "Python 3\.(1[1-9]|[2-9][0-9])") {
                return "py"
            }
        } catch {}
    }

    # 5. Check PATH python.exe (guard against 0-byte WindowsApps stub)
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

function Resolve-PsqlPath {
    $psqlCmd = Get-Command psql -ErrorAction SilentlyContinue
    if ($psqlCmd) {
        return $psqlCmd.Source
    }

    $defaultPaths = Get-ChildItem -Path "C:\Program Files\PostgreSQL" -Filter "psql.exe" -Recurse -Depth 3 -ErrorAction SilentlyContinue |
                     Sort-Object FullName -Descending
    if ($defaultPaths) {
        return $defaultPaths[0].FullName
    }

    return $null
}

Write-Section "SREMS (Free-Fall-Elevator) Setup"
Write-Host "Working Directory: $ScriptDir" -ForegroundColor Gray

# -----------------------------------------------------------------------------
# 1. Python Environment & Dependencies
# -----------------------------------------------------------------------------
if (-not $SkipPython) {
    Write-Section "1. Python Subsystem Setup"

    $detectedPy = Resolve-PythonPath $PythonPath
    if (-not $detectedPy) {
        Write-Failure "Python 3.11+ was not found on your system."
        Write-Host "Please install Python 3.11 or newer from https://www.python.org/downloads/ windows installer." -ForegroundColor Yellow
        exit 1
    }

    Write-Step "Using Python: $detectedPy"

    # Optional: Create .venv
    $venvPath = Join-Path $ScriptDir ".venv"
    if ($CreateVenv -and -not (Test-Path $venvPath)) {
        Write-Step "Creating virtual environment in $venvPath ..."
        if ($detectedPy -eq "py") {
            & py -3 -m venv $venvPath
        } else {
            & $detectedPy -m venv $venvPath
        }
        $venvPy = Join-Path $venvPath "Scripts\python.exe"
        if (Test-Path $venvPy) {
            $detectedPy = $venvPy
            Write-Success "Virtual environment created at: $venvPath"
        } else {
            Write-Warn "Could not verify virtual environment creation. Continuing with: $detectedPy"
        }
    }

    $reqFile = Join-Path $ScriptDir "requirements.txt"
    if (Test-Path $reqFile) {
        Write-Step "Installing backend Python dependencies from requirements.txt ..."
        if ($detectedPy -eq "py") {
            & py -3 -m pip install -r $reqFile
        } else {
            & $detectedPy -m pip install -r $reqFile
        }
        if ($LASTEXITCODE -eq 0) {
            Write-Success "Python dependencies successfully installed."
        } else {
            Write-Failure "Failed to install Python dependencies (pip exit code: $LASTEXITCODE)."
            exit $LASTEXITCODE
        }
    } else {
        Write-Warn "requirements.txt not found at: $reqFile"
    }

    if ($WithTests) {
        Write-Step "Installing test suite dependencies (pytest, pytest-asyncio) ..."
        if ($detectedPy -eq "py") {
            & py -3 -m pip install pytest pytest-asyncio httpx
        } else {
            & $detectedPy -m pip install pytest pytest-asyncio httpx
        }
        if ($LASTEXITCODE -eq 0) {
            Write-Success "Testing dependencies installed."
        }
    }
} else {
    Write-Warn "Skipping Python subsystem setup (-SkipPython specified)."
}

# -----------------------------------------------------------------------------
# 2. Node.js & Frontend Dependencies
# -----------------------------------------------------------------------------
if (-not $SkipFrontend) {
    Write-Section "2. Frontend Subsystem Setup"

    $nodeCmd = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCmd) {
        Write-Failure "Node.js was not found in PATH."
        Write-Host "Please install Node.js 18+ from https://nodejs.org/" -ForegroundColor Yellow
        exit 1
    }

    $nodeVer = & node -v
    Write-Success "Found Node.js: $nodeVer"

    $frontendDir = Join-Path $ScriptDir "frontend"
    if (-not (Test-Path $frontendDir)) {
        Write-Failure "Frontend folder not found at: $frontendDir"
        exit 1
    }

    Push-Location $frontendDir
    try {
        $pnpmCmd = Get-Command pnpm -ErrorAction SilentlyContinue
        if ($pnpmCmd) {
            Write-Step "Installing frontend packages via pnpm ..."
            & pnpm install
        } else {
            Write-Step "Installing frontend packages via npm ..."
            & npm install
        }

        if ($LASTEXITCODE -eq 0) {
            Write-Success "Frontend dependencies installed successfully."
        } else {
            Write-Failure "Frontend package installation failed (exit code: $LASTEXITCODE)."
            exit $LASTEXITCODE
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Warn "Skipping frontend setup (-SkipFrontend specified)."
}

# -----------------------------------------------------------------------------
# 3. PostgreSQL Database Setup & Seeding
# -----------------------------------------------------------------------------
if (-not $SkipDb) {
    Write-Section "3. PostgreSQL Database Setup"

    $seedFile = Join-Path $ScriptDir "seed.sql"
    if (-not (Test-Path $seedFile)) {
        Write-Warn "seed.sql was not found at $seedFile."
    } else {
        $psqlExe = Resolve-PsqlPath
        if (-not $psqlExe) {
            Write-Warn "psql CLI tool was not found in PATH or standard PostgreSQL directories."
            Write-Host "To initialize database manually, run:" -ForegroundColor Yellow
            Write-Host "  psql -U postgres -f seed.sql" -ForegroundColor White
        } else {
            Write-Success "Found psql CLI: $psqlExe"
            Write-Host "Target Database: srems (Host: $PostgresHost, Port: $PostgresPort, User: $PostgresUser)" -ForegroundColor Gray

            $proceedDb = $true
            if (-not $PostgresPassword -and [Environment]::UserInteractive) {
                $choice = Read-Host "Would you like to execute seed.sql now to initialize/update the database? [Y/n]"
                if ($choice -match "^[nN]") {
                    $proceedDb = $false
                }
            }

            if ($proceedDb) {
                Write-Step "Applying seed.sql schema and seed data ..."
                if ($PostgresPassword) {
                    $env:PGPASSWORD = $PostgresPassword
                }

                try {
                    & $psqlExe -h $PostgresHost -p $PostgresPort -U $PostgresUser -f $seedFile
                    if ($LASTEXITCODE -eq 0) {
                        Write-Success "Database schema and seed data initialized successfully."
                    } else {
                        Write-Warn "psql exited with code $LASTEXITCODE. If password or privileges were rejected,"
                        Write-Host "run: psql -U postgres -f seed.sql manually with your PostgreSQL superuser credentials." -ForegroundColor Yellow
                    }
                } finally {
                    if ($PostgresPassword) {
                        $env:PGPASSWORD = $null
                    }
                }
            } else {
                Write-Step "Skipped executing seed.sql as requested."
            }
        }
    }
} else {
    Write-Warn "Skipping PostgreSQL setup (-SkipDb specified)."
}

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Section "Setup Complete"
Write-Host "All configured subsystems have been prepared!" -ForegroundColor Green
Write-Host ""
Write-Host "Next step:" -ForegroundColor Cyan
Write-Host "  Run .\start.ps1 to power on all subsystems (FastAPI backend + React frontend)." -ForegroundColor White
Write-Host "  Run .\start.bat from Command Prompt or File Explorer." -ForegroundColor Gray
Write-Host ""
