# install.ps1 - bootstrap the shared RightMemory installer on native Windows.

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
  $PSNativeCommandUseErrorActionPreference = $false
}

if ($args -contains "-h" -or $args -contains "--help") {
  Write-Host "Usage: .\install.ps1 [--mode cli-agent|standalone] [<memory-root> <skills-target>]"
  exit 0
}

function Write-Stderr([string] $Text) {
  [Console]::Error.WriteLine($Text)
}

function Test-NativeCommand([string] $Name, [string[]] $VersionArgs) {
  if ($null -eq (Get-Command $Name -ErrorAction SilentlyContinue)) {
    return $false
  }
  try {
    & $Name @VersionArgs > $null 2> $null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  }
}

function Print-UvInstallGuidance {
  Write-Stderr @"

RightMemory uses uv to provision its Python runtime.
Install uv, restart PowerShell if needed, then rerun .\install.ps1.

Windows:
  Install uv from the official guide, then make sure uv is on PATH.

Official uv install guide:
  https://docs.astral.sh/uv/getting-started/installation/
"@
}

function Print-GitInstallGuidance {
  Write-Stderr @"

RightMemory uses Git for inspectable memory changes, rollback, isolated worktrees, and sync.
Install Git for Windows, restart PowerShell if needed, then rerun .\install.ps1.

Official git install guide:
  https://git-scm.com/book/en/v2/Getting-Started-Installing-Git
"@
}

function Print-UvPythonGuidance {
  Write-Stderr @"

Could not find or provision Python >=3.11 with uv.

Make sure uv Python downloads are enabled, install Python 3.11+, or upgrade uv,
then rerun .\install.ps1.

uv Python guide:
  https://docs.astral.sh/uv/guides/install-python/
"@
}

Write-Host "Checking installer requirements..."
$missing = $false
if (Test-NativeCommand "git" @("--version")) {
  Write-Host "  [ok]      git"
} else {
  Write-Stderr "Missing or unusable required command: git"
  Print-GitInstallGuidance
  $missing = $true
}
if (Test-NativeCommand "uv" @("--version")) {
  Write-Host "  [ok]      uv"
} else {
  Write-Stderr "Missing or unusable required command: uv"
  Print-UvInstallGuidance
  $missing = $true
}
if ($missing) {
  exit 1
}

$bootstrapOutput = @(& uv python find --no-project ">=3.11" 2> $null)
if ($LASTEXITCODE -ne 0 -or $bootstrapOutput.Count -eq 0) {
  Print-UvPythonGuidance
  exit 1
}
$bootstrapPython = [string] $bootstrapOutput[0]
if ([string]::IsNullOrWhiteSpace($bootstrapPython) -or -not (Test-Path -LiteralPath $bootstrapPython -PathType Leaf)) {
  Print-UvPythonGuidance
  exit 1
}
Write-Host "  [ok]      Python >=3.11 via uv"
Write-Host ""

$localAppData = $env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) {
  $localAppData = Join-Path $HOME "AppData\Local"
}
$runtimeBin = Join-Path $localAppData "RightMemory\bin"
$originalPath = $env:Path
$firstPathEntry = @($env:Path -split ";")[0]
$pathChanged = -not [string]::Equals(
  $firstPathEntry.TrimEnd("\"),
  $runtimeBin.TrimEnd("\"),
  [System.StringComparison]::OrdinalIgnoreCase
)
if ($pathChanged) {
  $env:Path = "$runtimeBin;$env:Path"
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$previousPythonUtf8 = $env:PYTHONUTF8
$env:PYTHONUTF8 = "1"
$installExitCode = 1
Push-Location $repoRoot
try {
  & $bootstrapPython -m rightmemory.install_core @args
  $installExitCode = $LASTEXITCODE
} catch {
  Write-Stderr "Installer failed: $($_.Exception.Message)"
} finally {
  Pop-Location
  if ($null -eq $previousPythonUtf8) {
    Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
  } else {
    $env:PYTHONUTF8 = $previousPythonUtf8
  }
}
if ($installExitCode -ne 0) {
  $env:Path = $originalPath
} elseif ($pathChanged) {
  Write-Host @"

  [notice]  rightmemory is available in this PowerShell session.
            To put $runtimeBin first on PATH for future terminals and agents:

              `$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
              [Environment]::SetEnvironmentVariable("Path", "$runtimeBin;`$userPath", "User")
"@
}
exit $installExitCode
