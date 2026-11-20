# Output only the path, so callers can use: $py = & "$baker\resolve_python.ps1"
$ErrorActionPreference = 'Stop'

function Get-InterpreterFile([string] $path) {
    if (-not $path) { return $null }
    $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    if ($item -is [IO.FileInfo] -and
        $item.FullName -notmatch '\\Microsoft\\WindowsApps\\python(?:\d+(?:\.\d+)*)?\.exe$') {
        return $item.FullName
    }
    return $null
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$configFile = if ($env:WH3_VFX_CONFIG) {
    $env:WH3_VFX_CONFIG
} elseif (Test-Path -LiteralPath (Join-Path $repoRoot 'wh3_vfx_config.json') -PathType Leaf) {
    Join-Path $repoRoot 'wh3_vfx_config.json'
} else {
    Join-Path $PSScriptRoot 'wh3_vfx_config.json'
}

$value = $env:PYTHON_EXE
$source = 'environment PYTHON_EXE'
if (-not $value -and (Test-Path -LiteralPath $configFile -PathType Leaf)) {
    $cfg = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfg.PSObject.Properties.Name -contains 'PYTHON_EXE') {
        $value = $cfg.PYTHON_EXE
        $source = (Get-Item -LiteralPath $configFile).FullName
    }
}
if ($value) {
    $path = [Environment]::ExpandEnvironmentVariables([string]$value)
    if ($path -match '^~(?:[\\/]|$)') { $path = $HOME + $path.Substring(1) }
    if (-not $env:PYTHON_EXE -and -not [IO.Path]::IsPathRooted($path)) {
        $path = Join-Path (Split-Path -Parent $source) $path
    }
    $exe = Get-InterpreterFile $path
    if (-not $exe) { throw "Invalid Python in $source (missing file or Store alias): $path" }
    return $exe
}

# Prefer repository virtual environment created by install.ps1
foreach ($candidate in @(
    (Join-Path $repoRoot '.venv\Scripts\python.exe'),
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
)) {
    $exe = Get-InterpreterFile $candidate
    if ($exe) { return $exe }
}

foreach ($cmd in @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
    $exe = Get-InterpreterFile $cmd.Source
    if ($exe) { return $exe }
}

throw 'No usable Python found. Run install.ps1 to create .venv, or set PYTHON_EXE in wh3_vfx_config.json.'
