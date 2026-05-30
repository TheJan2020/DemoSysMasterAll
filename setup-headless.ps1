# PW Demo Master — headless Windows bootstrap.
#
# Run ONCE on the Windows box, as Administrator:
#     powershell -ExecutionPolicy Bypass -File .\setup-headless.ps1
#
# Idempotent — safe to re-run after a code update or partial failure.
#
# After this finishes:
#   * The FastAPI app runs as a Windows service ("PWDemoMaster"), started
#     automatically at boot before any user logs in.
#   * OpenSSH server is enabled — you can `ssh` in from the Mac.
#   * Git, NSSM, Tailscale are installed via winget.
#
# Two manual steps it CANNOT do for you:
#   1. BIOS: set "AC Power Recovery" / "Restore on AC Power Loss" → ON.
#      Without this, a power blip leaves the box dark.
#   2. Tailscale: open the tray, log in once, enable "Run unattended" in
#      Tailscale → Preferences.  After that the box is reachable on its
#      *.tailnet.ts.net hostname even when no user is logged in.

[CmdletBinding()]
param(
    [string]$RepoPath = $PSScriptRoot,
    [int]   $Port     = 8080,
    [string]$ServiceName = "PWDemoMaster"
)

$ErrorActionPreference = "Stop"

function Require-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell (Administrator)."
    }
}

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok  ($msg) { Write-Host "    ok: $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    warn: $msg" -ForegroundColor Yellow }

function Ensure-Winget-Package($id, $name) {
    if (winget list --id $id -e 2>$null | Select-String -Quiet $id) {
        Ok "$name already installed"
        return
    }
    Step "Installing $name ($id)"
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget install $id failed (exit $LASTEXITCODE)" }
    Ok "$name installed"
}

# ---------------------------------------------------------------------------
Require-Admin

if (-not (Test-Path $RepoPath)) { throw "RepoPath '$RepoPath' does not exist." }
$RepoPath = (Resolve-Path $RepoPath).Path
Write-Host "Repo path:    $RepoPath"
Write-Host "Service name: $ServiceName"
Write-Host "Port:         $Port"

# ---------------------------------------------------------------------------
Step "Install prerequisites via winget"
Ensure-Winget-Package "NSSM.NSSM"             "NSSM"
Ensure-Winget-Package "Git.Git"               "Git"
Ensure-Winget-Package "Tailscale.Tailscale"   "Tailscale"
Ensure-Winget-Package "Python.Python.3.12"    "Python 3.12"

# winget puts new tools on PATH only for new shells — refresh this session.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User")

# ---------------------------------------------------------------------------
Step "Enable OpenSSH server"
$cap = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
if ($cap.State -ne "Installed") {
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
    Ok "OpenSSH.Server capability installed"
} else {
    Ok "OpenSSH.Server already installed"
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Ok "sshd running, set to auto-start"

if (-not (Get-NetFirewallRule -Name "sshd" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow `
        -LocalPort 22 | Out-Null
    Ok "Firewall rule for port 22 added"
} else {
    Ok "Firewall rule for port 22 already present"
}

# ---------------------------------------------------------------------------
Step "Create Python venv + install requirements"
$venv   = Join-Path $RepoPath ".venv"
$pyExe  = Join-Path $venv "Scripts\python.exe"
$reqs   = Join-Path $RepoPath "backend\requirements.txt"

if (-not (Test-Path $pyExe)) {
    python -m venv $venv
    Ok "venv created at $venv"
} else {
    Ok "venv already exists"
}

& $pyExe -m pip install --quiet --upgrade pip
& $pyExe -m pip install --quiet -r $reqs
Ok "requirements installed"

# ---------------------------------------------------------------------------
Step "Register NSSM service '$ServiceName'"
$logs = Join-Path $RepoPath "logs"
if (-not (Test-Path $logs)) { New-Item -ItemType Directory -Path $logs | Out-Null }

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Warn "Service already exists — stopping and reconfiguring"
    nssm stop $ServiceName confirm | Out-Null
} else {
    # NSSM `install` is non-interactive when given full argv.
    nssm install $ServiceName $pyExe `
        "-m" "uvicorn" "app.main:app" "--app-dir" "backend" `
        "--host" "0.0.0.0" "--port" "$Port" | Out-Null
    Ok "service registered"
}

# (Re)apply config every run — keeps state consistent if you edit this script.
nssm set $ServiceName AppDirectory     $RepoPath              | Out-Null
nssm set $ServiceName Start            SERVICE_AUTO_START     | Out-Null
nssm set $ServiceName AppStdout        (Join-Path $logs "stdout.log") | Out-Null
nssm set $ServiceName AppStderr        (Join-Path $logs "stderr.log") | Out-Null
nssm set $ServiceName AppRotateFiles   1                      | Out-Null
nssm set $ServiceName AppRotateBytes   10485760               | Out-Null
nssm set $ServiceName AppExit Default  Restart                | Out-Null
nssm set $ServiceName AppRestartDelay  5000                   | Out-Null
Ok "service config applied"

nssm start $ServiceName | Out-Null
Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName
Ok "service status: $($svc.Status)"

# ---------------------------------------------------------------------------
Step "Smoke test"
try {
    $r = Invoke-WebRequest "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 5
    Ok "GET / → HTTP $($r.StatusCode)"
} catch {
    Warn "GET / failed: $($_.Exception.Message)"
    Warn "Check $logs\stderr.log for backend errors."
}

# ---------------------------------------------------------------------------
Write-Host "`nDone. Remaining manual steps:" -ForegroundColor Cyan
Write-Host "  1. BIOS: set 'AC Power Recovery' / 'Restore on AC Power Loss' to ON."
Write-Host "  2. Tailscale: open the tray icon, sign in, then in Preferences"
Write-Host "     enable 'Run unattended'. Note the *.tailnet.ts.net hostname."
Write-Host "  3. From the Mac, verify SSH:  ssh $env:USERNAME@<hostname>"
Write-Host "`nUpdate workflow from Mac:"
Write-Host "  ssh $env:USERNAME@<hostname> \"cd '$RepoPath'; git pull; nssm restart $ServiceName\""
