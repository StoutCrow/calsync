# One-click setup of calsync on a Windows home server with Docker Desktop and Tailscale.
# Run it by double-clicking "Set up calsync with Docker (Windows).bat".
#
# What it does, in order (each step is printed):
#   1. checks Docker and Tailscale are there
#   2. downloads the newest calsync source from GitHub into  %USERPROFILE%\calsync-docker
#      (your config folder is never overwritten)
#   3. creates config\calsync.env with a private access code, and points calsync at Immich
#      when Immich's Docker network is found
#   4. builds and starts the container (restarts by itself after reboots)
#   5. shares it ONLY on your Tailscale network with:  tailscale serve
# Safe to run again: running it again updates calsync and keeps your settings.

$ErrorActionPreference = "Stop"
$Repo = if ($env:CALSYNC_REPO) { $env:CALSYNC_REPO } else { "StoutCrow/calsync" }
$Dir = Join-Path $env:USERPROFILE "calsync-docker"
$Port = 8766

function Say($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host ""; Write-Host "Problem: $m" -ForegroundColor Red; exit 1 }

Say "1/5 Checking Docker and Tailscale"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop and run this again." }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker is installed but not running. Open Docker Desktop, wait for 'Engine running', and run this again." }
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts) {
    $p = "C:\Program Files\Tailscale\tailscale.exe"
    if (Test-Path $p) { $ts = $p } else { Fail "Tailscale is not installed. Get it from https://tailscale.com/download, sign in, and run this again." }
} else { $ts = $ts.Source }
& $ts status *> $null
if ($LASTEXITCODE -ne 0) { Fail "Tailscale is stopped or signed out. Open Tailscale, sign in, and run this again." }
Write-Host "Docker and Tailscale are ready."

Say "2/5 Downloading calsync from github.com/$Repo"
$tmp = Join-Path $env:TEMP "calsync-src"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory $tmp | Out-Null
$zip = Join-Path $tmp "src.zip"
try { Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing }
catch { Fail "Could not download from GitHub: $($_.Exception.Message)" }
Expand-Archive $zip -DestinationPath $tmp -Force
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
if (-not $src) { Fail "The download was empty." }
New-Item -ItemType Directory $Dir -Force | Out-Null
# copy program files over, but never touch config\calsync.env
Get-ChildItem $src.FullName -Force | Where-Object { $_.Name -ne "config" } | ForEach-Object {
    Copy-Item $_.FullName -Destination $Dir -Recurse -Force
}
New-Item -ItemType Directory (Join-Path $Dir "config") -Force | Out-Null
$env_file = Join-Path $Dir "config\calsync.env"
if (-not (Test-Path $env_file)) { Copy-Item (Join-Path $src.FullName "config\calsync.env.example") $env_file }
Remove-Item $tmp -Recurse -Force

Say "3/5 Settings"
function Get-Setting($key) {
    $line = Get-Content $env_file | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) { return ($line -split "=", 2)[1].Trim() } else { return "" }
}
function Set-Setting($key, $value) {
    $lines = @(Get-Content $env_file)
    $hit = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$key=") { $lines[$i] = "$key=$value"; $hit = $true; break }
    }
    if (-not $hit) { $lines += "$key=$value" }
    [IO.File]::WriteAllLines($env_file, $lines, (New-Object Text.UTF8Encoding($false)))
}
if (-not (Get-Setting "CALSYNC_APP_TOKEN")) {
    $b = New-Object byte[] 16
    (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($b)
    Set-Setting "CALSYNC_APP_TOKEN" ([Convert]::ToBase64String($b).TrimEnd("=").Replace("+", "-").Replace("/", "_"))
}
$token = Get-Setting "CALSYNC_APP_TOKEN"

$files = @("-f", "docker-compose.yml")
$net = docker network ls --format "{{.Name}}" | Where-Object { $_ -eq "immich_default" }
if ($net) {
    $files += @("-f", "docker-compose.immich.yml")
    Set-Setting "IMMICH_URL" "http://immich_server:2283"
    Write-Host "Found Immich's Docker network (immich_default). calsync will reach it at http://immich_server:2283."
    if (-not (Get-Setting "IMMICH_API_KEY")) { Write-Host "Still needed: an Immich API key. Add it in calsync's Settings page." }
} else {
    Write-Host "Immich's Docker network was not found, so Immich is skipped. (Set IMMICH_URL in Settings if it runs elsewhere.)"
}

Say "4/5 Building and starting calsync (the first build takes a few minutes)"
Push-Location $Dir
try {
    docker compose @files up -d --build
    if ($LASTEXITCODE -ne 0) { Fail "docker compose failed. Scroll up for the reason." }
} finally { Pop-Location }
$up = $false
for ($i = 0; $i -lt 40 -and -not $up; $i++) {
    Start-Sleep 1
    try { Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 3 | Out-Null; $up = $true } catch { }
}
if (-not $up) { Fail "calsync did not start. See the reason with:  docker logs calsync" }
Write-Host "calsync is running in Docker."

Say "5/5 Sharing it on your Tailscale network"
$url = "http://127.0.0.1:$Port"
& $ts serve --bg $Port *> $null
if ($LASTEXITCODE -eq 0) {
    try {
        $dns = ((& $ts status --json | ConvertFrom-Json).Self.DNSName).TrimEnd(".")
        if ($dns) { $url = "https://$dns" }
    } catch { }
} else {
    Write-Host "Could not run 'tailscale serve'. Try it yourself in a terminal:  tailscale serve --bg $Port"
    Write-Host "If it says HTTPS is off, turn on HTTPS certificates in the Tailscale admin console (DNS page)."
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host ""
Write-Host "  Open this on any device signed in to your Tailscale network:"
Write-Host "    $url/#t=$token"
Write-Host ""
Write-Host "  On iPhone: open it in Safari, then Share > Add to Home Screen."
Write-Host "  Keep the part after # private: it is your access code."
Write-Host "  Fill in your keys on the Settings page (the gear). They are saved in:"
Write-Host "    $env_file"
Write-Host ""
Write-Host "  Stop:    docker stop calsync        Logs:   docker logs calsync"
Write-Host "  Update:  run this setup again"
