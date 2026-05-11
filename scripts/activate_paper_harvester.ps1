param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CondaHook = "D:\anaconda\shell\condabin\conda-hook.ps1"
$EnvironmentName = "paper-harvester"

Set-Location $ProjectRoot

if (-not (Test-Path $CondaHook)) {
    throw "Conda hook not found: $CondaHook"
}

& $CondaHook
conda activate $EnvironmentName

$Command = Get-Command paper-harvester -ErrorAction SilentlyContinue
if (-not $Command) {
    if (-not $Quiet) {
        Write-Host "Installing project command: paper-harvester"
    }
    python -m pip install -e . | Out-Host
}

if (-not $Quiet) {
    Write-Host "Activated conda environment: $EnvironmentName"
    Write-Host "Project: $ProjectRoot"
    python --version
    Write-Host "Try: paper-harvester --help"
}
