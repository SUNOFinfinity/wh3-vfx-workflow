<#
.SYNOPSIS
    Removes wh3-vfx-workflow skills from Codex and clears the WH3_VFX_BAKER user env var.
#>
[CmdletBinding()]
param(
    [string]$SkillsDir = (Join-Path $HOME '.codex\skills')
)

$ErrorActionPreference = 'Stop'
$SkillNames = @(
    'wh3-vfx-author-preview',
    'wh3-vfx-bake-pack',
    'wh3-action-vfx-from-pack',
    'vfx-art-direction'
)

foreach ($name in $SkillNames) {
    $dst = Join-Path $SkillsDir $name
    if (Test-Path -LiteralPath $dst) {
        Remove-Item -LiteralPath $dst -Recurse -Force
        Write-Host "Removed skill: $dst" -ForegroundColor Yellow
    }
}

$BakerDir = Join-Path $PSScriptRoot 'baker'
$current = [Environment]::GetEnvironmentVariable('WH3_VFX_BAKER', 'User')
if ($current -eq $BakerDir) {
    [Environment]::SetEnvironmentVariable('WH3_VFX_BAKER', $null, 'User')
    Write-Host "Cleared user environment variable WH3_VFX_BAKER" -ForegroundColor Yellow
}

Write-Host "Uninstall complete. Local repository files were kept intact." -ForegroundColor Green
