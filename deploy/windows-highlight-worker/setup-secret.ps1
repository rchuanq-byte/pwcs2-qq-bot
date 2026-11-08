[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Secure = Read-Host "Highlight worker shared secret (at least 32 characters)" -AsSecureString
$Entropy = [Text.Encoding]::UTF8.GetBytes("NiuniuHighlightWorker/v1")
$Bstr = [IntPtr]::Zero
$Chars = $null
$Plain = $null
try {
    $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    [int]$CharCount = [Runtime.InteropServices.Marshal]::ReadInt32($Bstr, -4) / 2
    if ($CharCount -lt 32) { throw "Secret must contain at least 32 characters." }
    $Chars = [char[]]::new($CharCount)
    [Runtime.InteropServices.Marshal]::Copy($Bstr, $Chars, 0, $CharCount)
    $Plain = [Text.Encoding]::UTF8.GetBytes($Chars)
    $Protected = [Security.Cryptography.ProtectedData]::Protect(
        $Plain,
        $Entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    [IO.File]::WriteAllBytes((Join-Path $Root "worker-secret.dpapi"), $Protected)
    Write-Host "Saved a CurrentUser-DPAPI protected worker secret."
}
finally {
    if ($null -ne $Plain) {
        [Array]::Clear($Plain, 0, $Plain.Length)
    }
    if ($null -ne $Chars) {
        [Array]::Clear($Chars, 0, $Chars.Length)
    }
    if ($Bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
    }
}
