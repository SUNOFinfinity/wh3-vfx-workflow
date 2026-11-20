# Output only the path to hython.exe, so callers can use: $hy = & "$baker\resolve_hython.ps1"
$ErrorActionPreference = 'Stop'

$py = & (Join-Path $PSScriptRoot 'resolve_python.ps1')
$hython = & $py -c "import sys, pathlib; sys.path.insert(0, r'$PSScriptRoot'); import baker_config as c; p = pathlib.Path(c.HOUDINI_BIN_DIR) / 'hython.exe' if c.HOUDINI_BIN_DIR else pathlib.Path(); print(p if p.is_file() else '')"
if (-not $hython) {
    throw 'hython.exe not found. Install Houdini or set HOUDINI_BIN_DIR in wh3_vfx_config.json.'
}
return $hython.Trim()
