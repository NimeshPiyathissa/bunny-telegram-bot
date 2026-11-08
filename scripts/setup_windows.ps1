# ==============================================================================
# Bunny Stream Telegram Downloader Bot - Windows 10/11 Automated Setup Script
# ==============================================================================
# Execution Policy Note: If script execution is restricted on your machine, run:
# Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
# ==============================================================================

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  🎬 Bunny Stream to Telegram Bot - Windows Automated Setup" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Verify Python Installation
Write-Host "[1/7] Checking Python installation..." -ForegroundColor Yellow
try {
    $pythonVersion = python --version 2>&1
    Write-Host "  Found: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "  [ERROR] Python is not installed or not in PATH." -ForegroundColor Red
    Write-Host "  Please install Python 3.11+ from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "  Ensure 'Add Python to PATH' is checked during installation." -ForegroundColor Red
    Exit 1
}

# 2. Verify FFmpeg & FFprobe Installation
Write-Host "[2/7] Checking FFmpeg and FFprobe media tools..." -ForegroundColor Yellow
try {
    $ffmpegVersion = ffmpeg -version 2>&1 | Select-Object -First 1
    Write-Host "  Found FFmpeg: $ffmpegVersion" -ForegroundColor Green
} catch {
    Write-Host "  [WARNING] FFmpeg is not detected in your PATH." -ForegroundColor Yellow
    Write-Host "  FFmpeg is required for video/audio remuxing and HLS streams." -ForegroundColor Yellow
    Write-Host "  Attempting auto-install via winget..." -ForegroundColor Cyan
    try {
        winget install Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
        Write-Host "  FFmpeg installed. Please restart your PowerShell terminal afterwards." -ForegroundColor Green
    } catch {
        Write-Host "  Could not auto-install via winget. Please install manually:" -ForegroundColor Red
        Write-Host "  1. Download build from https://www.gyan.dev/ffmpeg/builds/" -ForegroundColor Red
        Write-Host "  2. Extract and add the bin folder to your System PATH." -ForegroundColor Red
    }
}

# 3. Create Virtual Environment
Write-Host "[3/7] Setting up Python virtual environment (.venv)..." -ForegroundColor Yellow
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-Host "  Virtual environment created." -ForegroundColor Green
} else {
    Write-Host "  Virtual environment (.venv) already exists." -ForegroundColor Green
}

# 4. Install Dependencies
Write-Host "[4/7] Installing Python dependencies..." -ForegroundColor Yellow
& ".\.venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "  All dependencies successfully installed." -ForegroundColor Green
} else {
    Write-Host "  [ERROR] Failed to install dependencies. Check your internet connection." -ForegroundColor Red
    Exit 1
}

# 5. Environment File Configuration
Write-Host "[5/7] Checking .env configuration..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "  Created .env from .env.example." -ForegroundColor Green
    Write-Host "  --> PLEASE OPEN .env AND CONFIGURE YOUR BOT_TOKEN, API_ID, API_HASH, AND OWNER_ID <--" -ForegroundColor Magenta
} else {
    Write-Host "  Existing .env found." -ForegroundColor Green
}

# 6. Ensure Application Directories
Write-Host "[6/7] Creating runtime directories..." -ForegroundColor Yellow
$dirs = @("data", "downloads", "temp", "logs", "backups")
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}
Write-Host "  Directory tree prepared (data/, downloads/, temp/, logs/, backups/)." -ForegroundColor Green

# 7. Validate Configuration
Write-Host "[7/7] Validating bot configuration..." -ForegroundColor Yellow
try {
    $valOutput = & ".\.venv\Scripts\python.exe" -c "
from config import config
errors = config.validate(exit_on_error=False)
if errors:
    print('CONFIG_INCOMPLETE: ' + ', '.join(errors))
else:
    print('CONFIG_OK')
"
    if ($valOutput -match "CONFIG_OK") {
        Write-Host "  Configuration status: Valid and ready to run!" -ForegroundColor Green
    } else {
        Write-Host "  $valOutput" -ForegroundColor Yellow
        Write-Host "  Please edit .env with your credentials before launching." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Could not validate config automatically. Verify .env manually." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "  Setup Completed Successfully!" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "How to operate the bot on Windows:" -ForegroundColor White
Write-Host "  1. Edit credentials (if not already done):" -ForegroundColor Yellow
Write-Host "     notepad .env" -ForegroundColor White
Write-Host "  2. Activate virtual environment:" -ForegroundColor Yellow
Write-Host "     .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host "  3. Start the bot:" -ForegroundColor Yellow
Write-Host "     python bot.py" -ForegroundColor White
Write-Host "  4. To stop: Press Ctrl + C in the terminal" -ForegroundColor Yellow
Write-Host "  5. Run test suite:" -ForegroundColor Yellow
Write-Host "     pytest -v" -ForegroundColor White
Write-Host ""
