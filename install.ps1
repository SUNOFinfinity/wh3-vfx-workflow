<#
.SYNOPSIS
    One-click installer for wh3-vfx-workflow (Codex Skills + WH3 Route-A VFX Baker).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -WithActionTools
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -WithActionTools -AssetEditorDir "D:\Tools\AssetEditor"
#>
[CmdletBinding()]
param(
    [switch]$WithActionTools,
    [switch]$SkipDownloads,
    [switch]$SkipDoctor,
    [string]$GameDir = '',
    [string]$AssetEditorDir = '',
    [string]$RpfmCli = '',
    [string]$HoudiniBinDir = '',
    [string]$SkillsDir = (Join-Path $HOME '.codex\skills')
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RepoRoot  = $PSScriptRoot
$BakerDir  = Join-Path $RepoRoot 'baker'
$ToolsDir  = Join-Path $RepoRoot 'tools'
$VenvDir   = Join-Path $RepoRoot '.venv'
$ConfigFile = Join-Path $RepoRoot 'wh3_vfx_config.json'

function Write-Step([string]$Msg) { Write-Host "`n==> $Msg" -ForegroundColor Cyan }
function Write-Ok([string]$Msg)   { Write-Host "  [OK] $Msg" -ForegroundColor Green }
function Write-Warn([string]$Msg) { Write-Host "  [WARN] $Msg" -ForegroundColor Yellow }

function Get-RealPython {
    foreach ($cmd in @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        if ($cmd.Source -notmatch '\\Microsoft\\WindowsApps\\python') {
            return $cmd.Source
        }
    }
    foreach ($cmd in @(Get-Command py.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $resolved = & $cmd.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($resolved -and (Test-Path -LiteralPath $resolved.Trim())) {
            return $resolved.Trim()
        }
    }
    return $null
}

function Get-GitHubReleaseAsset([string]$Repo, [string]$Pattern) {
    $headers = @{ 'User-Agent' = 'wh3-vfx-workflow-installer' }
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $headers -TimeoutSec 30
    $asset = $rel.assets | Where-Object { $_.name -match $Pattern } | Select-Object -First 1
    if (-not $asset) {
        throw "No release asset matching '$Pattern' in $Repo ($($rel.tag_name))"
    }
    return $asset
}

function Download-File([string]$Url, [string]$OutFile) {
    $parent = Split-Path -Parent $OutFile
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Write-Host "  Downloading $Url ..."
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 180
}

Write-Host "============================================================" -ForegroundColor White
Write-Host "  wh3-vfx-workflow Installer (Codex Skills + VFX Baker)" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor White

New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null

# 1. Python virtual environment
Write-Step "1/6 Setting up Python environment (.venv)"
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPy)) {
    $sysPy = Get-RealPython
    if (-not $sysPy) {
        throw "No usable Python 3.10+ found. Please install Python from https://www.python.org/downloads/ (check 'Add python.exe to PATH') and re-run install.ps1."
    }
    Write-Host "  Creating virtual environment with $sysPy ..."
    & $sysPy -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv" }
}
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $RepoRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "Failed to install Python dependencies from requirements.txt" }
Write-Ok "Python ready: $VenvPy"

# 2. Core external tools: texconv & ffmpeg
Write-Step "2/6 Checking core tools (texconv, ffmpeg)"
$TexconvLocal = Join-Path $ToolsDir 'texconv\texconv.exe'
if ((Test-Path -LiteralPath $TexconvLocal) -or (Get-Command texconv.exe -ErrorAction SilentlyContinue)) {
    Write-Ok "texconv.exe already available"
} elseif (-not $SkipDownloads) {
    try {
        $asset = Get-GitHubReleaseAsset -Repo 'microsoft/DirectXTex' -Pattern '^texconv\.exe$'
        Download-File -Url $asset.browser_download_url -OutFile $TexconvLocal
        Write-Ok "Installed texconv.exe -> $TexconvLocal"
    } catch {
        Write-Warn "Auto-download of texconv.exe failed: $($_.Exception.Message)"
        Write-Warn "Manual fix: download texconv.exe from https://github.com/microsoft/DirectXTex/releases into $TexconvLocal"
    }
} else {
    Write-Warn "Skipped texconv.exe download (-SkipDownloads)"
}

$FfmpegLocal = Join-Path $ToolsDir 'ffmpeg\bin\ffmpeg.exe'
if ((Test-Path -LiteralPath $FfmpegLocal) -or (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
    Write-Ok "ffmpeg.exe already available"
} elseif (-not $SkipDownloads) {
    try {
        $zipPath = Join-Path $ToolsDir 'ffmpeg-essentials.zip'
        Download-File -Url 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile $zipPath
        $extractDir = Join-Path $ToolsDir '_ffmpeg_tmp'
        if (Test-Path -LiteralPath $extractDir) { Remove-Item -LiteralPath $extractDir -Recurse -Force }
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
        $found = Get-ChildItem -LiteralPath $extractDir -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
        if (-not $found) { throw "ffmpeg.exe not found inside archive" }
        New-Item -ItemType Directory -Path (Split-Path -Parent $FfmpegLocal) -Force | Out-Null
        Copy-Item -LiteralPath $found.FullName -Destination $FfmpegLocal -Force
        Remove-Item -LiteralPath $extractDir,$zipPath -Recurse -Force
        Write-Ok "Installed ffmpeg.exe -> $FfmpegLocal"
    } catch {
        Write-Warn "Auto-download of ffmpeg.exe failed: $($_.Exception.Message)"
        Write-Warn "Manual fix: install ffmpeg (e.g. 'winget install Gyan.FFmpeg.Essentials') or place ffmpeg.exe at $FfmpegLocal"
    }
} else {
    Write-Warn "Skipped ffmpeg.exe download (-SkipDownloads)"
}

# 3. Optional Action Workflow tools (RPFM CLI + WH3 Schema + AssetEditor)
if ($WithActionTools) {
    Write-Step "3/6 Checking Action Workflow tools (RPFM CLI, WH3 Schema, AssetEditor)"
    $RpfmDir = Join-Path $ToolsDir 'rpfm'
    $RpfmLocal = Join-Path $RpfmDir 'rpfm_cli.exe'
    $SchemaLocal = Join-Path $RpfmDir 'schema_wh3.ron'
    $SchemaAppData = Join-Path $HOME 'AppData\Roaming\FrodoWazEre\rpfm\config\schemas\schema_wh3.ron'

    if ($RpfmCli -and (Test-Path -LiteralPath $RpfmCli)) {
        Write-Ok "Using provided RPFM_CLI: $RpfmCli"
    } elseif ((Test-Path -LiteralPath $RpfmLocal) -or (Get-Command rpfm_cli.exe -ErrorAction SilentlyContinue)) {
        Write-Ok "rpfm_cli.exe already available"
    } elseif (-not $SkipDownloads) {
        try {
            $asset = Get-GitHubReleaseAsset -Repo 'Frodo45127/rpfm' -Pattern 'rpfm-v.*-x86_64-pc-windows-msvc\.zip$'
            $zipPath = Join-Path $ToolsDir 'rpfm.zip'
            Download-File -Url $asset.browser_download_url -OutFile $zipPath
            $tmp = Join-Path $ToolsDir '_rpfm_tmp'
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
            Expand-Archive -LiteralPath $zipPath -DestinationPath $tmp -Force
            $foundCli = Get-ChildItem -LiteralPath $tmp -Recurse -Filter 'rpfm_cli.exe' | Select-Object -First 1
            if (-not $foundCli) { throw "rpfm_cli.exe not found inside archive" }
            New-Item -ItemType Directory -Path $RpfmDir -Force | Out-Null
            Copy-Item -LiteralPath $foundCli.FullName -Destination $RpfmLocal -Force
            Remove-Item -LiteralPath $tmp,$zipPath -Recurse -Force
            Write-Ok "Installed rpfm_cli.exe -> $RpfmLocal"
        } catch {
            Write-Warn "Auto-download of rpfm_cli.exe failed: $($_.Exception.Message)"
            Write-Warn "Manual fix: download RPFM from https://github.com/Frodo45127/rpfm/releases and place rpfm_cli.exe at $RpfmLocal"
        }
    }

    if ((Test-Path -LiteralPath $SchemaLocal) -or (Test-Path -LiteralPath $SchemaAppData)) {
        Write-Ok "schema_wh3.ron already available"
    } elseif (-not $SkipDownloads) {
        try {
            Download-File -Url 'https://raw.githubusercontent.com/Frodo45127/rpfm-schemas/master/schemas/schema_wh3.ron' -OutFile $SchemaLocal
            Write-Ok "Installed schema_wh3.ron -> $SchemaLocal"
        } catch {
            Write-Warn "Auto-download of schema_wh3.ron failed: $($_.Exception.Message)"
            Write-Warn "Manual fix: download schema_wh3.ron from https://github.com/Frodo45127/rpfm-schemas into $SchemaLocal"
        }
    }

    $AeLocal = Join-Path $ToolsDir 'AssetEditor'
    if ($AssetEditorDir -and (Test-Path -LiteralPath (Join-Path $AssetEditorDir 'Shared.GameFormats.dll'))) {
        Write-Ok "Using provided AssetEditorDir: $AssetEditorDir"
    } elseif (Test-Path -LiteralPath (Join-Path $AeLocal 'Shared.GameFormats.dll')) {
        Write-Ok "AssetEditor already available at $AeLocal"
    } elseif (-not $SkipDownloads) {
        try {
            $asset = Get-GitHubReleaseAsset -Repo 'olekristianhomelien/TheAssetEditor' -Pattern '\.zip$'
            $zipPath = Join-Path $ToolsDir 'AssetEditor.zip'
            Download-File -Url $asset.browser_download_url -OutFile $zipPath
            $tmp = Join-Path $ToolsDir '_ae_tmp'
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
            Expand-Archive -LiteralPath $zipPath -DestinationPath $tmp -Force
            $dll = Get-ChildItem -LiteralPath $tmp -Recurse -Filter 'Shared.GameFormats.dll' | Select-Object -First 1
            if (-not $dll) { throw "Shared.GameFormats.dll not found inside AssetEditor archive" }
            if (Test-Path -LiteralPath $AeLocal) { Remove-Item -LiteralPath $AeLocal -Recurse -Force }
            Copy-Item -LiteralPath $dll.Directory.FullName -Destination $AeLocal -Recurse -Force
            Remove-Item -LiteralPath $tmp,$zipPath -Recurse -Force
            Write-Ok "Installed AssetEditor -> $AeLocal"
        } catch {
            Write-Warn "Auto-download of AssetEditor failed: $($_.Exception.Message)"
            Write-Warn "Manual fix: extract AssetEditor into $AeLocal or pass -AssetEditorDir <path>"
        }
    }
} else {
    Write-Step "3/6 Action Workflow tools skipped (pass -WithActionTools if you use wh3-action-vfx-from-pack)"
}

# 4. Write or merge wh3_vfx_config.json
Write-Step "4/6 Updating wh3_vfx_config.json"
$cfg = [ordered]@{}
if (Test-Path -LiteralPath $ConfigFile -PathType Leaf) {
    $existing = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($p in $existing.PSObject.Properties) {
        $cfg[$p.Name] = $p.Value
    }
}
$cfg['PYTHON_EXE'] = '.venv/Scripts/python.exe'
if ($GameDir)        { $cfg['WH3_GAME_DIR']    = $GameDir }
if ($AssetEditorDir) { $cfg['ASSETEDITOR_DIR'] = $AssetEditorDir }
if ($RpfmCli)        { $cfg['RPFM_CLI']        = $RpfmCli }
if ($HoudiniBinDir)  { $cfg['HOUDINI_BIN_DIR'] = $HoudiniBinDir }

$jsonText = $cfg | ConvertTo-Json -Depth 4
[IO.File]::WriteAllText($ConfigFile, $jsonText + "`n", [Text.UTF8Encoding]::new($false))
Write-Ok "Saved config -> $ConfigFile"

# 5. Install Codex Skills & register WH3_VFX_BAKER
Write-Step "5/6 Installing Codex Skills to $SkillsDir"
New-Item -ItemType Directory -Path $SkillsDir -Force | Out-Null
$SkillNames = @(
    'wh3-vfx-author-preview',
    'wh3-vfx-bake-pack',
    'wh3-action-vfx-from-pack',
    'vfx-art-direction'
)
foreach ($name in $SkillNames) {
    $src = Join-Path $RepoRoot "skills\$name"
    $dst = Join-Path $SkillsDir $name
    if (Test-Path -LiteralPath $dst) {
        Remove-Item -LiteralPath $dst -Recurse -Force
    }
    Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
    Write-Ok "Installed skill: $name"
}

$env:WH3_VFX_BAKER = $BakerDir
[Environment]::SetEnvironmentVariable('WH3_VFX_BAKER', $BakerDir, 'User')
Write-Ok "Set user environment variable WH3_VFX_BAKER = $BakerDir"

# 6. Run doctor.py verification
Write-Step "6/6 Running environment diagnostics (doctor.py)"
if ($SkipDoctor) {
    Write-Warn "Skipped doctor.py (-SkipDoctor)"
} else {
    New-Item -ItemType Directory -Path (Join-Path $RepoRoot 'work') -Force | Out-Null
    $profile = if ($WithActionTools) { 'action' } else { 'basic' }
    $null = & $VenvPy (Join-Path $BakerDir 'doctor.py') --profile $profile --json (Join-Path $RepoRoot 'work\doctor_report.json')
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "All required checks PASSED (profile=$profile)!"
    } else {
        Write-Warn "Some required checks failed (see stderr lines above or work\doctor_report.json)."
        Write-Warn "Fix the reported path(s) in wh3_vfx_config.json and re-run:"
        Write-Host "  `"$VenvPy`" `"$BakerDir\doctor.py`" --profile $profile" -ForegroundColor Gray
    }
}

Write-Host "`nInstallation finished. Restart Codex (or open a new terminal) so WH3_VFX_BAKER takes effect." -ForegroundColor Green
Write-Host "Next step (smoke test):" -ForegroundColor White
Write-Host "  `"$VenvPy`" `"$RepoRoot\examples\generate_smoke_ir.py`" --out `"$RepoRoot\work\smoke_ir`"" -ForegroundColor Gray
Write-Host "  `"$VenvPy`" `"$BakerDir\preview_ir.py`" --ir `"$RepoRoot\work\smoke_ir`" --out-dir `"$RepoRoot\work\smoke_preview`" --keep-sandbox-pack --env daylight --cam-mode ground --fps 12" -ForegroundColor Gray
