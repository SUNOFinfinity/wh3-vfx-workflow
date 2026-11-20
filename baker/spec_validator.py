"""L1 Automated Preflight & Post-Bake Pack Validator for WH3 Route-A VFX (v1.0).

Modes:
  1. --ir <ir_dir>
     Validates a Houdini/DCC/AI-exported IR directory against WH3_VFX_AUTHORING_SPEC.md
     and manifest.schema.json before baking.
  2. --pack <pack_path> --wrappers <vfx_key1,vfx_key2,...>
     Performs strict binary readback verification on a compiled PFH5 .pack:
       - RMV2 v7 header & Group0 pivot_xyz == (0,0,0) at ghdr[628:640]
       - .wsmodel / .xml.material / .dds full asset closure
       - BC7 DDS texconv decode resolution (>=256 for decals/opticals) & zero-border leak check
       - VFX XML 40-emitter paging, 3-tier quality sync, wrapper<->library 100% reachability
       - Curve keyframe budget (<=28 warn/<=60 hard), ParticleScale<=30, velocity_stretch_cap hygiene
       - Decal render_pass / pre_multiplied_alpha / ps_lighting pairing

Exit code: 0 on PASS, 1 on FAIL (any ERROR).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET

import baker_config  # retains the configured DLL search handle

import numpy as np
from PIL import Image

from dds_compiler import TEXCONV_PATH, audit_dds_border
from pfh5 import PFH5Pack, norm_pack_path
from vfx_xml_compiler import parse_wh3_vfx_xml

from baker_config import WH3_DATA_DIR
ROUTE_A_SHADERS = {
    "shaders/wh3_vfx_distortion_mesh.xml.shader",
    "shaders/vfx_rigid_unlit_emissive_fresnel.xml.shader",
    "shaders/wh_vfx_mesh_vertex_anim.xml.shader",
    "shaders/wh3_rigid_baked_fluid_simulation.xml.shader",
}
VALID_ARCHETYPES = {
    "sky_beam_vortex",
    "projectile_trail",
    "explosion_shockwave",
    "aura_buff_debuff",
    "fluid_shatter_vat",
}


class AuditReport:
    def __init__(self, mode: str, target: str):
        self.mode = mode
        self.target = target
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.metrics: dict = {}

    def error(self, code: str, message: str, item: str = "", suggestion=None):
        self.errors.append({"severity":"ERROR","code":code,"item":item,"message":message,**({"path":item} if item.startswith("/") else {}),**({"suggested_value":suggestion} if suggestion is not None else {})})

    def warn(self, code: str, message: str, item: str = ""):
        self.warnings.append({"severity":"WARNING","code": code, "item": item, "message": message})

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "target": self.target,
            "status": "PASS" if self.passed else "FAIL",
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "metrics": self.metrics,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    def print_summary(self):
        status = "PASS" if self.passed else "FAIL"
        print(f"=== WH3 VFX L1 Spec Validator [{self.mode}] : {status} ===")
        print(f"Target   : {self.target}")
        print(f"Errors   : {len(self.errors)} | Warnings : {len(self.warnings)}")
        if self.metrics:
            for k, v in self.metrics.items():
                print(f"  metric.{k}: {v}")
        for e in self.errors:
            loc = f" ({e['item']})" if e["item"] else ""
            print(f"  [ERROR] {e['code']}{loc}: {e['message']}")
        for w in self.warnings:
            loc = f" ({w['item']})" if w["item"] else ""
            print(f"  [WARN ] {w['code']}{loc}: {w['message']}")


def parse_ca_curve_string(text: str) -> dict[str, list[tuple[float, float]]]:
    """Parse CA curve string into axis -> list[(t, v)] (combining min & max keys)."""
    result: dict[str, list[tuple[float, float]]] = {}
    if not text or ":" not in text:
        return result
    curve_part = text.split(":", 1)[0]
    for seg in curve_part.split("|"):
        tokens = seg.split(";")
        if len(tokens) < 2:
            continue
        header = tokens[0].split(",")
        axis_name = header[0]
        pts = []
        for p in tokens[1:]:
            fields = p.split(",")
            if len(fields) >= 2:
                try:
                    pts.append((float(fields[0]), float(fields[1])))
                except ValueError:
                    pass
        result[axis_name] = pts
    return result


def inspect_rmv2_binary(raw: bytes) -> dict:
    """Parse RMV2 v7 file header, LOD0 header, and Group0 header (including pivot_xyz)."""
    if len(raw) < 140 + 28 + 2248:
        raise ValueError(f"RMV2 too small: {len(raw)} bytes")
    magic = raw[:4]
    ver = struct.unpack_from("<I", raw, 4)[0]
    lod_count = struct.unpack_from("<I", raw, 8)[0]
    if magic != b"RMV2":
        raise ValueError(f"Invalid RMV2 magic: {magic!r}")
    if ver != 7:
        raise ValueError(f"Unsupported RMV2 version: {ver} (expected 7)")
    if lod_count < 1:
        raise ValueError(f"RMV2 has 0 LODs")
    # LOD0 28B header at offset 140: <4If2I = (group_count, vdata_size, idata_size, first_group_offset, lod_dist, u1, u2)
    gcnt, vsz, isz, goff, lod_dist, _, _ = struct.unpack_from("<4If2I", raw, 140)
    if goff + 2248 > len(raw):
        raise ValueError(f"Group0 header out of bounds: goff={goff}, len={len(raw)}")
    ghdr = raw[goff:goff + 2248]
    # Group0 pivot_xyz at ghdr[628:640] (file offset 796..808 for standard 1-LOD RMV2)
    pivot = struct.unpack_from("<3f", ghdr, 628)
    # Vertex / index counts are at ghdr[12:20] or ghdr[24:32] depending on vformat; check bbox at ghdr[28:52]
    # Extract shader/vformat/counts safely
    bb_min = struct.unpack_from("<3f", ghdr, 24)
    bb_max = struct.unpack_from("<3f", ghdr, 36)
    _,glen,vo,vc,io,ic=struct.unpack_from("<6I",ghdr)
    if vc==0 or io-vo != 32*vc or goff+glen>len(raw) or glen!=io+2*ic: raise ValueError("Invalid static RMV2 spans")
    pos=np.ndarray((vc,3),dtype="<f2",buffer=raw,offset=goff+vo,strides=(32,2)).astype(np.float32)
    if not np.isfinite(pos).all() or not np.isfinite([bb_min,bb_max]).all() or np.any(np.array(bb_min)>bb_max):
        raise ValueError("Nonfinite geometry or inverted bbox")
    contained=bool((pos.min(0)>=bb_min).all() and (pos.max(0)<=bb_max).all())
    return {
        "version": ver,
        "lod_count": lod_count,
        "group_count": gcnt,
        "goff": goff,
        "vdata_size": vsz,
        "idata_size": isz,
        "pivot_xyz": tuple(float(x) for x in pivot),
        "contains_quantized_vertices":contained,
        "bbox_min": tuple(float(x) for x in bb_min),
        "bbox_max": tuple(float(x) for x in bb_max),
    }


def validate_ir(ir_dir: str) -> AuditReport:
    rep = AuditReport("IR_PREFLIGHT", os.path.abspath(ir_dir))
    manifest_path = os.path.join(ir_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        rep.error("IR_MISSING_MANIFEST", f"manifest.json not found in {ir_dir}")
        return rep

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            m = json.load(f)
    except Exception as exc:
        rep.error("IR_INVALID_JSON", f"Failed to parse manifest.json: {exc}")
        return rep

    # Top-level required keys
    for k in ("schema_version", "meta", "archetype", "host_binding", "textures", "meshes", "materials"):
        if k not in m:
            rep.error("SCHEMA_MISSING_KEY", f"Missing required top-level key '{k}'")
    if not rep.passed:
        return rep

    if m.get("schema_version") != "1.0.0":
        rep.error("SCHEMA_VERSION", f"Unsupported schema_version={m.get('schema_version')!r}, expected '1.0.0'")

    arch = m.get("archetype")
    if arch not in VALID_ARCHETYPES:
        rep.error("SCHEMA_ARCHETYPE", f"Unknown archetype={arch!r}; expected one of {sorted(VALID_ARCHETYPES)}")

    meta = m.get("meta", {})
    for mk in ("effect_id", "prefix", "duration_sec"):
        if mk not in meta:
            rep.error("SCHEMA_META_KEY", f"meta missing '{mk}'")

    wrappers = m.get("host_binding", {}).get("wrappers", [])
    wrapper_keys = {w["vfx_key"] for w in wrappers if isinstance(w, dict) and "vfx_key" in w}
    if not wrapper_keys:
        rep.error("HOST_NO_WRAPPERS", "host_binding.wrappers must define at least one wrapper")

    occupied={}
    from collections import Counter
    counts=Counter(e.get("wrapper_vfx_key") for group in ("rigid_emitters","decal_emitters","particle_emitters","distortion_emitters","lights") for e in m.get(group,[]))
    for i,w in enumerate(wrappers):
        if not isinstance(w,dict) or not all(isinstance(w.get(k),str) and w[k] for k in ("vfx_key","library_base")):
            rep.error("HOST_KEY","Supply vfx_key and library_base",f"/host_binding/wrappers/{i}");continue
        key=w["vfx_key"];base=w["library_base"]
        for field,value in [("vfx_key",key)]+[("library_base",base if p==0 else f"{base}_p{p:02d}") for p in range((counts[key]+39)//40)]:
            lp=f"/host_binding/wrappers/{i}/{field}"
            if value.lower() in occupied:rep.error("HOST_PATH_COLLISION",f"Generated XML {value!r} also belongs to {occupied[value.lower()]}",lp)
            occupied[value.lower()]=lp
    if not rep.passed:return rep
    for i,e in enumerate(m.get("rigid_emitters",[])):
        if "terrain_attach" not in e:rep.error("RIGID_TERRAIN_ATTACH","Declare terrain_attach",f"/rigid_emitters/{i}/terrain_attach","none")
    textures = m.get("textures", {})
    materials = m.get("materials", {})
    meshes = m.get("meshes", {})

    # 1. Validate textures
    for tid, tspec in textures.items():
        sp = os.path.join(ir_dir, tspec.get("source_path", ""))
        usage = tspec.get("usage", "")
        fmt = tspec.get("format", "")
        if not os.path.isfile(sp):
            rep.error("TEX_MISSING_FILE", f"Source texture missing: {sp}", tid)
            continue
        # Load image or numpy array
        arr = None
        if sp.lower().endswith(".npy"):
            arr = np.load(sp)
        elif sp.lower().endswith(".npz"):
            z = np.load(sp)
            arr = z[list(z.keys())[0]]
        else:
            img = Image.open(sp).convert("RGBA")
            arr = np.asarray(img, dtype=np.float32) / 255.0

        if not np.all(np.isfinite(arr)):
            rep.error("TEX_NONFINITE", "Texture contains NaN/Inf values", tid)
            continue

        if usage in ("decal_emissive", "decal_scorch", "particle_optical"):
            h, w = arr.shape[:2]
            t_sz = tspec.get("target_size", [w, h])
            eff_w, eff_h = int(t_sz[0]), int(t_sz[1])
            if eff_w < 256 or eff_h < 256:
                rep.error("TEX_LOW_RES", f"{usage} target size {eff_w}x{eff_h} < 256x256 (causes Minecraft blockiness!)", tid)
            if w < 128 or h < 128:
                rep.warn("TEX_SMALL_SRC", f"Source {w}x{h} is small for {usage}; prefer >=256 native synthesis", tid)
            if tspec.get("require_zero_border", True) and arr.ndim == 3 and arr.shape[2] >= 4:
                border_a = np.concatenate([arr[0, :, 3], arr[-1, :, 3], arr[:, 0, 3], arr[:, -1, 3]])
                if float(np.max(border_a)) > 0.01:
                    rep.error("TEX_BORDER_LEAK", f"Non-tileable {usage} has border alpha max={float(np.max(border_a)):.3f} > 0.01", tid)

        # Non-power-of-two 2D texture check (ensures consistent DX11 & L2 mipmap chains)
        if usage not in ("vectorfield_3d", "vat_position_2d", "vat_normal_2d","vat_uv_2d") and arr.ndim >= 2:
            h, w = arr.shape[:2]
            t_sz = tspec.get("target_size", [w, h])
            eff_w, eff_h = int(t_sz[0]), int(t_sz[1])
            if (eff_w & (eff_w - 1)) != 0 or (eff_h & (eff_h - 1)) != 0:
                rep.warn("TEX_NON_POT", f"2D texture target size {eff_w}x{eff_h} is not power-of-two (prefer 256/512/1024/2048 for clean DX11 mipmaps)", tid)

        if usage == "mesh_flux":
            lum = arr if arr.ndim == 2 else arr[:, :, 0]
            mn, mx = float(np.min(lum)), float(np.max(lum))
            if mn > 0.40:
                rep.warn("TEX_FLUX_FLAT_FLOOR", f"mesh_flux min={mn:.3f} > 0.40 kills directional energy rushing contrast", tid)
            if mx < 0.70:
                rep.warn("TEX_FLUX_LOW_PEAK", f"mesh_flux max={mx:.3f} < 0.70 weakens highlights", tid)

        if usage == "decal_scorch" and not tspec.get("premultiply_alpha", False):
            rep.warn("TEX_SCORCH_PREMUL", "decal_scorch should enable premultiply_alpha=true for DEFERRED_DECAL", tid)

        if usage in ("vectorfield_3d",) and fmt != "A16B16G16R16F_3D":
            rep.error("TEX_VF_FORMAT", f"vectorfield_3d must use A16B16G16R16F_3D, got {fmt}", tid)
        if usage in ("vat_position_2d", "vat_normal_2d","vat_uv_2d") and fmt != "A32B32G32R32F_2D":
            rep.error("TEX_VAT_FORMAT", f"{usage} must use A32B32G32R32F_2D, got {fmt}", tid)

    # 2. Validate materials
    for mat_id, mspec in materials.items():
        shader = mspec.get("shader", "")
        if shader == "vfx_rigid_unlit_emissive_fresnel" and (mspec.get("glow_strength") != 0 or mspec.get("glow_colour") != [0,0,0]):
            rep.error("MAT_UNSUPPORTED_UNLIT_PROFILE", "Only the audited zero-tint/zero-emissive profile is supported", mat_id)
        if shader == "wh3_vfx_distortion_mesh" and mspec.get("depth_fade_distance", 2.) <= 0:
            rep.error("MAT_DIST_DEPTH_FADE", "Native depth fade divisor must be positive", mat_id)
        if shader not in ("wh_vfx_mesh_vertex_anim", "wh3_rigid_baked_fluid_simulation", "vfx_rigid_unlit_emissive_fresnel", "wh3_vfx_distortion_mesh"):
            rep.error("MAT_INVALID_SHADER", f"Shader '{shader}' is not an approved Route-A shader", mat_id)
        for slot in ("tex_a", "tex_b", "tex_c", "global_mask", "colour_overlay"):
            tid = mspec.get(slot)
            if tid and tid not in textures:
                rep.error("MAT_MISSING_TEX", f"Slot {slot} references unknown texture_id '{tid}'", mat_id)
        if shader=="wh3_rigid_baked_fluid_simulation":
            if not mspec.get("vat_uv_tex"):
                rep.warn("VAT_LEGACY_LAYOUT","Legacy VAT compiles for compatibility; supply native position, normal and UV atlases plus UV0 lookup coordinates before visual signoff",mat_id)
            else:
                for field in ("vat_pos_tex","vat_nrm_tex","vat_uv_tex"):
                    if mspec.get(field) not in textures:rep.error("VAT_SLOT","Missing VAT texture reference",f"/materials/{mat_id}/{field}")
                    elif textures[mspec[field]].get("format")!="A32B32G32R32F_2D":rep.error("VAT_SLOT_FORMAT","Native lava-splat profile uses float4 2D DDS",f"/textures/{mspec[field]}/format","A32B32G32R32F_2D")
        if shader == "wh_vfx_mesh_vertex_anim":
            for channel in ("tex_a_channels","tex_b_channels","tex_c_channels"):
                try:
                    weights=[float(v) for v in mspec.get(channel,"1,0,0,0").split(",")]
                    if len(weights)!=4 or not np.isfinite(weights).all():raise ValueError
                except (AttributeError,ValueError):
                    rep.error("MAT_CHANNELS","Use four finite comma-separated RGBA weights",f"/materials/{mat_id}/{channel}", "1,0,0,0")
            t_alpha = float(mspec.get("total_alpha_strength", 1.0))
            glow_str = float(mspec.get("glow_strength", 1.0))
            gc = mspec.get("glow_colour", [1.0, 1.0, 1.0])
            chroma = max(gc) - min(gc) if len(gc) == 3 else 0.0
            if t_alpha > 1.5 and glow_str < 3.5 and chroma < 0.35:
                rep.warn("MAT_MATTE_CLAY_RISK",
                         f"High alpha ({t_alpha}) + low glow ({glow_str}) + pale glow_colour ({gc}) risks matte beige clay under WH3 alpha blend!",
                         mat_id)
            if not mspec.get("global_mask"):
                rep.warn("MAT_NO_GLOBAL_MASK", "No global_mask set; geometric silhouette may look overly uniform", mat_id)

    # 3. Validate meshes
    for mesh_id, mspec in meshes.items():
        if mspec.get("material_id") not in materials:
            rep.error("MESH_MISSING_MAT", f"References unknown material_id '{mspec.get('material_id')}'", mesh_id)
        npz_path = os.path.join(ir_dir, mspec.get("source_npz", ""))
        if not os.path.isfile(npz_path):
            rep.error("MESH_MISSING_NPZ", f"Source mesh missing: {npz_path}", mesh_id)
            continue
        z = np.load(npz_path)
        for req in ("vertices", "indices", "normals", "uvs"):
            if req not in z:
                rep.error("MESH_MISSING_ARRAY", f".npz missing required array '{req}'", mesh_id)
        if not all(r in z for r in ("vertices", "indices", "normals", "uvs")):
            continue
        verts = np.asarray(z["vertices"], dtype=np.float32)
        inds = np.asarray(z["indices"], dtype=np.int64).reshape(-1, 3)
        norms = np.asarray(z["normals"], dtype=np.float32)
        uvs = np.asarray(z["uvs"], dtype=np.float32)
        if verts.shape[0] >= 65535:
            rep.error("MESH_VERT_LIMIT", f"Vertex count {verts.shape[0]} >= 65535 uint16 limit", mesh_id)
        if verts.shape[0] == 0 or inds.shape[0] == 0:
            rep.error("MESH_EMPTY", "Mesh has 0 vertices or 0 triangles", mesh_id)
            continue
        if not (np.all(np.isfinite(verts)) and np.all(np.isfinite(norms)) and np.all(np.isfinite(uvs))):
            rep.error("MESH_NONFINITE", "Mesh arrays contain NaN/Inf", mesh_id)
        if int(np.max(inds)) >= verts.shape[0] or int(np.min(inds)) < 0:
            rep.error("MESH_INDEX_OOB", "Triangle indices out of vertex range", mesh_id)
            continue
        vat=materials.get(mspec.get("material_id"),{})
        if vat.get("vat_uv_tex") and all(vat.get(f) in textures for f in ("vat_pos_tex","vat_nrm_tex","vat_uv_tex")):
            try:
                from bake_from_ir import _load_texture_array
                from vat_native import texels
                shapes=[_load_texture_array(os.path.join(ir_dir,textures[vat[f]]["source_path"])).shape for f in ("vat_pos_tex","vat_nrm_tex","vat_uv_tex")]
                h,w,c=shapes[0];frames=vat.get("vat_frame_count",60)
                if c!=4 or len(set(shapes))!=1 or frames<1 or h%frames or vat.get("vat_fps",30)<=0:raise ValueError("Need three matching HxWx4 atlases; positive fps and whole rows per frame")
                uv=uvs[:,:2].astype(np.float16).astype(np.float32)
                for frame in (0,frames-1):texels(uv,w,h,frames,frame)
            except (KeyError,TypeError,ValueError,OSError) as exc:rep.error("VAT_NATIVE_LAYOUT",str(exc),f"/meshes/{mesh_id}/source_npz")
        # DirectX CW winding check: cross(p1-p0, p2-p0) . avg_N >= 0
        p0, p1, p2 = verts[inds[:, 0]], verts[inds[:, 1]], verts[inds[:, 2]]
        fn = np.cross(p1 - p0, p2 - p0)
        avg_n = norms[inds[:, 0]] + norms[inds[:, 1]] + norms[inds[:, 2]]
        dots = np.sum(fn * avg_n, axis=1)
        cw_ratio = float(np.mean(dots >= 0.0))
        if cw_ratio < 0.5 and not mspec.get("auto_fix_winding", True):
            rep.error("MESH_WINDING_CCW", f"Only {cw_ratio*100:.1f}% faces are DX CW and auto_fix_winding=false", mesh_id)
        elif cw_ratio < 0.5:
            rep.warn("MESH_WINDING_AUTOCORRECT", f"Only {cw_ratio*100:.1f}% faces are DX CW (baker will auto-flip)", mesh_id)
        if "coverage" in z:
            cov = np.asarray(z["coverage"], dtype=np.float32)
            if float(np.min(cov)) < -0.01 or float(np.max(cov)) > 1.01:
                rep.error("MESH_COVERAGE_RANGE", f"coverage out of [0,1]: [{np.min(cov):.3f}, {np.max(cov):.3f}]", mesh_id)
        # RMV2 float16 position quantization check: span > 64m causes visible ~0.06m stair-stepping
        ps = mspec.get("prescale_xyz", [1.0, 1.0, 1.0])
        v_scaled = verts * np.array(ps, dtype=np.float32)[None, :]
        max_abs = float(np.max(np.abs(v_scaled)))
        if max_abs>65504 or np.abs(uvs).max()>65504:
            rep.error("MESH_F16_OVERFLOW","Finite binary16 maximum is 65504; reduce the source coordinates or prescale.",f"/meshes/{mesh_id}/source_npz")
        if mspec.get("donor_profile","cylinder") not in ("cylinder","sphere","plane"):rep.error("RMV2_DONOR","Unknown static donor",f"/meshes/{mesh_id}/donor_profile","cylinder")
        if max_abs > 64.0:
            rep.warn("MESH_F16_PRECISION", f"Mesh max coord {max_abs:.1f}m > 64m (RMV2 float16 quantization ~{max_abs/1024:.3f}m; prefer smaller base mesh * ParticleScale)", mesh_id)

    # 4. Validate emitters
    def check_curve(curve, label, item, max_pts=28):
        if curve is None:
            return
        if len(curve) > max_pts:
            rep.error("CURVE_TOO_MANY_KEYS", f"{label} has {len(curve)} keyframes > {max_pts} budget", item)
        ts = [pt[0] for pt in curve]
        if ts != sorted(ts):
            rep.error("CURVE_UNSORTED", f"{label} timestamps are not monotonically sorted", item)

    total_emitters_per_wrapper: dict[str, int] = {k: 0 for k in wrapper_keys}
    wrapper_rigid_layers: dict[str, list[dict]] = {k: [] for k in wrapper_keys}

    for em in m.get("rigid_emitters", []):
        name = em.get("name", "?")
        wk = em.get("wrapper_vfx_key", "")
        if wk not in wrapper_keys:
            rep.error("EMITTER_UNKNOWN_WRAPPER", f"Unknown wrapper_vfx_key '{wk}'", name)
        else:
            total_emitters_per_wrapper[wk] += 1
        if em.get("anim_source"):
            try:
                from native_root_track import source
                source(ir_dir,em["anim_source"])
                if meshes[em["mesh_id"]].get("rigid_root_bone")!="building" or not em.get("anim_pack_path","").endswith(".anim"):
                    raise ValueError("building root or AnimFile missing")
            except (OSError,ValueError,KeyError,struct.error) as exc:rep.error("RIGID_ROOT_CLIP",str(exc),name)
        if em.get("mesh_id") not in meshes:
            rep.error("RIGID_UNKNOWN_MESH", f"Unknown mesh_id '{em.get('mesh_id')}'", name)
        for ck in ("position_curve", "rotation_deg_curve", "scale_curve", "colour_curve", "alpha_curve"):
            check_curve(em.get(ck), ck, name)
        sc = em.get("scale_curve")
        if sc:
            max_s = max(max(abs(pt[1]), abs(pt[2]), abs(pt[3])) for pt in sc)
            if max_s > 30.0:
                rep.error("RIGID_SCALE_OVER_BUDGET", f"Max ParticleScale={max_s:.2f} > 30.0 (pre-scale source mesh!)", name)
            elif max_s > 20.0:
                rep.warn("RIGID_SCALE_HIGH", f"Max ParticleScale={max_s:.2f} > 20.0", name)
        # Check eccentric EmitterPosition + non-zero EmitterOrientation lever-arm fling risk in WH3!
        pc = em.get("position_curve") or []
        rc = em.get("rotation_deg_curve") or []
        # WH3 Engine Law: continuous_rigid ONLY samples EmitterPosition at spawn time (t_spawn)!
        if int(em.get("burst_count", 1)) == 1 and len(pc) > 1:
            p_start = pc[0][1:4]
            max_disp = max(
                math.sqrt((pt[1] - p_start[0])**2 + (pt[2] - p_start[1])**2 + (pt[3] - p_start[2])**2)
                for pt in pc[1:]
            )
            if max_disp > 0.05:
                rep.warn(
                    "EMITTER_POS_ANIM_IGNORED_BY_WH3",
                    f"Single-shot rigid emitter has animated position_curve (max_disp={max_disp:.2f}m): WH3 ONLY samples EmitterPosition at t_spawn and will NOT move the particle after spawn! Use force_xyz (ForceA) or skeletal .anim.",
                    name,
                )
        max_pos_r = max((math.sqrt(pt[1]**2 + pt[2]**2 + pt[3]**2) for pt in pc), default=0.0)
        max_rot_deg = max((max(abs(pt[1]), abs(pt[2]), abs(pt[3])) for pt in rc), default=0.0)
        if max_pos_r > 1.0 and max_rot_deg > 5.0:
            rep.warn(
                "ECCENTRIC_EMITTER_ORIENTATION",
                f"EmitterPosition radius={max_pos_r:.2f}m > 1.0m combined with EmitterOrientation={max_rot_deg:.1f}deg: WH3 LOCAL_SPACE_POSITION_ROTATION couples rotation with eccentric offset (lever-arm fling! Prefer centered pivot or skeletal .anim)",
                name,
            )
        if wk in wrapper_rigid_layers and em.get("mesh_id") in meshes:
            mat_id = meshes[em["mesh_id"]].get("material_id", "")
            mat_spec = materials.get(mat_id, {})
            p0 = pc[0][1:4] if pc else [0.0, 0.0, 0.0]
            wrapper_rigid_layers[wk].append({
                "name": name,
                "pos0": p0,
                "sort": int(mat_spec.get("sort_override", 255)),
                "ta": float(mat_spec.get("total_alpha_strength", 1.0)),
                "shader": mat_spec.get("shader", ""),
            })

    # Pivot sort inversion check per wrapper: opaque matte backing vs translucent layers with misaligned EmitterPosition & equal sort_override
    for wk, layers in wrapper_rigid_layers.items():
        opaques = [l for l in layers if l["shader"] == "wh_vfx_mesh_vertex_anim" and l["ta"] >= 1.5]
        for op in opaques:
            misaligned = []
            for other in layers:
                if other["name"] == op["name"] or other["shader"] != "wh_vfx_mesh_vertex_anim":
                    continue
                if other["ta"] >= 1.5 or other["sort"] != op["sort"]:
                    continue
                dist = math.sqrt(sum((op["pos0"][i] - other["pos0"][i]) ** 2 for i in range(3)))
                if dist > 1.5:
                    misaligned.append((other["name"], dist))
            if misaligned:
                sample = ", ".join(f"{n}({d:.1f}m)" for n, d in misaligned[:4])
                rep.warn(
                    "PIVOT_SORT_INVERSION_RISK",
                    f"Opaque/matte layer '{op['name']}' (total_alpha={op['ta']}, sort={op['sort']}) has EmitterPosition offset >1.5m from sibling translucent layers [{sample}] with equal sort_override — wrapper rotation/camera angles will invert WH3 draw order and paint matte over glowing layers!",
                    wk,
                )
                break

    for em in m.get("particle_emitters", []):
        name = em.get("name", "?")
        wk = em.get("wrapper_vfx_key", "")
        if wk not in wrapper_keys:
            rep.error("EMITTER_UNKNOWN_WRAPPER", f"Unknown wrapper_vfx_key '{wk}'", name)
        else:
            total_emitters_per_wrapper[wk] += 1
        if em.get("texture_id") not in textures:
            rep.error("PARTICLE_UNKNOWN_TEX", f"Unknown texture_id '{em.get('texture_id')}'", name)
        cap = float(em.get("velocity_stretch_cap", 0.0))
        if cap > 14.0:
            rep.error("PARTICLE_STRETCH_CAP", f"velocity_stretch_cap={cap} > 14.0 (risks screen-spanning laser spikes!)", name)
        for ck in ("size_curve", "colour_curve", "alpha_curve"):
            check_curve(em.get(ck), ck, name)

    for em in m.get("decal_emitters", []):
        name = em.get("name", "?")
        wk = em.get("wrapper_vfx_key", "")
        if wk not in wrapper_keys:
            rep.error("EMITTER_UNKNOWN_WRAPPER", f"Unknown wrapper_vfx_key '{wk}'", name)
        else:
            total_emitters_per_wrapper[wk] += 1
        if em.get("texture_id") not in textures:
            rep.error("DECAL_UNKNOWN_TEX", f"Unknown texture_id '{em.get('texture_id')}'", name)
        rp = em.get("render_pass")
        premul = bool(em.get("pre_multiplied_alpha"))
        keep_lit = bool(em.get("keep_ps_lighting"))
        if rp == "DEFERRED_DECAL" and (not premul or not keep_lit):
            rep.error("DECAL_SCORCH_PAIRING", "DEFERRED_DECAL (dark scorch) MUST set pre_multiplied_alpha=true and keep_ps_lighting=true", name)
        if rp == "DEFERRED_DECAL_EMISSIVE" and premul:
            rep.warn("DECAL_EMISSIVE_PAIRING", "Premultiplied emissive decal: verify texture was authored for this pairing", name)
        for ck in ("radius_curve", "colour_curve", "alpha_curve"):
            check_curve(em.get(ck), ck, name)

    for em in m.get("distortion_emitters", []) + m.get("lights", []):
        wk = em.get("wrapper_vfx_key", "")
        if wk in total_emitters_per_wrapper:
            total_emitters_per_wrapper[wk] += 1

    rep.metrics = {
        "archetype": arch,
        "textures": len(textures),
        "meshes": len(meshes),
        "materials": len(materials),
        "emitters_per_wrapper": total_emitters_per_wrapper,
    }
    return rep


def _load_vanilla_lookup():
    """Collect known vanilla asset paths across base packs so asset closure distinguishes missing vs vanilla."""
    from vanilla_index import VanillaIndex, game_packs
    return VanillaIndex(game_packs())


def validate_pack(pack_path: str, wrapper_keys: list[str], check_dds_borders: bool = True) -> AuditReport:
    rep = AuditReport("PACK_READBACK", os.path.abspath(pack_path))
    if not os.path.isfile(pack_path):
        rep.error("PACK_MISSING", f"Pack file not found: {pack_path}")
        return rep

    pk = PFH5Pack(pack_path)
    vanilla_paths = _load_vanilla_lookup()

    def exists_anywhere(rel: str) -> bool:
        n = norm_pack_path(rel).lower()
        return pk.has(n) or n in vanilla_paths or n in {r"commontextures\default_white.dds",r"commontextures\default_black.dds",r"commontextures\default_grey.dds",r"commontextures\default_normal.dds"}

    checked_libs: set[str] = set()
    checked_wsmodels: set[str] = set()
    checked_rmv2s: set[str] = set()
    checked_materials: set[str] = set()
    non_tileable_dds: set[str] = set()
    total_emitters = 0
    rmv2_pivots_ok = 0
    dds_borders_ok = 0

    # 1. Inspect every requested wrapper and union enabled emitters across wrappers
    lib_to_enabled: dict[str, set[str]] = {}
    lib_to_wrappers: dict[str, set[str]] = {}
    lib_has_infinite: dict[str, bool] = {}
    wrapper_ref_meta: dict[str, list[dict]] = {}
    for wk in wrapper_keys:
        wpath = f"vfx\\{wk}.xml"
        if not pk.has(wpath):
            rep.error("WRAPPER_MISSING", f"Wrapper XML not found in pack: {wpath}", wk)
            continue
        wroot = parse_wh3_vfx_xml(pk.read(wpath))
        refs = list(wroot.findall("./vfx_references/vfx"))
        direct_em = list(wroot.findall("./vfx/emitters/emitter"))
        if not refs and not direct_em:
            rep.error("WRAPPER_EMPTY", f"Wrapper {wpath} has neither vfx_references nor direct emitters", wk)
            continue
        for ref in refs:
            lkey = (ref.get("vfx_ref") or "").strip()
            if not lkey:
                continue
            lib_pack_path = f"vfx\\{lkey}.xml" if not lkey.lower().endswith(".xml") else norm_pack_path(lkey)
            if not lib_pack_path.lower().startswith("vfx\\"):
                lib_pack_path = "vfx\\" + lib_pack_path
            lib_to_wrappers.setdefault(lib_pack_path, set()).add(wk)
            inf_flag = (ref.get("infinite_life") or "").strip().lower() == "true"
            if inf_flag:
                lib_has_infinite[lib_pack_path] = True
            rx = abs(float(ref.get("rot_x", "0") or 0))
            ry = abs(float(ref.get("rot_y", "0") or 0))
            rz = abs(float(ref.get("rot_z", "0") or 0))
            en_names = set()
            for em_ref in ref.findall("./emitter"):
                if (em_ref.get("enable") or "").lower() == "true":
                    en_names.add(em_ref.get("name", ""))
                    lib_to_enabled.setdefault(lib_pack_path, set()).add(em_ref.get("name", ""))
            wrapper_ref_meta.setdefault(wk, []).append({
                "lib": lib_pack_path,
                "infinite_life": inf_flag,
                "rotated": (rx > 1.0 or ry > 1.0 or rz > 1.0),
                "enabled": en_names,
            })
        if direct_em:
            lib_to_wrappers.setdefault(wpath, set()).add(wk)
            for em in direct_em:
                lib_to_enabled.setdefault(wpath, set()).add(em.get("id", ""))

    # 1b. Inspect every referenced library XML (emitters + behaviour_chains + effect_render_stacks)
    zombie_flagged_count = 0
    eccentric_flagged_count = 0
    lib_rigid_meta: dict[str, list[dict]] = {}
    for lib_pack_path, expected_names in sorted(lib_to_enabled.items()):
        if not pk.has(lib_pack_path):
            rep.error("LIB_MISSING", f"Referenced library XML missing from pack: {lib_pack_path}", lib_pack_path)
            continue
        lroot = parse_wh3_vfx_xml(pk.read(lib_pack_path))
        lib_emitters = list(lroot.findall("./vfx/emitters/emitter"))
        bchains = {bc.get("id", ""): bc for bc in lroot.findall("./vfx/behaviour_chains/behaviour_chain")}
        rstacks = {rs.get("id", ""): rs for rs in lroot.findall("./vfx/effect_render_stacks/effect_render_stack")}

        if len(lib_emitters) > 40:
            rep.error("LIB_EXCEEDS_40_EMITTERS", f"Library has {len(lib_emitters)} > 40 emitters (must auto-page!)", lib_pack_path)

        present_names = {em.get("id", "") for em in lib_emitters}
        missing = expected_names - present_names
        if missing:
            rep.error("WRAPPER_BROKEN_REF", f"Wrapper enables missing emitters: {sorted(missing)}", lib_pack_path)
        orphans = present_names - expected_names
        if orphans:
            rep.warn("LIB_ORPHAN_EMITTER", f"Defined but never enabled across wrappers: {sorted(orphans)}", lib_pack_path)

        checked_libs.add(lib_pack_path)

        for em in lib_emitters:
            total_emitters += 1
            ename = em.get("id", "<unnamed>")
            # Resolve linked behaviour_chain and effect_render_stack
            bc_id = ""
            rs_id = ""
            for p in em.findall("./property"):
                if p.get("name") == "BehaviourChain":
                    bc_id = p.get("value", "")
                elif p.get("name") == "RenderStack":
                    rs_id = p.get("value", "")
            bc = bchains.get(bc_id) or bchains.get(ename)
            rs = rstacks.get(rs_id) or rstacks.get(ename)

            # Quality tier Enable check
            en_quals = {p.get("quality") for p in em.findall("./property[@name='Enable']") if (p.get("value") or "").lower() == "true"}
            if not {"0", "1", "2"}.issubset(en_quals):
                rep.warn("QUALITY_TIER_INCOMPLETE", f"Enable=true missing tiers: have {sorted(en_quals)}", ename)

            # L1-1: Zombie particle respawn check (MaxParticles==1 + EmLife>=PLife or infinite_life + non-zero tail EmissionRateCurve)
            # L1-3: Eccentric EmitterPosition + non-zero EmitterOrientation check
            if ename in expected_names:
                q0_props = {p.get("name", ""): p for p in em.findall("./property") if p.get("quality") in (None, "0")}
                lt_rp = em.find("./ranged_property[@name='LifeTime'][@quality='0']")
                em_life = float(lt_rp.get("min", "0") or 0) if lt_rp is not None else 0.0
                mp_p = q0_props.get("MaximumParticles")
                max_p = int(float(mp_p.get("value", "0") or 0)) if mp_p is not None and mp_p.get("value") else 0
                plt_p = q0_props.get("ParticleLifeTime")
                p_life = 0.0
                if plt_p is not None and plt_p.text:
                    pcv = parse_ca_curve_string(plt_p.text.strip())
                    xs = pcv.get("x_min") or []
                    if xs:
                        p_life = xs[0][1]
                er_p = q0_props.get("EmissionRateCurve")
                tail_rate = 0.0
                if er_p is not None and er_p.text:
                    erc = parse_ca_curve_string(er_p.text.strip())
                    xs = erc.get("x_min") or []
                    if xs:
                        tail_rate = xs[-1][1]
                if max_p == 1 and p_life > 0.0:
                    is_inf = lib_has_infinite.get(lib_pack_path, False)
                    if is_inf or em_life >= p_life * 0.99:
                        zombie_flagged_count += 1
                        if zombie_flagged_count <= 8:
                            msg = f"MaxParticles=1 with EmLife={em_life:.3f}s >= PLife={p_life:.3f}s (or infinite_life={is_inf}, tailRate={tail_rate:.1f}) — WH3 spawn accumulator WILL fire Zombie Particle #2 when Particle #1 dies! Set EmLife strictly < ParticleLifeTime."
                            rep.error("ZOMBIE_RESPAWN_RISK", msg, ename)

                ep_p = q0_props.get("EmitterPosition")
                eo_p = q0_props.get("EmitterOrientation")
                sim_p = q0_props.get("ParticleSimulationSpace")
                sim_v = (sim_p.get("value") or "") if sim_p is not None else ""
                ep_c = parse_ca_curve_string((ep_p.text or "").strip()) if ep_p is not None else {}
                eo_c = parse_ca_curve_string((eo_p.text or "").strip()) if eo_p is not None else {}
                p0 = [
                    (ep_c.get("x_min") or [(0.0, 0.0)])[0][1],
                    (ep_c.get("y_min") or [(0.0, 0.0)])[0][1],
                    (ep_c.get("z_min") or [(0.0, 0.0)])[0][1],
                ]
                if em.get("type") == "continuous_rigid" and max_p == 1:
                    xs_ep = ep_c.get("x_min") or [(0.0, 0.0)]
                    ys_ep = ep_c.get("y_min") or [(0.0, 0.0)]
                    zs_ep = ep_c.get("z_min") or [(0.0, 0.0)]
                    span_x = max(v for _, v in xs_ep) - min(v for _, v in xs_ep)
                    span_y = max(v for _, v in ys_ep) - min(v for _, v in ys_ep)
                    span_z = max(v for _, v in zs_ep) - min(v for _, v in zs_ep)
                    span_3d = math.sqrt(span_x * span_x + span_y * span_y + span_z * span_z)
                    if span_3d > 0.05:
                        rep.warn(
                            "EMITTER_POS_ANIM_IGNORED_BY_WH3",
                            f"Single-shot continuous_rigid has animated EmitterPosition (span={span_3d:.2f}m): WH3 only reads EmitterPosition at t_spawn; post-spawn translation requires ForceA or skeletal .anim!",
                            ename,
                        )
                max_pos_r = max(
                    math.sqrt(
                        max((abs(v) for _, v in ep_c.get("x_min", [(0, 0)])), default=0.0) ** 2
                        + max((abs(v) for _, v in ep_c.get("y_min", [(0, 0)])), default=0.0) ** 2
                        + max((abs(v) for _, v in ep_c.get("z_min", [(0, 0)])), default=0.0) ** 2
                    ),
                    0.0,
                )
                max_ori_deg = max(
                    max((abs(v) for _, v in eo_c.get("x_min", [(0, 0)])), default=0.0),
                    max((abs(v) for _, v in eo_c.get("y_min", [(0, 0)])), default=0.0),
                    max((abs(v) for _, v in eo_c.get("z_min", [(0, 0)])), default=0.0),
                )
                if em.get("type") == "continuous_rigid" and sim_v == "LOCAL_SPACE_POSITION_ROTATION" and max_pos_r > 1.0 and max_ori_deg > 5.0:
                    eccentric_flagged_count += 1
                    if eccentric_flagged_count <= 8:
                        rep.warn(
                            "ECCENTRIC_EMITTER_ORIENTATION",
                            f"Eccentric EmitterPosition (r={max_pos_r:.2f}m) + EmitterOrientation ({max_ori_deg:.1f}deg) under LOCAL_SPACE_POSITION_ROTATION risks WH3 lever-arm fling!",
                            ename,
                        )
                if em.get("type") == "continuous_rigid":
                    af=em.find(".//AnimFile")
                    ap=norm_pack_path(af.get("value","")) if af is not None else ""
                    if ap and not exists_anywhere(ap): rep.error("ANIM_MISSING",ap,ename)
                    from native_root_track import matches_profile
                    if ap and pk.has(ap) and matches_profile(pk.read(ap)):
                        try:
                            from native_root_track import decode
                            decode(pk.read(ap))
                        except (ValueError,struct.error) as exc:rep.error("ANIM_INVALID_ROOT",str(exc),ename)
                    mf_el = em.find(".//ModelFile")
                    ws_val = norm_pack_path(mf_el.get("value", "")) if mf_el is not None else ""
                    if ws_val:
                        lib_rigid_meta.setdefault(lib_pack_path, []).append({"name": ename, "wsmodel": ws_val, "pos0": p0})

            # RenderPass & lighting/premul checks on RenderStack + BehaviourChain q0
            render_pass = ""
            premul_val = ""
            has_ps_lighting = False
            if rs is not None:
                for n in rs.iter():
                    if n.tag == "property" and n.get("name") in ("scene_view_type", "RenderPass"):
                        render_pass = (n.get("value") or "").strip()
                    elif n.tag == "render_pass" and n.text:
                        render_pass = n.text.strip()
            if bc is not None:
                bc_q0 = bc.find("./quality[@value='0']")
                bc_scan = bc_q0 if bc_q0 is not None else bc
                for mod in bc_scan.findall("./modifier"):
                    frag = (mod.get("fragment") or "").lower()
                    if frag == "ps_lighting":
                        has_ps_lighting = True
                    elif frag == "ps_texture":
                        p_pma = mod.find("./property[@name='pre_multiplied_alpha']")
                        if p_pma is not None:
                            premul_val = (p_pma.get("value") or "").strip().lower()

            if render_pass == "DEFERRED_DECAL":
                if premul_val == "false" or not has_ps_lighting:
                    rep.error("DECAL_SCORCH_CONFIG", f"DEFERRED_DECAL requires pre_multiplied_alpha=true & ps_lighting (got premul={premul_val!r}, ps_lighting={has_ps_lighting})", ename)
            elif render_pass == "DEFERRED_DECAL_EMISSIVE":
                if premul_val == "true":
                    rep.warn("DECAL_EMISSIVE_CONFIG", "Vanilla permits premultiplied emissive decals; inspect texture convention", ename)

            # Inspect emitter + behaviour_chain + render_stack properties
            scan_nodes = [n for n in (em, bc, rs) if n is not None]
            is_rigid_mesh = any(
                ".wsmodel" in str(v).lower()
                for rn in scan_nodes
                for n in rn.iter()
                for v in list(n.attrib.values()) + [n.text or ""]
            )
            for root_n in scan_nodes:
                for p in root_n.iter("property"):
                    pname = p.get("name", "")
                    txt = (p.text or "").strip()
                    if "|" in txt and ":" in txt and ";" in txt:
                        axes = parse_ca_curve_string(txt)
                        axis_peaks = {}
                        for ax, pts in axes.items():
                            if len(pts) > 60:
                                rep.error("CURVE_ENGINE_LIMIT", f"{pname}.{ax} has {len(pts)} > 60 keys", ename)
                            elif len(pts) > 28:
                                rep.warn("CURVE_OVER_BUDGET", f"{pname}.{ax} has {len(pts)} > 28 spec keys", ename)
                            if pts:
                                axis_peaks[ax] = max(abs(v) for _, v in pts)
                        if pname == "ParticleScale" and is_rigid_mesh and axis_peaks:
                            mx = max(axis_peaks.values())
                            y_peak = max(axis_peaks.get("y_min", 0.0), axis_peaks.get("y_max", 0.0))
                            xz_peak = max(
                                axis_peaks.get("x_min", 0.0), axis_peaks.get("x_max", 0.0),
                                axis_peaks.get("z_min", 0.0), axis_peaks.get("z_max", 0.0),
                                1e-4,
                            )
                            if mx > 80.0:
                                rep.error("RIGID_SCALE_EXTREME", f"Rigid mesh ParticleScale peak={mx:.2f} > 80.0 (pre-scale source mesh!)", ename)
                            elif y_peak > 30.0 and (y_peak / xz_peak) > 4.0:
                                rep.error("RIGID_VERTICAL_STRETCH", f"Rigid mesh vertical Y stretch peak={y_peak:.2f} (Y/XZ={y_peak/xz_peak:.1f}x) — pre-scale beam mesh!", ename)
                            elif mx > 35.0:
                                rep.warn("RIGID_SCALE_HIGH", f"Rigid mesh ParticleScale peak={mx:.2f} > 35.0", ename)

                for n in root_n.iter():
                    # Velocity stretch hygiene
                    if n.get("name") in ("velocity_stretch_cap", "VelocityStretchCap") or n.tag == "velocity_stretch_cap":
                        val_s = n.get("value") or (n.text or "").strip()
                        try:
                            cap = float(val_s)
                            if cap >= 100.0:
                                rep.error("STRETCH_CAP_LASER_BUG", f"velocity_stretch_cap={cap} >= 100 (inherited vanilla spike bug!)", ename)
                            elif cap > 16.0:
                                rep.warn("STRETCH_CAP_HIGH", f"velocity_stretch_cap={cap} > 16.0", ename)
                        except ValueError:
                            pass

                    # Asset path collection (scan ALL attributes + text so no tag schema variant is missed)
                    for cand in list(n.attrib.values()) + [n.text or ""]:
                        s = str(cand).strip()
                        lo = s.lower()
                        if lo.endswith(".wsmodel"):
                            checked_wsmodels.add(norm_pack_path(s))
                        elif lo.endswith(".dds"):
                            np_dds = norm_pack_path(s)
                            if not exists_anywhere(np_dds):
                                rep.error("ASSET_MISSING_DDS", f"Missing DDS '{np_dds}'", ename)
                            elif pk.has(np_dds):
                                if render_pass in ("DEFERRED_DECAL", "DEFERRED_DECAL_EMISSIVE", "FULLRES_DEFERRED_ALPHA") or "opt_" in lo or "decal" in lo:
                                    if not any(ex in lo for ex in ("distort", "flux", "vf_", "vectorfield", "vat_", "mask", "overlay", "surge", "refract", "lens")):
                                        non_tileable_dds.add(np_dds)

    # 2. Inspect every referenced .wsmodel -> .rigid_model_v2 + .xml.material
    for wsp in sorted(checked_wsmodels):
        if not exists_anywhere(wsp):
            rep.error("ASSET_MISSING_WSMODEL", f"Missing wsmodel: {wsp}", wsp)
            continue
        if not pk.has(wsp):
            continue  # vanilla wsmodel
        try:
            ws_root = ET.fromstring(pk.read(wsp))
        except Exception as exc:
            rep.error("WSMODEL_INVALID_XML", f"Cannot parse wsmodel: {exc}", wsp)
            continue
        geo = ws_root.find(".//geometry")
        if geo is not None and geo.text:
            checked_rmv2s.add(norm_pack_path(geo.text.strip()))
        for mat_el in ws_root.findall(".//material"):
            if mat_el.text:
                checked_materials.add(norm_pack_path(mat_el.text.strip()))

    # 3. Inspect every custom RMV2 binary (CRITICAL: pivot_xyz == (0,0,0)!)
    for rmp in sorted(checked_rmv2s):
        if not exists_anywhere(rmp):
            rep.error("ASSET_MISSING_RMV2", f"Missing RMV2: {rmp}", rmp)
            continue
        if not pk.has(rmp):
            continue
        try:
            info = inspect_rmv2_binary(pk.read(rmp))
        except Exception as exc:
            rep.error("RMV2_CORRUPT", str(exc), rmp)
            continue
        if not info["contains_quantized_vertices"]:
            rep.warn("RMV2_BBOX_CLIPS","BBox does not contain every stored vertex; rebuild bounds from encoded positions",rmp)
        px, py, pz = info["pivot_xyz"]
        if abs(px) > 1e-4 or abs(py) > 1e-4 or abs(pz) > 1e-4:
            rep.error("RMV2_NONZERO_PIVOT",
                      f"Group0 pivot_xyz=({px:.4f}, {py:.4f}, {pz:.4f}) != (0,0,0) at ghdr[628:640] — WILL TELEPORT SCALED MESH IN-GAME!",
                      rmp)
        else:
            rmv2_pivots_ok += 1

    # 4. Inspect every custom .xml.material (and collect sort_override / total_alpha for Pivot Sort Inversion check)
    ws_to_mat: dict[str, str] = {}
    for wsp in sorted(checked_wsmodels):
        if pk.has(wsp):
            try:
                ws_root = ET.fromstring(pk.read(wsp))
                mel = ws_root.find(".//material")
                if mel is not None and mel.text:
                    ws_to_mat[wsp] = norm_pack_path(mel.text.strip())
            except Exception:
                pass

    mat_sort_info: dict[str, dict] = {}
    custom_dds_to_check_pot: set[str] = set()
    for mp in sorted(checked_materials):
        if not exists_anywhere(mp):
            rep.error("ASSET_MISSING_MATERIAL", f"Missing material: {mp}", mp)
            continue
        if not pk.has(mp):
            continue
        try:
            mroot = ET.fromstring(pk.read(mp))
        except Exception as exc:
            rep.error("MATERIAL_INVALID_XML", f"Cannot parse material XML: {exc}", mp)
            continue
        sh_el = mroot.find("./shader")
        sh = (sh_el.text or "").strip().replace("\\", "/") if sh_el is not None else ""
        if sh not in ROUTE_A_SHADERS:
            rep.error("MATERIAL_NON_ROUTE_A", f"Material uses non-Route-A shader: '{sh}'", mp)
        if sh.endswith("/vfx_rigid_unlit_emissive_fresnel.xml.shader"):
            pp = {x.findtext("name"): x.findtext("value","") for x in mroot.findall("./params/param")}
            slots = {x.findtext("slot"): x.findtext("source","").lower() for x in mroot.findall("./textures/texture")}
            try:
                valid = (all([float(y) for y in pp[k].split(",")]==[0.,0.,0.] for k in ("diffuse_tint_colour","emissive_colour"))
                         and float(pp["fresnel_multiplier"])==0 and slots.get("t_xml_dither")=="commontextures/default_black.dds")
            except (ValueError, KeyError):
                valid=False
            if not valid: rep.error("MAT_UNSUPPORTED_UNLIT_PROFILE","Opaque profile does not reduce to zero radiance with full depth coverage",mp)
        if sh.endswith("/wh3_vfx_distortion_mesh.xml.shader"):
            slots = {x.findtext("slot"): x.findtext("source","") for x in mroot.findall("./textures/texture")}
            if not all(slots.get(k) for k in ("t_xml_panning_texture","t_xml_24dfd100_83d2_4215_8ecc_e510422b4e5e")):
                rep.error("MAT_MESH_LENS_TEXTURES","Native sphere refraction needs panning noise and screen mask",mp)
        sort_el = mroot.find("./sort_override")
        sort_v = int(float((sort_el.text or "255").strip())) if sort_el is not None and sort_el.text else 255
        ta_v = 1.0
        for pa in mroot.findall("./params/param"):
            pn = (pa.findtext("./name") or "").strip()
            if pn == "total_alpha_strength":
                try:
                    ta_v = float((pa.findtext("./value") or "1.0").strip())
                except ValueError:
                    pass
        mat_sort_info[mp] = {"shader": sh, "sort": sort_v, "ta": ta_v}
        for tex_el in mroot.findall("./textures/texture"):
            src_el = tex_el.find("./source")
            if src_el is not None and src_el.text:
                tpath = norm_pack_path(src_el.text.strip())
                if not exists_anywhere(tpath):
                    rep.error("MATERIAL_MISSING_TEX", f"Material references missing DDS: {tpath}", mp)
                elif pk.has(tpath):
                    custom_dds_to_check_pot.add(tpath)

    # 4b. L1-2: Pivot Sort Inversion check across each wrapper's enabled rigid layers
    for wk, ref_list in wrapper_ref_meta.items():
        w_layers = []
        any_rotated = any(r["rotated"] for r in ref_list)
        for r in ref_list:
            for lm in lib_rigid_meta.get(r["lib"], []):
                if lm["name"] not in r["enabled"]:
                    continue
                mp = ws_to_mat.get(lm["wsmodel"], "")
                mi = mat_sort_info.get(mp)
                if mi and mi["shader"] == "shaders/wh_vfx_mesh_vertex_anim.xml.shader":
                    w_layers.append({"name": lm["name"], "pos0": lm["pos0"], "sort": mi["sort"], "ta": mi["ta"]})
        opaques = [l for l in w_layers if l["ta"] >= 1.5]
        for op in opaques:
            mis = []
            for ot in w_layers:
                if ot["name"] == op["name"] or ot["ta"] >= 1.5 or ot["sort"] != op["sort"]:
                    continue
                dist = math.sqrt(sum((op["pos0"][i] - ot["pos0"][i]) ** 2 for i in range(3)))
                if dist > 1.5:
                    mis.append((ot["name"], dist))
            if mis:
                sample = ", ".join(f"{n}({d:.1f}m)" for n, d in mis[:4])
                msg = f"Opaque matte '{op['name']}' (total_alpha={op['ta']}, sort_override={op['sort']}) has EmitterPosition offset >1.5m from [{sample}] with equal sort_override (rotated_wrapper={any_rotated}) — WH3 Pivot distance sorting will paint matte over glowing layers!"
                if any_rotated:
                    rep.error("PIVOT_SORT_INVERSION_RISK", msg, wk)
                else:
                    rep.warn("PIVOT_SORT_INVERSION_RISK", msg, wk)
                break

    # 4c. L1-4: Non-POT 2D DDS header audit
    for dds_p in sorted(custom_dds_to_check_pot | non_tileable_dds):
        if not pk.has(dds_p):
            continue
        raw_d = pk.read(dds_p)
        if len(raw_d) >= 128 and raw_d[:4] == b"DDS ":
            h_d, w_d = struct.unpack_from("<II", raw_d, 12)
            caps2 = struct.unpack_from("<I", raw_d, 112)[0]
            is_vol = bool(caps2 & 0x200000)
            if not is_vol and (((w_d & (w_d - 1)) != 0) or ((h_d & (h_d - 1)) != 0)):
                rep.warn("DDS_NON_POT", f"Custom 2D DDS is {w_d}x{h_d} (non-power-of-two)", dds_p)

    if zombie_flagged_count > 8:
        rep.warn("ZOMBIE_RESPAWN_SUMMARY", f"Total {zombie_flagged_count} emitters flagged for ZOMBIE_RESPAWN_RISK (showing first 8)")
    if eccentric_flagged_count > 8:
        rep.warn("ECCENTRIC_ORIENT_SUMMARY", f"Total {eccentric_flagged_count} emitters flagged for ECCENTRIC_EMITTER_ORIENTATION (showing first 8)")

    # 5. Decode and audit custom non-tileable optical/decal DDS borders & resolution via texconv
    mip_leaks={}
    if check_dds_borders and non_tileable_dds:
        with tempfile.TemporaryDirectory() as td:
            for dds_rel in sorted(non_tileable_dds):
                tmp_dds = os.path.join(td, os.path.basename(dds_rel))
                with open(tmp_dds, "wb") as f:
                    f.write(pk.read(dds_rel))
                try:
                    aud = audit_dds_border(tmp_dds, expected_neutral_u8=(0, 0, 0, 0), max_tol=1, check_channels=(3,))
                    mip_leaks[dds_rel]=[m for m in aud.get("mips",[])[1:] if max(m["max_err"].values())>1]
                    h, w = aud["shape"][0], aud["shape"][1]
                    if h < 256 or w < 256:
                        rep.error("DDS_LOW_RES", f"Decoded optical/decal DDS is {w}x{h} < 256x256", dds_rel)
                    else:
                        dds_borders_ok += 1
                except Exception as exc:
                    rep.error("DDS_BORDER_LEAK", str(exc), dds_rel)

    rep.metrics = {
        "dds_mip_edge_observations":{p:m for p,m in mip_leaks.items() if m},
        "wrappers_checked": len(wrapper_keys),
        "libraries_checked": len(checked_libs),
        "total_emitters": total_emitters,
        "custom_rmv2_pivot_zero_ok": rmv2_pivots_ok,
        "custom_materials_checked": len(checked_materials),
        "optical_decal_dds_border_ok": dds_borders_ok,
    }
    return rep


def main():
    ap = argparse.ArgumentParser(description="WH3 Route-A VFX L1 Automated Spec Validator")
    ap.add_argument("--ir", help="IR directory containing manifest.json to preflight-validate")
    ap.add_argument("--pack", help="Compiled PFH5 .pack path to readback-validate")
    ap.add_argument("--wrappers", help="Comma-separated wrapper VFX keys (without vfx/ or .xml) for --pack mode")
    ap.add_argument("--skip-dds-border", action="store_true", help="Skip texconv decode border check in --pack mode")
    ap.add_argument("--json-out", help="Optional path to write machine-readable JSON report")
    args = ap.parse_args()

    if bool(args.ir) == bool(args.pack):
        ap.error("Specify exactly one of --ir or --pack")

    if args.ir:
        rep = validate_ir(args.ir)
    else:
        if not args.wrappers:
            ap.error("--pack requires --wrappers <key1,key2,...>")
        wkeys = [w.strip() for w in args.wrappers.split(",") if w.strip()]
        rep = validate_pack(args.pack, wkeys, check_dds_borders=not args.skip_dds_border)

    rep.print_summary()
    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(rep.to_dict(), f, indent=2, ensure_ascii=False)
    sys.exit(0 if rep.passed else 1)


if __name__ == "__main__":
    main()
