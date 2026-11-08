[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [switch]$InstallDependencies,
    [string]$CsdmPath = "",
    [string]$Cs2Path = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvRoot = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$Requirements = Join-Path $Root "requirements.txt"

function Resolve-Executable {
    param([string]$ExplicitPath, [string]$CommandName, [string[]]$Fallbacks)
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "Executable was not found: $ExplicitPath"
        }
        return (Resolve-Path -LiteralPath $ExplicitPath -ErrorAction Stop).Path
    }
    $Command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    foreach ($Candidate in $Fallbacks) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    return $null
}

function Resolve-BasePython {
    param([string]$ExplicitPath)
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "Python executable was not found: $ExplicitPath"
        }
        return [PSCustomObject]@{
            Executable = (Resolve-Path -LiteralPath $ExplicitPath).Path
            PrefixArguments = @()
        }
    }

    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($Launcher) {
        foreach ($Selector in @("-3.12", "-3.13", "-3.11", "-3.10")) {
            $Probe = Invoke-NativeCapture -Executable $Launcher.Source -Arguments @(
                $Selector, "-c", "import sys; print(sys.executable)"
            )
            if ($Probe.ExitCode -eq 0) {
                return [PSCustomObject]@{
                    Executable = $Launcher.Source
                    PrefixArguments = @($Selector)
                }
            }
        }
    }

    $Python = Get-Command python -ErrorAction SilentlyContinue
    if ($Python) {
        return [PSCustomObject]@{
            Executable = $Python.Source
            PrefixArguments = @()
        }
    }
    throw "Python 3.10-3.13 was not found. Rerun with -PythonPath <python.exe>."
}

function Invoke-NativeCapture {
    param([string]$Executable, [string[]]$Arguments)
    $PreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $Lines = @()
    $ExitCode = -1
    try {
        $Lines = @(& $Executable @Arguments 2>&1)
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousPreference
    }
    return [PSCustomObject]@{
        ExitCode = $ExitCode
        Output = ($Lines | Out-String).Trim()
    }
}

function Invoke-BasePython {
    param(
        [PSCustomObject]$Python,
        [string[]]$Arguments,
        [string]$FailureMessage
    )
    $AllArguments = @()
    $AllArguments += $Python.PrefixArguments
    $AllArguments += $Arguments
    & $Python.Executable @AllArguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)."
    }
}

if (-not (Test-Path -LiteralPath $Requirements -PathType Leaf)) {
    throw "Missing dependency lock file: $Requirements"
}

$VenvCreated = $false
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    if (Test-Path -LiteralPath $VenvRoot) {
        throw "The local .venv exists but Scripts\python.exe is missing. Move the broken .venv aside and rerun setup."
    }
    $BasePython = Resolve-BasePython -ExplicitPath $PythonPath
    Invoke-BasePython -Python $BasePython -Arguments @(
        "-c",
        "import platform, sys; print(sys.version); ok=platform.python_implementation() == 'CPython' and (3, 10) <= sys.version_info[:2] < (3, 14); raise SystemExit(0 if ok else 4)"
    ) -FailureMessage "The selected interpreter must be CPython 3.10-3.13"
    Invoke-BasePython -Python $BasePython -Arguments @(
        "-m", "venv", $VenvRoot
    ) -FailureMessage "Failed to create the worker-local virtual environment"
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "Python reported success but .venv\Scripts\python.exe was not created."
    }
    $VenvCreated = $true
}

$VenvProbe = Invoke-NativeCapture -Executable $VenvPython -Arguments @(
    "-c",
    "import platform, sys; print(sys.version); ok=platform.python_implementation() == 'CPython' and (3, 10) <= sys.version_info[:2] < (3, 14); raise SystemExit(0 if ok else 4)"
)
if ($VenvProbe.ExitCode -ne 0) {
    throw "The worker .venv must use CPython 3.10-3.13; detected: $($VenvProbe.Output)"
}
$VenvVersion = $VenvProbe.Output

if ($InstallDependencies) {
    & $VenvPython -m pip install --disable-pip-version-check --only-binary=:all: --requirement $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed (exit code $LASTEXITCODE)."
    }
}

$DependencyProbe = Invoke-NativeCapture -Executable $VenvPython -Arguments @(
    "-c",
    "from importlib.metadata import version; import demoparser2; value=version('demoparser2'); print('demoparser2 ' + value); raise SystemExit(0 if value == '0.41.4' else 5)"
)
$DependencyVersion = $DependencyProbe.Output
if ($DependencyProbe.ExitCode -ne 0) {
    if ($InstallDependencies) {
        throw "The pinned demoparser2 dependency could not be verified: $DependencyVersion"
    }
    throw "Worker dependencies are missing or stale. Rerun setup-worker.ps1 with -InstallDependencies. Details: $DependencyVersion"
}

$DefaultCsdm = Join-Path $env:LOCALAPPDATA "Programs\cs-demo-manager\csdm.cmd"
$LegacyCsdm = Join-Path $env:LOCALAPPDATA "Programs\CS Demo Manager\csdm.cmd"
$CsdmExe = Resolve-Executable -ExplicitPath $CsdmPath -CommandName "csdm" -Fallbacks @(
    $DefaultCsdm,
    $LegacyCsdm
)
if (-not $CsdmExe) { throw "CS Demo Manager CLI (csdm.cmd) was not found." }
$CsdmProbe = Invoke-NativeCapture -Executable $CsdmExe -Arguments @()
$CsdmVersion = $CsdmProbe.Output
if ($CsdmProbe.ExitCode -ne 0) { throw "csdm startup probe failed: $CsdmVersion" }
if ($CsdmVersion -notmatch '(?<!\d)3\.20(?:\.\d+)?(?!\d)') {
    throw "CS Demo Manager 3.20.x is required; detected: $CsdmVersion"
}

$PostgreSqlRoot = Join-Path $env:ProgramFiles "PostgreSQL"
$PostgreSqlFallbacks = @()
if (Test-Path -LiteralPath $PostgreSqlRoot -PathType Container) {
    $PostgreSqlFallbacks = @(
        Get-ChildItem -LiteralPath $PostgreSqlRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object -Property Name -Descending |
            ForEach-Object { Join-Path $_.FullName "bin\psql.exe" }
    )
}
$PsqlExe = Resolve-Executable -ExplicitPath "" -CommandName "psql" -Fallbacks $PostgreSqlFallbacks
if (-not $PsqlExe) {
    throw "PostgreSQL psql.exe was not found. Install PostgreSQL client tools before rendering."
}
$PsqlProbe = Invoke-NativeCapture -Executable $PsqlExe -Arguments @("--version")
if ($PsqlProbe.ExitCode -ne 0) { throw "psql version probe failed: $($PsqlProbe.Output)" }

$SteamRoot = ""
try {
    $SteamRoot = (Get-ItemProperty -LiteralPath "HKCU:\Software\Valve\Steam" -ErrorAction Stop).SteamPath
}
catch {
    $SteamRoot = ""
}
$DefaultCs2 = if ($SteamRoot) {
    Join-Path $SteamRoot "steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe"
} else { "" }
$ProgramFilesCs2 = if (${env:ProgramFiles(x86)}) {
    Join-Path ${env:ProgramFiles(x86)} "Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe"
} else { "" }
$Cs2Exe = Resolve-Executable -ExplicitPath $Cs2Path -CommandName "cs2" -Fallbacks @(
    $DefaultCs2,
    $ProgramFilesCs2
)
if (-not $Cs2Exe) {
    throw "CS2 was not found in the primary Steam library; rerun with -Cs2Path <path-to-cs2.exe>."
}

if ($VenvCreated) { Write-Host "CREATED worker virtual environment: $VenvRoot" }
if ($InstallDependencies) {
    Write-Host "INSTALLED pinned dependencies from: $Requirements"
} else {
    Write-Host "No packages were installed; existing pinned dependencies were verified."
}
Write-Host "OK Python: $VenvPython ($VenvVersion)"
Write-Host "OK dependency: $DependencyVersion"
Write-Host "OK CS Demo Manager: $CsdmExe ($CsdmVersion)"
Write-Host "OK PostgreSQL client: $PsqlExe ($($PsqlProbe.Output))"
Write-Host "OK CS2: $Cs2Exe"
Write-Host "Next: copy config.example.json to config.json, set csdm_executable, then run setup-secret.ps1."
