"""Run capability probes; stdout JSON, stderr short human-readable summary. No game process."""
import argparse
import ctypes
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import baker_config as cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, help="Optional path to save full JSON report")
    ap.add_argument(
        "--profile",
        choices=("basic", "action"),
        default="basic",
        help="basic = VFX author/preview/bake workflow; action = also require RPFM/AssetEditor for action workflow",
    )
    args = ap.parse_args()
    require_action = args.profile == "action"
    checks = []

    def probe(key, fn, fix, required=True):
        try:
            result = fn()
            row = dict(check=key, status="PASS", detail=result)
        except Exception as e:
            row = dict(check=key, status="FAIL" if required else "UNAVAILABLE", detail=str(e), fix=fix)
        checks.append(row)
        print(f"{row['status']:11} {key}: {row['detail']}", file=sys.stderr)

    def version():
        exe = Path(cfg.WH3_GAME_DIR) / "Warhammer3.exe"
        v = ctypes.windll.version
        size = v.GetFileVersionInfoSizeW(str(exe), None)
        if not size:
            raise OSError(str(exe))
        data = ctypes.create_string_buffer(size)
        if not v.GetFileVersionInfoW(str(exe), 0, size, data):
            raise OSError("GetFileVersionInfoW")
        ptr = ctypes.c_void_p()
        n = ctypes.c_uint()
        if not v.VerQueryValueW(data, "\\", ctypes.byref(ptr), ctypes.byref(n)):
            raise OSError("VerQueryValueW")
        a, b = (ctypes.c_uint32 * 4).from_address(ptr.value)[2:4]
        return ".".join(map(str, (a >> 16, a & 65535, b >> 16, b & 65535)))

    def pack(name):
        from pfh5 import PFH5Pack

        p = Path(cfg.WH3_DATA_DIR) / name
        k = PFH5Pack(p)
        k.read_raw(k.entries[0][0])
        return dict(bytes=p.stat().st_size, mtime_ns=p.stat().st_mtime_ns, entries=len(k.entries))

    def zstd():
        from pfh5 import decode_payload, zstd_backend, zstd_compress

        data = bytes(range(256)) * 32
        if decode_payload(zstd_compress(data), 1) != data:
            raise ValueError("round trip differs")
        return zstd_backend()

    def run(argv):
        res = subprocess.run(argv, capture_output=True)
        if res.returncode:
            raise RuntimeError((res.stderr + res.stdout)[-900:].decode(errors="replace"))

    def texconv(td):
        import numpy as np
        from PIL import Image
        from dds_compiler import encode_bc7_srgb_dds

        p = td / "check.dds"
        x = np.zeros((32, 32, 4), "f")
        x[8:24, 8:24] = 1
        encode_bc7_srgb_dds(x, str(p))
        if struct.unpack_from("<I", p.read_bytes(), 128)[0] != 99:
            raise ValueError("DXGI format != 99")
        run([cfg.TEXCONV_PATH, "-nologo", "-y", "-ft", "png", "-o", str(td), str(p)])
        with Image.open(p.with_suffix(".png")) as im:
            a = np.array(im.convert("RGBA"))
        if a[0, 0, 3] > 1 or a[16, 16, 3] < 254:
            raise ValueError("decoded alpha sample mismatch")
        return cfg.TEXCONV_PATH

    def ffmpeg(td):
        p = td / "check.mp4"
        run(
            [
                cfg.FFMPEG_PATH,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=32x32:rate=4",
                "-frames:v",
                "2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(p),
            ]
        )
        run([cfg.FFMPEG_PATH, "-v", "error", "-i", str(p), "-f", "null", "-"])
        return dict(bytes=p.stat().st_size, encoder="libx264")

    def gpu():
        from wh3_true_preview import WH3TrueRenderer

        r = WH3TrueRenderer(32, 32)
        r.gl.glGetString.argtypes = [ctypes.c_uint]
        r.gl.glGetString.restype = ctypes.c_char_p
        name = r.gl.glGetString(0x1F01).decode()
        ver = r.gl.glGetString(0x1F02).decode()
        if "Direct3D11" not in name or "OpenGL ES 3" not in ver:
            raise ValueError(name + " " + ver)
        return dict(renderer=name, version=ver, rgba16f_mrt="complete")

    def check_file(path):
        p = Path(path)
        if not str(path) or not p.is_file():
            raise FileNotFoundError(str(path) or "<unset>")
        return str(p)

    def check_dir(path):
        p = Path(path)
        if not str(path) or not p.is_dir():
            raise FileNotFoundError(str(path) or "<unset>")
        return str(p)

    probe("game_version", version, "Set WH3_GAME_DIR in wh3_vfx_config.json to the Total War: WARHAMMER III root")
    for n in "shaders vfx_desc vfx_models vfx_models_dds vfx data db".split():
        probe(n + ".pack", lambda n=n: pack(n + ".pack"), "Set WH3_DATA_DIR to the installed WH3 data directory")
    probe(
        "python",
        lambda: subprocess.check_output(
            [cfg.PYTHON_EXE, "-c", "import numpy,PIL,sys; print(sys.executable,sys.version,numpy.__version__,PIL.__version__)"],
            text=True,
        ).strip(),
        "Run install.ps1 to create .venv or set PYTHON_EXE to a Python with numpy and Pillow",
    )
    probe("zstd", zstd, "Install 'zstandard' in Python (pip install zstandard) or set ZSTD_DLL_PATH to zstd.dll")
    with tempfile.TemporaryDirectory() as tmp:
        probe("texconv BC7 roundtrip", lambda: texconv(Path(tmp)), "Run install.ps1 or set TEXCONV_PATH to DirectXTex texconv.exe")
        probe("ffmpeg H264", lambda: ffmpeg(Path(tmp)), "Run install.ps1 or set FFMPEG_PATH to an ffmpeg build with libx264")
    probe(
        "ANGLE GLES3 D3D11 RGBA16F",
        gpu,
        "Set ANGLE_DLL_DIR to a directory with 64-bit libEGL.dll & libGLESv2.dll; run on a D3D11-enabled Windows desktop",
    )

    # Action workflow (wh3-action-vfx-from-pack) tools
    probe(
        "rpfm_cli",
        lambda: check_file(cfg.RPFM_CLI),
        "For action workflow: run install.ps1 -WithActionTools or set RPFM_CLI in wh3_vfx_config.json",
        required=require_action,
    )
    probe(
        "RPFM WH3 schema",
        lambda: check_file(cfg.RPFM_SCHEMA),
        "For action workflow: run install.ps1 -WithActionTools or set RPFM_SCHEMA to schema_wh3.ron",
        required=require_action,
    )
    probe(
        "AssetEditor Shared.GameFormats.dll",
        lambda: check_file(Path(cfg.ASSETEDITOR_DIR) / "Shared.GameFormats.dll"),
        "For action workflow: set ASSETEDITOR_DIR to your AssetEditor folder containing Shared.GameFormats.dll",
        required=require_action,
    )
    probe(
        "WH3 AnimationSlots table",
        lambda: check_file(cfg.WH3_SLOT_FILE),
        "For action workflow: set ASSETEDITOR_DIR or WH3_SLOT_FILE to Warhammer3AnimationSlots*.txt",
        required=require_action,
    )

    # Optional: host_csc validation
    node = shutil.which("node")

    def node_runtime():
        if not node:
            raise FileNotFoundError("node not found on PATH")
        ver = subprocess.check_output([node, "--version"], text=True, timeout=10).strip()
        return {"path": node, "version": ver}

    def csc_codec():
        path = Path(cfg.CSC_CODEC_PATH)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if not node:
            raise RuntimeError(f"{path} exists; Node.js is needed to check its exports")
        js = (
            "import {pathToFileURL} from 'node:url';"
            "const m=await import(pathToFileURL(process.argv[1]).href);"
            "if(typeof m.parse!=='function'||typeof m.processCsc!=='function')"
            "throw Error('Missing CSC exports: parse / processCsc');"
        )
        run([node, "--input-type=module", "-e", js, "--", str(path)])
        return str(path)

    probe("node", node_runtime, "Optional (for host_csc): install Node.js 18+ on PATH", False)
    probe("CSC_CODEC_PATH", csc_codec, "Optional (for host_csc): bundled at baker/csc_codec/csc-codec.mjs", False)

    # Optional DCC / inspection tools
    hython = Path(cfg.HOUDINI_BIN_DIR) / "hython.exe" if cfg.HOUDINI_BIN_DIR else Path("")
    probe("hython", lambda: check_file(hython), "Optional (for Houdini scenes): install Houdini or set HOUDINI_BIN_DIR", False)
    probe("D3DDisassemble", lambda: check_file(Path(cfg.WH3_GAME_DIR) / "d3dcompiler_47.dll"), "Optional", False)
    probe("Assembly Kit", lambda: check_dir(Path(cfg.WH3_GAME_DIR) / "assembly_kit"), "Optional", False)

    report = dict(
        profile=args.profile,
        status="FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS",
        paths={k: {"path": getattr(cfg, k), "source": v} for k, v in cfg.sources.items()},
        checks=checks,
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    print(payload)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(payload, encoding="utf8")
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
