$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$VenvUvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Не найден $VenvPython. Сначала создайте окружение: python -m venv .venv"
    exit 1
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceAlias -notmatch "Loopback|vEthernet|WSL|Hyper-V|Tailscale|ZeroTier" -and
    $_.IPAddress -notlike "169.254.*" -and
    $_.IPAddress -ne "127.0.0.1"
} | Select-Object -First 1).IPAddress

if (-not $ip) { $ip = "127.0.0.1" }

Write-Host "==================================================" -ForegroundColor Green
Write-Host "Используется Python из .venv: $VenvPython" -ForegroundColor Cyan
& $VenvPython --version
Write-Host "Главная (программа): http://$($ip):8000/"
Write-Host "Трансляция:          http://$($ip):8000/broadcast"
Write-Host "Пульт ведущего:      http://$($ip):8000/control"
Write-Host "Вход игроков:        http://$($ip):8000/play"
Write-Host "Админ-панель:        http://$($ip):8000/admin"
Write-Host "==================================================" -ForegroundColor Green

Set-Location $ProjectRoot
if (Test-Path $VenvUvicorn) {
    & $VenvUvicorn app.main:app --host 0.0.0.0 --port 8000
} else {
    & $VenvPython -m uvicorn app.main:app --host 0.0.0.0 --port 8000
}
