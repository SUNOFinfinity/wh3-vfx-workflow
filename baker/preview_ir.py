"""Stage-1 Sandbox L1+L2 Preview Runner for WH3 Route-A VFX IR (v1.0).

Decouples VFX authoring & visual sign-off from target MOD .pack baking:
  - NEVER touches or modifies any production/target MOD .pack!
  - Runs L1-A (spec_validator --ir)
  - Compiles IR through the exact production binary compilers (DDS/RMV2/Material/VFX-XML)
    into a tiny lightweight standalone sandbox PFH5 pack (<10MB)
  - Runs L1-B (spec_validator --pack) to prove RMV2 pivot==(0,0,0), BC7 zero-leak border,
    3-tier quality sync, and 100% asset closure
  - Runs L2 (wh3_true_preview.py) directly from that compiled binary pack and MANDATORILY
    produces:
      * <effect_id>_l2_preview.mp4  (L1+L2 verified WH3-shader + Bloom/Tonemap video!)
      * <effect_id>_l2_sheet.png    (Tactical + Ground Close-Up 6-phase sheet)
      * <effect_id>_l2_diag.png     (Step1 Shader+Blend vs Step2A Bloom vs Step2B Reinhard)
      * <effect_id>_l2_telemetry.json & <effect_id>_preview_signoff.json

Once the user inspects the L1+L2 video and approves it, hand the exact same IR directory
to Stage-2 (`bake_from_ir.py` / `wh3-vfx-bake-pack`) to bake into the target MOD .pack.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from environment_presets import NAMES as ENV_NAMES
from baker_config import PYTHON_EXE

from bake_from_ir import (
    _compile_emitters_and_wrappers,
    _compile_materials,
    _compile_meshes,
    _compile_textures,
)
from db_compiler import build_particle_effects_table, registration_keys
from pfh5 import write_standalone_pfh5
from spec_validator import validate_ir, validate_pack


def main():
    ap = argparse.ArgumentParser(description="Stage-1 WH3 Route-A IR -> L1+L2 Verified Preview Video (Zero MOD Pack Modification)")
    ap.add_argument("--ir", required=True, help="IR directory containing manifest.json")
    ap.add_argument("--out-dir", help="Output directory for L1 reports, L2 images, and L2 MP4 video")
    ap.add_argument("--env", default="daylight", choices=ENV_NAMES, help="WH3 environment preset")
    ap.add_argument("--cam-mode", default="auto", choices=["auto", "beam", "ground", "close"], help="Camera framing preset")
    ap.add_argument("--supersample", type=int, choices=[1, 2], default=1, help="Internal SSAA factor (2 = TAA-like anti-aliasing)")
    ap.add_argument("--fps", type=int, default=24, help="Preview MP4 frame rate (default: 24)")
    ap.add_argument("--width", type=int, default=640, help="Per-view width (video outputs 2*width side-by-side)")
    ap.add_argument("--height", type=int, default=360, help="Per-view height")
    ap.add_argument("--compare-env", choices=ENV_NAMES, nargs="+", help="Render additional sheets from the same sandbox, pose, camera and slope")
    ap.add_argument("--host-pack",help="Read-only host CSC baseline, used only if host_csc is declared")
    ap.add_argument("--keep-sandbox-pack", action="store_true", help="Keep lightweight _sandbox_preview.pack on disk")
    ap.add_argument("--l3-sandbox", action="store_true", help="Export dedicated standalone L3 sandbox pack + Terry/AssetEditor launch guide (zero MOD pack modification)")
    ap.add_argument("--l3-test-wrapper", help="Optional vanilla/test wrapper vfx_key alias to inject ONLY inside the standalone L3 sandbox pack (e.g. wh2_dlc12_abilities_burning_alignment)")
    ap.add_argument("--export-loose", action="store_true", help="With --l3-sandbox, also export loose compiled vfx/ and db/ files for direct Assembly Kit / Terry loading")
    ap.add_argument("--l3-only", action="store_true", help="Build & L1-verify the standalone L3 sandbox pack only (skip re-rendering L2 MP4 video)")
    ap.add_argument("--slope-deg",type=float, default=0.)
    args = ap.parse_args()
    if not -35<=args.slope_deg<=35:ap.error("--slope-deg range: -35..35")

    # 1. Gate L1-A: IR Preflight Validation
    print("[Stage-1 / L1-A] Running preflight spec validation on IR...")
    ir_rep = validate_ir(args.ir)
    ir_rep.print_summary()
    if not ir_rep.passed:
        print("[ABORT] IR failed L1-A validation. Fix errors above before rendering L2 preview.")
        sys.exit(1)

    with open(os.path.join(args.ir, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)

    from host_checks import check_hosts
    host_evidence=check_hosts(manifest,args.host_pack)
    eff_id = manifest["meta"]["effect_id"]
    out_dir = args.out_dir or os.path.join(os.path.dirname(os.path.abspath(args.ir)), f"preview_{eff_id}")
    from pfh5 import guard_output
    guard_output(out_dir)
    staging_dir = os.path.join(out_dir, "_staging")
    os.makedirs(staging_dir, exist_ok=True)

    ir_rep_path = os.path.join(out_dir, f"{eff_id}_l1_ir_report.json")
    with open(ir_rep_path, "w", encoding="utf-8") as f:
        json.dump(ir_rep.to_dict(), f, indent=2, ensure_ascii=False)

    # 2. Exact Production Binary Compilation into Lightweight Standalone Sandbox Pack
    print("[Stage-1 / Sandbox Compile] Compiling DDS/RMV2/Material/VFX-XML into standalone sandbox pack...")
    pack_assets, tex_vpaths = _compile_textures(args.ir, manifest, staging_dir)
    mat_assets, mat_vpaths = _compile_materials(manifest, tex_vpaths)
    pack_assets.update(mat_assets)
    mesh_assets, mesh_wsmodels = _compile_meshes(args.ir, manifest, mat_vpaths)
    pack_assets.update(mesh_assets)
    vfx_assets, lib_keys, wrapper_keys = _compile_emitters_and_wrappers(manifest, tex_vpaths, mesh_wsmodels,args.ir)
    pack_assets.update(vfx_assets)
    db_slice = f"db\\particle_effects_tables\\{manifest['meta']['prefix']}_{eff_id}"
    pack_assets[db_slice] = build_particle_effects_table(registration_keys(manifest,lib_keys))

    # Optional L3 test-wrapper alias (injected ONLY into the isolated standalone sandbox pack!)
    l3_alias_key = (args.l3_test_wrapper or "").strip()
    if l3_alias_key and wrapper_keys:
        primary_wpath = f"vfx\\{wrapper_keys[0]}.xml"
        if primary_wpath in pack_assets:
            pack_assets[f"vfx\\{l3_alias_key}.xml"] = pack_assets[primary_wpath]
            if l3_alias_key not in wrapper_keys:
                wrapper_keys = list(wrapper_keys) + [l3_alias_key]

    sandbox_pack = os.path.join(out_dir, f"{eff_id}_sandbox_preview.pack")
    pfh_stats = write_standalone_pfh5(sandbox_pack, pack_assets)
    print(f"[Stage-1 / Sandbox PFH5] Built lightweight sandbox pack ({pfh_stats['pack_bytes'] / 1024:.1f} KB, {pfh_stats['total_entries']} entries)")

    # 3. Gate L1-B: Binary Pack Readback Audit
    print("[Stage-1 / L1-B] Auditing compiled sandbox pack binaries (RMV2 pivot=0, BC7 zero-border, XML closure)...")
    pk_rep = validate_pack(sandbox_pack, wrapper_keys, check_dds_borders=True)
    pk_rep.print_summary()
    pk_rep_path = os.path.join(out_dir, f"{eff_id}_l1_pack_report.json")
    with open(pk_rep_path, "w", encoding="utf-8") as f:
        json.dump(pk_rep.to_dict(), f, indent=2, ensure_ascii=False)
    if not pk_rep.passed:
        print("[ABORT] Compiled sandbox pack failed L1-B binary validation!")
        sys.exit(1)

    # 3b. Optional Dedicated L3 Standalone Sandbox Bundle (Zero Modification to Production MOD .pack!)
    l3_artifacts = {}
    if args.l3_sandbox or args.l3_only:
        import shutil
        l3_dir = os.path.join(out_dir, "l3_sandbox")
        os.makedirs(l3_dir, exist_ok=True)
        l3_pack = os.path.join(l3_dir, f"{eff_id}_l3_sandbox.pack")
        shutil.copy2(sandbox_pack, l3_pack)
        l3_artifacts["l3_sandbox_pack"] = os.path.abspath(l3_pack)

        if args.export_loose:
            loose_root = os.path.join(l3_dir, "loose")
            for rel_p, blob in pack_assets.items():
                dst = os.path.join(loose_root, rel_p.replace("/", os.sep).replace("\\", os.sep))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as lf:
                    lf.write(blob)
            l3_artifacts["l3_loose_dir"] = os.path.abspath(loose_root)

        guide_path = os.path.join(l3_dir, f"{eff_id}_l3_launch_guide.txt")
        w_list = "\n".join(f"  - vfx\\{wk}.xml" for wk in wrapper_keys)
        guide_txt = (
            f"=== WH3 L3 Standalone Sandbox Verification Guide ({eff_id}) ===\n"
            f"Sandbox Pack ({pfh_stats['pack_bytes'] / 1024:.1f} KB, {pfh_stats['total_entries']} entries):\n"
            f"  {os.path.abspath(l3_pack)}\n\n"
            f"Wrappers inside sandbox pack:\n{w_list}\n\n"
            f"Option A (Recommended - AssetEditor -> Terry Real Engine Preview):\n"
            f"  1. Open your installed AssetEditor\n"
            f"  2. Open the sandbox pack above (zero impact on your main MOD .pack!)\n"
            f"  3. Open vfx\\{wrapper_keys[0]}.xml and launch Terry Preview\n\n"
            f"Option B (Standalone Custom Battle Test):\n"
            f"  - Temporarily load ONLY this small sandbox .pack in your Mod Manager"
            + (f" (overrides test wrapper: {l3_alias_key})" if l3_alias_key else "")
            + ", test in Custom Battle, then uncheck/delete when done.\n"
            + (f"\nOption C (Loose Files for Assembly Kit working_data):\n  {os.path.abspath(l3_artifacts['l3_loose_dir'])}\n" if "l3_loose_dir" in l3_artifacts else "")
            + "\nSafety: Production MOD .pack untouched; game data/ untouched; no auto-launch.\n"
        )
        with open(guide_path, "w", encoding="utf-8") as gf:
            gf.write(guide_txt)
        l3_artifacts["l3_launch_guide_txt"] = os.path.abspath(guide_path)
        print(f"[Stage-1 / L3 Sandbox] Ready -> {l3_pack} ({pfh_stats['pack_bytes'] / 1024:.1f} KB)")

    # 4. Gate L2: Pack-driven WH3 GPU Preview + MANDATORY MP4 Video
    out_sheet = os.path.join(out_dir, f"{eff_id}_l2_sheet.png")
    out_diag = os.path.join(out_dir, f"{eff_id}_l2_diag.png")
    out_video = os.path.join(out_dir, f"{eff_id}_l2_preview.mp4")
    out_tel = os.path.join(out_dir, f"{eff_id}_l2_telemetry.json")

    if not args.l3_only:
        print("[Stage-1 / L2] Rendering Pack-driven WH3 Preview Sheet + Diagnostic Strip + MP4 Video...")
        dur = float(manifest["meta"]["duration_sec"])
        phases = manifest["meta"].get("preview_phases_sec") or [round(dur * f, 2) for f in (0.06, 0.22, 0.40, 0.58, 0.78, 0.94)]

        cmd = [
            PYTHON_EXE, os.path.join(HERE, "wh3_true_preview.py"),
            "--pack", sandbox_pack,
            "--wrappers", ",".join(wrapper_keys),
            "--out-sheet", out_sheet,
            "--diagnostic-strip", out_diag,
            "--video", out_video,
            "--telemetry-out", out_tel,
            "--env", args.env, "--slope-deg", str(args.slope_deg),
            "--cam-mode", args.cam_mode,
            "--supersample", str(args.supersample),
            "--times", ",".join(str(x) for x in phases),
            "--diag-time", str(phases[len(phases) // 2]),
            "--width", str(args.width),
            "--height", str(args.height),
            "--video-duration", str(dur),
            "--video-fps", str(args.fps),
        ]
        if any(w.get("terrain_attach") for w in manifest["host_binding"]["wrappers"]):
            cmd.append("--sample-spawn-box")
        if args.cam_mode == "auto" and "preview_framing_m" in manifest["meta"]:
            cmd.extend(["--framing-m", *map(str, manifest["meta"]["preview_framing_m"])])
        res = subprocess.run(cmd)
        if res.returncode != 0 or not os.path.isfile(out_video):
            print("[FAIL] L2 True WH3 preview video rendering failed!")
            sys.exit(res.returncode or 1)

    comparisons={}
    if not args.l3_only and args.compare_env:
        for env_name in dict.fromkeys(args.compare_env):
            if env_name==args.env:
                comparisons[env_name]=os.path.abspath(out_sheet);continue
            extra=list(cmd)
            for flag in ("--video","--video-duration","--video-fps","--diagnostic-strip"):
                i=extra.index(flag);del extra[i:i+2]
            sheet=os.path.join(out_dir,f"{eff_id}_l2_{env_name}.png")
            for key,value in (("--env",env_name),("--out-sheet",sheet),("--telemetry-out",os.path.splitext(sheet)[0]+".json")):extra[extra.index(key)+1]=value
            subprocess.run(extra,check=True);comparisons[env_name]=os.path.abspath(sheet)
    if not args.keep_sandbox_pack and os.path.isfile(sandbox_pack):
        try:
            os.remove(sandbox_pack)
        except OSError:
            pass

    artifacts = {
        "l1_ir_report_json": os.path.abspath(ir_rep_path),
        "l1_pack_report_json": os.path.abspath(pk_rep_path),
    }
    if not args.l3_only:
        artifacts.update({
            "video_mp4": os.path.abspath(out_video),
            "qa_sheet_png": os.path.abspath(out_sheet),
            "diagnostic_strip_png": os.path.abspath(out_diag),
            "telemetry_json": os.path.abspath(out_tel),
        })
    artifacts.update(l3_artifacts)

    omissions=[]
    if not args.l3_only:
        with open(out_tel,encoding="utf8") as f: omissions=json.load(f)["omissions"]
    signoff = {
        "effect_id": eff_id,
        "ir_dir": os.path.abspath(args.ir),
        "verified_host_scenes":host_evidence,
        "archetype": manifest["archetype"],
        "wrappers": wrapper_keys,
        "env_preset": args.env, "comparison_sheets":comparisons, "slope_deg":args.slope_deg,
        "camera":args.cam_mode,"width":args.width,"height":args.height,"supersample":args.supersample,
        "l1_ir_status": "PASS",
        "l1_pack_status": "PASS",
        "l2_preview_status": "SKIPPED_L3_ONLY" if args.l3_only else ("PARTIAL" if omissions else "PASS"),
        "omissions":omissions,
        "l3_sandbox_ready": bool(l3_artifacts),
        "artifacts": artifacts,
        "target_mod_pack_modified": False,
        "next_step": "Review video_mp4 + diagnostic_strip_png (and optionally test l3_sandbox_pack in AssetEditor/Terry). When approved, invoke wh3-vfx-bake-pack (bake_from_ir.py) to bake into target MOD .pack.",
    }
    signoff_path = os.path.join(out_dir, f"{eff_id}_preview_signoff.json")
    with open(signoff_path, "w", encoding="utf-8") as f:
        json.dump(signoff, f, indent=2, ensure_ascii=False)

    print("\n=== STAGE-1 PREVIEW COMPLETE (ZERO MOD PACK MODIFIED) ===")
    if not args.l3_only:
        print(f"  L2 Preview Video : {out_video}")
        print(f"  L2 QA Sheet      : {out_sheet}")
        print(f"  L2 Diag Strip    : {out_diag}")
    if "l3_sandbox_pack" in l3_artifacts:
        print(f"  L3 Sandbox Pack  : {l3_artifacts['l3_sandbox_pack']}")
        print(f"  L3 Launch Guide  : {l3_artifacts['l3_launch_guide_txt']}")
    if "l3_loose_dir" in l3_artifacts:
        print(f"  L3 Loose Dir     : {l3_artifacts['l3_loose_dir']}")
    print(f"  Signoff Summary  : {signoff_path}")


if __name__ == "__main__":
    main()
