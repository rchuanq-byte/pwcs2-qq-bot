[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner = Join-Path $Root "run-worker.ps1"
$EnableMarker = Join-Path $Root ".recording-enabled"
$SwitchActive = $env:NIUNIU_HIGHLIGHT_SWITCH_ACTIVE

# Default-deny gate. Only the local graphical switch creates this marker, so
# an old shortcut or accidental invocation cannot contact the server or CSDM.
if ($SwitchActive -ne "1" -or -not (Test-Path -LiteralPath $EnableMarker -PathType Leaf)) {
    Write-Host "Local highlight recording is disabled; enable it in the recording switch."
    exit 0
}

$Ssh = Join-Path $env:WINDIR "System32\OpenSSH\ssh.exe"
$Key = Join-Path $HOME ".ssh\example_bot_key"
$LocalPort = 18791

function Test-LocalPort {
    param([int]$Port)

    $Client = [Net.Sockets.TcpClient]::new()
    try {
        $Connect = $Client.ConnectAsync("127.0.0.1", $Port)
        if (-not $Connect.Wait(1000)) { return $false }
        return $Client.Connected
    }
    catch {
        return $false
    }
    finally {
        $Client.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "Missing worker runner: $Runner"
}
if (-not (Test-Path -LiteralPath $Ssh -PathType Leaf)) {
    throw "Windows OpenSSH client is not installed: $Ssh"
}
if (-not (Test-Path -LiteralPath $Key -PathType Leaf)) {
    throw "Missing Aliyun SSH key: $Key"
}

$Tunnel = $null
if (-not (Test-LocalPort -Port $LocalPort)) {
    $SshArguments = @(
        "-NT",
        "-i", $Key,
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-L", "127.0.0.1:${LocalPort}:127.0.0.1:18791",
        "admin@192.0.2.10"
    )
    $Tunnel = Start-Process -FilePath $Ssh -ArgumentList $SshArguments `
        -WindowStyle Hidden -PassThru

    $Ready = $false
    foreach ($Attempt in 1..20) {
        Start-Sleep -Milliseconds 500
        if ($Tunnel.HasExited) {
            throw "SSH tunnel exited before becoming ready (code $($Tunnel.ExitCode))."
        }
        if (Test-LocalPort -Port $LocalPort) {
            $Ready = $true
            break
        }
    }
    if (-not $Ready) {
        Stop-Process -Id $Tunnel.Id -Force -ErrorAction SilentlyContinue
        throw "SSH tunnel did not listen on 127.0.0.1:$LocalPort within 10 seconds."
    }
}

try {
    # Claim at most one server-created job, then exit. A Scheduled Task can run
    # this script periodically without leaving a worker or tunnel resident.
    & $Runner -Once
    exit $LASTEXITCODE
}
finally {
    if ($null -ne $Tunnel -and -not $Tunnel.HasExited) {
        Stop-Process -Id $Tunnel.Id -Force -ErrorAction SilentlyContinue
    }
}
