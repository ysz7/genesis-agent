# genesis-agent setup / bootstrap (Windows).
#
#   Bootstrap into an empty folder (downloads the repo, then sets it up):
#     irm https://raw.githubusercontent.com/ysz7/genesis-agent/main/scripts/install.ps1 | iex
#
#   Local (already inside the cloned repo):
#     powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# Set the repo below (or override with $env:GENESIS_REPO).
#
# Note: we deliberately do NOT set $ErrorActionPreference = "Stop" — git and uv
# write normal progress to stderr, which "Stop" would treat as a fatal error.
# Instead we check $LASTEXITCODE after each native command.

function Assert-Ok($what) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "ERROR: $what failed (exit $LASTEXITCODE)." -ForegroundColor Red
        exit 1
    }
}

if ($env:GENESIS_REPO) { $Repo = $env:GENESIS_REPO }
else { $Repo = "https://github.com/ysz7/genesis-agent" }

Write-Host ""
Write-Host "=== genesis-agent setup ==="

# --- Locate or download the project -----------------------------------------
$root = $null
if ($PSScriptRoot -and (Test-Path (Join-Path (Split-Path $PSScriptRoot -Parent) "pyproject.toml"))) {
    $root = Split-Path $PSScriptRoot -Parent          # run as -File from scripts/
}
elseif (Test-Path "pyproject.toml") {
    $root = (Get-Location).Path                        # run from repo root
}

if (-not $root) {
    # Bootstrap mode: fetch the repo into the current (empty) folder.
    Write-Host "Downloading from $Repo ..."
    if (Get-Command git -ErrorAction SilentlyContinue) {
        git clone --depth 1 $Repo .
        Assert-Ok "git clone"
    }
    else {
        $zip = Join-Path $env:TEMP "genesis-agent.zip"
        Invoke-WebRequest "$Repo/archive/refs/heads/main.zip" -OutFile $zip -ErrorAction Stop
        Expand-Archive $zip -DestinationPath ".\_dl" -Force -ErrorAction Stop
        $inner = Get-ChildItem ".\_dl" -Directory | Select-Object -First 1
        Move-Item (Join-Path $inner.FullName "*") "." -Force
        Remove-Item ".\_dl", $zip -Recurse -Force
    }
    $root = (Get-Location).Path
}
Set-Location $root

# --- 1) uv (standalone binary; brings its own Python) -----------------------
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "uv already installed ($(uv --version))"
}
else {
    Write-Host "Installing uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"   # use it this session
    Write-Host "uv installed ($(uv --version))"
}

# --- 2) Dependencies --------------------------------------------------------
Write-Host "Installing dependencies..."
uv sync
Assert-Ok "uv sync"

# --- 3) Secrets file --------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from template."
}
else {
    Write-Host ".env already exists -- left untouched."
}

Write-Host ""
Write-Host "=== Agent ready ==="
Write-Host "  To begin, run:   .\start.cmd"
Write-Host "  The first launch walks you through provider, model, and key."
Write-Host ""
