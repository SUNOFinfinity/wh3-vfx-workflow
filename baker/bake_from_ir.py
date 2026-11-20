"""Universal Data-Driven Route-A Baker for Total War: WARHAMMER III (v1.0).

Accepts ANY IR directory conforming to manifest.schema.json (all 5 archetypes:
sky_beam_vortex, projectile_trail, explosion_shockwave, aura_buff_debuff, fluid_shatter_vat)
and executes the full 3-gate production pipeline:
  1. L1-A Preflight Gate : spec_validator.py --ir <ir_dir>
  2. Universal Bake      : DDS + RMV2 (pivot=0) + WSModel + Material + Paged VFX XML + DB -> PFH5 .pack
                           (with immutable backup + SHA-256 readback verification)
  3. L1-B Pack Gate      : spec_validator.py --pack <target.pack> --wrappers <...>
  4. L2 WH3 True Preview : wh3_true_preview.py --pack <target.pack> --wrappers <...>
                           (Supported stock shader and HDR profiles; see omissions for partial output)

Never copies into game data/ and never launches Warhammer3.exe or Terry.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from environment_presets import NAMES as ENV_NAMES
from baker_config import PYTHON_EXE

import baker_config  # retains the configured DLL search handle

import numpy as np
from PIL import Image

import native_rigid_channels as native_rigid
from db_compiler import build_particle_effects_table, registration_keys
from dds_compiler import (
    encode_bc7_srgb_dds,
    encode_dxt5nm_distortion_dds,
    encode_vat_f32_dds,
    encode_vectorfield_3d_dds,
    upsample_clamped,
    upsample_seamless,
)
from pfh5 import PFH5Pack, norm_pack_path
from rmv2_compiler import (
    build_rmv2_v7,
    build_wh3_vat_fluid_material,
    build_wh_vfx_mesh_vertex_anim_material,
    build_wsmodel,
)
from spec_validator import validate_ir, validate_pack
from vfx_xml_compiler import (
    DonorVfxBank,
    build_library_xml,
    colour_curve,
    dump_wh3_vfx_xml,
    encode_curve,
    ensure_all_quality_tiers,
    scalar_curve,
    set_prop,
    vec3_curve,
)


def _compute_tangent_frame(verts: np.ndarray, uvs: np.ndarray, normals: np.ndarray, faces: np.ndarray):
    n = len(verts)
    tan = np.zeros((n, 3), dtype=np.float32)
    bitan = np.zeros((n, 3), dtype=np.float32)
    p0, p1, p2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    uv0, uv1, uv2 = uvs[faces[:, 0]], uvs[faces[:, 1]], uvs[faces[:, 2]]
    e1, e2 = p1 - p0, p2 - p0
    d1, d2 = uv1 - uv0, uv2 - uv0
    det = d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]
    inv = np.where(np.abs(det) > 1e-8, 1.0 / np.where(np.abs(det) > 1e-8, det, 1.0), 0.0)
    t_face = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) * inv[:, None]
    b_face = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) * inv[:, None]
    for k in range(3):
        np.add.at(tan, faces[:, k], t_face)
        np.add.at(bitan, faces[:, k], b_face)
    # Gram-Schmidt orthogonalize against normals
    nrm = normals / np.maximum(1e-8, np.linalg.norm(normals, axis=-1, keepdims=True))
    tan = tan - nrm * np.sum(tan * nrm, axis=-1, keepdims=True)
    tlen = np.linalg.norm(tan, axis=-1, keepdims=True)
    fallback = np.where(np.abs(nrm[:, 1:2]) < 0.9, np.array([[0.0, 1.0, 0.0]], np.float32), np.array([[1.0, 0.0, 0.0]], np.float32))
    fallback = np.cross(nrm, fallback)
    tan = np.where(tlen > 1e-6, tan / np.maximum(tlen, 1e-6), fallback)
    bitan = np.cross(nrm, tan)
    return tan.astype(np.float32), bitan.astype(np.float32)


def _load_texture_array(path: str) -> np.ndarray:
    lo = path.lower()
    if lo.endswith(".npy"):
        return np.load(path).astype(np.float32)
    if lo.endswith(".npz"):
        z = np.load(path)
        return z[list(z.keys())[0]].astype(np.float32)
    im = Image.open(path).convert("RGBA")
    return np.asarray(im, dtype=np.float32) / 255.0


def _compile_single_texture(ir_dir: str, tid: str, tspec: dict, tex_cache_dir: str) -> tuple[str, str, bytes]:
    src = os.path.join(ir_dir, tspec["source_path"])
    vpath = norm_pack_path(tspec["pack_path"])
    fmt = tspec["format"]
    usage = tspec["usage"]

    # Content-hash disk cache (5x-10x faster iteration when tweaking curves/materials!)
    h = hashlib.sha256()
    h.update(b"wh3_dds_cache_rect_v5|" if tspec.get("target_size",[1,1])[0]!=tspec.get("target_size",[1,1])[1] else b"wh3_dds_cache_v2|")
    with open(src, "rb") as f:
        h.update(f.read())
    if fmt in ("BC7_UNORM","A32B32G32R32F_2D"):h.update(b"native-layout-v4")
    h.update(json.dumps(tspec, sort_keys=True).encode("utf-8"))
    cache_path = os.path.join(tex_cache_dir, f"{tid}_{h.hexdigest()[:16]}.dds")
    if os.path.isfile(cache_path):
        with open(cache_path, "rb") as f:
            return tid, vpath, f.read()

    arr = _load_texture_array(src)

    if fmt == "A16B16G16R16F_3D":
        encode_vectorfield_3d_dds(arr, cache_path)
    elif fmt == "A32B32G32R32F_2D":
        encode_vat_f32_dds(arr, cache_path)
    elif fmt == "BC3_UNORM_DXT5NM":
        if arr.ndim == 2:
            arr = np.stack([arr, arr, np.ones_like(arr), np.ones_like(arr)], axis=-1)
        tgt = tuple(tspec.get("target_size", [arr.shape[1], arr.shape[0]]))
        if arr.shape[:2] != tgt[::-1]:
            arr = upsample_clamped(arr, tgt, border_px=8, neutral_rgba=(0.5, 0.5, 1.0, 0.0))
        vx = (arr[:, :, 0] - 0.5) * 2.0 * arr[:, :, 3]
        vy = (arr[:, :, 1] - 0.5) * 2.0 * arr[:, :, 3]
        encode_dxt5nm_distortion_dds(vx, vy, cache_path)
        if tspec.get("exact_zero_distortion_border"):
            from dds_compiler import exact_zero_dxt5nm_padding
            exact_zero_dxt5nm_padding(cache_path)
    else:
        # BC7_UNORM_SRGB / BC7_UNORM
        if arr.ndim == 2:
            arr = np.stack([arr, arr, arr, np.ones_like(arr)], axis=-1)
        elif arr.shape[2] == 3:
            arr = np.concatenate([arr, np.ones((*arr.shape[:2], 1), dtype=np.float32)], axis=-1)
        tgt = tuple(tspec.get("target_size", [arr.shape[1], arr.shape[0]]))
        tileable = bool(tspec.get("tileable", usage in ("mesh_flux", "mesh_global_mask", "mesh_colour_overlay")))
        target_hw = tuple(reversed(tspec.get("target_size", [arr.shape[1], arr.shape[0]])))
        if arr.shape[:2] != target_hw:
            if tileable:
                arr = upsample_seamless(arr, tgt)
            else:
                arr = upsample_clamped(arr, tgt, border_px=8, neutral_rgba=(0.0, 0.0, 0.0, 0.0))
        premul = bool(tspec.get("premultiply_alpha", False))
        if premul:
            arr = arr.copy()
            arr[:, :, :3] *= arr[:, :, 3:4]

        # CRITICAL FIX: Prevent double-sRGB washout on standard .png files!
        # .npy/.npz default to linear float; .png defaults to sRGB unless source_color_space=="linear".
        cs = str(tspec.get("source_color_space", "auto")).lower()
        if cs == "linear":
            rgb_is_linear = True
        elif cs == "srgb":
            rgb_is_linear = False
        else:
            rgb_is_linear = src.lower().endswith((".npy", ".npz"))

        # CRITICAL FIX: Only particle_vfx_main decodes Alpha via sRGB->Linear EOTF;
        # wh_vfx_mesh_vertex_anim leaves mesh texture alpha (e.g. colour_overlay.a) linear UNORM!
        if "srgb_alpha_oetf" in tspec:
            alpha_is_linear = bool(tspec["srgb_alpha_oetf"])
        else:
            alpha_is_linear = usage not in ("mesh_flux", "mesh_global_mask", "mesh_colour_overlay")

        encode_bc7_srgb_dds(
            arr,
            cache_path,
            alpha_is_linear=alpha_is_linear,
            rgb_is_linear=rgb_is_linear,
            verify_zero_alpha_border=(not tileable) and bool(tspec.get("require_zero_border", True)),
            tileable=tileable,
            separate_alpha=(not premul),
            srgb_format=fmt=="BC7_UNORM_SRGB",
        )

    with open(cache_path, "rb") as f:
        return tid, vpath, f.read()


def _compile_textures(ir_dir: str, manifest: dict, staging_dir: str) -> tuple[dict[str, bytes], dict[str, str]]:
    pack_assets: dict[str, bytes] = {}
    id_to_vpath: dict[str, str] = {}
    tex_cache_dir = os.path.join(staging_dir, "dds_cache")
    os.makedirs(tex_cache_dir, exist_ok=True)

    items = list(manifest["textures"].items())
    if len(items) <= 1:
        results = [_compile_single_texture(ir_dir, tid, tspec, tex_cache_dir) for tid, tspec in items]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
            futs = [pool.submit(_compile_single_texture, ir_dir, tid, tspec, tex_cache_dir) for tid, tspec in items]
            results = [f.result() for f in futs]

    for tid, vpath, data in results:
        pack_assets[vpath] = data
        id_to_vpath[tid] = vpath.replace("\\", "/")
    return pack_assets, id_to_vpath


def _compile_materials(manifest: dict, tex_vpaths: dict[str, str]) -> tuple[dict[str, bytes], dict[str, str]]:
    pack_assets: dict[str, bytes] = {}
    id_to_vpath: dict[str, str] = {}
    default_white = "commontextures/default_white.dds"

    for mat_id, mspec in manifest["materials"].items():
        vpath = norm_pack_path(mspec["pack_path"])
        shader = mspec.get("shader", "wh_vfx_mesh_vertex_anim")
        if shader in (native_rigid.BLACK, native_rigid.LENS):
            pack_assets[vpath] = native_rigid.material(shader, mat_id, mspec)
            id_to_vpath[mat_id] = vpath.replace(chr(92), "/")
            continue
        if shader == "wh3_rigid_baked_fluid_simulation":
            if mspec.get("vat_uv_tex"):
                from vat_native import material
                mat_bytes=material(mat_id,mspec,tex_vpaths)
            else:
                mat_bytes = build_wh3_vat_fluid_material(
                    name=mat_id,
                    vat_pos_tex=tex_vpaths.get(mspec.get("vat_pos_tex", ""), default_white),
                    vat_nrm_tex=tex_vpaths.get(mspec.get("vat_nrm_tex", ""), default_white),
                    base_colour_tex=tex_vpaths.get(mspec.get("tex_a", ""), default_white),
                    glow_colour=tuple(mspec.get("glow_colour", [1.0, 1.0, 1.0])),
                    glow_strength=float(mspec.get("glow_strength", 2.0)),
                    vat_fps=float(mspec.get("vat_fps", 30.0)),
                    vat_frame_count=int(mspec.get("vat_frame_count", 60)),
                    bounds_min=tuple(mspec.get("vat_bounds_min", [-5.0, -5.0, -5.0])),
                    bounds_max=tuple(mspec.get("vat_bounds_max", [5.0, 5.0, 5.0])),
                    sort_override=int(mspec.get("sort_override", 128)),
                )
            pack_assets[vpath] = mat_bytes
            id_to_vpath[mat_id] = vpath.replace("\\", "/")
            continue

        tex_a = tex_vpaths.get(mspec.get("tex_a", ""), default_white)
        tex_b = tex_vpaths.get(mspec.get("tex_b", ""), default_white)
        tex_c = tex_vpaths.get(mspec.get("tex_c", ""), default_white)
        gmask = tex_vpaths.get(mspec.get("global_mask", ""), default_white)
        ov = tex_vpaths.get(mspec.get("colour_overlay", ""), default_white)
        disp_t = tex_vpaths.get(mspec.get("disp_tex", ""), tex_a)

        if "normal_fade_in_out" in mspec:
            nfio = float(mspec["normal_fade_in_out"])
        else:
            nfio = 0.5 + 0.5 * min(1.0, float(mspec.get("fresnel_strength", 0.0)))
        b_abc = mspec.get("brightness_abc", [1.45, 1.45, 1.45])
        vdisp = float(mspec.get("vertex_displacement", 0.0))

        mat_bytes = build_wh_vfx_mesh_vertex_anim_material(
            name=mat_id,
            tex_a=tex_a,
            tex_b=tex_b,
            tex_c=tex_c,
            disp_tex=disp_t,
            tile_a=tuple(mspec.get("tile_a", [1.0, 1.0])),
            scroll_a=tuple(mspec.get("scroll_a", [0.0, 0.0])),
            tile_b=tuple(mspec.get("tile_b", [1.0, 1.0])),
            scroll_b=tuple(mspec.get("scroll_b", [0.0, 0.0])),
            tile_c=tuple(mspec.get("tile_c", [1.0, 1.0])),
            scroll_c=tuple(mspec.get("scroll_c", [0.0, 0.0])),
            glow_colour=tuple(mspec.get("glow_colour", [1.0, 1.0, 1.0])),
            glow_strength=float(mspec.get("glow_strength", 4.0)),
            total_alpha_strength=float(mspec.get("total_alpha_strength", 1.0)),
            normal_fade_in_out=nfio,
            normal_fade_power=max(0.25, float(mspec.get("fresnel_power", 1.0))),
            depth_fade_switch=1.0 if float(mspec.get("depth_fade_distance", 0.0)) > 0.01 else 0.0,
            pixel_depth_fade=max(0.2, float(mspec.get("depth_fade_distance", 1.0))),
            vertex_displacement=vdisp,
            vertex_disp_scale=float(mspec.get("vertex_disp_scale", 0.2)),
            vertex_disp_tile=tuple(mspec.get("vertex_disp_tile", [2.0, 4.0])),
            vertex_disp_scroll=tuple(mspec.get("vertex_disp_scroll", [0.0, 1.5])),
            brightness_abc=(float(b_abc[0]), float(b_abc[1]), float(b_abc[2])),
            global_mask=gmask,
            global_mask_tile=tuple(mspec.get("global_mask_tile", [1.0, 1.0])),
            global_mask_pan=tuple(mspec.get("global_mask_pan", [0.0, 0.0])),
            colour_overlay=ov,
            colour_overlay_tile=tuple(mspec.get("colour_overlay_tile", [1.0, 1.0])),
            colour_overlay_pan=tuple(mspec.get("colour_overlay_pan", [0.0, 0.0])),
            colour_overlay_strength=float(mspec.get("colour_overlay_strength", 1.0)),
            sort_override=int(mspec.get("sort_override", 255)),
            tex_a_channels=str(mspec.get("tex_a_channels", "1,0,0,0")),
            tex_b_channels=str(mspec.get("tex_b_channels", "1,0,0,0")),
            tex_c_channels=str(mspec.get("tex_c_channels", "1,0,0,0")),
            vertex_anim=float(mspec.get("vertex_anim",0.0)),vertex_anim_speed=float(mspec.get("vertex_anim_speed",0.0)),
            vertex_oscilation_scale=float(mspec.get("vertex_oscilation_scale", 0.0)),
            vertex_amp_xyz=tuple(mspec.get("vertex_amp_xyz", [0.0, 0.0, 0.0])),
            vertex_freq_xyz=tuple(mspec.get("vertex_freq_xyz", [1.0, 1.0, 1.0])),
        )
        pack_assets[vpath] = mat_bytes
        id_to_vpath[mat_id] = vpath.replace("\\", "/")
    return pack_assets, id_to_vpath


def _compile_meshes(ir_dir: str, manifest: dict, mat_vpaths: dict[str, str]) -> tuple[dict[str, bytes], dict[str, str]]:
    pack_assets: dict[str, bytes] = {}
    id_to_wsmodel: dict[str, str] = {}

    for mesh_id, mspec in manifest["meshes"].items():
        z = np.load(os.path.join(ir_dir, mspec["source_npz"]))
        verts = np.asarray(z["vertices"], dtype=np.float32)
        faces = np.asarray(z["indices"], dtype=np.uint32).reshape(-1, 3)
        norms = np.asarray(z["normals"], dtype=np.float32)
        uvs = np.asarray(z["uvs"], dtype=np.float32)
        if "coverage" in z:
            cov = np.asarray(z["coverage"], dtype=np.float32)
        elif "colors" in z and z["colors"].shape[-1] >= 4:
            cov = np.asarray(z["colors"][:, 3], dtype=np.float32)
        else:
            cov = np.ones((len(verts),), dtype=np.float32)

        vrgb = None
        if "colors" in z and z["colors"].ndim == 2 and z["colors"].shape[1] >= 3:
            vrgb = np.asarray(z["colors"][:, :3], dtype=np.float32)

        ps = mspec.get("prescale_xyz")
        if ps:
            verts = verts * np.array(ps, dtype=np.float32)[None, :]

        # DirectX CW winding check & auto-flip
        p0, p1, p2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
        fn = np.cross(p1 - p0, p2 - p0)
        avg_n = norms[faces[:, 0]] + norms[faces[:, 1]] + norms[faces[:, 2]]
        flip = bool(mspec.get("auto_fix_winding", True)) and float(np.mean(np.sum(fn * avg_n, axis=1) >= 0.0)) < 0.5
        if flip:
            faces = faces[:, ::-1]

        # Double-sided support: duplicate with flipped faces & negated normals (essential for thin nebula/ribbon/arc sheets!)
        if bool(mspec.get("double_sided", False)):
            n_orig = len(verts)
            verts = np.concatenate([verts, verts], axis=0)
            norms = np.concatenate([norms, -norms], axis=0)
            uvs = np.concatenate([uvs, uvs], axis=0)
            cov = np.concatenate([cov, cov], axis=0)
            if vrgb is not None:
                vrgb = np.concatenate([vrgb, vrgb], axis=0)
            faces = np.concatenate([faces, faces[:, ::-1] + n_orig], axis=0)

        tan, bitan = _compute_tangent_frame(verts, uvs, norms, faces)
        rmv2_bytes = build_rmv2_v7(
            positions=verts,
            uvs=uvs,
            normals=norms,
            tangents=tan,
            bitangents=bitan,
            coverage_alpha=cov,
            faces=faces,
            group_name=f"{mesh_id}_LOD01",
            sqrt_compensate_alpha=bool(mspec.get("coverage_is_linear", True)),
            flip_winding=False,
            vertex_colors_rgb=vrgb,
            donor_profile=mspec.get("donor_profile","cylinder"),
        )
        rmv2_vp = norm_pack_path(mspec["rmv2_pack_path"])
        ws_vp = norm_pack_path(mspec["wsmodel_pack_path"])
        mat_vp = mat_vpaths[mspec["material_id"]]
        if mspec.get("rigid_root_bone")=="building":
            from native_root_track import bind_rmv
            rmv2_bytes=bind_rmv(rmv2_bytes)
        pack_assets[rmv2_vp] = rmv2_bytes
        pack_assets[ws_vp] = build_wsmodel(rmv2_vp.replace("\\", "/"), mat_vp)
        id_to_wsmodel[mesh_id] = ws_vp.replace("\\", "/")

    return pack_assets, id_to_wsmodel


def _build_vec3_curve(curve_pts: list | None, default=(0.0, 0.0, 0.0)) -> str:
    if not curve_pts:
        return vec3_curve(*default)
    return encode_curve({
        "x": [(float(p[0]), float(p[1])) for p in curve_pts],
        "y": [(float(p[0]), float(p[2])) for p in curve_pts],
        "z": [(float(p[0]), float(p[3])) for p in curve_pts],
    })


def _build_scalar_curve(curve_pts: list | None, axis: str = "x", default: float = 1.0) -> str:
    if not curve_pts:
        return encode_curve({axis: [(0.0, default)]})
    return encode_curve({axis: [(float(p[0]), float(p[1])) for p in curve_pts]})


def _one_shot_rate_curve(rate: float, win: float) -> str:
    """Emit a low-rate pulse EmissionRateCurve that fires at u=0 and drops to 0 immediately after spawn,
    avoiding pending-spawn accumulator buildup in WH3.
    """
    r = max(float(rate or 40.0), 30.0)
    w = max(float(win), 0.01)
    # Keep rate active for ~1-2 frames (~0.025s), then drop cleanly to 0 well before u=1.0
    u_hi = min(0.20, max(0.003, 0.025 / w))
    u_drop = min(0.35, u_hi + max(0.003, 0.015 / w))
    return encode_curve({"x": [(0.0, r), (u_hi, r), (u_drop, 0.0), (1.0, 0.0)]})


def _one_shot_em_life(lt: float, has_carrier_anim: bool = False, requested_win: float | None = None) -> tuple[float, str]:
    """Compute WH3-safe (EmLife, EmissionRateCurve) for a single-shot (MaxParticles=1) emitter.

    WH3 Engine Law (proven in real L3 battle testing):
      If EmLife >= ParticleLifeTime (even with infinite_life="false" and a pulsing EmissionRateCurve),
      WH3's spawn accumulator fires a frozen Zombie Particle #2 on the exact boundary frame when
      Particle #1 dies!
      Vanilla single-shot rigids (e.g. main_beam_outer: LifeTime=0.10, PLife=10.8) prevent this by
      guaranteeing EmLife < ParticleLifeTime strictly.
    """
    lt = max(float(lt), 0.01)
    if has_carrier_anim:
        # Emitter needs to stay alive to drive EmitterOrientation(u), so stop strictly BEFORE ParticleLifeTime
        margin = min(0.15, max(0.012, lt * 0.04))
        max_safe = max(0.008, lt - margin)
        win = min(float(requested_win), max_safe) if requested_win is not None else max_safe
        win = max(0.008, min(win, max_safe))
        return win, _one_shot_rate_curve(max(40.0, 2.0 / win), win)
    else:
        # Standard single-shot: die right after frame-0 spawn (vanilla main_beam_outer pattern)
        max_safe = min(0.05, max(0.008, lt * 0.4))
        win = min(float(requested_win), max_safe) if requested_win is not None else max_safe
        win = max(0.008, min(win, max_safe))
        rate = max(60.0, 2.0 / win)
        return win, scalar_curve(rate)


def _compile_emitters_and_wrappers(
    manifest: dict,
    tex_vpaths: dict[str, str],
    mesh_wsmodels: dict[str, str],
    ir_dir: str | None = None,
) -> tuple[dict[str, bytes], list[str], list[str]]:
    bank = DonorVfxBank()
    cull_far, cull_max = manifest["meta"].get("cull_distances", [3000.0, 3750.0])
    triples_by_wrapper: dict[str, list[tuple[ET.Element, ET.Element, ET.Element]]] = {
        w["vfx_key"]: [] for w in manifest["host_binding"]["wrappers"]
    }

    native_banks = {}
    # 1. Rigid mesh emitters
    for spec in manifest.get("rigid_emitters", []):
        wk = spec["wrapper_vfx_key"]
        eid = spec["name"]
        shader = manifest["materials"][manifest["meshes"][spec["mesh_id"]]["material_id"]]["shader"]
        if shader in (native_rigid.BLACK, native_rigid.LENS):
            from vfx_xml_compiler import WH3_VFX_DESC_PACK, parse_wh3_vfx_xml
            lib, template = (("vfx/wh3_dlc25_lib_unit_garagrim.xml", "continuous_rigid2") if shader == native_rigid.BLACK
                             else ("vfx/wh3_main_lib_env_kislev.xml", "giant_distortion"))
            if lib not in native_banks:
                donor = DonorVfxBank(lib)
                native_banks[lib] = donor
            em, bc, rs = native_banks[lib].clone_triple(template, eid)
            set_prop(em,"SpawnAreaBox",text=vec3_curve(0,0,0))
            set_prop(em,"SpawnAreaCurve",text=encode_curve({"inner_radius":[(0.,0.)],"outer_radius":[(0.,0.)]}))
            for key in ("ForceB","ForceC","EmitterOrientation"):
                set_prop(em,key,text=vec3_curve(0,0,0))
            if shader == native_rigid.LENS:
                for prop in list(rs.findall("./property[@name='scene_view_type']")): rs.remove(prop)
                for tier in ("0","1","2"):
                    ET.SubElement(rs,"property",{"name":"scene_view_type","quality":tier,"value":"DISTORTION"})
        else:
            em, bc, rs = bank.clone_triple("main_beam_outer", eid)
        st = float(spec["start_time"])
        lt = float(spec["life_time"])
        burst = int(spec.get("burst_count", 1))
        has_rot_anim = len(spec.get("rotation_deg_curve") or []) > 1
        req_win = float(spec["window_sec"]) if "window_sec" in spec else None
        if burst == 1 and not bool(spec.get("continuous_respawn", False)):
            win, rate_txt = _one_shot_em_life(lt, has_carrier_anim=has_rot_anim, requested_win=req_win)
        else:
            win = req_win if req_win is not None else max(0.05, lt)
            em_rate = float(spec.get("emission_rate", 60.0)) or 60.0
            rate_txt = scalar_curve(em_rate)
        set_prop(em, "StartTime", tag="ranged_property", extra_attrs={"min": f"{st:.4f}", "max": f"{st:.4f}"})
        set_prop(em, "LifeTime", tag="ranged_property", extra_attrs={"min": f"{win:.4f}", "max": f"{win:.4f}"})
        set_prop(em, "MaximumParticles", value=str(burst))
        set_prop(em, "EmissionRateCurve", text=rate_txt)
        set_prop(em, "ParticleLifeTime", text=scalar_curve(lt))
        set_prop(em, "EmitterPosition", text=_build_vec3_curve(spec.get("position_curve"), (0.0, 0.0, 0.0)))
        set_prop(em, "ParticleScale", text=_build_vec3_curve(spec.get("scale_curve"), (1.0, 1.0, 1.0)))
        set_prop(em, "ParticleInitialScale", text=vec3_curve(1.0, 1.0, 1.0))
        set_prop(em, "ParticleInitialAlpha", text=scalar_curve(1.0))
        set_prop(em, "ParticleInitialSpeed", text=scalar_curve(float(spec.get("initial_speed", 0.0))))
        if "drag" in spec:
            set_prop(em, "ParticleDeceleration", text=scalar_curve(float(spec.get("drag", 0.0))))
        if spec.get("force_xyz"):
            fx, fy, fz = [float(v) * 100.0 for v in spec["force_xyz"]]
            set_prop(em, "ForceA", text=vec3_curve(fx, fy, fz))
        else:
            set_prop(em, "ForceA", text=vec3_curve(0.0, float(spec.get("gravity", 0.0)) * 100.0, 0.0))
        # Orientation & Spin: honor rotation_deg_curve (EmitterOrientation) OR explicit initial_rotation_deg/spin_deg_per_sec/random_yaw
        if spec.get("rotation_deg_curve"):
            set_prop(em, "EmitterOrientation", text=_build_vec3_curve(spec["rotation_deg_curve"]))
            for key in ("ParticleOrientationMin", "ParticleOrientationMax", "ParticleSpin_Min", "ParticleSpin_Max"):
                set_prop(em, key, extra_attrs=dict(x="0", y="0", z="0"))
        elif any(k in spec for k in ("initial_rotation_deg", "spin_deg_per_sec", "random_yaw")):
            rot0 = spec.get("initial_rotation_deg", [0.0, 0.0, 0.0])
            spin = float(spec.get("spin_deg_per_sec", 0.0))
            ryaw = bool(spec.get("random_yaw", False))
            ymin = 0.0 if ryaw else float(rot0[1])
            ymax = 360.0 if ryaw else float(rot0[1])
            set_prop(em, "ParticleOrientationMin", extra_attrs={"x": f"{float(rot0[0]):.3f}", "y": f"{ymin:.3f}", "z": f"{float(rot0[2]):.3f}"})
            set_prop(em, "ParticleOrientationMax", extra_attrs={"x": f"{float(rot0[0]):.3f}", "y": f"{ymax:.3f}", "z": f"{float(rot0[2]):.3f}"})
            set_prop(em, "ParticleSpinDirectionY", value="CW" if spin >= 0 else "CCW")
            set_prop(em, "ParticleSpin_Min", extra_attrs={"x": "0.00", "y": f"{abs(spin):.3f}", "z": "0.00"})
            set_prop(em, "ParticleSpin_Max", extra_attrs={"x": "0.00", "y": f"{abs(spin):.3f}", "z": "0.00"})
        else:
            # Never silently inherit donor random yaw/spin on unrotated rigid emitters!
            for key in ("ParticleOrientationMin", "ParticleOrientationMax", "ParticleSpin_Min", "ParticleSpin_Max"):
                set_prop(em, key, extra_attrs=dict(x="0", y="0", z="0"))
        ta = spec["terrain_attach"]
        set_prop(em, "ParticleSimulationSpace", value="LOCAL_SPACE_POSITION_ONLY" if "align" in ta else "LOCAL_SPACE_POSITION_ROTATION")

        rd = em.find("./property_rigid_data[@name='RigidData']")
        if rd is not None:
            anim_path = str(spec.get("anim_pack_path", "") or "").replace("\\", "/")
            anim_spd = float(spec.get("anim_speed", 1.0))
            for re_node in rd.findall("./rigid_entry"):
                mf = re_node.find("./ModelFile")
                if mf is not None:
                    mf.set("value", mesh_wsmodels[spec["mesh_id"]])
                af = re_node.find("./AnimFile")
                if af is not None:
                    af.set("value", anim_path)
                asp = re_node.find("./AnimSpeed")
                if asp is not None:
                    asp.set("value", f"{anim_spd:.4f}")

        q0 = bc.find("./quality[@value='0']")
        if q0 is not None:
            for mod in q0.findall("./modifier"):
                frag = mod.get("fragment")
                if frag == "vs_colour":
                    cc = spec.get("colour_curve")
                    bc_pts = spec.get("brightness_curve")
                    bk = [(float(p[0]), float(p[1])) for p in bc_pts] if bc_pts else [(0.0, 1.0)]
                    if cc:
                        mod.find("./property[@name='curve_colour']").text = encode_curve({
                            "r": [(float(p[0]), float(p[1])) for p in cc],
                            "g": [(float(p[0]), float(p[2])) for p in cc],
                            "b": [(float(p[0]), float(p[3])) for p in cc],
                            "brightness": bk,
                        })
                    else:
                        mod.find("./property[@name='curve_colour']").text = colour_curve(1.0, 1.0, 1.0, brightness_keys=bk)
                elif frag == "vs_vertex_alpha":
                    mod.find("./property[@name='curve_alpha']").text = _build_scalar_curve(spec.get("alpha_curve"), "alpha", 1.0)
        ensure_all_quality_tiers(em, cull_far=cull_far, cull_max=cull_max)
        triples_by_wrapper[wk].append((em, bc, rs))

    # 2. Decal emitters
    for spec in manifest.get("decal_emitters", []):
        wk = spec["wrapper_vfx_key"]
        eid = spec["name"]
        rp = spec["render_pass"]
        em, bc, rs = bank.clone_triple("TRAIL_decal_emissive", eid)
        st = float(spec["start_time"])
        lt = float(spec["life_time"])
        d_win, d_rate = _one_shot_em_life(lt, has_carrier_anim=False)
        set_prop(em, "StartTime", tag="ranged_property", extra_attrs={"min": f"{st:.4f}", "max": f"{st:.4f}"})
        set_prop(em, "LifeTime", tag="ranged_property", extra_attrs={"min": f"{d_win:.4f}", "max": f"{d_win:.4f}"})
        set_prop(em, "MaximumParticles", value="1")
        set_prop(em, "EmissionRateCurve", text=d_rate)
        set_prop(em, "ParticleLifeTime", text=scalar_curve(lt))
        rc = spec["radius_curve"]
        # z=12.0m projection box depth prevents decal clipping on battlefield slopes!
        set_prop(em, "ParticleScale", text=encode_curve({
            "x": [(float(p[0]), float(p[1])) for p in rc],
            "y": [(float(p[0]), float(p[1])) for p in rc],
            "z": [(0.0, 12.0)],
        }))
        set_prop(em, "ParticleOrientationMin", extra_attrs={"x": "90.00", "y": "0.00", "z": "0.00"})
        set_prop(em, "ParticleOrientationMax", extra_attrs={"x": "90.00", "y": "0.00", "z": "0.00"})
        rot_spd = float(spec.get("rotation_speed_deg", 0.0))
        if abs(rot_spd) > 1e-4:
            set_prop(em, "ParticleSpinDirectionY", value="CW" if rot_spd >= 0 else "CCW")
            set_prop(em, "ParticleSpin_Min", extra_attrs={"x": "0.00", "y": f"{abs(rot_spd):.3f}", "z": "0.00"})
            set_prop(em, "ParticleSpin_Max", extra_attrs={"x": "0.00", "y": f"{abs(rot_spd):.3f}", "z": "0.00"})
        q0 = bc.find("./quality[@value='0']")
        if q0 is not None:
            for mod in list(q0.findall("./modifier")):
                frag = mod.get("fragment")
                if frag == "ps_texture":
                    mod.find("./texture_property[@name='diffuse']").set("path", tex_vpaths[spec["texture_id"]].replace("/", "\\"))
                    mod.find("./property[@name='pre_multiplied_alpha']").set("value", "true" if spec["pre_multiplied_alpha"] else "false")
                elif frag == "ps_lighting":
                    if not spec["keep_ps_lighting"]:
                        q0.remove(mod)
                    elif rp == "DEFERRED_DECAL":
                        mod.find("./property[@name='emissive']").set("value", "0.0")
                        mod.find("./property[@name='additive_blend_mul']").set("value", "0.0")
                    else:
                        mod.find("./property[@name='emissive']").set("value", f"{float(spec.get('colour_multiplier', 1.5)):.2f}")
                        mod.find("./property[@name='additive_blend_mul']").set("value", "0.75")
                elif frag == "vs_vertex_alpha":
                    mod.find("./property[@name='curve_alpha']").text = _build_scalar_curve(spec.get("alpha_curve"), "alpha", 1.0)
                elif frag == "vs_colour":
                    cc = spec.get("colour_curve")
                    if cc:
                        mod.find("./property[@name='curve_colour']").text = encode_curve({
                            "r": [(float(p[0]), float(p[1])) for p in cc],
                            "g": [(float(p[0]), float(p[2])) for p in cc],
                            "b": [(float(p[0]), float(p[3])) for p in cc],
                            "brightness": [(0.0, float(spec.get("colour_multiplier", 1.0)))],
                        })
        for p in list(rs.findall("./property[@name='scene_view_type']")):
            rs.remove(p)
        ET.SubElement(rs, "property", {"name": "scene_view_type", "quality": "0", "value": rp})
        ensure_all_quality_tiers(em, cull_far=cull_far, cull_max=cull_max)
        triples_by_wrapper[wk].append((em, bc, rs))

    # 3. Particle / billboard emitters (Smart Donor Split: START_flash for horizontal/static flares, TRAIL_embers_bounce for kinetic sparks)
    for spec in manifest.get("particle_emitters", []):
        wk = spec["wrapper_vfx_key"]
        eid = spec["name"]
        is_horiz = (spec.get("billboard_orientation") == "horizontal")
        is_static_flare = (
            float(spec.get("velocity_stretch_mul", 0.0)) <= 1e-4
            and abs(float(spec.get("velocity_radial", 0.0))) <= 1e-4
            and abs(float(spec.get("gravity", 0.0))) <= 1e-4
            and int(spec.get("max_particles", spec.get("burst_count", 1) or 1)) <= 8
        )
        default_donor = "START_flash" if (is_horiz or is_static_flare) else "TRAIL_embers_bounce"
        em, bc, rs = bank.clone_triple(spec.get("template_emitter", default_donor), eid)
        st = float(spec["start_time"])
        dur = float(spec["duration"])
        pl = spec.get("particle_life", [0.8, 1.2])
        rate = float(spec.get("emission_rate", 40.0))
        burst = int(spec.get("burst_count", 0))
        max_p = int(spec.get("max_particles", max(burst, int(rate * float(pl[1]) * 1.4), 64 if default_donor != "START_flash" else 1)))
        set_prop(em, "StartTime", tag="ranged_property", extra_attrs={"min": f"{st:.4f}", "max": f"{st:.4f}"})
        set_prop(em, "MaximumParticles", value=str(max_p))
        eff_rate = max(rate, float(burst) / max(dur, 0.05) if burst > 0 else (60.0 if default_donor == "START_flash" else 0.0))
        if max_p == 1 and not bool(spec.get("continuous_respawn", False)):
            p_win, p_rate = _one_shot_em_life(float(pl[0]), has_carrier_anim=False, requested_win=dur)
            set_prop(em, "LifeTime", tag="ranged_property", extra_attrs={"min": f"{p_win:.4f}", "max": f"{p_win:.4f}"})
            set_prop(em, "EmissionRateCurve", text=p_rate)
        else:
            set_prop(em, "LifeTime", tag="ranged_property", extra_attrs={"min": f"{dur:.4f}", "max": f"{dur:.4f}"})
            set_prop(em, "EmissionRateCurve", text=scalar_curve(eff_rate))
        set_prop(em, "ParticleLifeTime", text=scalar_curve(float(pl[0]), float(pl[1])))
        set_prop(em, "EmitterPosition", text=vec3_curve(*spec.get("offset_xyz", [0.0, 0.0, 0.0])))
        rad = float(spec.get("emitter_radius", 0.0))
        if rad > 1e-4:
            r_in = rad * 0.7 if spec.get("emitter_shape") == "ring" else 0.0
            set_prop(em, "SpawnAreaCurve", text=encode_curve({"inner_radius": [(0.0, r_in)], "outer_radius": [(0.0, rad)]}))
        eh = float(spec.get("emitter_height", 0.0))
        if eh > 1e-4:
            set_prop(em, "SpawnAreaBox", text=vec3_curve(rad * 2.0, eh, rad * 2.0))
        set_prop(em, "ParticleInitialSpeed", text=scalar_curve(float(spec.get("velocity_radial", 0.0 if default_donor == "START_flash" else 6.0))))
        set_prop(em, "ParticleDeceleration", text=scalar_curve(float(spec.get("drag", 0.0))))
        set_prop(em, "ForceA", text=vec3_curve(0.0, float(spec.get("gravity", 0.0)) * 100.0, 0.0))
        if spec.get("vectorfield_texture_id") and spec["vectorfield_texture_id"] in tex_vpaths:
            set_prop(em, "VectorFieldTexture", value=tex_vpaths[spec["vectorfield_texture_id"]].replace("/", "\\"))
            set_prop(em, "VectorFieldSize", text=scalar_curve(float(spec.get("vectorfield_size", 40.0))))
            set_prop(em, "VectorFieldStrength", text=scalar_curve(float(spec.get("vectorfield_strength", 25.0))))
            set_prop(em, "VectorFieldTightness", text=scalar_curve(float(spec.get("vectorfield_tightness", 0.65))))
        if spec.get("size_curve"):
            sc = spec["size_curve"]
            set_prop(em, "ParticleScale", text=encode_curve({
                "x": [(float(p[0]), float(p[1])) for p in sc],
                "y": [(float(p[0]), float(p[1])) for p in sc],
                "z": [(0.0, 1.0)],
            }))

        # Resolve flipbook grid from emitter OR referenced texture spec
        tex_meta = manifest["textures"].get(spec["texture_id"], {})
        fb_grid = spec.get("flipbook_grid") or tex_meta.get("flipbook_grid")
        fb_fps = float(spec.get("flipbook_fps", 24.0))

        q0 = bc.find("./quality[@value='0']")
        if q0 is not None:
            for mod in list(q0.findall("./modifier")):
                frag = mod.get("fragment")
                if frag == "ps_texture":
                    mod.find("./texture_property[@name='diffuse']").set("path", tex_vpaths[spec["texture_id"]].replace("/", "\\"))
                    mod.find("./property[@name='pre_multiplied_alpha']").set("value", "true" if spec["pre_multiplied_alpha"] else "false")
                elif frag in ("vs_cell_texture","vs_animated_texture"):
                    if fb_grid or frag == "vs_cell_texture":
                        q0.remove(mod)
                elif frag == "ps_soft_edge":
                    sf = mod.find("./property[@name='soft_factor']")
                    if sf is not None and "soft_factor" in spec:
                        sf.set("value", f"{float(spec['soft_factor']):.2f}")
                elif frag == "ps_lighting":
                    if bool(spec.get("omit_ps_lighting", True)) and "emissive" not in spec and "additive_blend_mul" not in spec:
                        q0.remove(mod)
                    else:
                        pe = mod.find("./property[@name='emissive']")
                        if pe is not None:
                            pe.set("value", f"{float(spec.get('emissive', 2.0)):.2f}")
                        pa = mod.find("./property[@name='additive_blend_mul']")
                        if pa is not None:
                            pa.set("value", f"{float(spec.get('additive_blend_mul', 0.75)):.2f}")
                elif frag == "vs_translation":
                    pr = mod.find("./property[@name='restriction']")
                    if pr is not None:
                        pr.set("value", "2" if is_horiz else "0")
                elif frag == "vs_translation_velocity_v2":
                    pm = mod.find("./property[@name='velocity_stretch_mul']")
                    if pm is not None:
                        pm.text = scalar_curve(float(spec["velocity_stretch_mul"]))
                    pc = mod.find("./property[@name='velocity_stretch_cap']")
                    if pc is not None:
                        pc.set("value", f"{float(spec['velocity_stretch_cap']):.2f}")
                elif frag == "vs_vertex_alpha" and spec.get("alpha_curve"):
                    mod.find("./property[@name='curve_alpha']").text = _build_scalar_curve(spec["alpha_curve"], "alpha", 1.0)
                elif frag == "vs_colour" and spec.get("colour_curve"):
                    cc = spec["colour_curve"]
                    mod.find("./property[@name='curve_colour']").text = encode_curve({
                        "r": [(float(p[0]), float(p[1])) for p in cc],
                        "g": [(float(p[0]), float(p[2])) for p in cc],
                        "b": [(float(p[0]), float(p[3])) for p in cc],
                        "brightness": [(0.0, float(spec.get("colour_multiplier", 1.0)))],
                    })
            if fb_grid and (int(fb_grid[0]) > 1 or int(fb_grid[1]) > 1):
                from vfx_xml_compiler import animated_modifier
                q0.insert(0, animated_modifier(int(fb_grid[0]), int(fb_grid[1]), fb_fps))
        for p in list(rs.findall("./property[@name='scene_view_type']")):
            rs.remove(p)
        ET.SubElement(rs, "property", {"name": "scene_view_type", "quality": "0", "value": spec["render_pass"]})
        ensure_all_quality_tiers(em, cull_far=cull_far, cull_max=cull_max)
        triples_by_wrapper[wk].append((em, bc, rs))

    # 3b. Distortion emitters (DISTORTION render pass -> DXT5nm normal refraction!)
    for spec in manifest.get("distortion_emitters", []):
        wk = spec["wrapper_vfx_key"]
        eid = spec["name"]
        if spec.get("donor_library"):
            from pathlib import Path
            from vfx_xml_compiler import parse_wh3_vfx_xml
            if not ir_dir: raise ValueError("Donor library requires the source IR directory")
            lib = (Path(ir_dir)/spec["donor_library"]).resolve()
            if not lib.is_relative_to(Path(ir_dir).resolve()): raise ValueError("Donor library must be inside IR")
            if lib not in native_banks:
                dbank=object.__new__(DonorVfxBank);dbank.vfx=parse_wh3_vfx_xml(lib.read_bytes()).find("vfx");native_banks[lib]=dbank
            em,bc,rs=native_banks[lib].clone_triple(spec["donor_id"],eid)
            assert any(n.get("value")=="DISTORTION" for n in rs.findall("property"))
            for mod in bc.findall("./quality/modifier[@fragment='vs_flip_uv']/property"):
                if mod.get("name") in ("flip_u","flip_v"):mod.set("value","0")
            for key in ("ParticleOrientationMin","ParticleOrientationMax","ParticleSpin_Min","ParticleSpin_Max"):
                set_prop(em,key,extra_attrs=dict(x="0",y="0",z="0"))
            for mod in bc.findall("./quality/modifier[@fragment='ps_soft_edge']"):
                for k,attr,default in (("soft_factor","soft_factor",1.5),("scale_strength","soft_scale_strength",0.0)):
                    prop=mod.find(f"./property[@name='{k}']")
                    if prop is not None:prop.set("value",str(spec.get(attr,default)))
            set_prop(em,"ParticleInitialAlpha",text=scalar_curve(float(spec["initial_alpha"])))
        else:
            em, bc, rs = bank.clone_triple("START_distortion", eid)
        st = float(spec["start_time"])
        lt = float(spec["life_time"])
        dist_win, dist_rate = _one_shot_em_life(lt, has_carrier_anim=False)
        count=1
        if spec.get("donor_library"):
            count=int(spec.get("burst_count",1));dist_win=float(spec.get("window_sec",dist_win))
            if not (1<=count<=64 and 0<dist_win<lt):raise ValueError("Native distortion burst: require count 1..64 and emission window shorter than particle life")
            dist_rate=scalar_curve(max(1000.,count/dist_win))

        set_prop(em, "StartTime", tag="ranged_property", extra_attrs={"min": f"{st:.4f}", "max": f"{st:.4f}"})
        set_prop(em, "LifeTime", tag="ranged_property", extra_attrs={"min": f"{dist_win:.4f}", "max": f"{dist_win:.4f}"})
        set_prop(em, "MaximumParticles", value=str(count))
        set_prop(em, "EmissionRateCurve", text=dist_rate)
        set_prop(em, "ParticleLifeTime", text=scalar_curve(lt))
        set_prop(em, "EmitterPosition", text=vec3_curve(*spec.get("offset_xyz", [0.0, 0.0, 0.0])))
        set_prop(em, "ParticleInitialScale", text=vec3_curve(1.0, 1.0, 1.0))
        sc = spec["scale_curve"]
        set_prop(em, "ParticleScale", text=encode_curve({
            "x": [(float(p[0]), float(p[1])) for p in sc],
            "y": [(float(p[0]), float(p[1])) for p in sc],
            "z": [(0.0, 1.0)],
        }))
        set_prop(em, "ParticleSimulationSpace", value="LOCAL_SPACE_POSITION_ONLY")
        q0 = bc.find("./quality[@value='0']")
        if q0 is not None:
            for mod in list(q0.findall("./modifier")):
                frag = mod.get("fragment")
                if frag == "ps_texture":
                    n_tex = mod.find("./texture_property[@name='normal']")
                    if n_tex is not None:
                        n_tex.set("path", tex_vpaths[spec["texture_id"]].replace("/", "\\"))
                elif frag == "vs_translation":
                    r_p = mod.find("./property[@name='restriction']")
                    if r_p is not None:
                        r_p.set("value", "0")
                elif frag == "vs_vertex_alpha":
                    mod.find("./property[@name='curve_alpha']").text = _build_scalar_curve(spec.get("strength_curve"), "alpha", 1.0)
        for p in list(rs.findall("./property[@name='scene_view_type']")):
            rs.remove(p)
        ET.SubElement(rs, "property", {"name": "scene_view_type", "quality": "0", "value": "DISTORTION"})
        ensure_all_quality_tiers(em, cull_far=cull_far, cull_max=cull_max)
        triples_by_wrapper[wk].append((em, bc, rs))

    # 3c. Dynamic point lights (DEFERRED_DYNAMIC_LIGHT)
    for spec in manifest.get("lights", []):
        wk = spec["wrapper_vfx_key"]
        eid = spec["name"]
        em, bc, rs = bank.clone_triple("TRAIL_light", eid)
        st = float(spec["start_time"])
        lt = float(spec["life_time"])
        l_win, l_rate = _one_shot_em_life(lt, has_carrier_anim=False)
        set_prop(em, "StartTime", tag="ranged_property", extra_attrs={"min": f"{st:.4f}", "max": f"{st:.4f}"})
        set_prop(em, "LifeTime", tag="ranged_property", extra_attrs={"min": f"{l_win:.4f}", "max": f"{l_win:.4f}"})
        set_prop(em, "MaximumParticles", value="1")
        set_prop(em, "EmissionRateCurve", text=l_rate)
        set_prop(em, "ParticleLifeTime", text=scalar_curve(lt))
        set_prop(em, "EmitterPosition", text=vec3_curve(*spec.get("offset_xyz", [0.0, 2.0, 0.0])))
        set_prop(em, "ParticleInitialScale", text=vec3_curve(1.0, 1.0, 1.0))
        rc = spec["radius_curve"]
        set_prop(em, "ParticleScale", text=encode_curve({
            "x": [(float(p[0]), float(p[1])) for p in rc],
            "y": [(float(p[0]), float(p[1])) for p in rc],
            "z": [(float(p[0]), float(p[1])) for p in rc],
        }))
        set_prop(em, "ParticleSimulationSpace", value="LOCAL_SPACE_POSITION_ONLY")
        q0 = bc.find("./quality[@value='0']")
        if q0 is not None:
            for mod in list(q0.findall("./modifier")):
                frag = mod.get("fragment")
                if frag == "vs_colour" and spec.get("colour_curve"):
                    cc = spec["colour_curve"]
                    mod.find("./property[@name='curve_colour']").text = encode_curve({
                        "r": [(float(p[0]), float(p[1])) for p in cc],
                        "g": [(float(p[0]), float(p[2])) for p in cc],
                        "b": [(float(p[0]), float(p[3])) for p in cc],
                        "brightness": [(0.0, 1.0)],
                    })
                elif frag == "vs_vertex_alpha":
                    mod.find("./property[@name='curve_alpha']").text = _build_scalar_curve(spec.get("intensity_curve"), "alpha", 12.0)
        for p in list(rs.findall("./property[@name='scene_view_type']")):
            rs.remove(p)
        ET.SubElement(rs, "property", {"name": "scene_view_type", "quality": "0", "value": "DEFERRED_DYNAMIC_LIGHT"})
        ensure_all_quality_tiers(em, cull_far=cull_far, cull_max=cull_max)
        triples_by_wrapper[wk].append((em, bc, rs))

    # 4. Auto-page libraries (<= 40 emitters per page) & generate wrapper XMLs
    pack_assets: dict[str, bytes] = {}
    lib_keys: list[str] = []
    wrapper_keys: list[str] = []

    for wspec in manifest["host_binding"]["wrappers"]:
        wk = wspec["vfx_key"]
        lbase = wspec["library_base"]
        wrapper_keys.append(wk)
        wtriples = triples_by_wrapper.get(wk, [])
        if manifest["meta"].get("sync_render_pass_qualities"):
            for _, _, stack in wtriples:
                views={v.get("quality","0"):v for v in stack.findall("./property[@name='scene_view_type']")}
                if not views: continue  # Native rigid pass is selected by material, with no explicit scene_view property.
                if "0" not in views: raise ValueError("Quality 0 render pass is required for quality sync")
                for tier in ("1","2"):
                    if tier not in views:
                        ET.SubElement(stack,"property",{**views["0"].attrib,"quality":tier})
        # Optional CSC wrapper contract. Omission retains the original donor behaviour.
        attach = wspec.get("terrain_attach")
        if attach:
            ground = attach == "map_zero_y_to_terrain_and_align"
            for node, chain, stack in wtriples:
                if ground:
                    # Vanilla trail uses no SimulationSpace property (world space).
                    for prop in node.findall("./property[@name='ParticleSimulationSpace']"):
                        node.remove(prop)
                else:
                    prop = node.find("./property[@name='ParticleSimulationSpace']")
                    if prop is None:
                        set_prop(node, "ParticleSimulationSpace", value="LOCAL_SPACE_POSITION_ROTATION")
                set_prop(node, "ParticlesFollowTerrain", value="true" if ground else "false")
                view = stack.find("./property[@name='scene_view_type']")
                pass_name = view.get("value") if view is not None else ""
                if pass_name in ("DISTORTION","DEFERRED_DECAL","DEFERRED_DECAL_EMISSIVE","DEFERRED_DYNAMIC_LIGHT"):
                    # These IR types describe a fixed optic/footprint/light. Do not inherit a
                    # random launch offset or upward ballistic drift from a vanilla donor.
                    set_prop(node, "SpawnAreaBox", text=vec3_curve(0,0,0))
                    set_prop(node, "SpawnAreaCurve", text=encode_curve({"inner_radius":[(0.,0.)],"outer_radius":[(0.,0.)]}))
                    set_prop(node, "ParticleInitialSpeed", text=scalar_curve(0))
                    for force in ("ForceA","ForceB","ForceC"):
                        set_prop(node, force, text=vec3_curve(0,0,0))
                if ground and pass_name == "DISTORTION":
                    for p in chain.findall("./quality/modifier[@fragment='vs_translation']/property[@name='restriction']"):
                        p.set("value", "2")
        pages = []
        for i in range(0, max(1, len(wtriples)), 40):
            chunk = wtriples[i:i + 40]
            if not chunk:
                continue
            pkey = lbase if i == 0 else f"{lbase}_p{i // 40:02d}"
            pages.append((pkey, chunk))
            lib_keys.append(pkey)
            pack_assets[f"vfx\\{pkey}.xml"] = build_library_xml(chunk)

        root = ET.Element("root")
        ET.SubElement(root, "shader_library", {"file": "fx/particle_vfx_library.hlsl"})
        vfx = ET.SubElement(root, "vfx", {"revision": "2", "construction_type": "2", "enable_in_ted": "false", "camera_following": "false", "ref_spawn_speed": "-1.00"})
        ET.SubElement(vfx, "emitters")
        ET.SubElement(vfx, "behaviour_chains")
        ET.SubElement(vfx, "effect_render_stacks")
        vrefs = ET.SubElement(root, "vfx_references")
        sc = float(wspec.get("scale", 1.0))
        dly = float(wspec.get("delay_sec", 0.0))
        w_pos = wspec.get("pos_xyz", [0.0, 0.0, 0.0])
        w_rot = wspec.get("rot_xyz", [0.0, 0.0, 0.0])
        w_inf = bool(wspec.get("infinite_life", manifest["meta"].get("looping", False)))
        w_spd = float(wspec.get("simulation_speed_multiplier", 1.0))
        for pi, (pkey, chunk) in enumerate(pages):
            ref = ET.SubElement(vrefs, "vfx", {
                "inst_name": f"{wk}_p{pi:02d}",
                "vfx_ref": pkey,
                "notes": "",
                "scale_min": f"{sc:.4f}", "scale_max": f"{sc:.4f}",
                "pos_x": f"{float(w_pos[0]):.4f}", "pos_y": f"{float(w_pos[1]):.4f}", "pos_z": f"{float(w_pos[2]):.4f}",
                "rot_x": f"{float(w_rot[0]):.4f}", "rot_y": f"{float(w_rot[1]):.4f}", "rot_z": f"{float(w_rot[2]):.4f}",
                "start_time_min": f"{dly:.4f}", "start_time_max": f"{dly:.4f}",
                "infinite_life": "true" if w_inf else "false",
                "simulation_speed_multiplier": f"{w_spd:.4f}",
            })
            for em, _, _ in chunk:
                ET.SubElement(ref, "emitter", {
                    "name": em.get("id"),
                    "enable": "true",
                    "tint_min_r": "1.00", "tint_min_g": "1.00", "tint_min_b": "1.00", "tint_min_a": "1.00",
                    "tint_max_r": "1.00", "tint_max_g": "1.00", "tint_max_b": "1.00", "tint_max_a": "1.00",
                    "particle_lifetime_bias_min": "0.00", "particle_lifetime_bias_max": "0.00",
                    "emitter_lifetime_bias_min": "0.00", "emitter_lifetime_bias_max": "0.00",
                    "spawn_rate_scale_factor_min": "1.00", "spawn_rate_scale_factor_max": "1.00",
                    "sound_record": "",
                })
        pack_assets[f"vfx\\{wk}.xml"] = dump_wh3_vfx_xml(root)

    for em in manifest.get("rigid_emitters",[]):
        if em.get("anim_source"):
            from native_root_track import source
            key=norm_pack_path(em["anim_pack_path"])
            blob=source(ir_dir,em["anim_source"])
            if key in pack_assets and blob!=pack_assets[key]:raise ValueError("Conflicting animation pack path: "+key)
            pack_assets[key]=blob
    return pack_assets, lib_keys, wrapper_keys


def main():
    ap = argparse.ArgumentParser(description="WH3 Route-A Universal Data-Driven VFX Baker (IR -> .pack + L1 + L2)")
    ap.add_argument("--ir", required=True, help="IR directory containing manifest.json")
    ap.add_argument("--pack", required=True, help="Target PFH5 .pack path to update")
    ap.add_argument("--baseline-pack", help="Optional immutable clean baseline pack (defaults to <pack>.bak_pre_ir)")
    ap.add_argument("--work-dir", help="Optional staging & preview output directory")
    ap.add_argument("--env", default="daylight", choices=ENV_NAMES, help="WH3 environment preset for L2 preview")
    ap.add_argument("--preview-pack",help="Before writing, compare every compiled asset to the reviewed Stage-1 sandbox pack")
    ap.add_argument("--width",type=int,default=640);ap.add_argument("--height",type=int,default=360)
    ap.add_argument("--supersample",type=int,choices=(1,2),default=1);ap.add_argument("--cam-mode",choices=("auto","ground","beam","close"),default="auto")
    ap.add_argument("--skip-l2", action="store_true", help="Skip L2 GPU preview generation")
    ap.add_argument("--render-video", action="store_true", help="Also render 30fps L2 preview MP4")
    ap.add_argument("--slope-deg",type=float, default=0.)
    args = ap.parse_args()
    if not -35<=args.slope_deg<=35:ap.error("--slope-deg range: -35..35")

    # Step 1: L1-A Preflight Gate
    print("[Gate L1-A] Running preflight spec validation on IR...")
    ir_rep = validate_ir(args.ir)
    ir_rep.print_summary()
    if not ir_rep.passed:
        print("[ABORT] IR failed L1-A spec validation. Fix errors above before baking.")
        sys.exit(1)

    with open(os.path.join(args.ir, "manifest.json"), "r", encoding="utf-8") as f:
        manifest = json.load(f)

    eff_id = manifest["meta"]["effect_id"]
    work_dir = args.work_dir or os.path.join(os.path.dirname(os.path.abspath(args.ir)), f"build_{eff_id}")
    from pfh5 import guard_output
    guard_output(work_dir);guard_output(args.pack)
    backup_path=args.baseline_pack or args.pack+".bak_pre_ir"
    if not os.path.isfile(backup_path):guard_output(backup_path)
    staging_dir = os.path.join(work_dir, "staging")
    os.makedirs(staging_dir, exist_ok=True)

    from host_checks import check_hosts
    base=args.baseline_pack or (args.pack+".bak_pre_ir")
    check_hosts(manifest,base if os.path.isfile(base) else args.pack)

    # Step 2: Compile Assets
    print("[Bake 1/4] Compiling DDS textures...")
    pack_assets, tex_vpaths = _compile_textures(args.ir, manifest, staging_dir)
    print("[Bake 2/4] Compiling Route-A materials...")
    mat_assets, mat_vpaths = _compile_materials(manifest, tex_vpaths)
    pack_assets.update(mat_assets)
    print("[Bake 3/4] Compiling RMV2 v7 meshes (pivot=0) & .wsmodels...")
    mesh_assets, mesh_wsmodels = _compile_meshes(args.ir, manifest, mat_vpaths)
    pack_assets.update(mesh_assets)
    print("[Bake 4/4] Compiling auto-paged VFX XMLs, wrappers & particle_effects_tables...")
    vfx_assets, lib_keys, wrapper_keys = _compile_emitters_and_wrappers(manifest, tex_vpaths, mesh_wsmodels,args.ir)
    pack_assets.update(vfx_assets)

    db_slice_path = f"db\\particle_effects_tables\\{manifest['meta']['prefix']}_{eff_id}"
    pack_assets[db_slice_path] = build_particle_effects_table(registration_keys(manifest,lib_keys))

    if args.preview_pack:
        approved=PFH5Pack(args.preview_pack)
        if {norm_pack_path(n).lower() for n in pack_assets}!={norm_pack_path(row[0]).lower() for row in approved.entries}:
            raise ValueError("Reviewed sandbox inventory changed: review again before writing")
        for name,data in pack_assets.items():
            if not approved.has(name) or approved.read(name)!=data:raise ValueError("Preview asset differs; review again before writing: "+name)
        print(f"[Parity] {len(pack_assets)} compiled payloads match reference sandbox")
    # Immutable backup & PFH5 write + SHA-256 readback verify
    baseline = args.baseline_pack or (args.pack + ".bak_pre_ir")
    if os.path.realpath(baseline)==os.path.realpath(args.pack):raise ValueError("Immutable baseline cannot be the output pack")
    if not os.path.isfile(baseline):
        if os.path.isfile(args.pack):
            shutil.copy2(args.pack, baseline)
            print(f"[Backup] Created immutable baseline backup -> {baseline}")
        else:
            raise FileNotFoundError(f"Target pack does not exist and no --baseline-pack supplied: {args.pack}")

    base_pk = PFH5Pack(baseline)
    stats = base_pk.write_updated(args.pack, pack_assets, compress_threshold=0)
    print(f"[PFH5] Write + SHA-256 readback verified: {stats}")

    # Step 3: L1-B Post-Bake Pack Gate
    print("[Gate L1-B] Running post-bake binary pack readback validation...")
    pk_rep = validate_pack(args.pack, wrapper_keys, check_dds_borders=True)
    pk_rep.print_summary()
    if not pk_rep.passed:
        print("[FAIL] Compiled pack failed L1-B binary audit!")
        sys.exit(1)

    # Step 4: L2 Pack-driven WH3 GPU Preview
    if not args.skip_l2:
        print("[Gate L2] Running Pack-driven WH3 GPU preview...")
        preview_py = os.path.join(HERE, "wh3_true_preview.py")
        out_sheet = os.path.join(work_dir, f"{eff_id}_l2_sheet.png")
        out_diag = os.path.join(work_dir, f"{eff_id}_l2_diag.png")
        out_tel = os.path.join(work_dir, f"{eff_id}_l2_telemetry.json")
        dur = float(manifest["meta"]["duration_sec"])
        phases = manifest["meta"].get("preview_phases_sec") or [round(dur * f, 2) for f in (0.06, 0.22, 0.40, 0.58, 0.78, 0.94)]
        cmd = [
            PYTHON_EXE, preview_py,
            "--pack", args.pack, "--width",str(args.width),"--height",str(args.height),"--cam-mode",args.cam_mode,"--supersample",str(args.supersample),
            "--wrappers", ",".join(wrapper_keys),
            "--out-sheet", out_sheet,
            "--diagnostic-strip", out_diag,
            "--telemetry-out", out_tel,
            "--env", args.env, "--slope-deg", str(args.slope_deg),
            "--times", ",".join(str(x) for x in phases),
            "--diag-time", str(phases[len(phases) // 2]),
        ]
        if any(w.get("terrain_attach") for w in manifest["host_binding"]["wrappers"]):cmd.append("--sample-spawn-box")
        if args.cam_mode=="auto" and "preview_framing_m" in manifest["meta"]:cmd.extend(["--framing-m",*map(str,manifest["meta"]["preview_framing_m"])])
        if args.render_video:
            cmd.extend(["--video", os.path.join(work_dir, f"{eff_id}_l2_preview.mp4"), "--video-duration", str(dur)])
        res = subprocess.run(cmd)
        if res.returncode != 0:
            print("[FAIL] L2 previewer exited with error!")
            sys.exit(res.returncode)

    if not args.skip_l2:
        with open(out_tel,encoding="utf8") as f: omissions=json.load(f)["omissions"]
        if omissions: print("[L2 PARTIAL]", *omissions,sep="\n")
    print(f"\n[DONE] Universal Route-A bake complete for '{eff_id}' -> {args.pack}")


if __name__ == "__main__":
    main()
