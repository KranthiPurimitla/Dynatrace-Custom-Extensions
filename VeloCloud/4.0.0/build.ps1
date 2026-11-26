# VeloCloud Dynatrace Extension Build Script
# This script builds and validates the extension

param(
    [switch]$Clean = $false,
    [switch]$Test = $false,
    [switch]$Package = $false
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "🔨 VeloCloud Extension Build Script" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "Project Root: $projectRoot`n" -ForegroundColor Gray

# Step 1: Clean (optional)
if ($Clean) {
    Write-Host "🧹 Cleaning build artifacts..." -ForegroundColor Yellow
    Remove-Item -Path "$projectRoot\build" -Recurse -ErrorAction SilentlyContinue
    Remove-Item -Path "$projectRoot\dist" -Recurse -ErrorAction SilentlyContinue
    Remove-Item -Path "$projectRoot\*.egg-info" -Recurse -ErrorAction SilentlyContinue
    Write-Host "✅ Clean complete`n"
}

# Step 2: Verify Python version
Write-Host "🐍 Verifying Python version..." -ForegroundColor Yellow
$pythonVersion = python --version 2>&1
Write-Host "   $pythonVersion"
if ($pythonVersion -notmatch "3\.1[4-9]") {
    Write-Host "⚠️  Warning: Python 3.14+ is recommended" -ForegroundColor Red
}
Write-Host ""

# Step 3: Install dependencies
Write-Host "📦 Installing dependencies..." -ForegroundColor Yellow
pip install -q -r "$projectRoot\requirements.txt" --upgrade
Write-Host "✅ Dependencies installed`n"

# Step 4: Syntax check
Write-Host "🔍 Performing syntax checks..." -ForegroundColor Yellow
python -m py_compile "$projectRoot\velocloudextensionrevised\__main__.py"
python -m py_compile "$projectRoot\velocloudextensionrevised\api_client.py"
Write-Host "✅ Syntax check passed`n"

# Step 5: Test (optional)
if ($Test) {
    Write-Host "🧪 Running tests..." -ForegroundColor Yellow
    pytest "$projectRoot\tests" -v
    Write-Host ""
}

# Step 6: Build distribution package
if ($Package) {
    Write-Host "📦 Building distribution package..." -ForegroundColor Yellow
    python setup.py sdist bdist_wheel
    Write-Host "✅ Package built`n"
    Write-Host "📁 Build artifacts in: $projectRoot\dist"
}

# Final summary
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "✅ Build process complete!" -ForegroundColor Green
Write-Host "`nNext steps:" -ForegroundColor Cyan
Write-Host "  1. Run: dt-sdk run --activation-config activation.json"
Write-Host "  2. Monitor logs for [WARNING] Rate limited (429) messages"
Write-Host "  3. Verify success rate > 95%"
Write-Host ""
