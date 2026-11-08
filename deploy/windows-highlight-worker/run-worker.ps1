[CmdletBinding()]
param(
    [switch]$Once,
    [string]$LocalDemo = "",
    [string]$SteamId = "",
    [ValidateSet("perfectworld", "valve")]
    [string]$Source = "valve",
    [switch]$NoRender
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Worker = Join-Path $Root "worker.py"
$Config = Join-Path $Root "config.json"

function Add-PostgreSqlToolsToPath {
    if (Get-Command psql -ErrorAction SilentlyContinue) { return }
    $PostgreSqlRoot = Join-Path $env:ProgramFiles "PostgreSQL"
    if (-not (Test-Path -LiteralPath $PostgreSqlRoot -PathType Container)) { return }
    $Installations = Get-ChildItem -LiteralPath $PostgreSqlRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object -Property Name -Descending
    foreach ($Installation in $Installations) {
        $Bin = Join-Path $Installation.FullName "bin"
        $Psql = Join-Path $Bin "psql.exe"
        if (Test-Path -LiteralPath $Psql -PathType Leaf) {
            $env:Path = "$Bin;$env:Path"
            return
        }
    }
}

Add-PostgreSqlToolsToPath

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw "Missing worker-local .venv. Run setup-worker.ps1 -InstallDependencies first."
}
if (-not (Test-Path -LiteralPath $Worker -PathType Leaf)) {
    throw "Missing worker.py: $Worker"
}
$PreviousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$DependencyLines = @()
$DependencyExitCode = -1
try {
    $DependencyLines = @(& $VenvPython -c "import platform, sys; from importlib.metadata import version; import demoparser2; value=version('demoparser2'); print(platform.python_implementation(), sys.version.split()[0], 'demoparser2', value); ok=platform.python_implementation() == 'CPython' and (3, 10) <= sys.version_info[:2] < (3, 14) and value == '0.41.4'; raise SystemExit(0 if ok else 5)" 2>&1)
    $DependencyExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $PreviousPreference
}
$DependencyCheck = ($DependencyLines | Out-String).Trim()
if ($DependencyExitCode -ne 0) {
    throw "The worker .venv is incomplete. Rerun setup-worker.ps1 -InstallDependencies. Details: $DependencyCheck"
}

$Arguments = @($Worker, "--config", $Config)
if ($LocalDemo) {
    if (-not $SteamId) { throw "-SteamId is required with -LocalDemo." }
    if ($Once) { throw "-Once cannot be combined with -LocalDemo." }
    if (-not $NoRender -and -not (Test-Path -LiteralPath $Config -PathType Leaf)) {
        throw "Missing config.json; copy config.example.json before rendering."
    }
    $Arguments += @("--local-demo", $LocalDemo, "--steam-id", $SteamId, "--source", $Source)
    if ($NoRender) { $Arguments += "--no-render" }
    & $VenvPython @Arguments
    exit $LASTEXITCODE
}
if ($NoRender -or $SteamId) { throw "-NoRender and -SteamId require -LocalDemo." }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
    throw "Missing config.json; copy config.example.json and configure the worker first."
}

$SecretFile = Join-Path $Root "worker-secret.dpapi"
if (-not (Test-Path -LiteralPath $SecretFile -PathType Leaf)) {
    throw "Missing worker-secret.dpapi; run setup-secret.ps1 first."
}

$Protected = [IO.File]::ReadAllBytes($SecretFile)
$Entropy = [Text.Encoding]::UTF8.GetBytes("NiuniuHighlightWorker/v1")
if (-not ("System.Security.Cryptography.ProtectedData" -as [type])) {
    Add-Type -AssemblyName System.Security
}
$Plain = [System.Security.Cryptography.ProtectedData]::Unprotect(
    $Protected,
    $Entropy,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
)
$WorkerExitCode = 1
try {
    $env:NIUNIU_HIGHLIGHT_WORKER_SECRET = [Text.Encoding]::UTF8.GetString($Plain)
    if ($Once) { $Arguments += "--once" }
    & $VenvPython @Arguments
    $WorkerExitCode = $LASTEXITCODE
}
finally {
    [Array]::Clear($Plain, 0, $Plain.Length)
    Remove-Item Env:NIUNIU_HIGHLIGHT_WORKER_SECRET -ErrorAction SilentlyContinue
}
exit $WorkerExitCode
