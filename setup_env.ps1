# Setup Python venv for comfy-runner on Windows
$ErrorActionPreference = "Stop"

$venvDir = Join-Path $PSScriptRoot ".venv"

# Find a real Python 3 interpreter (skip the Windows Store stub)
$pythonExe = $null
$pyArgs = @()
foreach ($candidate in @("python3", "python")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found -and $found.Source -notlike "*WindowsApps*") {
        $pythonExe = $found.Source
        break
    }
}
# Fall back to the py launcher
if (-not $pythonExe) {
    $found = Get-Command py -ErrorAction SilentlyContinue
    if ($found) {
        $pythonExe = $found.Source
        $pyArgs = @("-3")
    }
}
if (-not $pythonExe) {
    Write-Host "Could not find Python 3. Please install Python 3.10+ and try again." -ForegroundColor Red
    exit 1
}

Write-Host "Using Python: $pythonExe $pyArgs" -ForegroundColor Cyan

if (-not (Test-Path $venvDir)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    & $pythonExe @pyArgs -m venv $venvDir
} else {
    Write-Host "Virtual environment already exists." -ForegroundColor Green
}

Write-Host "Installing dependencies..." -ForegroundColor Cyan
& "$venvDir\Scripts\pip.exe" install --quiet -r "$PSScriptRoot\requirements.txt"

Write-Host ""
Write-Host "Setup complete. Run comfy-runner with:" -ForegroundColor Green
Write-Host "  .venv\Scripts\python.exe comfy_runner.py <command>" -ForegroundColor Yellow
