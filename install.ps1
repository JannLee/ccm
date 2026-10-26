[CmdletBinding()]
param(
    [string]$Python,
    [string]$BinDir = $env:CCM_BIN_DIR,
    [switch]$NoPath
)

$ErrorActionPreference = 'Stop'
if (-not $BinDir) { $BinDir = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.local\bin' }
$BinDir = [IO.Path]::GetFullPath($BinDir)
$ccmScript = Join-Path $PSScriptRoot 'ccm'
$candidates = if ($Python) { @($Python) } else { @('py', 'python', 'python3') }
$pythonPath = $null
foreach ($candidate in $candidates) {
    $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $prefix = if ($command.Name -in @('py', 'py.exe')) { @('-3') } else { @() }
    try {
        $found = & $command.Source @prefix -c 'import json, sys; sys.exit(1) if sys.version_info < (3, 8) else print(json.dumps(sys.executable))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) {
            $resolvedPython = [string]($found | ConvertFrom-Json)
            if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) { continue }
            $pythonPath = $resolvedPython
            break
        }
    } catch { continue }
}
if (-not $pythonPath) { throw 'Python 3.8 이상을 찾지 못했습니다. Python 설치 후 다시 실행하거나 -Python 경로를 지정하세요.' }

$previousBin = $env:CCM_BIN_DIR
try {
    $env:CCM_BIN_DIR = $BinDir
    & $pythonPath -X utf8 $ccmScript install
    if ($LASTEXITCODE -ne 0) { throw 'ccm 실행 파일을 설치하지 못했습니다.' }
} finally {
    $env:CCM_BIN_DIR = $previousBin
}

if (-not $NoPath) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $entries = @($userPath -split ';' | Where-Object { $_ })
    if (@($entries | ForEach-Object { $_.TrimEnd('\') }) -notcontains $BinDir.TrimEnd('\')) {
        [Environment]::SetEnvironmentVariable('Path', (($entries + $BinDir) -join ';'), 'User')
    }
}
if (($env:Path -split ';').TrimEnd('\') -notcontains $BinDir.TrimEnd('\')) { $env:Path = "$BinDir;$env:Path" }
Write-Host "설치 완료: $BinDir\ccm.cmd 및 ccm.ps1"
Write-Host '새 PowerShell에서 ccm setup 실행 후 안내에 따라 프로필을 다시 불러오세요.'
