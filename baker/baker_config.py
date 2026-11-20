"""Installation paths: environment > wh3_vfx_config.json > repo tools & system auto-discovery."""
import json
import os
import re
import shutil
import sys
from pathlib import Path

BAKER_DIR = Path(__file__).resolve().parent
REPO_ROOT = BAKER_DIR.parent
TOOLS_DIR = REPO_ROOT / "tools"


def _find_config_path() -> Path:
    env_cfg = os.getenv("WH3_VFX_CONFIG")
    if env_cfg:
        return Path(os.path.expandvars(env_cfg)).expanduser()
    for candidate in (REPO_ROOT / "wh3_vfx_config.json", BAKER_DIR / "wh3_vfx_config.json"):
        if candidate.is_file():
            return candidate
    return REPO_ROOT / "wh3_vfx_config.json"


config_path = _find_config_path()
settings = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {}
sources = {}


def resolve(key, candidates, fallback):
    value = os.environ.get(key) or settings.get(key)
    if value:
        sources[key] = "environment" if os.environ.get(key) else str(config_path)
        p = Path(os.path.expandvars(str(value))).expanduser()
        if not p.is_absolute() and not os.environ.get(key):
            p = config_path.parent / p
        return str(p.absolute())
    for p in candidates:
        if p and Path(p).exists():
            sources[key] = "detected"
            return str(Path(p).resolve())
    sources[key] = "unresolved"
    return str(fallback)


def steam_games():
    roots = [Path(os.getenv("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Steam"]
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.insert(0, Path(winreg.QueryValueEx(k, "SteamPath")[0]))
        except OSError:
            pass
    for root in roots.copy():
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.is_file():
            roots.extend(
                Path(s.replace("\\\\", "\\"))
                for s in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf8", errors="replace"))
            )
    games = (root / "steamapps/common/Total War WARHAMMER III" for root in roots)
    # A Steam library can contain the Assembly Kit alone. Only auto-detected
    # candidates are filtered; explicit overrides remain visible to doctor.
    return [p for p in games if (p / "Warhammer3.exe").is_file() and (p / "data/shaders.pack").is_file()]


def _houdini_candidates():
    out = []
    if os.getenv("HFS"):
        out.append(Path(os.environ["HFS"]) / "bin")
    hython = shutil.which("hython")
    if hython:
        out.append(Path(hython).parent)
    if (Path(sys.executable).parent.parent / "bin/hython.exe").is_file():
        out.append(Path(sys.executable).parent.parent / "bin")
    for base in (Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Side Effects Software",):
        if base.is_dir():
            bins = sorted(base.glob("Houdini */bin"), reverse=True)
            out.extend(p for p in bins if (p / "hython.exe").is_file())
    return out


def _angle_candidates(game_dir: str):
    candidates = [
        TOOLS_DIR / "angle",
        Path(game_dir),
    ]
    candidates.extend(Path(s) for s in os.get_exec_path() if s)
    # Chromium/Edge/Chrome installations on Windows commonly include ANGLE DLLs.
    for root in (
        Path(os.getenv("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application",
        Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Microsoft/Edge/Application",
        Path(os.getenv("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application",
        Path(os.getenv("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Google/Chrome/Application",
    ):
        if root.is_dir():
            candidates.extend(sorted((p for p in root.iterdir() if p.is_dir()), reverse=True))
    return [p for p in candidates if p.is_dir() and all((p / d).is_file() for d in ("libEGL.dll", "libGLESv2.dll"))]


def _asseteditor_candidates():
    out = []
    if os.getenv("ASSETEDITOR_PATH"):
        out.append(Path(os.environ["ASSETEDITOR_PATH"]).parent)
    if settings.get("ASSETEDITOR_PATH"):
        p = Path(os.path.expandvars(str(settings["ASSETEDITOR_PATH"]))).expanduser()
        if not p.is_absolute():
            p = config_path.parent / p
        out.append(p.parent)
    for name in ("AssetEditor", "AssetEditor.CN", "TheAssetEditor"):
        d = TOOLS_DIR / name
        if (d / "Shared.GameFormats.dll").is_file():
            out.append(d)
    return out


def _slot_file_candidates(asseteditor_dir: str):
    out = []
    slot_dir = Path(asseteditor_dir) / "Resources" / "AnimationSlots"
    preferred = slot_dir / "Warhammer3AnimationSlots_dlc24.txt"
    if preferred.is_file():
        out.append(preferred)
    if slot_dir.is_dir():
        out.extend(sorted(slot_dir.glob("Warhammer3AnimationSlots*.txt"), reverse=True))
    return out


WH3_GAME_DIR = resolve("WH3_GAME_DIR", steam_games(), Path("C:/Program Files (x86)/Steam/steamapps/common/Total War WARHAMMER III"))
WH3_DATA_DIR = resolve("WH3_DATA_DIR", [], Path(WH3_GAME_DIR) / "data")
PYTHON_EXE = resolve("PYTHON_EXE", [sys.executable], sys.executable)
HOUDINI_BIN_DIR = resolve("HOUDINI_BIN_DIR", _houdini_candidates(), "")
TEXCONV_PATH = resolve(
    "TEXCONV_PATH",
    [TOOLS_DIR / "texconv" / "texconv.exe", TOOLS_DIR / "texconv.exe", shutil.which("texconv")],
    TOOLS_DIR / "texconv" / "texconv.exe",
)
FFMPEG_PATH = resolve(
    "FFMPEG_PATH",
    [TOOLS_DIR / "ffmpeg" / "bin" / "ffmpeg.exe", TOOLS_DIR / "ffmpeg" / "ffmpeg.exe", shutil.which("ffmpeg")],
    TOOLS_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
)
ANGLE_DLL_DIR = resolve("ANGLE_DLL_DIR", _angle_candidates(WH3_GAME_DIR), TOOLS_DIR / "angle")
ZSTD_DLL_PATH = resolve(
    "ZSTD_DLL_PATH",
    [
        TOOLS_DIR / "zstd" / "zstd.dll",
        TOOLS_DIR / "zstd.dll",
        shutil.which("zstd.dll"),
        Path(WH3_GAME_DIR) / "zstd.dll",
        Path(HOUDINI_BIN_DIR) / "zstd.dll" if HOUDINI_BIN_DIR else None,
    ],
    TOOLS_DIR / "zstd" / "zstd.dll",
)
CSC_CODEC_PATH = resolve(
    "CSC_CODEC_PATH",
    [BAKER_DIR / "csc_codec" / "csc-codec.mjs"],
    BAKER_DIR / "csc_codec" / "csc-codec.mjs",
)

# Action workflow (wh3-action-vfx-from-pack) shared configuration
ASSETEDITOR_DIR = resolve("ASSETEDITOR_DIR", _asseteditor_candidates(), TOOLS_DIR / "AssetEditor")
RPFM_CLI = resolve(
    "RPFM_CLI",
    [TOOLS_DIR / "rpfm" / "rpfm_cli.exe", shutil.which("rpfm_cli")],
    TOOLS_DIR / "rpfm" / "rpfm_cli.exe",
)
RPFM_SCHEMA = resolve(
    "RPFM_SCHEMA",
    [
        TOOLS_DIR / "rpfm" / "schema_wh3.ron",
        Path.home() / "AppData/Roaming/FrodoWazEre/rpfm/config/schemas/schema_wh3.ron",
    ],
    TOOLS_DIR / "rpfm" / "schema_wh3.ron",
)
WH3_SLOT_FILE = resolve(
    "WH3_SLOT_FILE",
    _slot_file_candidates(ASSETEDITOR_DIR),
    Path(ASSETEDITOR_DIR) / "Resources/AnimationSlots/Warhammer3AnimationSlots_dlc24.txt",
)

WH3_VFX_MODELS_PACK = str(Path(WH3_DATA_DIR) / "vfx_models.pack")
WH3_VFX_DESC_PACK = str(Path(WH3_DATA_DIR) / "vfx_desc.pack")

# Keep the handle alive: closing/discarding it removes the DLL search path.
_dll = (
    os.add_dll_directory(HOUDINI_BIN_DIR)
    if HOUDINI_BIN_DIR and hasattr(os, "add_dll_directory") and Path(HOUDINI_BIN_DIR).is_dir()
    else None
)
