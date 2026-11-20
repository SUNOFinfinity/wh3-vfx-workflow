"""Pack-driven offline VFX diagnostic (ANGLE GLES3 / D3D11).
Reads compiled RMV2, materials, DDS and VFX XML. Stock-derived mesh and particle
equations run against analytic ground, finite simulation profiles and a simplified HDR
pipeline. See WH3_VFX_AUTHORING_SPEC.md for supported passes and L3-dependent output.
Telemetry reports known missing assets/materials separately from render success.
"""
from __future__ import annotations
import zlib
from environment_presets import NAMES as ENV_NAMES, parameters

import argparse
import ctypes
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import baker_config  # retains the configured DLL search handle

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import native_rigid_channels as native_rigid
import native_root_track
from dds_compiler import TEXCONV_PATH
from pfh5 import PFH5Pack, norm_pack_path
from vfx_xml_compiler import parse_wh3_vfx_xml

from baker_config import ANGLE_DLL_DIR
from baker_config import FFMPEG_PATH
from baker_config import WH3_DATA_DIR

# Real vanilla WH3 .environment presets extracted from data.pack
WH3_ENV_PRESETS = {
    "daylight": {
        "bloom_threshold": 1.20,
        "bloom_strength": 0.015,
        "bloom_particle_strength": 0.50,
        "tm_brightness": 0.78,
        "tm_burn": 0.06,
        "g_brightness": 1.00,
        "inv_gamma": 1.0 / 2.2,
        "sharpen": 0.15,
        "sky_zenith": (0.14, 0.24, 0.42),
        "sky_horizon": (0.48, 0.54, 0.60),
        "ground_a": (0.14, 0.155, 0.11),
        "ground_b": (0.20, 0.185, 0.145),
    },
    "dusk": {
        "bloom_threshold": 1.00,
        "bloom_strength": 0.015,
        "bloom_particle_strength": 0.50,
        "tm_brightness": 0.85,
        "tm_burn": 0.08,
        "g_brightness": 1.00,
        "inv_gamma": 1.0 / 2.2,
        "sharpen": 0.15,
        "sky_zenith": (0.06, 0.08, 0.16),
        "sky_horizon": (0.34, 0.22, 0.16),
        "ground_a": (0.08, 0.08, 0.065),
        "ground_b": (0.12, 0.105, 0.085),
    },
    "dark_forge": {
        "bloom_threshold": 1.50,
        "bloom_strength": 0.015,
        "bloom_particle_strength": 0.50,
        "tm_brightness": 1.00,
        "tm_burn": 0.04,
        "g_brightness": 1.00,
        "inv_gamma": 1.0 / 2.2,
        "sharpen": 0.15,
        "sky_zenith": (0.025, 0.028, 0.042),
        "sky_horizon": (0.09, 0.075, 0.070),
        "ground_a": (0.045, 0.042, 0.038),
        "ground_b": (0.070, 0.062, 0.052),
    },
}

# ============================================================================
# 1. CA VFX XML Curve Evaluator & Binary .pack Loader
# ============================================================================

def parse_ca_curve(text: str) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    if not text or ":" not in text:
        return out
    for seg in text.split(":", 1)[0].split("|"):
        toks = seg.split(";")
        if len(toks) < 2:
            continue
        ax = toks[0].split(",")[0]
        pts = []
        for p in toks[1:]:
            f = p.split(",")
            if len(f) >= 2:
                try:
                    pts.append((float(f[0]), float(f[1])))
                except ValueError:
                    pass
        if pts:
            out[ax] = sorted(pts,key=lambda p:p[0])
    return out


def eval_curve(pts: list[tuple[float, float]] | None, u: float, default: float = 0.0) -> float:
    if not pts:
        return default
    if len(pts) == 1 or u <= pts[0][0]:
        return pts[0][1]
    if u >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        t0, v0 = pts[i]
        t1, v1 = pts[i + 1]
        if t0 <= u <= t1:
            dt = t1 - t0
            f = (u - t0) / dt if dt > 1e-7 else 0.0
            return v0 + (v1 - v0) * f
    return pts[-1][1]


def eval_axis_pair(curves: dict, name: str, u: float, default: float = 0.0, rng_u: float = 0.5) -> float:
    lo = eval_curve(curves.get(f"{name}_min"), u, default)
    hi = eval_curve(curves.get(f"{name}_max"), u, lo)
    return lo + (hi - lo) * rng_u


def parse_rmv2_v7(raw: bytes) -> dict:
    """Parse RMV2 v7 binary including REAL Group0 pivot_xyz at ghdr[628:640]."""
    if raw[:4] != b"RMV2":
        raise ValueError("Not RMV2")
    gcnt, vsz, isz, goff, lod_dist, _, _ = struct.unpack_from("<4If2I", raw, 140)
    shader_t, glen, voff, vcnt, ioff, icnt = struct.unpack_from("<6I", raw, goff)
    pivot = struct.unpack_from("<3f", raw, goff + 628)
    vstart = goff + voff
    istart = goff + ioff
    if struct.unpack_from("<I",raw,4)[0] != 7 or gcnt!=1 or ioff-voff!=32*vcnt or voff<640:
        raise ValueError("Unsupported RMV2 version, groups or vertex layout")
    stride = 32
    vbytes = np.frombuffer(raw, dtype=np.uint8, count=vcnt * stride, offset=vstart).reshape(vcnt, stride)
    pos = vbytes[:, 0:8].copy().view(np.float16).reshape(vcnt, 4)[:, :3].astype(np.float32)
    uv = vbytes[:, 8:12].copy().view(np.float16).reshape(vcnt, 2).astype(np.float32)
    # BGRA8 normal: (nz, ny, nx, 0)
    n_bgra = vbytes[:, 16:20].astype(np.float32) / 255.0 * 2.0 - 1.0
    nrm = np.stack([n_bgra[:, 2], n_bgra[:, 1], n_bgra[:, 0]], axis=-1)
    nlen = np.maximum(np.linalg.norm(nrm, axis=-1, keepdims=True), 1e-6)
    nrm = (nrm / nlen).astype(np.float32)
    # BGRA8 COLOR0: (b, g, r, a)
    c_bgra = vbytes[:, 28:32].astype(np.float32) / 255.0
    col = np.stack([c_bgra[:, 2], c_bgra[:, 1], c_bgra[:, 0], c_bgra[:, 3]], axis=-1).astype(np.float32)
    idx = np.frombuffer(raw, dtype=np.uint16, count=icnt, offset=istart).copy()
    return {
        "pos": pos,
        "uv": uv,
        "nrm": nrm,
        "col": col,
        "idx": idx,
        "pivot": tuple(float(x) for x in pivot),
    }


def parse_material_xml(raw: bytes) -> dict:
    root = ET.fromstring(raw)
    so_txt = (root.findtext("./sort_override") or "255").strip()
    try:
        sort_ov = int(float(so_txt))
    except ValueError:
        sort_ov = 255
    mat = {
        "shader": (root.findtext("./shader") or "").strip(),
        "sort_override": sort_ov,
        "textures": {},
        "params": {},
    }
    for t in root.findall("./textures/texture"):
        slot = (t.findtext("./slot") or "").strip()
        src = norm_pack_path((t.findtext("./source") or "").strip())
        if slot:
            mat["textures"][slot] = src
    for p in root.findall("./params/param"):
        name = (p.findtext("./name") or "").strip()
        ptype = (p.findtext("./type") or "").strip()
        val_s = (p.findtext("./value") or "").strip()
        if not name:
            continue
        if ptype == "float":
            try:
                mat["params"][name] = float(val_s)
            except ValueError:
                pass
        elif ptype.startswith("float"):
            try:
                mat["params"][name] = [float(x) for x in val_s.split(",")]
            except ValueError:
                pass
        else:
            mat["params"][name] = val_s
    return mat


class PackAssetResolver:
    """Resolves custom assets from target pack first, then falls back to vanilla WH3 packs."""
    def __init__(self, target_pack_path: str):
        self.target = PFH5Pack(target_pack_path)
        from vanilla_index import VanillaIndex, game_packs
        self.vanilla = VanillaIndex(game_packs())
        self.omissions = set()
        self.vat_diagnostics={}
        self.vat_geometry={}
        self._tex_cache: dict[str, np.ndarray] = {}
        self._vf_cache: dict[str, np.ndarray | None] = {}

    def decode_vectorfield_3d(self, rel_path: str) -> np.ndarray | None:
        """Decode WH3 D3DFMT_A16B16G16R16F (FourCC=113) 3D Volume VectorField DDS into float32 [D,H,W,3]."""
        key = norm_pack_path(rel_path).lower()
        if key in self._vf_cache:
            return self._vf_cache[key]
        raw = self.read(rel_path)
        if raw is None or len(raw) < 128 or raw[:4] != b"DDS ":
            self._vf_cache[key] = None
            return None
        _, flags, h, w, _, d, _ = struct.unpack_from("<7I", raw, 4)
        _, _, fourcc, _ = struct.unpack_from("<4I", raw, 76)
        if fourcc != 113 or d < 1 or h < 1 or w < 1:
            self._vf_cache[key] = None
            return None
        need = d * h * w * 4 * 2
        if len(raw) < 128 + need:
            self._vf_cache[key] = None
            return None
        arr = np.frombuffer(raw, dtype=np.float16, count=d * h * w * 4, offset=128).reshape(d, h, w, 4)[:, :, :, :3].astype(np.float32)
        self._vf_cache[key] = arr
        return arr

    def read(self, rel_path: str) -> bytes | None:
        np_p = norm_pack_path(rel_path)
        if self.target.has(np_p):
            return self.target.read(np_p)
        return self.vanilla.read(np_p)

    def decode_dds_rgba_u8(self, rel_path: str, linear: bool = False) -> np.ndarray:
        """Decode DDS via texconv.exe to raw uint8 RGBA.
        - linear=False (default, for BC7_UNORM_SRGB): decodes to R8G8B8A8_UNORM_SRGB so GLSL runs WH3's exact single EOTF.
        - linear=True (for BC3_UNORM DXT5nm distortion): decodes to R8G8B8A8_UNORM preserving exact 128=0.5 neutral point!
        """
        key = f"{norm_pack_path(rel_path).lower()}|{'L' if linear else 'S'}"
        if key in self._tex_cache:
            return self._tex_cache[key]
        if "default_white" in key or not rel_path:
            arr = np.full((4, 4, 4), 255, dtype=np.uint8)
            self._tex_cache[key] = arr
            return arr
        if "default_black" in key:
            arr = np.zeros((4, 4, 4), dtype=np.uint8)
            self._tex_cache[key] = arr
            return arr
        raw = self.read(rel_path)
        if raw is None:
            self.omissions.add("Missing DDS: " + rel_path)
            arr = np.full((4, 4, 4), 255, dtype=np.uint8)
            self._tex_cache[key] = arr
            return arr
        with tempfile.TemporaryDirectory() as td:
            in_dds = os.path.join(td, "in.dds")
            with open(in_dds, "wb") as f:
                f.write(raw)
            out_fmt = "R8G8B8A8_UNORM" if linear else "R8G8B8A8_UNORM_SRGB"
            res = subprocess.run(
                [TEXCONV_PATH, "-nologo", "-y", "-ft", "png", "-f", out_fmt, "-o", td, in_dds],
                capture_output=True,
            )
            png_p = os.path.join(td, "in.png")
            if res.returncode != 0 or not os.path.isfile(png_p):
                raise RuntimeError(f"texconv decode failed: {rel_path}: {res.stdout[-500:]!r}")
            else:
                arr = np.asarray(Image.open(png_p).convert("RGBA"), dtype=np.uint8)
        self._tex_cache[key] = arr
        return arr


class ParsedEmitter:
    def __init__(self, eid: str, etype: str, em_el: ET.Element, bc_el: ET.Element | None, rs_el: ET.Element | None):
        self.id = eid
        self.etype = etype  # continuous_rigid / continuous_mesh / etc.
        self.render_pass = ""
        self.start_time = 0.0
        self.life_time = 10.0
        self.max_particles = 1
        self.emission_rate = 10.0
        self.emission_rate_curves: dict = {}
        self.particle_life_min = 1.0
        self.particle_life_max = 1.0
        # Wrapper <vfx_references><vfx> instance transform & lifecycle
        self.wrapper_pos = np.zeros(3, dtype=np.float32)
        self.wrapper_rot_deg = np.zeros(3, dtype=np.float32)
        self.wrapper_scale = 1.0
        self.tint=np.ones(4,dtype=np.float32)
        self.wrapper_delay = 0.0
        self.wrapper_infinite_life = False
        self.wrapper_sim_speed = 1.0
        self.pos_curves: dict = {}
        self.carrier_orientation: dict = {}
        self.simulation_space = ""
        self.scale_curves: dict = {}
        self.init_scale_curves: dict = {}
        self.init_speed_curves: dict = {}
        self.spawn_area_curves: dict = {}
        self.spawn_box_curves: dict = {}
        self.sample_spawn_box = False
        self.force_a_curves: dict = {}
        self.orient_min = (0.0, 0.0, 0.0)
        self.orient_max = (0.0, 0.0, 0.0)
        self.spin_min = (0.0, 0.0, 0.0)
        self.spin_max = (0.0, 0.0, 0.0)
        self.spin_dir_y = 1.0
        self.colour_curves: dict = {}
        self.alpha_curves: dict = {}
        self.root_track=None; self.anim_path=""; self.anim_speed=1.
        self.wsmodel: str = ""
        self.diffuse_dds: str = ""
        self.normal_dds: str = ""
        self.pre_multiplied_alpha: bool = False
        self.has_ps_lighting: bool = False
        self.emissive_gain: float = 1.0
        self.additive_blend_mul: float = 0.0
        self.restriction: int = 0  # 0=billboard, 2=horizontal quad
        self.velocity_stretch_mul: float = 0.0
        self.velocity_stretch_cap: float = 1.0
        self.init_alpha_curves = {}
        self.has_soft_edge = False
        self.soft_scale_strength = 1.0
        self.soft_factor: float = -1.
        self.fb_base=0;self.fb_mode="fixed_animation_speed";self.fb_loop=True;self.fb_loops=(1.,1.);self.fb_random=False;self.fb_blend=False;self.fb_row=None;self.fb_random_row=False
        self.fb_cols: int = 1
        self.fb_rows: int = 1
        self.fb_fps: float = 24.0
        self.drag: float = 0.0
        self.vf_dds: str = ""
        self.vf_size: float = 40.0
        self.vf_strength: float = 0.0

        self._parse(em_el, bc_el, rs_el)

    def _parse(self, em: ET.Element, bc: ET.Element | None, rs: ET.Element | None):
        for rp in em.findall("./ranged_property"):
            if rp.get("quality") not in (None, "0"):
                continue
            n = rp.get("name")
            if n == "StartTime":
                self.start_time = float(rp.get("min", "0"))
            elif n == "LifeTime":
                self.life_time = float(rp.get("min", "10"))
        for p in em.findall("./property"):
            if p.get("quality") not in (None, "0"):
                continue
            n = p.get("name")
            v = p.get("value", "")
            t = (p.text or "").strip()
            if n == "MaximumParticles" and v:
                self.max_particles = max(1, int(float(v)))
            elif n == "EmissionRateCurve" and t:
                c = parse_ca_curve(t)
                self.emission_rate_curves = c
                self.emission_rate = eval_axis_pair(c, "x", 0.0, 10.0, 0.5)
            elif n == "ParticleLifeTime" and t:
                c = parse_ca_curve(t)
                self.particle_life_min = eval_curve(c.get("x_min"), 0.0, 1.0)
                self.particle_life_max = eval_curve(c.get("x_max"), 0.0, self.particle_life_min)
            elif n == "ParticleInitialAlpha" and t:
                self.init_alpha_curves=parse_ca_curve(t)
            elif n == "EmitterPosition" and t:
                self.pos_curves = parse_ca_curve(t)
            elif n == "EmitterOrientation" and t:
                self.carrier_orientation = parse_ca_curve(t)
            elif n == "ParticleSimulationSpace":
                self.simulation_space = v
            elif n == "ParticleScale" and t:
                self.scale_curves = parse_ca_curve(t)
            elif n == "ParticleInitialScale" and t:
                self.init_scale_curves = parse_ca_curve(t)
            elif n == "ParticleInitialSpeed" and t:
                self.init_speed_curves = parse_ca_curve(t)
            elif n == "SpawnAreaCurve" and t:
                self.spawn_area_curves = parse_ca_curve(t)
            elif n == "SpawnAreaBox" and t:
                self.spawn_box_curves = parse_ca_curve(t)
            elif n == "ForceA" and t:
                self.force_a_curves = parse_ca_curve(t)
            elif n == "ParticleDeceleration" and t:
                c = parse_ca_curve(t)
                self.drag = max(0.0, eval_axis_pair(c, "x", 0.0, 0.0, 0.5))
            elif n == "VectorFieldTexture" and v:
                self.vf_dds = norm_pack_path(v)
            elif n == "VectorFieldSize" and t:
                c = parse_ca_curve(t)
                self.vf_size = max(1.0, eval_axis_pair(c, "x", 0.0, 40.0, 0.5))
            elif n == "VectorFieldStrength" and t:
                c = parse_ca_curve(t)
                self.vf_strength = eval_axis_pair(c, "x", 0.0, 0.0, 0.5)
            elif n == "ParticleOrientationMin":
                self.orient_min = (float(p.get("x", "0")), float(p.get("y", "0")), float(p.get("z", "0")))
            elif n == "ParticleOrientationMax":
                self.orient_max = (float(p.get("x", "0")), float(p.get("y", "0")), float(p.get("z", "0")))
            elif n == "ParticleSpin_Min":
                self.spin_min = (float(p.get("x", "0")), float(p.get("y", "0")), float(p.get("z", "0")))
            elif n == "ParticleSpin_Max":
                self.spin_max = (float(p.get("x", "0")), float(p.get("y", "0")), float(p.get("z", "0")))
            elif n == "ParticleSpinDirectionY":
                self.spin_dir_y = 1.0 if v.upper() == "CW" else -1.0

        rd = em.find("./property_rigid_data[@name='RigidData']")
        if rd is not None:
            af=rd.find("./rigid_entry/AnimFile");asp=rd.find("./rigid_entry/AnimSpeed")
            self.anim_path=norm_pack_path(af.get("value","")) if af is not None else ""
            self.anim_speed=float(asp.get("value","1")) if asp is not None else 1.
            mf = rd.find("./rigid_entry/ModelFile")
            if mf is not None:
                self.wsmodel = norm_pack_path(mf.get("value", ""))

        if rs is not None:
            for p in rs.findall("./property[@name='scene_view_type']"):
                if p.get("quality") in (None, "0"):
                    self.render_pass = (p.get("value") or "").strip()

        if bc is not None:
            q0 = bc.find("./quality[@value='0']")
            if q0 is None and len(bc) > 0:
                q0 = bc[0]
            if q0 is not None:
                for mod in q0.findall("./modifier"):
                    frag = mod.get("fragment", "")
                    if frag == "vs_colour":
                        pc = mod.find("./property[@name='curve_colour']")
                        if pc is not None and pc.text:
                            self.colour_curves = parse_ca_curve(pc.text.strip())
                    elif frag == "vs_vertex_alpha":
                        pa = mod.find("./property[@name='curve_alpha']")
                        if pa is not None and pa.text:
                            self.alpha_curves = parse_ca_curve(pa.text.strip())
                    elif frag == "ps_texture":
                        dt = mod.find("./texture_property[@name='diffuse']")
                        if dt is not None:
                            self.diffuse_dds = norm_pack_path(dt.get("path", ""))
                        nt = mod.find("./texture_property[@name='normal']")
                        if nt is not None:
                            self.normal_dds = norm_pack_path(nt.get("path", ""))
                        pma = mod.find("./property[@name='pre_multiplied_alpha']")
                        if pma is not None:
                            self.pre_multiplied_alpha = (pma.get("value", "").lower() == "true")
                    elif frag == "ps_lighting":
                        self.has_ps_lighting = True
                        pe = mod.find("./property[@name='emissive']")
                        if pe is not None:
                            self.emissive_gain = float(pe.get("value", "1.0"))
                        pa = mod.find("./property[@name='additive_blend_mul']")
                        if pa is not None:
                            self.additive_blend_mul = float(pa.get("value", "0.0"))
                    elif frag == "ps_soft_edge":
                        self.soft_factor=1.5
                        self.has_soft_edge=True
                        scale_prop=mod.find("./property[@name='scale_strength']")
                        self.soft_scale_strength=float(scale_prop.get("value","1")) if scale_prop is not None else 1.
                        ps = mod.find("./property[@name='soft_factor']")
                        if ps is not None:
                            try:
                                self.soft_factor = float(ps.get("value", "1.5"))
                            except ValueError:
                                pass
                    elif frag == "vs_cell_texture":
                        ps={n.get("name"):n.get("value","") for n in mod.findall("property")}
                        self.fb_cols=max(1,int(ps.get("number_of_cells_x","1")));self.fb_rows=max(1,int(ps.get("number_of_cells_y","1")))
                        self.fb_base=int(ps.get("cell","0")); self.fb_random=ps.get("randomize_cell")=="true";self.fb_mode="static"
                    elif frag == "vs_animated_texture":
                        ps={n.get("name"):n.get("value","") for n in mod.findall("property")}
                        def pair(name,default):
                            v=ps.get(name,default).removeprefix("float2(").rstrip(")");return tuple(map(float,v.split(",")))
                        self.fb_cols,self.fb_rows=[max(1,int(v)) for v in pair("no_cells_xy","1,1")]
                        self.fb_loops=pair("no_loops","1,1");self.fb_loop=ps.get("loop")=="true";self.fb_random=ps.get("randomize_cell")=="true";self.fb_blend=ps.get("cell_blend")=="true"
                        self.fb_mode=ps.get("time_mode","using_particle_lifetime")
                        self.fb_row=int(ps.get("row_restriction","0")) if ps.get("row_restricted")=="true" else None
                        self.fb_random_row=ps.get("randomize_row")=="true"
                    elif frag == "vs_translation":
                        pr = mod.find("./property[@name='restriction']")
                        if pr is not None:
                            self.restriction = int(float(pr.get("value", "0")))
                    elif frag == "vs_translation_velocity_v2":
                        pm = mod.find("./property[@name='velocity_stretch_mul']")
                        if pm is not None and pm.text:
                            c = parse_ca_curve(pm.text.strip())
                            self.velocity_stretch_mul = eval_axis_pair(c, "x", 0.0, 0.0, 0.5)
                        pc = mod.find("./property[@name='velocity_stretch_cap']")
                        if pc is not None:
                            self.velocity_stretch_cap = float(pc.get("value", "1.0"))

    def simulate_particles(self, t_now: float, resolver: PackAssetResolver | None = None) -> list[dict]:
        """Deterministic seeded particle instances active at t_now (with wrapper timing, infinite_life respawn, drag & 3D VectorField)."""
        t_local = (t_now - self.wrapper_delay) * self.wrapper_sim_speed
        if t_local < self.start_time:
            return []
        vf_arr = resolver.decode_vectorfield_3d(self.vf_dds) if (resolver is not None and self.vf_dds and abs(self.vf_strength) > 1e-3) else None
        seed = zlib.crc32(self.id.encode("utf-8"))
        rng = np.random.default_rng(seed)

        # Compute spawn times inside [start_time, start_time + life_time] (plus WH3 infinite_life / EmLife>=PLife respawn!)
        win = max(self.life_time, 1e-4)
        rate = max(self.emission_rate, 0.0)
        if self.max_particles == 1:
            spawn_times = []
            life_est = max(0.5 * (self.particle_life_min + self.particle_life_max), 1e-4)
            r0 = eval_axis_pair(self.emission_rate_curves, "x", 0.0, rate, 0.5) if self.emission_rate_curves else rate
            accum_overflow = (r0 * min(win, 0.05) >= 1.5)
            t_sp = self.start_time
            for sp_idx in range(32):
                if t_sp > t_local + 1e-5:
                    break
                if not self.wrapper_infinite_life and t_sp > self.start_time + win + 1e-4:
                    break
                u_sp = min(max((t_sp - self.start_time) / win, 0.0), 1.0)
                r_sp = eval_axis_pair(self.emission_rate_curves, "x", u_sp, rate, 0.5) if self.emission_rate_curves else rate
                # WH3 real-engine behavior: pending spawn accumulator fires Zombie Particle #2 on boundary EmLife>=PLife even if tail rate==0!
                if r_sp <= 1e-3 and not (sp_idx > 0 and accum_overflow):
                    break
                spawn_times.append(t_sp)
                # Next frame after particle dies: WH3 respawns if emitter is still alive (EmLife >= PLife or infinite_life)
                t_next = t_sp + life_est
                if t_next > t_local + 1e-5:
                    break
                if not self.wrapper_infinite_life and t_next > self.start_time + win + 1e-4:
                    break
                t_sp = t_next
        elif rate >= 500.0:
            # Burst-style (up to max_particles)
            n = min(self.max_particles, 64)
            dt = min(win / max(n, 1), 0.15)
            spawn_times = [self.start_time + i * dt for i in range(n) if self.start_time + i * dt <= self.start_time + win]
        else:
            step = 1.0 / max(rate, 0.1)
            n_est = min(int(win / step) + 1, self.max_particles * 6, 400)
            spawn_times = [self.start_time + i * step for i in range(n_est) if self.start_time + i * step <= self.start_time + win]

        active = []
        local_rigid = self.etype == "continuous_rigid" and self.simulation_space in (
            "LOCAL_SPACE_POSITION_ONLY", "LOCAL_SPACE_POSITION_ROTATION")
        carrier_u = min(max((t_local - self.start_time) / win, 0.0), 1.0) if local_rigid else 0.0
        carrier_rot = None
        if local_rigid and self.simulation_space == "LOCAL_SPACE_POSITION_ROTATION" and self.carrier_orientation:
            carrier_rot = np.array([eval_axis_pair(self.carrier_orientation, axis, carrier_u, 0.0, 0.5)
                                    for axis in "xyz"], dtype=np.float32)
        force_m = np.array([
            eval_axis_pair(self.force_a_curves, "x", 0.0, 0.0, 0.5),
            eval_axis_pair(self.force_a_curves, "y", 0.0, 0.0, 0.5),
            eval_axis_pair(self.force_a_curves, "z", 0.0, 0.0, 0.5),
        ], dtype=np.float32) / 100.0  # WH3 ForceA is in cm/s^2 -> m/s^2!
        r_in = eval_axis_pair(self.spawn_area_curves, "inner_radius", 0.0, 0.0, 0.5)
        r_out = eval_axis_pair(self.spawn_area_curves, "outer_radius", 0.0, r_in, 0.5)
        init_spd = eval_axis_pair(self.init_speed_curves, "x", 0.0, 0.0, 0.5)

        box = np.array([eval_axis_pair(self.spawn_box_curves, ax, 0, 0, 0.5) for ax in "xyz"], dtype=np.float32)
        box_rng = np.random.default_rng(seed ^ 0x791D) if self.sample_spawn_box and self.etype != "continuous_rigid" and np.any(box) else None
        for particle_id, st in enumerate(spawn_times):
            jitter = ((box_rng.random(3).astype(np.float32) - 0.5) * box if box_rng is not None else 0.0)
            u_life = float(rng.random())
            u_ang = float(rng.random()) * 2.0 * math.pi
            u_rad = float(rng.random())
            u_yaw = float(rng.random())
            u_col = float(rng.random())
            u_ele = float(rng.random()) * 0.85 + 0.15

            life = self.particle_life_min + (self.particle_life_max - self.particle_life_min) * u_life
            life = max(life, 1e-4)
            age = t_local - st
            if age < 0.0 or age > life:
                continue
            u = min(max(age / life, 0.0), 1.0)

            # WH3 real-engine behavior: EmitterPosition is sampled ONLY at spawn time (u_spawn), NEVER post-spawn!
            u_spawn = min(max((st - self.start_time) / win, 0.0), 1.0)
            base_pos = np.array([
                eval_axis_pair(self.pos_curves, "x", u_spawn, 0.0, 0.5),
                eval_axis_pair(self.pos_curves, "y", u_spawn, 0.0, 0.5),
                eval_axis_pair(self.pos_curves, "z", u_spawn, 0.0, 0.5),
            ], dtype=np.float32)
            rad = r_in + (r_out - r_in) * math.sqrt(u_rad) if r_out > 1e-4 else 0.0
            dir_xz = np.array([math.cos(u_ang), 0.0, math.sin(u_ang)], dtype=np.float32)
            p0 = base_pos + dir_xz * rad + jitter

            # Velocity: radial + upward for particles, zero for static decals/rigid
            if init_spd > 1e-3:
                vel0 = np.array([dir_xz[0] * init_spd * 0.85, init_spd * u_ele * 0.55, dir_xz[2] * init_spd * 0.85], dtype=np.float32)
            else:
                vel0 = np.zeros(3, dtype=np.float32)

            if vf_arr is not None or self.drag > 1e-3:
                # Sub-step integration (8 steps) for accurate drag & 3D VectorField swirl advection!
                pos = p0.copy()
                vel = vel0.copy()
                n_sub = 8
                dt_sub = age / float(n_sub)
                D, H, W, _ = vf_arr.shape if vf_arr is not None else (1, 1, 1, 3)
                half_vf = max(self.vf_size * 0.5, 1.0)
                for _ in range(n_sub):
                    acc = force_m.copy()
                    if vf_arr is not None:
                        rel = (pos - base_pos) / half_vf * 0.5 + 0.5
                        if 0.0 <= rel[0] <= 1.0 and 0.0 <= rel[1] <= 1.0 and 0.0 <= rel[2] <= 1.0:
                            ix = min(W - 1, max(0, int(rel[0] * (W - 1))))
                            iy = min(H - 1, max(0, int(rel[1] * (H - 1))))
                            iz = min(D - 1, max(0, int(rel[2] * (D - 1))))
                            acc += vf_arr[iz, iy, ix] * (self.vf_strength * 0.35)
                    vel = vel + acc * dt_sub
                    if self.drag > 1e-3:
                        vel *= max(0.0, 1.0 - self.drag * dt_sub)
                    pos = pos + vel * dt_sub
            else:
                pos = p0 + vel0 * age + 0.5 * force_m * (age * age)
                vel = vel0 + force_m * age
            # Bounce on ground plane y=0 if falling particles
            if self.etype != "continuous_rigid" and pos[1] < 0.05 and p0[1] >= 0.0:
                pos[1] = max(0.05, abs(pos[1]) * 0.5)

            sx = eval_axis_pair(self.scale_curves, "x", u, 1.0, 0.5) * eval_axis_pair(self.init_scale_curves, "x", 0.0, 1.0, 0.5)
            sy = eval_axis_pair(self.scale_curves, "y", u, sx, 0.5) * eval_axis_pair(self.init_scale_curves, "y", 0.0, 1.0, 0.5)
            sz = eval_axis_pair(self.scale_curves, "z", u, sx, 0.5) * eval_axis_pair(self.init_scale_curves, "z", 0.0, 1.0, 0.5)

            rx = self.orient_min[0] + (self.orient_max[0] - self.orient_min[0]) * 0.5
            ry = self.orient_min[1] + (self.orient_max[1] - self.orient_min[1]) * u_yaw + self.spin_dir_y * self.spin_min[1] * age
            rz = self.orient_min[2] + (self.orient_max[2] - self.orient_min[2]) * 0.5

            cr = eval_axis_pair(self.colour_curves, "r", u, 1.0, u_col)
            cg = eval_axis_pair(self.colour_curves, "g", u, 1.0, u_col)
            cb = eval_axis_pair(self.colour_curves, "b", u, 1.0, u_col)
            cbr = eval_axis_pair(self.colour_curves, "brightness", u, 1.0, 0.5)
            ca = eval_axis_pair(self.alpha_curves, "alpha", u, 1.0, 0.5)
            if self.render_pass=="DISTORTION" and self.etype != "continuous_rigid":
                ca *= eval_axis_pair(self.init_alpha_curves,"x",min(1.,max(0.,(st-self.start_time)/win)),1.,u_col)

            active.append({
                "pos": pos,
                "base_pos": base_pos,
                "vel": vel,
                "scale": np.array([sx, sy, sz], dtype=np.float32),
                "rot_deg": np.array([rx, ry, rz], dtype=np.float32),
                **({"parent_rot_deg": carrier_rot} if carrier_rot is not None and np.any(carrier_rot) else {}),
                "rgb": np.array([cr * cbr, cg * cbr, cb * cbr], dtype=np.float32)*self.tint[:3],
                "alpha": float(ca*self.tint[3]),
                "age": float(age), "life_u":u, "random_u":u_yaw, "particle_id":particle_id,
            })
            if len(active) >= self.max_particles:
                break
        return active


def load_vfx_scene_from_pack(pack_path: str, wrapper_keys: list[str]) -> tuple[PackAssetResolver, list[ParsedEmitter]]:
    res = PackAssetResolver(pack_path)
    ref_specs: list[dict] = []
    for wk in wrapper_keys:
        wraw = res.read(f"vfx\\{wk}.xml")
        if wraw is None:
            raise FileNotFoundError(f"Wrapper vfx\\{wk}.xml not found in {pack_path}")
        wroot = parse_wh3_vfx_xml(wraw)
        for ref in wroot.findall("./vfx_references/vfx"):
            lkey = (ref.get("vfx_ref") or "").strip()
            if not lkey:
                continue
            lp = f"vfx\\{lkey}.xml" if not lkey.lower().endswith(".xml") else norm_pack_path(lkey)
            en_ids = {er.get("name", "") for er in ref.findall("./emitter") if (er.get("enable") or "").lower() == "true"}
            if not en_ids:
                continue
            sc_min = float(ref.get("scale_min", "1.0") or 1.0)
            sc_max = float(ref.get("scale_max", str(sc_min)) or sc_min)
            ref_specs.append({
                "lp": lp,
                "tints":{e.get("name"):np.array([(float(e.get("tint_min_"+c,1))+float(e.get("tint_max_"+c,1)))*.5 for c in "rgba"]) for e in ref.findall("emitter") if e.get("name") in en_ids},
                "en_ids": en_ids,
                "pos": np.array([float(ref.get("pos_x", "0") or 0), float(ref.get("pos_y", "0") or 0), float(ref.get("pos_z", "0") or 0)], dtype=np.float32),
                "rot_deg": np.array([float(ref.get("rot_x", "0") or 0), float(ref.get("rot_y", "0") or 0), float(ref.get("rot_z", "0") or 0)], dtype=np.float32),
                "scale": 0.5 * (sc_min + sc_max),
                "delay": float(ref.get("start_time_min", "0") or 0),
                "infinite_life": (ref.get("infinite_life") or "").strip().lower() == "true",
                "sim_speed": float(ref.get("simulation_speed_multiplier", "1.0") or 1.0),
            })
        direct = list(wroot.findall("./vfx/emitters/emitter"))
        if direct:
            ref_specs.append({
                "lp": f"vfx\\{wk}.xml",
                "en_ids": {e.get("id", "") for e in direct},
                "pos": np.zeros(3, dtype=np.float32),
                "rot_deg": np.zeros(3, dtype=np.float32),
                "scale": 1.0,
                "delay": 0.0,
                "infinite_life": False,
                "sim_speed": 1.0,
            })

    parsed_libs: dict[str, tuple[list[ET.Element], dict, dict]] = {}
    emitters: list[ParsedEmitter] = []
    for rs_spec in ref_specs:
        lp = rs_spec["lp"]
        if lp not in parsed_libs:
            lraw = res.read(lp)
            if lraw is None:
                res.omissions.add("Missing library: "+lp)
                continue
            lroot = parse_wh3_vfx_xml(lraw)
            bcs = {b.get("id", ""): b for b in lroot.findall("./vfx/behaviour_chains/behaviour_chain")}
            rss = {r.get("id", ""): r for r in lroot.findall("./vfx/effect_render_stacks/effect_render_stack")}
            parsed_libs[lp] = (list(lroot.findall("./vfx/emitters/emitter")), bcs, rss)
        em_els, bcs, rss = parsed_libs[lp]
        for em in em_els:
            eid = em.get("id", "")
            if eid not in rs_spec["en_ids"]:
                continue
            pe = ParsedEmitter(eid, em.get("type", ""), em, bcs.get(eid), rss.get(eid))
            pe.tint=rs_spec.get("tints",{}).get(eid,pe.tint)
            pe.wrapper_pos = rs_spec["pos"].copy()
            pe.wrapper_rot_deg = rs_spec["rot_deg"].copy()
            pe.wrapper_scale = float(rs_spec["scale"])
            pe.wrapper_delay = float(rs_spec["delay"])
            pe.wrapper_infinite_life = bool(rs_spec["infinite_life"])
            pe.wrapper_sim_speed = float(rs_spec["sim_speed"])
            emitters.append(pe)
    return res, emitters


# ============================================================================
# 2. ANGLE OpenGL ES 3.0 Headless GPU Context & Exact WH3 GLSL Shaders
# ============================================================================

# GL Constants
GL_FALSE = 0
GL_TRUE = 1
GL_TRIANGLES = 0x0004
GL_TRIANGLE_STRIP = 0x0005
GL_DEPTH_TEST = 0x0B71
GL_CULL_FACE = 0x0B44
GL_BLEND = 0x0BE2
GL_CW = 0x0900
GL_CCW = 0x0901
GL_BACK = 0x0405
GL_LEQUAL = 0x0203
GL_ONE = 1
GL_ZERO = 0
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_COLOR_BUFFER_BIT = 0x4000
GL_DEPTH_BUFFER_BIT = 0x0100
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_LINEAR = 0x2601
GL_NEAREST = 0x2600
GL_LINEAR_MIPMAP_LINEAR = 0x2703
GL_REPEAT = 0x2901
GL_CLAMP_TO_EDGE = 0x812F
GL_RGBA = 0x1908
GL_RED = 0x1903
GL_RGBA8 = 0x8058
GL_RGBA16F = 0x881A
GL_R16F = 0x822D
GL_DEPTH_COMPONENT = 0x1902
GL_DEPTH_COMPONENT24 = 0x81A6
GL_UNSIGNED_BYTE = 0x1401
GL_UNSIGNED_SHORT = 0x1403
GL_UNSIGNED_INT = 0x1405
GL_FLOAT = 0x1406
GL_ARRAY_BUFFER = 0x8892
GL_ELEMENT_ARRAY_BUFFER = 0x8893
GL_STATIC_DRAW = 0x88E4
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_COLOR_ATTACHMENT1 = 0x8CE1
GL_DEPTH_ATTACHMENT = 0x8D00
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
GL_VERTEX_SHADER = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82

# --- Shaders (Stock-derived equations for the supported profiles) ---

FS_QUAD_VS = """#version 300 es
precision highp float;
out vec2 v_uv;
void main() {
    vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0, (gl_VertexID == 2) ? 3.0 : -1.0);
    v_uv = p * 0.5 + 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""

# Opaque Battleground Terrain + Sky Pass (writes linear HDR + particle_mask=0 + real depth)
TERRAIN_VS = """#version 300 es
precision highp float;
layout(location=0) in vec3 a_pos;
uniform mat4 u_vp;
out vec3 v_world;
void main() {
    v_world = a_pos;
    gl_Position = u_vp * vec4(a_pos, 1.0);
}
"""

TERRAIN_FS = """#version 300 es
precision highp float;
in vec3 v_world;
uniform vec3 u_ground_a;
uniform vec3 u_ground_b;
uniform vec3 u_light_pos;
uniform vec3 u_light_col;
uniform float u_light_rad;
layout(location=0) out vec4 o_hdr;
layout(location=1) out vec4 o_mask;

float hash21(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}
float vnoise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash21(i), hash21(i + vec2(1,0)), f.x),
               mix(hash21(i + vec2(0,1)), hash21(i + vec2(1,1)), f.x), f.y);
}
void main() {
    vec2 xz = v_world.xz;
    float n1 = vnoise(xz * 0.14);
    float n2 = vnoise(xz * 0.65);
    float n3 = vnoise(xz * 2.4);
    float pat = n1 * 0.55 + n2 * 0.30 + n3 * 0.15;
    vec3 albedo = mix(u_ground_a, u_ground_b, pat);
    // Subtle 10m tactical scale grid lines (very faint, 6% contrast) so radii are easy to judge
    vec2 g = abs(fract(xz * 0.1 - 0.5) - 0.5) / 0.1;
    float grid = 1.0 - smoothstep(0.0, 0.14, min(g.x, g.y));
    albedo *= (1.0 + 0.06 * grid);
    // WH3 DEFERRED_DYNAMIC_LIGHT contribution
    if (u_light_rad > 0.1) {
        float d = length(v_world - u_light_pos);
        float att = clamp(1.0 - d / u_light_rad, 0.0, 1.0);
        att = att * att;
        albedo += albedo * u_light_col * att * 0.85;
    }
    // Gentle distance aerial fog toward horizon
    float dist = length(xz);
    float fog = smoothstep(80.0, 420.0, dist);
    vec3 col = mix(albedo, vec3(0.32, 0.36, 0.40), fog * 0.65);
    o_hdr = vec4(col, 1.0);
    o_mask = vec4(0.0, 0.0, 0.0, 1.0);
}
"""

SKY_FS = """#version 300 es
precision highp float;
in vec2 v_uv;
uniform vec3 u_sky_zenith;
uniform vec3 u_sky_horizon;
layout(location=0) out vec4 o_hdr;
layout(location=1) out vec4 o_mask;
void main() {
    float h = clamp(v_uv.y, 0.0, 1.0);
    vec3 col = mix(u_sky_horizon, u_sky_zenith, pow(h, 0.72));
    o_hdr = vec4(col, 1.0);
    o_mask = vec4(0.0, 0.0, 0.0, 1.0);
}
"""

# Exact Disassembled wh_vfx_mesh_vertex_anim.xml.shader!
WH3_MESH_VS = """#version 300 es
precision highp float;
layout(location=0) in vec3 a_pos;
layout(location=1) in vec2 a_uv;
layout(location=2) in vec3 a_nrm;
layout(location=3) in vec4 a_col;

uniform mat4 u_model;
uniform mat4 u_vp;
uniform vec3 u_pivot;
uniform sampler2D u_tex_disp;
uniform float u_time;
uniform float u_vdisp_enable;
uniform float u_vdisp_scale;
uniform vec2 u_vdisp_tile;
uniform vec2 u_vdisp_scroll;
uniform float u_osc_scale,u_anim_time,u_vertex_anim,u_anim_speed;
uniform vec3 u_osc_amp;
uniform vec3 u_osc_freq;

out vec2 v_uv;
out vec3 v_world_pos;
out vec3 v_world_nrm;
out vec4 v_col;

void main() {
    // Faithfully apply RMV2 Group0 pivot_xyz + WH3 normal vertex displacement + harmonic oscillation!
    vec4 wp=u_model*vec4(a_pos+u_pivot,1.);
    vec3 wn=normalize(mat3(u_model)*normalize(a_nrm));
    vec2 duv=(a_uv+u_anim_time*u_vdisp_scroll)*u_vdisp_tile;
    float disp=textureLod(u_tex_disp,duv,0.).b;
    // VS 423be610, ld t2.xzyw to r1.y selects Z (blue); scalar offsets all world axes.
    float delta=wn.x*u_vdisp_scale*disp*(1.-a_uv.y)*u_vdisp_enable;
    delta+=u_vertex_anim*sin((dot(wp.xyz,vec3(1.))*u_osc_scale+u_anim_time*u_anim_speed)*u_osc_freq.x)*u_osc_amp.x*a_col.r;
    wp.xyz+=vec3(delta);
    v_world_pos = wp.xyz;
    v_world_nrm = normalize(mat3(u_model) * a_nrm);
    v_uv = a_uv;
    v_col = a_col;
    gl_Position = u_vp * wp;
}
"""

WH3_MESH_FS = """#version 300 es
precision highp float;
in vec2 v_uv;
in vec3 v_world_pos;
in vec3 v_world_nrm;
in vec4 v_col;

uniform sampler2D u_tex_a;
uniform sampler2D u_tex_b;
uniform sampler2D u_tex_c;
uniform sampler2D u_tex_mask;
uniform sampler2D u_tex_overlay;
uniform sampler2D u_scene_depth;

uniform float u_time;
uniform vec2 u_scroll_a, u_tile_a;
uniform vec2 u_scroll_b, u_tile_b;
uniform vec2 u_scroll_c, u_tile_c;
uniform vec2 u_mask_pan, u_mask_tile;
uniform vec2 u_ov_pan, u_ov_tile;
uniform float u_ov_strength;
uniform float u_has_overlay;

uniform vec3 u_glow_colour;
uniform float u_glow_strength;
uniform float u_total_alpha;
uniform vec3 u_scroll_bright;
uniform vec4 u_cha,u_chb,u_chc;
uniform mat4 u_invvp;
uniform float u_vertex_colour, u_mesh_alpha;
uniform float u_nfio;
uniform float u_npow;
uniform float u_df_switch;
uniform float u_pdf;
uniform float u_alpha_cutoff;

uniform vec3 u_vfx_rgb;
uniform float u_vfx_alpha;
uniform vec3 u_cam_pos;
uniform vec2 u_inv_res;
uniform float u_near;
uniform float u_far;

layout(location=0) out vec4 o_hdr;
layout(location=1) out vec4 o_mask;

float srgb_to_lin_1(float c) {
    return (c <= 0.04045) ? (c / 12.92) : pow(max((c + 0.055) / 1.055, 0.0), 2.4);
}
vec4 sample_srgb(sampler2D s, vec2 uv) {
    // DX11 BC7_UNORM_SRGB hardware sampler decodes RGB sRGB->Linear and leaves Alpha linear
    vec4 t = texture(s, uv);
    return t; // GL_SRGB8_ALPHA8 performs RGB EOTF before filtering
}
float lin_depth(float z_ndc) {
    float z = z_ndc * 2.0 - 1.0;
    return (2.0 * u_near * u_far) / (u_far + u_near - z * (u_far - u_near));
}

void main() {
    // 1. Exact WH3 UV scroll equation: (uv + time * scroll) * tile
    vec2 uvA = (v_uv + u_time * u_scroll_a) * u_tile_a;
    vec2 uvB = (v_uv + u_time * u_scroll_b) * u_tile_b;
    vec2 uvC = (v_uv + u_time * u_scroll_c) * u_tile_c;
    vec2 uvM = (v_uv + u_time * u_mask_pan) * u_mask_tile;
    vec2 uvO = (v_uv + u_time * u_ov_pan) * u_ov_tile;

    float tA = dot(sample_srgb(u_tex_a, uvA),u_cha) * u_scroll_bright.x;
    float tB = dot(sample_srgb(u_tex_b, uvB),u_chb) * u_scroll_bright.y;
    float tC = dot(sample_srgb(u_tex_c, uvC),u_chc) * u_scroll_bright.z;
    float gM = sample_srgb(u_tex_mask, uvM).r;
    vec4 cOv = sample_srgb(u_tex_overlay, uvO);

    // 2. Exact disassembled Fresnel (lines 74-82 of wh_vfx_mesh_vertex_anim):
    //    mix(pow(1 - |N.V|, npow), pow(|N.V|, npow), normal_fade_in_out)
    vec3 V = normalize(u_cam_pos - v_world_pos);
    vec3 N = normalize(v_world_nrm);
    float ndv = clamp(abs(dot(N, V)), 1e-4, 1.0);
    float f_in = pow(ndv, max(u_npow, 0.01));
    float f_out = pow(clamp(1.0 - ndv, 1e-4, 1.0), max(u_npow, 0.01));
    float fresnel = mix(f_out, f_in, clamp(u_nfio, 0.0, 1.0));

    // 3. Ray-aligned Soft Depth Fade against real terrain plane (y = 0)
    vec2 screen=gl_FragCoord.xy*u_inv_res;
    float z=texelFetch(u_scene_depth,ivec2(gl_FragCoord.xy),0).r;
    vec4 q=u_invvp*vec4(screen*2.-1.,z*2.-1.,1.);
    float depth_fade=mix(1.,clamp(length(v_world_pos-q.xyz/q.w)/max(u_pdf,0.00001),0.,1.),u_df_switch);

    // 4. Exact disassembled alpha equation: |COLOR0.a|^2 * total_alpha * vfx_alpha
    vec4 vc=mix(vec4(1.),v_col,u_vertex_colour);
    float v_alpha_sq = vc.a * vc.a;
    float alpha_raw = tA * tB * tC * depth_fade * fresnel * gM * u_total_alpha * v_alpha_sq * mix(1., floor(u_vfx_alpha*1000.)*.001,u_mesh_alpha);
    if (alpha_raw <= u_alpha_cutoff + 1e-4) discard;

    // 5. Exact disassembled colour_overlay equation
    vec3 ov = cOv.rgb * mix(1.0, u_ov_strength, cOv.a);
    vec3 out_rgb = max(alpha_raw, 0.0) * ov * vc.rgb * u_glow_colour * u_glow_strength * u_vfx_rgb;
    float out_a = clamp(alpha_raw, 0.0, 1.0);

    o_hdr = vec4(out_rgb, out_a);
    // Write particle mask so WH3 Bloom applies g_bloom_particle_strength = 0.5!
    o_mask = vec4(out_a, 0.0, 0.0, out_a);
}
"""

# Exact particle_vfx_main / DEFERRED_DECAL / DEFERRED_DECAL_EMISSIVE shader (with ps_soft_edge + Flipbook!)
WH3_QUAD_VS = """#version 300 es
precision highp float;
layout(location=0) in vec2 a_corner;  // [-0.5, +0.5]
uniform mat4 u_vp;
uniform vec3 u_center;
uniform vec3 u_axis_x;
uniform vec3 u_axis_y;
out vec2 v_uv;
out vec3 v_world_pos;
void main() {
    v_uv = a_corner + 0.5;
    vec3 wp = u_center + u_axis_x * a_corner.x + u_axis_y * a_corner.y;
    v_world_pos = wp;
    gl_Position = u_vp * vec4(wp, 1.0);
}
"""

WH3_QUAD_FS = """#version 300 es
precision highp float;
in vec2 v_uv;
in vec3 v_world_pos;
uniform sampler2D u_tex;
uniform vec3 u_rgb;
uniform float u_alpha;
uniform float u_premul;
uniform float u_emissive;
uniform float u_add_mul;
uniform float u_soft_factor;
uniform highp sampler2D u_depth_copy;
uniform vec2 u_nf;
uniform vec2 u_fb_grid;
uniform float u_fb_frame,u_fb_blend,u_fb_next;
uniform vec3 u_cam_pos;
uniform int u_mode; // 0=billboard/emissive_decal, 1=dark_scorch_decal, 2=soft_billboard
layout(location=0) out vec4 o_hdr;
layout(location=1) out vec4 o_mask;

float srgb_to_lin_1(float c) {
    return (c <= 0.04045) ? (c / 12.92) : pow(max((c + 0.055) / 1.055, 0.0), 2.4);
}
void main() {
    // Flipbook UV slicing (vs_cell_texture)
    vec2 uv = v_uv;
    if (u_fb_grid.x > 1.5 || u_fb_grid.y > 1.5) {
        float total = u_fb_grid.x * u_fb_grid.y;
        float fi = mod(floor(u_fb_frame), total);
        float col = mod(fi, u_fb_grid.x);
        float row = floor(fi / u_fb_grid.x);
        uv = vec2((col + v_uv.x) / u_fb_grid.x, (row + v_uv.y) / u_fb_grid.y);
    }
    // Exact WH3 particle_vfx_library.hlsl: IEC 61966-2-1 sRGB->Linear on BOTH RGB and Alpha!
    vec4 raw = texture(u_tex, uv);
    if(u_fb_blend>.5 && u_mode!=1 && u_fb_grid.x*u_fb_grid.y>1.) {
        vec2 uv2=(vec2(mod(u_fb_next,u_fb_grid.x),floor(u_fb_next/u_fb_grid.x))+v_uv)/u_fb_grid;
        raw=mix(raw,texture(u_tex,uv2),fract(u_fb_frame));
    }
    vec4 t = vec4(raw.rgb,srgb_to_lin_1(raw.a));
    float soft = 1.0;
    if (u_mode == 2 && u_soft_factor >= 0.) {
        float scene=texelFetch(u_depth_copy,ivec2(gl_FragCoord.xy),0).r;
        float n=u_nf.x,f=u_nf.y;
        float zd=n*f/(f-scene*(f-n));
        float zp=n*f/(f-gl_FragCoord.z*(f-n));
        soft=clamp((zd-zp)/(u_soft_factor+.001),0.,1.);
    }
    float a = clamp(t.a * u_alpha * soft, 0.0, 1.0);
    if (a < 1e-4) discard;
    if (u_mode == 1) {
        // DEFERRED_DECAL dark scorch (premultiplied alpha darkens ground, particle_mask = 0)
        vec3 col = (u_premul>.5 ? min(t.rgb,vec3(t.a))*clamp(u_alpha,0.,1.) : t.rgb*a)*u_rgb;
        o_hdr = vec4(col, a);
        o_mask = vec4(0.0, 0.0, 0.0, 0.0);
    } else {
        // FULLRES_DEFERRED_ALPHA / DEFERRED_DECAL_EMISSIVE
        vec3 covered=u_premul>.5 ? min(t.rgb,vec3(t.a))*clamp(u_alpha,0.,1.)*soft : t.rgb*a;
        vec3 excess=u_premul>.5 ? clamp(t.rgb-vec3(t.a),0.,1.)*u_add_mul*soft : vec3(0.);
        vec3 src_rgb=(covered*u_emissive+excess)*u_rgb;
        float blend_a=a;
        o_hdr = vec4(src_rgb, blend_a);
        o_mask = vec4(a, 0.0, 0.0, a);
    }
}
"""

# Stage 1F: Exact WH3 DISTORTION Pass (DXT5nm BC3_UNORM: A=X, G=Y, neutral=0.5)
WH3_DISTORT_FS = """#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_tex;
uniform float u_alpha,u_soft,u_near,u_far;
uniform highp sampler2D u_depth;
out vec4 o_dist;
float eyeZ(float z){return u_near*u_far/(u_far-z*(u_far-u_near));}
void main() {
    vec4 raw=texture(u_tex,v_uv);
    // ps_normal, then final distortion PS: dv*length(projected normal), not raw XY alone.
    vec2 n=(vec2(raw.a,raw.g)+vec2(.0019607843))*2.-1.;
    vec3 nn=normalize(vec3(n,sqrt(max(0.,1.-dot(n,n)))));
    float alpha=u_alpha;
    if(u_soft>0.){
        float scene=texelFetch(u_depth,ivec2(gl_FragCoord.xy),0).r;
        alpha*=clamp((eyeZ(scene)-eyeZ(gl_FragCoord.z))/(u_soft+.001),0.,1.);
    }
    o_dist=vec4(nn.xy*length(nn.xy)*alpha,0.,alpha*length(nn.xy));
}
"""

# Stage 2A: Exact Disassembled fx_hdr_tone_mapper_hlsl_9b626129 Bloom Bright-Pass!
WH3_BLOOM_EXTRACT_FS = """#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_hdr;
uniform sampler2D u_mask;
uniform float u_bloom_threshold;
uniform float u_bloom_strength;
uniform float u_bloom_particle_strength;
out vec4 o_col;
void main() {
    vec3 hdr = texture(u_hdr, v_uv).rgb;
    float pmask = clamp(texture(u_mask, v_uv).r, 0.0, 1.0);
    float L = dot(hdr, vec3(0.2126, 0.7152, 0.0722));
    float w = clamp(L - max(u_bloom_threshold, 0.0), 0.0, 1.0);
    float str = mix(u_bloom_strength, u_bloom_particle_strength, pmask);
    o_col = vec4(hdr * w * str, 1.0);
}
"""

# Stage 2B: 9-tap Separable Gaussian Blur for Mip Pyramid
BLUR_FS = """#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_src;
uniform vec2 u_dir;
out vec4 o_col;
void main() {
    vec3 s = texture(u_src, v_uv).rgb * 0.2270270270;
    s += texture(u_src, v_uv + u_dir * 1.3846153846).rgb * 0.3162162162;
    s += texture(u_src, v_uv - u_dir * 1.3846153846).rgb * 0.3162162162;
    s += texture(u_src, v_uv + u_dir * 3.2307692308).rgb * 0.0702702703;
    s += texture(u_src, v_uv - u_dir * 3.2307692308).rgb * 0.0702702703;
    o_col = vec4(s, 1.0);
}
"""

# Stage 2C: Exact Disassembled fx_hdr_to_screen_hlsl_f9042970 Luminance-Scaled Extended Reinhard + Gamma!
WH3_TONEMAP_FS = """#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_hdr;
uniform sampler2D u_distort;
uniform sampler2D u_b0;
uniform sampler2D u_b1;
uniform sampler2D u_b2;
uniform sampler2D u_b3;
uniform sampler2D u_b4;
uniform float u_tm_brightness;
uniform float u_tm_burn;
uniform float u_g_brightness;
uniform float u_inv_gamma;
uniform int u_view_mode; // 0=full WH3 Stage2, 1=Stage1 raw linear-clamped, 2=Bloom buffer only
out vec4 o_col;
void main() {
    // Exact fx_hdr_to_screen_hlsl_f9042970 lines 227-241: screen-space UV bend scaled by 0.02!
    vec2 uv_s = v_uv;
    if (u_view_mode == 0) {
        vec4 accum = texture(u_distort, v_uv);
        // fx_hdr_to_screen f9042970, cb2[20].x == 0; UI/quality-specific alternatives are not selected here.
        vec2 dv = accum.xy * (1.0 - accum.w);
        uv_s = clamp(v_uv + dv * 0.02, vec2(0.001), vec2(0.999));
    }
    vec3 hdr = texture(u_hdr, uv_s).rgb;
    vec3 bloom = (
        texture(u_b0, uv_s).rgb * 0.32 +
        texture(u_b1, uv_s).rgb * 0.28 +
        texture(u_b2, uv_s).rgb * 0.20 +
        texture(u_b3, uv_s).rgb * 0.13 +
        texture(u_b4, uv_s).rgb * 0.07
    );
    if (u_view_mode == 1) {
        // Stage 1 Raw Linear-clamped (no bloom, no tonemap — pure shader + blend check)
        o_col = vec4(pow(clamp(hdr, 0.0, 1.0), vec3(u_inv_gamma)), 1.0);
        return;
    }
    if (u_view_mode == 2) {
        // Bloom extraction + pyramid visualization
        o_col = vec4(pow(clamp(bloom * 1.5, 0.0, 1.0), vec3(u_inv_gamma)), 1.0);
        return;
    }
    // Exact fx_hdr_to_screen_hlsl lines 244-273:
    vec3 total = hdr + bloom;
    float L = dot(total, vec3(0.2126, 0.7152, 0.0722));
    float exposure = u_tm_brightness * u_tm_brightness;
    float L_s = L * exposure;
    float inv_white_sq=u_tm_burn/(1.-.999*u_tm_burn);
    float L_out = (L_s * (1.0 + L_s * inv_white_sq)) / (1.0 + L_s);
    vec3 mapped = (L <= 1e-6) ? vec3(0.0) : total * (L_out / L);
    vec3 gamma_rgb = pow(clamp(mapped * u_g_brightness, 0.0, 1.0), vec3(u_inv_gamma));
    o_col = vec4(gamma_rgb, 1.0);
}
"""

# Stage 2D: Exact Disassembled fx_postprocessing_hlsl 5-Tap Laplacian Sharpen!
WH3_SHARPEN_FS = """#version 300 es
precision highp float;
in vec2 v_uv;
uniform sampler2D u_src;
uniform vec2 u_inv_res;
uniform float u_strength;
out vec4 o_col;
void main() {
    vec3 c = texture(u_src, v_uv).rgb;
    vec3 u = texture(u_src, v_uv + vec2(0.0, u_inv_res.y)).rgb;
    vec3 d = texture(u_src, v_uv - vec2(0.0, u_inv_res.y)).rgb;
    vec3 l = texture(u_src, v_uv - vec2(u_inv_res.x, 0.0)).rgb;
    vec3 r = texture(u_src, v_uv + vec2(u_inv_res.x, 0.0)).rgb;
    vec3 diff = 4.0 * c - u - d - l - r;
    o_col = vec4(clamp(c + u_strength * diff, 0.0, 1.0), 1.0);
}
"""


# ============================================================================
# 3. Minimal Math Helpers (Right-Handed Y-Up Matrices)
# ============================================================================

def mat4_perspective(fov_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / math.tan(math.radians(fov_deg) * 0.5)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2.0 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def mat4_lookat(eye: np.ndarray, target: np.ndarray, up: np.ndarray) -> np.ndarray:
    f = target - eye
    f /= max(np.linalg.norm(f), 1e-8)
    s = np.cross(f, up)
    s /= max(np.linalg.norm(s), 1e-8)
    u = np.cross(s, f)
    m = np.eye(4, dtype=np.float32)
    m[0, :3] = s
    m[1, :3] = u
    m[2, :3] = -f
    m[0, 3] = -float(np.dot(s, eye))
    m[1, 3] = -float(np.dot(u, eye))
    m[2, 3] = float(np.dot(f, eye))
    return m


def mat4_trs(pos: np.ndarray, rot_deg: np.ndarray, scale: np.ndarray) -> np.ndarray:
    rx, ry, rz = [math.radians(float(a)) for a in rot_deg]
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float32)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float32)
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float32)
    R = Ry @ Rx @ Rz
    S = np.diag([float(scale[0]), float(scale[1]), float(scale[2])]).astype(np.float32)
    M = np.eye(4, dtype=np.float32)
    M[:3, :3] = R @ S
    M[:3, 3] = pos
    return M


# ============================================================================
# 4. ANGLE GLES3 Renderer Implementation
# ============================================================================

class WH3TrueRenderer:
    def __init__(self, width: int, height: int, env_preset: str = "daylight", slope_deg=0.):
        self.w = width
        self.h = height
        self.env = {**WH3_ENV_PRESETS.get(env_preset,WH3_ENV_PRESETS["daylight"]),**parameters(env_preset)}
        self.slope=math.tan(math.radians(slope_deg))
        self._init_egl()
        self._setup_gl_sigs()
        self._build_programs()
        self._build_targets()
        self._build_static_geoms()
        self.gpu_textures: dict[str, int] = {}
        self.gpu_meshes: dict[str, dict] = {}

    def _init_egl(self):
        if os.path.isdir(ANGLE_DLL_DIR) and hasattr(os, "add_dll_directory"):
            self.angle_dll_dir = os.add_dll_directory(ANGLE_DLL_DIR)
        self.egl = ctypes.CDLL(os.path.join(ANGLE_DLL_DIR, "libEGL.dll"))
        self.gl = ctypes.CDLL(os.path.join(ANGLE_DLL_DIR, "libGLESv2.dll"))

        self.egl.eglGetDisplay.argtypes = [ctypes.c_void_p]
        self.egl.eglGetDisplay.restype = ctypes.c_void_p
        self.egl.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        self.egl.eglInitialize.restype = ctypes.c_uint
        self.egl.eglBindAPI.argtypes = [ctypes.c_uint]
        self.egl.eglBindAPI.restype = ctypes.c_uint
        self.egl.eglChooseConfig.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.egl.eglChooseConfig.restype = ctypes.c_uint
        self.egl.eglCreatePbufferSurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.egl.eglCreatePbufferSurface.restype = ctypes.c_void_p
        self.egl.eglCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.egl.eglCreateContext.restype = ctypes.c_void_p
        self.egl.eglMakeCurrent.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.egl.eglMakeCurrent.restype = ctypes.c_uint

        self.dpy = self.egl.eglGetDisplay(ctypes.c_void_p(0))
        maj, mn = ctypes.c_int(), ctypes.c_int()
        if not self.egl.eglInitialize(self.dpy, ctypes.byref(maj), ctypes.byref(mn)):
            raise RuntimeError("eglInitialize failed")
        self.egl.eglBindAPI(0x30A0)
        attribs = (ctypes.c_int * 15)(
            0x3033, 0x0001, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8, 0x3025, 24, 0x3040, 0x0040, 0x3038
        )
        cfg = ctypes.c_void_p()
        ncfg = ctypes.c_int()
        self.egl.eglChooseConfig(self.dpy, attribs, ctypes.byref(cfg), 1, ctypes.byref(ncfg))
        self.surf = self.egl.eglCreatePbufferSurface(self.dpy, cfg, (ctypes.c_int * 5)(0x3057, 16, 0x3056, 16, 0x3038))
        self.ctx = self.egl.eglCreateContext(self.dpy, cfg, ctypes.c_void_p(0), (ctypes.c_int * 3)(0x3098, 3, 0x3038))
        if not self.egl.eglMakeCurrent(self.dpy, self.surf, self.surf, self.ctx):
            raise RuntimeError("eglMakeCurrent failed")

    def _setup_gl_sigs(self):
        g = self.gl
        g.glCreateShader.argtypes = [ctypes.c_uint]; g.glCreateShader.restype = ctypes.c_uint
        g.glShaderSource.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int)]
        g.glCompileShader.argtypes = [ctypes.c_uint]
        g.glGetShaderiv.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
        g.glGetShaderInfoLog.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p]
        g.glCreateProgram.restype = ctypes.c_uint
        g.glAttachShader.argtypes = [ctypes.c_uint, ctypes.c_uint]
        g.glLinkProgram.argtypes = [ctypes.c_uint]
        g.glGetProgramiv.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
        g.glGetProgramInfoLog.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p]
        g.glUseProgram.argtypes = [ctypes.c_uint]
        g.glGetUniformLocation.argtypes = [ctypes.c_uint, ctypes.c_char_p]; g.glGetUniformLocation.restype = ctypes.c_int
        g.glUniform1i.argtypes = [ctypes.c_int, ctypes.c_int]
        g.glUniform1f.argtypes = [ctypes.c_int, ctypes.c_float]
        g.glUniform2f.argtypes = [ctypes.c_int, ctypes.c_float, ctypes.c_float]
        g.glUniform4f.argtypes=[ctypes.c_int]+[ctypes.c_float]*4
        g.glUniform3f.argtypes = [ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_float]
        g.glUniformMatrix4fv.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_ubyte, ctypes.c_void_p]
        g.glGenTextures.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        g.glBindTexture.argtypes = [ctypes.c_uint, ctypes.c_uint]
        g.glActiveTexture.argtypes = [ctypes.c_uint]
        g.glTexParameteri.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_int]
        g.glTexImage2D.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        g.glGenerateMipmap.argtypes = [ctypes.c_uint]
        g.glGenFramebuffers.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        g.glBindFramebuffer.argtypes = [ctypes.c_uint, ctypes.c_uint]
        g.glFramebufferTexture2D.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int]
        g.glDrawBuffers.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        g.glCheckFramebufferStatus.argtypes = [ctypes.c_uint]; g.glCheckFramebufferStatus.restype = ctypes.c_uint
        g.glGenVertexArrays.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        g.glBindVertexArray.argtypes = [ctypes.c_uint]
        g.glGenBuffers.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
        g.glBindBuffer.argtypes = [ctypes.c_uint, ctypes.c_uint]
        g.glBufferData.argtypes = [ctypes.c_uint, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint]
        g.glEnableVertexAttribArray.argtypes = [ctypes.c_uint]
        g.glVertexAttribPointer.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p]
        g.glViewport.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        g.glClearColor.argtypes = [ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float]
        g.glClear.argtypes = [ctypes.c_uint]
        g.glEnable.argtypes = [ctypes.c_uint]
        g.glDisable.argtypes = [ctypes.c_uint]
        g.glDepthMask.argtypes = [ctypes.c_ubyte]
        g.glDepthFunc.argtypes = [ctypes.c_uint]
        g.glFrontFace.argtypes = [ctypes.c_uint]
        g.glCullFace.argtypes = [ctypes.c_uint]
        g.glBlendFunc.argtypes = [ctypes.c_uint, ctypes.c_uint]
        g.glDrawArrays.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int]
        g.glDrawElements.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
        g.glReadPixels.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        g.glPixelStorei.argtypes = [ctypes.c_uint, ctypes.c_int]

    def _compile_prog(self, vs_src: str, fs_src: str) -> int:
        g = self.gl
        def _sh(kind, src):
            s = g.glCreateShader(kind)
            b = src.encode("utf-8")
            p = ctypes.c_char_p(b)
            ln = ctypes.c_int(len(b))
            g.glShaderSource(s, 1, ctypes.byref(p), ctypes.byref(ln))
            g.glCompileShader(s)
            ok = ctypes.c_int()
            g.glGetShaderiv(s, GL_COMPILE_STATUS, ctypes.byref(ok))
            if not ok.value:
                buf = ctypes.create_string_buffer(4096)
                g.glGetShaderInfoLog(s, 4096, None, buf)
                raise RuntimeError(f"Shader compile error:\n{buf.value.decode('utf-8','replace')}")
            return s
        vs = _sh(GL_VERTEX_SHADER, vs_src)
        fs = _sh(GL_FRAGMENT_SHADER, fs_src)
        prog = g.glCreateProgram()
        g.glAttachShader(prog, vs)
        g.glAttachShader(prog, fs)
        g.glLinkProgram(prog)
        ok = ctypes.c_int()
        g.glGetProgramiv(prog, GL_LINK_STATUS, ctypes.byref(ok))
        if not ok.value:
            buf = ctypes.create_string_buffer(4096)
            g.glGetProgramInfoLog(prog, 4096, None, buf)
            raise RuntimeError(f"Program link error:\n{buf.value.decode('utf-8','replace')}")
        return prog

    def _build_programs(self):
        self.p_sky = self._compile_prog(FS_QUAD_VS, SKY_FS)
        self.p_terrain = self._compile_prog(TERRAIN_VS, TERRAIN_FS)
        self.p_mesh = self._compile_prog(WH3_MESH_VS, WH3_MESH_FS)
        self.p_quad = self._compile_prog(WH3_QUAD_VS, WH3_QUAD_FS)
        self.p_distort = self._compile_prog(WH3_QUAD_VS, WH3_DISTORT_FS)
        self.p_bloom_ex = self._compile_prog(FS_QUAD_VS, WH3_BLOOM_EXTRACT_FS)
        self.p_blur = self._compile_prog(FS_QUAD_VS, BLUR_FS)
        self.p_tonemap = self._compile_prog(FS_QUAD_VS, WH3_TONEMAP_FS)
        self.p_sharpen = self._compile_prog(FS_QUAD_VS, WH3_SHARPEN_FS)

    def _make_tex(self, w: int, h: int, ifmt: int, fmt: int, tp: int, filt: int = GL_LINEAR, wrap: int = GL_CLAMP_TO_EDGE, data=None) -> int:
        g = self.gl
        tid = ctypes.c_uint()
        g.glGenTextures(1, ctypes.byref(tid))
        g.glBindTexture(GL_TEXTURE_2D, tid.value)
        g.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, filt)
        g.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, filt)
        g.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, wrap)
        g.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, wrap)
        ptr = data.ctypes.data_as(ctypes.c_void_p) if data is not None else ctypes.c_void_p(0)
        g.glTexImage2D(GL_TEXTURE_2D, 0, ifmt, w, h, 0, fmt, tp, ptr)
        return tid.value

    def _make_fbo(self, color_texs: list[int], depth_tex: int | None = None) -> int:
        g = self.gl
        fbo = ctypes.c_uint()
        g.glGenFramebuffers(1, ctypes.byref(fbo))
        g.glBindFramebuffer(GL_FRAMEBUFFER, fbo.value)
        bufs = (ctypes.c_uint * len(color_texs))()
        for i, ct in enumerate(color_texs):
            g.glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0 + i, GL_TEXTURE_2D, ct, 0)
            bufs[i] = GL_COLOR_ATTACHMENT0 + i
        g.glDrawBuffers(len(color_texs), bufs)
        if depth_tex is not None:
            g.glFramebufferTexture2D(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_TEXTURE_2D, depth_tex, 0)
        st = g.glCheckFramebufferStatus(GL_FRAMEBUFFER)
        if st != GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError(f"Incomplete FBO: {hex(st)}")
        return fbo.value

    def _build_targets(self):
        # Main HDR + Particle Mask + Depth FBO
        self.t_hdr = self._make_tex(self.w, self.h, GL_RGBA16F, GL_RGBA, GL_FLOAT)
        self.t_pmask = self._make_tex(self.w, self.h, GL_R16F, GL_RED, GL_FLOAT)
        self.t_depth = self._make_tex(self.w, self.h, GL_DEPTH_COMPONENT24, GL_DEPTH_COMPONENT, GL_UNSIGNED_INT, filt=GL_NEAREST)
        self.fbo_hdr = self._make_fbo([self.t_hdr, self.t_pmask], self.t_depth)
        # Screen-space Distortion Accumulation FBO (shares real terrain depth!)
        self.t_distort = self._make_tex(self.w, self.h, GL_RGBA16F, GL_RGBA, GL_FLOAT)
        self.fbo_distort = self._make_fbo([self.t_distort], self.t_depth)
        # Depth copy texture for depth_fade sampling while depth-testing
        self.t_depth_ro = self._make_tex(self.w, self.h, GL_DEPTH_COMPONENT24, GL_DEPTH_COMPONENT, GL_UNSIGNED_INT, filt=GL_NEAREST)

        # 5-level Bloom mip pyramid (ping-pong per level)
        self.bloom_levels = []
        bw, bh = max(self.w // 2, 16), max(self.h // 2, 16)
        for _ in range(5):
            ta = self._make_tex(bw, bh, GL_RGBA16F, GL_RGBA, GL_FLOAT)
            tb = self._make_tex(bw, bh, GL_RGBA16F, GL_RGBA, GL_FLOAT)
            fa = self._make_fbo([ta])
            fb = self._make_fbo([tb])
            self.bloom_levels.append((bw, bh, ta, fa, tb, fb))
            bw, bh = max(bw // 2, 8), max(bh // 2, 8)

        # LDR Tonemap + Final Sharpen FBOs
        self.t_tm = self._make_tex(self.w, self.h, GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE)
        self.fbo_tm = self._make_fbo([self.t_tm])
        self.t_out = self._make_tex(self.w, self.h, GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE)
        self.fbo_out = self._make_fbo([self.t_out])

    def _build_static_geoms(self):
        g = self.gl
        # Empty VAO for fullscreen triangle
        vao = ctypes.c_uint()
        g.glGenVertexArrays(1, ctypes.byref(vao))
        self.vao_empty = vao.value

        # Ground plane [-500..+500] at y=0 (DX CW when viewed from above)
        pts = np.array([
            [-500.0, 0.0, -500.0], [500.0, 0.0, -500.0], [500.0, 0.0, 500.0],
            [-500.0, 0.0, -500.0], [500.0, 0.0, 500.0], [-500.0, 0.0, 500.0],
        ], dtype=np.float32)
        pts[:,1]=pts[:,0]*self.slope
        vao_t = ctypes.c_uint(); vbo_t = ctypes.c_uint()
        g.glGenVertexArrays(1, ctypes.byref(vao_t))
        g.glGenBuffers(1, ctypes.byref(vbo_t))
        g.glBindVertexArray(vao_t.value)
        g.glBindBuffer(GL_ARRAY_BUFFER, vbo_t.value)
        g.glBufferData(GL_ARRAY_BUFFER, pts.nbytes, pts.ctypes.data_as(ctypes.c_void_p), GL_STATIC_DRAW)
        g.glEnableVertexAttribArray(0)
        g.glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 12, ctypes.c_void_p(0))
        self.vao_terrain = vao_t.value

        # Unit quad [-0.5..+0.5]
        qpts = np.array([[-0.5, -0.5], [0.5, -0.5], [-0.5, 0.5], [0.5, 0.5]], dtype=np.float32)
        vao_q = ctypes.c_uint(); vbo_q = ctypes.c_uint()
        g.glGenVertexArrays(1, ctypes.byref(vao_q))
        g.glGenBuffers(1, ctypes.byref(vbo_q))
        g.glBindVertexArray(vao_q.value)
        g.glBindBuffer(GL_ARRAY_BUFFER, vbo_q.value)
        g.glBufferData(GL_ARRAY_BUFFER, qpts.nbytes, qpts.ctypes.data_as(ctypes.c_void_p), GL_STATIC_DRAW)
        g.glEnableVertexAttribArray(0)
        g.glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 8, ctypes.c_void_p(0))
        self.vao_quad = vao_q.value

    def upload_texture(self, key: str, rgba_u8: np.ndarray, tileable: bool = True) -> int:
        ckey = f"{key}|{'R' if tileable else 'C'}"
        if ckey in self.gpu_textures:
            return self.gpu_textures[ckey]
        arr = np.ascontiguousarray(rgba_u8, dtype=np.uint8)
        h, w = arr.shape[:2]
        self.gl.glPixelStorei(0x0CF5, 1)  # GL_UNPACK_ALIGNMENT = 1
        tid = self._make_tex(w, h, (GL_RGBA8 if key.endswith(("|LIN","|nativeLinear")) else 0x8C43), GL_RGBA, GL_UNSIGNED_BYTE, GL_LINEAR, GL_REPEAT if tileable else GL_CLAMP_TO_EDGE, arr)
        # Generate full mip chain + Trilinear filtering (eliminates high-frequency shimmer in L2 videos!)
        if w >= 16 and h >= 16 and (w & (w - 1)) == 0 and (h & (h - 1)) == 0:
            self.gl.glBindTexture(GL_TEXTURE_2D, tid)
            self.gl.glGenerateMipmap(GL_TEXTURE_2D)
            self.gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
        self.gpu_textures[ckey] = tid
        return tid

    def upload_rmv2(self, key: str, parsed: dict) -> dict:
        if key in self.gpu_meshes:
            return self.gpu_meshes[key]
        g = self.gl
        inter = np.concatenate([parsed["pos"], parsed["uv"], parsed["nrm"], parsed["col"]], axis=1).astype(np.float32)
        inter = np.ascontiguousarray(inter)
        idx = np.ascontiguousarray(parsed["idx"], dtype=np.uint16)

        vao = ctypes.c_uint(); vbo = ctypes.c_uint(); ebo = ctypes.c_uint()
        g.glGenVertexArrays(1, ctypes.byref(vao))
        g.glGenBuffers(1, ctypes.byref(vbo))
        g.glGenBuffers(1, ctypes.byref(ebo))
        g.glBindVertexArray(vao.value)
        g.glBindBuffer(GL_ARRAY_BUFFER, vbo.value)
        g.glBufferData(GL_ARRAY_BUFFER, inter.nbytes, inter.ctypes.data_as(ctypes.c_void_p), GL_STATIC_DRAW)
        g.glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ebo.value)
        g.glBufferData(GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx.ctypes.data_as(ctypes.c_void_p), GL_STATIC_DRAW)
        stride = 12 * 4
        g.glEnableVertexAttribArray(0); g.glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(0))
        g.glEnableVertexAttribArray(1); g.glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(12))
        g.glEnableVertexAttribArray(2); g.glVertexAttribPointer(2, 3, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(20))
        g.glEnableVertexAttribArray(3); g.glVertexAttribPointer(3, 4, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(32))
        pos_arr = np.asarray(parsed["pos"], dtype=np.float32)
        bb_min = pos_arr.min(axis=0) if len(pos_arr) else np.zeros(3, dtype=np.float32)
        bb_max = pos_arr.max(axis=0) if len(pos_arr) else np.zeros(3, dtype=np.float32)
        rec = {"vao": vao.value, "icount": int(len(idx)), "pivot": parsed["pivot"], "bb_min": bb_min, "bb_max": bb_max}
        self.gpu_meshes[key] = rec
        return rec

    def _u1i(self, prog, name, v):
        loc = self.gl.glGetUniformLocation(prog, name.encode()); 
        if loc >= 0: self.gl.glUniform1i(loc, int(v))
    def _u1f(self, prog, name, v):
        loc = self.gl.glGetUniformLocation(prog, name.encode()); 
        if loc >= 0: self.gl.glUniform1f(loc, float(v))
    def _u2f(self, prog, name, x, y):
        loc = self.gl.glGetUniformLocation(prog, name.encode()); 
        if loc >= 0: self.gl.glUniform2f(loc, float(x), float(y))
    def _u3f(self, prog, name, x, y, z):
        loc = self.gl.glGetUniformLocation(prog, name.encode()); 
        if loc >= 0: self.gl.glUniform3f(loc, float(x), float(y), float(z))
    def _um4(self, prog, name, m: np.ndarray):
        loc = self.gl.glGetUniformLocation(prog, name.encode())
        if loc >= 0:
            mt = np.ascontiguousarray(m.T, dtype=np.float32)  # column-major for GLSL
            self.gl.glUniformMatrix4fv(loc, 1, GL_FALSE, mt.ctypes.data_as(ctypes.c_void_p))

    def render_frame(
        self,
        t_sec: float,
        cam_eye: np.ndarray,
        cam_target: np.ndarray,
        fov_deg: float,
        emitters: list[ParsedEmitter],
        resolver: PackAssetResolver,
        ws_cache: dict,
        view_mode: int = 0,
    ) -> tuple[np.ndarray, dict]:
        g = self.gl
        near, far = 0.25, 1200.0
        V = mat4_lookat(cam_eye, cam_target, np.array([0.0, 1.0, 0.0], dtype=np.float32))
        P = mat4_perspective(fov_deg, self.w / float(self.h), near, far)
        VP = P @ V
        cam_right = V[0, :3].copy()
        cam_up = V[1, :3].copy()

        # Gather active dynamic light for terrain pass
        l_pos = np.array([0.0, 4.0, 0.0], dtype=np.float32)
        l_col = np.zeros(3, dtype=np.float32)
        l_rad = 0.0
        for em in emitters:
            if em.render_pass == "DEFERRED_DYNAMIC_LIGHT":
                for p in em.simulate_particles(t_sec):
                    l_pos = p["pos"]
                    l_col = np.maximum(l_col, p["rgb"] * min(p["alpha"] * 0.12, 3.0))
                    l_rad = max(l_rad, float(p["scale"][0]))

        # ==============================================================
        # STAGE 1: Real WH3 Shader Math + Real Blend Modes -> RGBA16F HDR
        # ==============================================================
        g.glBindFramebuffer(GL_FRAMEBUFFER, self.fbo_hdr)
        g.glViewport(0, 0, self.w, self.h)
        g.glDepthMask(GL_TRUE)
        g.glClearColor(0.0, 0.0, 0.0, 0.0)
        g.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

        # 1a. Sky backdrop (no depth write)
        g.glDisable(GL_DEPTH_TEST)
        g.glDisable(GL_CULL_FACE)
        g.glDisable(GL_BLEND)
        g.glDepthMask(GL_FALSE)
        g.glUseProgram(self.p_sky)
        self._u3f(self.p_sky, "u_sky_zenith", *self.env["sky_zenith"])
        self._u3f(self.p_sky, "u_sky_horizon", *self.env["sky_horizon"])
        g.glBindVertexArray(self.vao_empty)
        g.glDrawArrays(GL_TRIANGLES, 0, 3)

        # 1b. Opaque Battlefield Terrain (writes real 24-bit depth!)
        g.glEnable(GL_DEPTH_TEST)
        g.glDepthFunc(GL_LEQUAL)
        g.glDepthMask(GL_TRUE)
        g.glUseProgram(self.p_terrain)
        self._um4(self.p_terrain, "u_vp", VP)
        self._u3f(self.p_terrain, "u_ground_a", *self.env["ground_a"])
        self._u3f(self.p_terrain, "u_ground_b", *self.env["ground_b"])
        self._u3f(self.p_terrain, "u_light_pos", *l_pos)
        self._u3f(self.p_terrain, "u_light_col", *l_col)
        self._u1f(self.p_terrain, "u_light_rad", l_rad)
        g.glBindVertexArray(self.vao_terrain)
        g.glDrawArrays(GL_TRIANGLES, 0, 6)

        # Optional L2 actor pass shares the terrain depth buffer; ordinary renders are unchanged.
        draw_actor = getattr(self, "draw_opaque_actor", None)
        if draw_actor is not None:
            draw_actor(VP)

        # Native rigid instances use the same emitter/carrier transforms as the ordinary mesh path.
        native_draws = {"opaque": [], "lens": []}
        for em in emitters:
            entry=ws_cache.get(em.wsmodel)
            if not entry or not entry.get("native_kind"): continue
            wrap=mat4_trs(em.wrapper_pos, em.wrapper_rot_deg, np.full(3,em.wrapper_scale))
            for p in em.simulate_particles(t_sec):
                if p["alpha"] <= 1e-4 or max(abs(p["scale"])) <= 1e-4: continue
                model=mat4_trs(p["pos"],p["rot_deg"],p["scale"])
                if "parent_rot_deg" in p:
                    model=mat4_trs(np.zeros(3),p["parent_rot_deg"],np.ones(3)) @ model
                if em.root_track is not None: model = model @ native_root_track.matrix(em.root_track,p["age"]*em.anim_speed)
                native_draws[entry["native_kind"]].append((entry,wrap@model,p))
        if native_draws["opaque"]:
            if not hasattr(self,"p_native_black"):
                self.p_native_black=self._compile_prog(native_rigid.NATIVE_VS,native_rigid.BLACK_FS)
            prog=self.p_native_black
            g.glUseProgram(prog); self._um4(prog,"u_vp",VP)
            g.glEnable(GL_CULL_FACE);g.glFrontFace(GL_CCW);g.glCullFace(GL_BACK)
            for entry,model,p in native_draws["opaque"]:
                gm=entry["gpu_mesh"]; self._um4(prog,"u_model",model)
                g.glBindVertexArray(gm["vao"]);g.glDrawElements(GL_TRIANGLES,gm["icount"],GL_UNSIGNED_SHORT,ctypes.c_void_p(0))
            g.glDisable(GL_CULL_FACE)
        if ws_cache or any(em.has_soft_edge for em in emitters):
            # Real opaque depth copy for ps_main_distortion. Do not sample an attached depth texture.
            if not hasattr(self,"fbo_native_depth_copy"):
                ct=self._make_tex(self.w,self.h,GL_RGBA8,GL_RGBA,GL_UNSIGNED_BYTE)
                self.fbo_native_depth_copy=self._make_fbo([ct],self.t_depth_ro)
                g.glBlitFramebuffer.argtypes=[ctypes.c_int]*8+[ctypes.c_uint,ctypes.c_uint]
            g.glBindFramebuffer(0x8CA8,self.fbo_hdr);g.glBindFramebuffer(0x8CA9,self.fbo_native_depth_copy)
            g.glBlitFramebuffer(0,0,self.w,self.h,0,0,self.w,self.h,GL_DEPTH_BUFFER_BIT,GL_NEAREST)
            g.glBindFramebuffer(GL_FRAMEBUFFER,self.fbo_hdr)
        # Copy opaque terrain depth for soft depth-fade lookup (glCopyTexSubImage2D on depth not always supported in ES3,
        # so we keep depth_fade disabled when sampling bound depth or use analytic y-height soft ground fade!).
        # Actually terrain is y=0 plane: analytic ground intersection distance gives exact zero-artifact terrain depth fade!
        g.glDepthMask(GL_FALSE)

        # 1c. DEFERRED_DECAL (dark scorch) & DEFERRED_DECAL_EMISSIVE (ground glowing runes/seals)
        g.glEnable(GL_BLEND)
        g.glDisable(GL_CULL_FACE)
        for pass_order in ("DEFERRED_DECAL", "DEFERRED_DECAL_EMISSIVE"):
            for em in emitters:
                if em.render_pass != pass_order:
                    continue
                tex_id = self.upload_texture(em.diffuse_dds, resolver.decode_dds_rgba_u8(em.diffuse_dds), tileable=False)
                g.glUseProgram(self.p_quad)
                g.glActiveTexture(GL_TEXTURE0)
                g.glBindTexture(GL_TEXTURE_2D, tex_id)
                self._u1i(self.p_quad, "u_tex", 0)
                self._um4(self.p_quad, "u_vp", VP)
                g.glBindVertexArray(self.vao_quad)
                g.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
                for p in em.simulate_particles(t_sec):
                    rad = float(p["scale"][0])
                    if rad <= 1e-3 or p["alpha"] <= 1e-4:
                        continue
                    c_pos = np.array([p["pos"][0], 0.04 if pass_order == "DEFERRED_DECAL" else 0.07, p["pos"][2]], dtype=np.float32)
                    ang = math.radians(float(p["rot_deg"][1]))
                    ax = np.array([math.cos(ang), 0.0, math.sin(ang)], dtype=np.float32) * rad
                    ay = np.array([-math.sin(ang), 0.0, math.cos(ang)], dtype=np.float32) * rad
                    c_pos[1]+=self.slope*c_pos[0]; ax[1]=self.slope*ax[0];ay[1]=self.slope*ay[0]
                    self._u3f(self.p_quad, "u_center", *c_pos)
                    self._u3f(self.p_quad, "u_axis_x", *ax)
                    self._u3f(self.p_quad, "u_axis_y", *ay)
                    self._u3f(self.p_quad, "u_rgb", *p["rgb"])
                    self._u1f(self.p_quad, "u_alpha", p["alpha"])
                    self._u1f(self.p_quad, "u_premul", 1.0 if em.pre_multiplied_alpha else 0.0)
                    self._u1f(self.p_quad, "u_emissive", em.emissive_gain if em.has_ps_lighting else 1.0)
                    self._u1f(self.p_quad, "u_add_mul", em.additive_blend_mul if em.has_ps_lighting else 0.0)
                    self._u1f(self.p_quad, "u_soft_factor", 0.0)
                    self._u2f(self.p_quad, "u_fb_grid", 1.0, 1.0)
                    self._u1f(self.p_quad, "u_fb_frame", 0.0)
                    self._u3f(self.p_quad, "u_cam_pos", *cam_eye)
                    self._u1i(self.p_quad, "u_mode", 1 if pass_order == "DEFERRED_DECAL" else 0)
                    g.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)

        # 1d. Rigid Mesh VFX (wh_vfx_mesh_vertex_anim) sorted by (sort_override ascending, world Emitter Pivot distance back-to-front = real WH3 order!)
        # Note: RMV2 outward faces satisfy dot(cross(p1-p0, p2-p0), N) >= 0 in right-handed Y-up space,
        # which projects to CCW in OpenGL (+Y up) — equivalent to DX11 CW (+Y down).
        g.glEnable(GL_DEPTH_TEST)
        g.glEnable(GL_CULL_FACE)
        g.glFrontFace(GL_CCW)
        g.glCullFace(GL_BACK)
        g.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        g.glUseProgram(self.p_mesh)
        self._um4(self.p_mesh, "u_vp", VP)
        self._um4(self.p_mesh, "u_invvp",np.linalg.inv(VP))
        self._u3f(self.p_mesh, "u_cam_pos", *cam_eye)
        self._u2f(self.p_mesh, "u_inv_res", 1.0 / self.w, 1.0 / self.h)
        self._u1f(self.p_mesh, "u_near", near)
        self._u1f(self.p_mesh, "u_far", far)
        self._u1f(self.p_mesh, "u_time", t_sec)
        for ti, uname in enumerate(("u_tex_a", "u_tex_b", "u_tex_c", "u_tex_mask", "u_tex_overlay", "u_scene_depth", "u_tex_disp")):
            self._u1i(self.p_mesh, uname, ti)
        g.glActiveTexture(GL_TEXTURE0 + 5)
        g.glBindTexture(GL_TEXTURE_2D, self.t_depth_ro)

        # Gather all active rigid draw instances and sort by WH3 exact order:
        # (material sort_override ascending, world-space Emitter Pivot distance to camera descending)
        rigid_draws = []
        for draw_idx, em in enumerate(emitters):
            if em.etype != "continuous_rigid" or em.wsmodel not in ws_cache:
                continue
            entry = ws_cache[em.wsmodel]
            if entry.get("native_kind"): continue
            sort_ord = int(entry["mat"].get("sort_override", 255))
            M_wrap = mat4_trs(em.wrapper_pos, em.wrapper_rot_deg, np.array([em.wrapper_scale] * 3, dtype=np.float32))
            for p in em.simulate_particles(t_sec):
                if p["alpha"] <= 1e-4 or np.max(np.abs(p["scale"])) <= 1e-4:
                    continue
                M_local = mat4_trs(p["pos"], p["rot_deg"], p["scale"])
                if "parent_rot_deg" in p:
                    # WH3 LOCAL_SPACE_POSITION_ROTATION applies EmitterOrientation on the carrier frame (lever-arm coupling when pos != 0!)
                    R_parent = mat4_trs(np.zeros(3, dtype=np.float32), p["parent_rot_deg"], np.ones(3, dtype=np.float32))
                    M_local = R_parent @ M_local
                if em.root_track is not None: M_local = M_local @ native_root_track.matrix(em.root_track,p["age"]*em.anim_speed)
                M_world = M_wrap @ M_local
                piv_local = p.get("base_pos", p["pos"])
                piv_world = (M_wrap @ np.array([piv_local[0], piv_local[1], piv_local[2], 1.0], dtype=np.float32))[:3]
                dist_cam = float(np.linalg.norm(piv_world - cam_eye))
                rigid_draws.append((sort_ord, -round(dist_cam, 3), draw_idx, em, entry, p, M_world))

        rigid_draws.sort(key=lambda item: (item[0], item[1], item[2]))
        last_ws = None
        for _, _, _, em, entry, p, M_world in rigid_draws:
            gmesh = entry["gpu_mesh"]
            if em.wsmodel != last_ws:
                last_ws = em.wsmodel
                mat = entry["mat"]
                pm = mat["params"]
                for key,un in (("Texture_A_channels","u_cha"),("Texture_B_Channels","u_chb"),("Texture_C_Channels","u_chc")):
                    self.gl.glUniform4f(self.gl.glGetUniformLocation(self.p_mesh,un.encode()),*pm.get(key,[1.,0.,0.,0.]))
                self._u1f(self.p_mesh,"u_vertex_colour",float(pm.get("vertex_colour",1)))
                self._u1f(self.p_mesh,"u_mesh_alpha",float(pm.get("vfx_mesh_alpha",1)))

                # Bind 5 material texture slots + optional vertex displacement texture
                for si, slot_name in enumerate(("t_xml_Texture_A", "t_xml_Texture_B", "t_xml_Texture_C", "t_xml_global_mask", "t_xml_colour_overlay")):
                    g.glActiveTexture(GL_TEXTURE0 + si)
                    g.glBindTexture(GL_TEXTURE_2D, entry["tex_ids"][slot_name])
                g.glActiveTexture(GL_TEXTURE0 + 6)
                g.glBindTexture(GL_TEXTURE_2D, entry["tex_ids"].get("t_xml_vertex_disp_texture", entry["tex_ids"]["t_xml_Texture_A"]))

                def _v2(k, d):
                    v = pm.get(k, d)
                    return (float(v[0]), float(v[1])) if isinstance(v, list) and len(v) >= 2 else d
                def _v3(k, d):
                    v = pm.get(k, d)
                    return (float(v[0]), float(v[1]), float(v[2])) if isinstance(v, list) and len(v) >= 3 else d

                self._u2f(self.p_mesh, "u_scroll_a", *_v2("scrolling_colour_scroll", (0.0, 0.0)))
                self._u2f(self.p_mesh, "u_tile_a", *_v2("scrolling_colour_tiling", (1.0, 1.0)))
                self._u2f(self.p_mesh, "u_scroll_b", *_v2("scrolling_colour_scroll2", (0.0, 0.0)))
                self._u2f(self.p_mesh, "u_tile_b", *_v2("scrolling_colour_tiling2", (1.0, 1.0)))
                self._u2f(self.p_mesh, "u_scroll_c", *_v2("scrolling_colour_scroll3", (0.0, 0.0)))
                self._u2f(self.p_mesh, "u_tile_c", *_v2("scrolling_colour_tiling3", (1.0, 1.0)))
                self._u2f(self.p_mesh, "u_mask_pan", *_v2("global_mask_pan", (0.0, 0.0)))
                self._u2f(self.p_mesh, "u_mask_tile", *_v2("global_mask_tiling", (1.0, 1.0)))
                self._u2f(self.p_mesh, "u_ov_pan", *_v2("colour_overlay_pan", (0.0, 0.0)))
                self._u2f(self.p_mesh, "u_ov_tile", *_v2("colour_overlay_tile", (1.0, 1.0)))
                self._u1f(self.p_mesh, "u_ov_strength", float(pm.get("colour_overlay_strength", 1.0)))
                self._u1f(self.p_mesh, "u_has_overlay", 1.0 if entry["has_overlay"] else 0.0)
                self._u3f(self.p_mesh, "u_glow_colour", *_v3("glow_colour", (1.0, 1.0, 1.0)))
                self._u1f(self.p_mesh, "u_glow_strength", float(pm.get("glow_strength", 1.0)))
                self._u1f(self.p_mesh, "u_total_alpha", float(pm.get("total_alpha_strength", 1.0)))
                self._u3f(
                    self.p_mesh, "u_scroll_bright",
                    float(pm.get("scrolling_brightness", 1.0)),
                    float(pm.get("scrolling_brightness2", 1.0)),
                    float(pm.get("scrolling_brightness3", 1.0)),
                )
                self._u1f(self.p_mesh, "u_nfio", float(pm.get("normal_fade_in_out", 0.5)))
                self._u1f(self.p_mesh, "u_npow", float(pm.get("normal_fade_power", 1.0)))
                self._u1f(self.p_mesh, "u_df_switch", float(pm.get("depth_fade_switch", 0.0)))
                self._u1f(self.p_mesh, "u_pdf", float(pm.get("pixel_depth_fade", 1.0)))
                self._u1f(self.p_mesh, "u_alpha_cutoff", float(pm.get("alpha_cutoff", 0.0)))
                self._u1f(self.p_mesh, "u_vdisp_enable", float(pm.get("vertex_displacement", 0.0)))
                self._u1f(self.p_mesh, "u_vdisp_scale", float(pm.get("vertex_disp_scale", 0.0)))
                self._u2f(self.p_mesh, "u_vdisp_tile", *_v2("vertex_disp_tiling", (2.0, 4.0)))
                self._u2f(self.p_mesh, "u_vdisp_scroll", *_v2("vertex_disp_scroll", (0.0, 1.5)))
                self._u1f(self.p_mesh, "u_osc_scale", float(pm.get("vertex_oscilation_scale", 0.0)))
                self._u3f(
                    self.p_mesh, "u_osc_amp",
                    float(pm.get("vertex_x_amp", 0.0)),
                    float(pm.get("vertex_y_amp", 0.0)),
                    float(pm.get("vertex_z_amp", 0.0)),
                )
                self._u3f(
                    self.p_mesh, "u_osc_freq",
                    float(pm.get("vertex_x_freq", 1.0)),
                    float(pm.get("vertex_y_freq", 1.0)),
                    float(pm.get("vertex_z_freq", 1.0)),
                )
                self._u3f(self.p_mesh, "u_pivot", *gmesh["pivot"])
                g.glBindVertexArray(gmesh["vao"])

            self._u1f(self.p_mesh,"u_anim_time",p["age"]+(t_sec-p["age"])*float(pm.get("time_source",0)))
            self._u1f(self.p_mesh,"u_vertex_anim",float(pm.get("vertex_anim",0)))
            self._u1f(self.p_mesh,"u_anim_speed",float(pm.get("vertex_anim_speed",0)))
            self._um4(self.p_mesh, "u_model", M_world)
            self._u3f(self.p_mesh, "u_vfx_rgb", *p["rgb"])
            self._u1f(self.p_mesh, "u_vfx_alpha", p["alpha"])
            g.glDrawElements(GL_TRIANGLES, gmesh["icount"], GL_UNSIGNED_SHORT, ctypes.c_void_p(0))

        # 1e. Billboards & Velocity-Stretched Particles (FULLRES_DEFERRED_ALPHA / HALF_RES_DEFERRED_ALPHA)
        g.glDisable(GL_CULL_FACE)
        g.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        g.glUseProgram(self.p_quad)
        self._um4(self.p_quad, "u_vp", VP)
        self._u1i(self.p_quad, "u_tex", 0)
        self._u3f(self.p_quad, "u_cam_pos", *cam_eye)
        self._u2f(self.p_quad,"u_nf",near,far);self._u1i(self.p_quad,"u_depth_copy",5)
        g.glActiveTexture(GL_TEXTURE0+5);g.glBindTexture(GL_TEXTURE_2D,self.t_depth_ro);g.glActiveTexture(GL_TEXTURE0)
        g.glBindVertexArray(self.vao_quad)

        for em in emitters:
            if em.render_pass not in ("FULLRES_DEFERRED_ALPHA", "HALF_RES_DEFERRED_ALPHA"):
                continue
            if not em.diffuse_dds:
                continue
            tex_id = self.upload_texture(em.diffuse_dds, resolver.decode_dds_rgba_u8(em.diffuse_dds), tileable=False)
            g.glActiveTexture(GL_TEXTURE0)
            g.glBindTexture(GL_TEXTURE_2D, tex_id)
            self._u1f(self.p_quad, "u_soft_factor", em.soft_factor)
            self._u2f(self.p_quad, "u_fb_grid", float(em.fb_cols), float(em.fb_rows))
            self._u1i(self.p_quad, "u_mode", 0 if em.restriction == 2 else 2)
            M_wrap = mat4_trs(em.wrapper_pos, em.wrapper_rot_deg, np.array([em.wrapper_scale] * 3, dtype=np.float32))
            for p in em.simulate_particles(t_sec, resolver=resolver):
                sx = float(p["scale"][0]) * em.wrapper_scale
                sy = float(p["scale"][1]) * em.wrapper_scale
                if sx <= 1e-4 or p["alpha"] <= 1e-4:
                    continue
                p_world = (M_wrap @ np.array([p["pos"][0], p["pos"][1], p["pos"][2], 1.0], dtype=np.float32))[:3]
                v_world = M_wrap[:3, :3] @ p["vel"]
                if em.restriction == 2:
                    # Horizontal ground-aligned ring/quad
                    ax = np.array([sx, 0.0, 0.0], dtype=np.float32)
                    ay = np.array([0.0, 0.0, sy], dtype=np.float32)
                elif em.velocity_stretch_mul > 1e-4 and np.linalg.norm(v_world) > 0.2:
                    # Velocity-stretched camera-aligned billboard (exact stretch cap enforcement!)
                    vdir = v_world / np.linalg.norm(v_world)
                    spd = float(np.linalg.norm(v_world))
                    stretch = min(max(1.0, spd * em.velocity_stretch_mul), max(1.0, em.velocity_stretch_cap))
                    to_cam = cam_eye - p_world
                    side = np.cross(vdir, to_cam)
                    sn = np.linalg.norm(side)
                    side = side / sn if sn > 1e-5 else cam_right
                    ax = side * sx
                    ay = vdir * (sy * stretch)
                else:
                    ax = cam_right * sx
                    ay = cam_up * sy
                self._u1f(self.p_quad,"u_soft_factor",em.soft_factor*(1+em.soft_scale_strength*(min(sx,sy)-1)) if em.has_soft_edge else -1.)
                self._u3f(self.p_quad, "u_center", *p_world)
                self._u3f(self.p_quad, "u_axis_x", *ax)
                self._u3f(self.p_quad, "u_axis_y", *ay)
                self._u3f(self.p_quad, "u_rgb", *p["rgb"])
                self._u1f(self.p_quad, "u_alpha", p["alpha"])
                self._u1f(self.p_quad, "u_premul", 1.0 if em.pre_multiplied_alpha else 0.0)
                self._u1f(self.p_quad, "u_emissive", em.emissive_gain if em.has_ps_lighting else 1.0)
                self._u1f(self.p_quad, "u_add_mul", em.additive_blend_mul if em.has_ps_lighting else 0.0)
                cells=em.fb_cols*(1 if em.fb_row is not None or em.fb_random_row else em.fb_rows)
                row=int(p["random_u"]*em.fb_rows)%em.fb_rows if em.fb_random_row else (em.fb_row or 0)
                u=p["life_u"];rate=em.fb_loops[0]+u*(em.fb_loops[1]-em.fb_loops[0])
                anim=(p["age"]*rate if em.fb_mode=="fixed_animation_speed" else (p["particle_id"]%cells)/cells if em.fb_mode=="trail_distribution" else u*rate)
                anim+=p["random_u"] if em.fb_random else 0
                frame=min((anim%1 if em.fb_loop else min(1.,max(0.,anim)))*cells,cells-1 if not em.fb_loop else cells)
                if em.fb_mode=="static":frame=(em.fb_base+int(p["random_u"]*cells)*int(em.fb_random))%cells
                self._u1f(self.p_quad,"u_fb_frame",row*em.fb_cols+frame)
                self._u1f(self.p_quad,"u_fb_blend",float(em.fb_blend))
                self._u1f(self.p_quad,"u_fb_next",row*em.fb_cols+((int(frame)+1)%cells if em.fb_loop else min(int(frame)+1,cells-1)))
                g.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)

        # Read back HDR + ParticleMask for quantitative telemetry!
        hdr_buf = np.empty((self.h, self.w, 4), dtype=np.float32)
        g.glReadPixels(0, 0, self.w, self.h, GL_RGBA, GL_FLOAT, hdr_buf.ctypes.data_as(ctypes.c_void_p))

        # 1f. WH3 DISTORTION Pass -> screen-space tangent XY accumulation buffer (depth-tested!)
        g.glBindFramebuffer(GL_FRAMEBUFFER, self.fbo_distort)
        g.glViewport(0, 0, self.w, self.h)
        g.glDepthMask(GL_FALSE)
        g.glClearColor(0.0, 0.0, 0.0, 0.0)
        g.glClear(GL_COLOR_BUFFER_BIT)
        g.glEnable(GL_DEPTH_TEST)
        g.glDisable(GL_CULL_FACE)
        g.glEnable(GL_BLEND)
        g.glBlendFunc(GL_ONE, GL_ONE)
        g.glUseProgram(self.p_distort)
        self._um4(self.p_distort, "u_vp", VP)
        self._u1i(self.p_distort, "u_tex", 0)
        self._u1f(self.p_distort,"u_near",near);self._u1f(self.p_distort,"u_far",far)
        self._u1i(self.p_distort,"u_depth",1)
        g.glActiveTexture(GL_TEXTURE0+1);g.glBindTexture(GL_TEXTURE_2D,self.t_depth_ro)
        g.glBindVertexArray(self.vao_quad)
        for em in emitters:
            if em.render_pass != "DISTORTION" or (em.wsmodel and ws_cache.get(em.wsmodel,{}).get("native_kind")=="lens"):
                continue
            dtex_path = em.normal_dds or em.diffuse_dds
            if not dtex_path:
                continue
            g.glActiveTexture(GL_TEXTURE0) # Preserve the read-only scene depth on unit 1 on first upload.
            tex_id = self.upload_texture(dtex_path + "|LIN", resolver.decode_dds_rgba_u8(dtex_path, linear=True), tileable=False)
            g.glActiveTexture(GL_TEXTURE0)
            g.glBindTexture(GL_TEXTURE_2D, tex_id)
            M_wrap = mat4_trs(em.wrapper_pos, em.wrapper_rot_deg, np.array([em.wrapper_scale] * 3, dtype=np.float32))
            for p in em.simulate_particles(t_sec):
                sx = float(p["scale"][0]) * em.wrapper_scale
                sy = float(p["scale"][1]) * em.wrapper_scale
                if sx <= 1e-4 or p["alpha"] <= 1e-4:
                    continue
                p_world = (M_wrap @ np.array([p["pos"][0], p["pos"][1], p["pos"][2], 1.0], dtype=np.float32))[:3]
                if em.restriction == 2:
                    ax = np.array([sx, 0.0, 0.0], dtype=np.float32)
                    ay = np.array([0.0, 0.0, sy], dtype=np.float32)
                else:
                    ax = cam_right * sx
                    ay = cam_up * sy
                self._u3f(self.p_distort, "u_center", *p_world)
                self._u3f(self.p_distort, "u_axis_x", *ax)
                self._u3f(self.p_distort, "u_axis_y", *ay)
                self._u1f(self.p_distort, "u_alpha", p["alpha"])
                soft=em.soft_factor*(1+em.soft_scale_strength*(min(sx,sy)-1)) if em.has_soft_edge else 0.
                self._u1f(self.p_distort,"u_soft",soft)
                g.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)

        if native_draws["lens"]:
            if not hasattr(self,"p_native_lens"):
                self.p_native_lens=self._compile_prog(native_rigid.NATIVE_VS,native_rigid.LENS_FS)
            prog=self.p_native_lens;g.glUseProgram(prog)
            self._um4(prog,"u_vp",VP);self._um4(prog,"u_invvp",np.linalg.inv(VP))
            self._u3f(prog,"u_eye",*cam_eye);self._u2f(prog,"u_invres",1/self.w,1/self.h);self._u1f(prog,"u_time",t_sec)
            for entry,model,p in native_draws["lens"]:
                pm=entry["mat"]["params"]; self._um4(prog,"u_model",model)
                for j,(un,tex) in enumerate([("u_depth",self.t_depth_ro),("u_mask",entry["tex_ids"][native_rigid.MASK]),("u_noise",entry["tex_ids"]["t_xml_panning_texture"])]):
                    g.glActiveTexture(GL_TEXTURE0+j);g.glBindTexture(GL_TEXTURE_2D,tex);self._u1i(prog,un,j)
                self._u2f(prog,"u_pan",*pm["panning_tex_direction"]);self._u2f(prog,"u_tile",*pm["panning_texture_scale"])
                self._u1f(prog,"u_fade",pm["depth_fade"]);self._u1f(prog,"u_strength",pm["distortion_tex_strength"])
                self._u1f(prog,"u_opacity",p["alpha"]);gm=entry["gpu_mesh"]
                g.glBindVertexArray(gm["vao"]);g.glDrawElements(GL_TRIANGLES,gm["icount"],GL_UNSIGNED_SHORT,ctypes.c_void_p(0))

        # ==============================================================
        # STAGE 2: Real WH3 Disassembled Bloom + Tone-Mapping + Sharpen
        # ==============================================================
        g.glDisable(GL_DEPTH_TEST)
        g.glDisable(GL_CULL_FACE)
        g.glDisable(GL_BLEND)
        g.glBindVertexArray(self.vao_empty)

        # 2A. Exact fx_hdr_tone_mapper_hlsl_9b626129 Bloom Bright-Pass -> Level 0
        bw0, bh0, ta0, fa0, tb0, fb0 = self.bloom_levels[0]
        g.glBindFramebuffer(GL_FRAMEBUFFER, fa0)
        g.glViewport(0, 0, bw0, bh0)
        g.glUseProgram(self.p_bloom_ex)
        g.glActiveTexture(GL_TEXTURE0); g.glBindTexture(GL_TEXTURE_2D, self.t_hdr)
        g.glActiveTexture(GL_TEXTURE0 + 1); g.glBindTexture(GL_TEXTURE_2D, self.t_pmask)
        self._u1i(self.p_bloom_ex, "u_hdr", 0)
        self._u1i(self.p_bloom_ex, "u_mask", 1)
        self._u1f(self.p_bloom_ex, "u_bloom_threshold", self.env["bloom_threshold"])
        self._u1f(self.p_bloom_ex, "u_bloom_strength", self.env["bloom_strength"])
        self._u1f(self.p_bloom_ex, "u_bloom_particle_strength", self.env["bloom_particle_strength"])
        g.glDrawArrays(GL_TRIANGLES, 0, 3)

        # 2B. 5-Level Separable Gaussian Mip Blur Pyramid
        g.glUseProgram(self.p_blur)
        self._u1i(self.p_blur, "u_src", 0)
        prev_tex = ta0
        for li, (bw, bh, ta, fa, tb, fb) in enumerate(self.bloom_levels):
            if li > 0:
                # Downsample from previous level into ta
                g.glBindFramebuffer(GL_FRAMEBUFFER, fa)
                g.glViewport(0, 0, bw, bh)
                g.glActiveTexture(GL_TEXTURE0); g.glBindTexture(GL_TEXTURE_2D, prev_tex)
                self._u2f(self.p_blur, "u_dir", 0.5 / bw, 0.0)
                g.glDrawArrays(GL_TRIANGLES, 0, 3)
            # Horizontal blur ta -> tb
            g.glBindFramebuffer(GL_FRAMEBUFFER, fb)
            g.glViewport(0, 0, bw, bh)
            g.glActiveTexture(GL_TEXTURE0); g.glBindTexture(GL_TEXTURE_2D, ta)
            self._u2f(self.p_blur, "u_dir", 1.0 / bw, 0.0)
            g.glDrawArrays(GL_TRIANGLES, 0, 3)
            # Vertical blur tb -> ta
            g.glBindFramebuffer(GL_FRAMEBUFFER, fa)
            g.glViewport(0, 0, bw, bh)
            g.glActiveTexture(GL_TEXTURE0); g.glBindTexture(GL_TEXTURE_2D, tb)
            self._u2f(self.p_blur, "u_dir", 0.0, 1.0 / bh)
            g.glDrawArrays(GL_TRIANGLES, 0, 3)
            prev_tex = ta

        # 2C. Exact fx_hdr_to_screen_hlsl_f9042970 Distortion Composite + Luminance-Scaled Extended Reinhard + Gamma
        g.glBindFramebuffer(GL_FRAMEBUFFER, self.fbo_tm)
        g.glViewport(0, 0, self.w, self.h)
        g.glUseProgram(self.p_tonemap)
        g.glActiveTexture(GL_TEXTURE0); g.glBindTexture(GL_TEXTURE_2D, self.t_hdr)
        self._u1i(self.p_tonemap, "u_hdr", 0)
        for li, (_, _, ta, _, _, _) in enumerate(self.bloom_levels):
            g.glActiveTexture(GL_TEXTURE0 + 1 + li)
            g.glBindTexture(GL_TEXTURE_2D, ta)
            self._u1i(self.p_tonemap, f"u_b{li}", 1 + li)
        g.glActiveTexture(GL_TEXTURE0 + 6); g.glBindTexture(GL_TEXTURE_2D, self.t_distort)
        self._u1i(self.p_tonemap, "u_distort", 6)
        self._u1f(self.p_tonemap, "u_tm_brightness", self.env["tm_brightness"])
        self._u1f(self.p_tonemap, "u_tm_burn", self.env["tm_burn"])
        self._u1f(self.p_tonemap, "u_g_brightness", self.env["g_brightness"])
        self._u1f(self.p_tonemap, "u_inv_gamma", self.env["inv_gamma"])
        self._u1i(self.p_tonemap, "u_view_mode", view_mode)
        g.glDrawArrays(GL_TRIANGLES, 0, 3)

        # 2D. Exact fx_postprocessing_hlsl 5-Tap Sharpen
        g.glBindFramebuffer(GL_FRAMEBUFFER, self.fbo_out)
        g.glViewport(0, 0, self.w, self.h)
        g.glUseProgram(self.p_sharpen)
        g.glActiveTexture(GL_TEXTURE0); g.glBindTexture(GL_TEXTURE_2D, self.t_tm)
        self._u1i(self.p_sharpen, "u_src", 0)
        self._u2f(self.p_sharpen, "u_inv_res", 1.0 / self.w, 1.0 / self.h)
        self._u1f(self.p_sharpen, "u_strength", self.env["sharpen"] if view_mode == 0 else 0.0)
        g.glDrawArrays(GL_TRIANGLES, 0, 3)

        out_u8 = np.empty((self.h, self.w, 4), dtype=np.uint8)
        g.glReadPixels(0, 0, self.w, self.h, GL_RGBA, GL_UNSIGNED_BYTE, out_u8.ctypes.data_as(ctypes.c_void_p))
        img_rgb = np.flipud(out_u8[:, :, :3]).copy()

        # Telemetry metrics
        hdr_rgb = np.flipud(hdr_buf[:, :, :3])
        lum = hdr_rgb[:, :, 0] * 0.2126 + hdr_rgb[:, :, 1] * 0.7152 + hdr_rgb[:, :, 2] * 0.0722
        scr_max = np.max(img_rgb, axis=-1)
        scr_min = np.min(img_rgb, axis=-1)
        whiteout_pct = float(np.mean(scr_min >= 250) * 100.0)
        bloom_active_pct = float(np.mean(lum > self.env["bloom_threshold"]) * 100.0)
        telemetry = {
            "t_sec": round(t_sec, 3),
            "hdr_luma_p99": round(float(np.percentile(lum, 99)), 3),
            "hdr_luma_max": round(float(np.max(lum)), 3),
            "bloom_active_pct": round(bloom_active_pct, 2),
            "screen_whiteout_pct": round(whiteout_pct, 2),
            "screen_mean_luma": round(float(np.mean(img_rgb)), 1),
        }
        return img_rgb, telemetry


def preload_wsmodels(renderer: WH3TrueRenderer, resolver: PackAssetResolver, emitters: list[ParsedEmitter]) -> dict:
    ws_cache = {}; root_tracks={}
    for em in emitters:
        if em.anim_path:
            if em.anim_path not in root_tracks:
                raw=resolver.read(em.anim_path)
                root_tracks[em.anim_path]=(native_root_track.decode(raw) if native_root_track.matches_profile(raw) else None)
            em.root_track=root_tracks[em.anim_path]
        if not em.wsmodel or em.wsmodel in ws_cache:
            continue
        wraw = resolver.read(em.wsmodel)
        if wraw is None:
            resolver.omissions.add("Missing model: "+em.wsmodel)
            continue
        try:
            wroot = ET.fromstring(wraw)
            geo_p = norm_pack_path((wroot.findtext(".//geometry") or "").strip())
            mat_p = norm_pack_path((wroot.findtext(".//material") or "").strip())
            graw = resolver.read(geo_p)
            mraw = resolver.read(mat_p)
            if graw is None or mraw is None:
                resolver.omissions.add("Missing geometry/material: "+em.wsmodel)
                continue
            parsed_mesh = parse_rmv2_v7(graw)
            gpu_mesh = renderer.upload_rmv2(geo_p, parsed_mesh)
            mat = parse_material_xml(mraw)
        except (ValueError, ET.ParseError, struct.error) as exc:
            resolver.omissions.add(f"{em.wsmodel}: {exc}")
            continue
        tex_ids = {}
        kind = native_rigid.kind(mat)
        if kind == "lens":
            for slot in (native_rigid.MASK, "t_xml_panning_texture"):
                tp=mat["textures"][slot]
                if resolver.read(tp) is None: raise ValueError("Missing native lens texture: "+tp)
                tex_ids[slot]=renderer.upload_texture(tp+"|nativeLinear",resolver.decode_dds_rgba_u8(tp,linear=True),tileable=slot!=native_rigid.MASK)
        if mat["shader"]=="shaders/wh3_rigid_baked_fluid_simulation.xml.shader":
            from vat_native import diagnose
            resolver.vat_diagnostics[em.wsmodel]=diagnose(mat,parsed_mesh,resolver,resolver.vat_geometry,em.wsmodel)
        if not kind and mat["shader"] != "shaders/wh_vfx_mesh_vertex_anim.xml.shader":
            resolver.omissions.add("Unsupported material: "+mat["shader"]+" @ "+em.wsmodel)
            continue
        has_overlay = False
        for slot in ("t_xml_Texture_A", "t_xml_Texture_B", "t_xml_Texture_C", "t_xml_global_mask", "t_xml_colour_overlay", "t_xml_vertex_disp_texture"):
            tp = mat["textures"].get(slot, "")
            if slot == "t_xml_colour_overlay" and tp and "default_white" not in tp.lower():
                has_overlay = True
            tex_ids[slot] = renderer.upload_texture(tp or "default_white", resolver.decode_dds_rgba_u8(tp), tileable=True)
        ws_cache[em.wsmodel] = {
            "gpu_mesh": gpu_mesh,
            "mat": mat,
            "tex_ids": tex_ids,
            "has_overlay": has_overlay, "native_kind": kind,
        }
    return ws_cache


def annotate_panel(img_rgb: np.ndarray, label: str) -> Image.Image:
    im = Image.fromarray(img_rgb, mode="RGB")
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, 0, im.width, 26], fill=(14, 16, 22))
    draw.text((8, 6), label, fill=(240, 232, 210))
    return im


def compute_camera_pair(cam_mode: str, emitters: list[ParsedEmitter], ws_cache: dict, times: list[float]):
    """Compute (cam_tactical, cam_ground) either from explicit preset or auto-framed from emitter bounds."""
    if cam_mode == "beam":
        return (
            (np.array([0.0, 42.0, 88.0], dtype=np.float32), np.array([0.0, 14.0, 0.0], dtype=np.float32), 45.0),
            (np.array([0.0, 19.0, 34.0], dtype=np.float32), np.array([0.0, 2.5, 0.0], dtype=np.float32), 45.0),
        )
    if cam_mode == "ground":
        return (
            (np.array([0.0, 28.0, 52.0], dtype=np.float32), np.array([0.0, 5.0, 0.0], dtype=np.float32), 45.0),
            (np.array([0.0, 12.0, 22.0], dtype=np.float32), np.array([0.0, 2.0, 0.0], dtype=np.float32), 45.0),
        )
    if cam_mode == "close":
        return (
            (np.array([0.0, 12.0, 24.0], dtype=np.float32), np.array([0.0, 3.0, 0.0], dtype=np.float32), 45.0),
            (np.array([0.0, 6.0, 12.0], dtype=np.float32), np.array([0.0, 1.5, 0.0], dtype=np.float32), 45.0),
        )
    # cam_mode == "auto": measure actual world-space spatial envelope (honoring wrapper transforms + RMV2 bounds!)
    pts_w = []
    for t in times:
        for em in emitters:
            M_wrap = mat4_trs(em.wrapper_pos, em.wrapper_rot_deg, np.array([em.wrapper_scale] * 3, dtype=np.float32))
            for p in em.simulate_particles(t):
                if p["alpha"] <= 1e-3:
                    continue
                if em.etype == "continuous_rigid" and em.wsmodel in ws_cache:
                    gm = ws_cache[em.wsmodel]["gpu_mesh"]
                    M_loc = mat4_trs(p["pos"], p["rot_deg"], p["scale"])
                    if "parent_rot_deg" in p:
                        M_loc = mat4_trs(np.zeros(3, dtype=np.float32), p["parent_rot_deg"], np.ones(3, dtype=np.float32)) @ M_loc
                    if em.root_track is not None: M_loc = M_loc @ native_root_track.matrix(em.root_track,p["age"]*em.anim_speed)
                    M_w = M_wrap @ M_loc
                    b0, b1 = gm["bb_min"], gm["bb_max"]
                    for cx in (b0[0], b1[0]):
                        for cy in (b0[1], b1[1]):
                            for cz in (b0[2], b1[2]):
                                pts_w.append((M_w @ np.array([cx, cy, cz, 1.0], dtype=np.float32))[:3])
                elif em.render_pass in ("DEFERRED_DECAL", "DEFERRED_DECAL_EMISSIVE", "FULLRES_DEFERRED_ALPHA"):
                    pw = (M_wrap @ np.array([p["pos"][0], p["pos"][1], p["pos"][2], 1.0], dtype=np.float32))[:3]
                    r_e = max(abs(float(p["scale"][0])), abs(float(p["scale"][1]))) * em.wrapper_scale * 0.6
                    pts_w.append(pw + np.array([r_e, r_e, r_e], dtype=np.float32))
                    pts_w.append(pw - np.array([r_e, 0.0, r_e], dtype=np.float32))
    if pts_w:
        arr = np.asarray(pts_w, dtype=np.float32)
        c_min = np.percentile(arr, 2, axis=0)
        c_max = np.percentile(arr, 98, axis=0)
        ctr = 0.5 * (c_min + c_max)
        span = np.maximum(c_max - c_min, np.array([6.0, 4.0, 6.0], dtype=np.float32))
        rad = float(max(span[0], span[1], span[2]) * 0.65)
        rad = min(max(rad, 6.0), 160.0)
        tac_dist = max(rad * 2.15, 22.0)
        tac_eye = ctr + np.array([0.0, tac_dist * 0.55, tac_dist * 0.95], dtype=np.float32)
        gnd_dist = max(rad * 1.25, 13.0)
        gnd_eye = ctr + np.array([0.0, gnd_dist * 0.45, gnd_dist * 0.92], dtype=np.float32)
        return (
            (tac_eye, ctr.astype(np.float32), 45.0),
            (gnd_eye, ctr.astype(np.float32), 45.0),
        )
    return (
        (np.array([0.0, 14.0, 26.0], dtype=np.float32), np.array([0.0, 4.0, 0.0], dtype=np.float32), 45.0),
        (np.array([0.0, 8.0, 15.0], dtype=np.float32), np.array([0.0, 2.0, 0.0], dtype=np.float32), 45.0),
    )


def main():
    ap = argparse.ArgumentParser(description="WH3 L2 pack-driven GPU preview")
    ap.add_argument("--pack", required=True, help="Compiled PFH5 .pack path")
    ap.add_argument("--wrappers", required=True, help="Comma-separated wrapper VFX keys (without vfx/ or .xml)")
    ap.add_argument("--out-sheet", required=True, help="Output multi-phase multi-angle QA PNG sheet path")
    ap.add_argument("--diagnostic-strip", help="Optional output PNG showing Stage1(Raw) vs Bloom vs Stage2(WH3 Final)")
    ap.add_argument("--video", help="Optional output MP4 preview path")
    ap.add_argument("--telemetry-out", help="Optional output JSON telemetry path")
    ap.add_argument("--env", choices=ENV_NAMES, default="daylight", help="Vanilla WH3 environment preset")
    ap.add_argument("--cam-mode", choices=["auto", "beam", "ground", "close"], default="auto", help="Camera framing preset (default: auto)")
    ap.add_argument("--supersample", type=int, choices=[1, 2], default=1, help="Internal SSAA factor (2 = razor-clean TAA-like anti-aliasing)")
    ap.add_argument("--times", default="0.6,2.2,4.0,5.8,7.8,9.8", help="Comma-separated phase times in seconds")
    ap.add_argument("--diag-time", type=float, default=4.0, help="Timestamp for Stage1/Bloom/Stage2 diagnostic strip")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=360)
    ap.add_argument("--sample-spawn-box", action="store_true", help="Use the compiled birth volume for space-aware wrappers")
    ap.add_argument("--framing-m", nargs=3, type=float, metavar=("BOTTOM","TOP","FOOT_RADIUS"), help="Optional authored full-height and ground shot bounds in metres (auto only)")
    ap.add_argument("--video-duration", type=float, default=11.0)
    ap.add_argument("--video-fps", type=int, default=30)
    ap.add_argument("--slope-deg",type=float,default=0.,help="Diagnostic X slope in degrees (-35..35); not a CSC runtime simulation")
    args = ap.parse_args()
    if not -35<=args.slope_deg<=35: ap.error("--slope-deg range: -35..35")

    wkeys = [w.strip() for w in args.wrappers.split(",") if w.strip()]
    times = [float(x) for x in args.times.split(",") if x.strip()]

    print(f"[WH3-L2] Loading compiled pack directly: {args.pack}")
    resolver, emitters = load_vfx_scene_from_pack(args.pack, wkeys)
    for em in emitters:
        em.sample_spawn_box = args.sample_spawn_box
        if not em.wsmodel and em.render_pass not in {"FULLRES_DEFERRED_ALPHA","HALF_RES_DEFERRED_ALPHA","DEFERRED_DECAL","DEFERRED_DECAL_EMISSIVE","DEFERRED_DYNAMIC_LIGHT","DISTORTION"}:
            resolver.omissions.add(f"{em.id}: unhandled render pass {em.render_pass!r}")
    print(f"[WH3-L2] Loaded {len(emitters)} active emitters from wrappers={wkeys}")

    if not emitters: raise ValueError("No enabled emitters in requested wrappers")
    ss = int(args.supersample)
    renderer = WH3TrueRenderer(args.width * ss, args.height * ss, env_preset=args.env,slope_deg=args.slope_deg)
    ws_cache = preload_wsmodels(renderer, resolver, emitters)
    print(f"[WH3-L2] Uploaded {len(ws_cache)} RMV2+Material pipelines (particles upload DDS on first draw); {len(renderer.gpu_textures)} textures before rendering (env={args.env}, ss={ss}x, cam={args.cam_mode})")

    cam_tactical, cam_ground = compute_camera_pair(args.cam_mode, emitters, ws_cache, times)

    if args.cam_mode == "auto" and args.framing_m is not None:
        lo, hi, radius = args.framing_m
        if not lo < hi or radius <= 0:
            ap.error("framing-m needs BOTTOM < TOP and positive FOOT_RADIUS")
        target = np.array([0., (lo + hi) * 0.5, 0.], dtype=np.float32)
        # Shallow three-quarter elevation preserves the entire vertical drop in projection.
        dist = (hi - lo) * 0.57 / math.tan(math.radians(22.5))
        cam_tactical = (target + np.array([0., dist*.19, dist*.982]), target, 45.)
        cam_ground = (np.array([radius*.84, radius*.82, radius*1.58]), np.array([0., radius*.15, 0.]), 45.)

    def _render_aa(t_sec, cam, vm=0):
        img, tel = renderer.render_frame(t_sec, *cam, emitters, resolver, ws_cache, view_mode=vm)
        if ss > 1:
            img = np.asarray(Image.fromarray(img, mode="RGB").resize((args.width, args.height), Image.Resampling.LANCZOS), dtype=np.uint8)
        return img, tel

    os.makedirs(os.path.dirname(os.path.abspath(args.out_sheet)), exist_ok=True)
    ncols = len(times)
    sheet = Image.new("RGB", (args.width * ncols, args.height * 2))
    telem_records = []

    for ci, t in enumerate(times):
        img_tac, tel_tac = _render_aa(t, cam_tactical, 0)
        img_gnd, tel_gnd = _render_aa(t, cam_ground, 0)
        p1 = annotate_panel(img_tac, f"Tactical t={t:.2f}s | HDRmax={tel_tac['hdr_luma_max']} White={tel_tac['screen_whiteout_pct']}%")
        p2 = annotate_panel(img_gnd, f"Ground   t={t:.2f}s | HDRmax={tel_gnd['hdr_luma_max']} White={tel_gnd['screen_whiteout_pct']}%")
        sheet.paste(p1, (ci * args.width, 0))
        sheet.paste(p2, (ci * args.width, args.height))
        telem_records.append({"t_sec": t, "tactical": tel_tac, "ground": tel_gnd})

    sheet.save(args.out_sheet)
    if resolver.vat_geometry:
        from vat_native import save_wires
        save_wires(resolver.vat_geometry,os.path.splitext(args.out_sheet)[0]+"_vat_geometry.png")
    print(f"[WH3-L2] Saved 2-angle QA sheet -> {args.out_sheet}")

    # Optional Stage 1 vs Bloom vs Stage 2 diagnostic strip
    if args.diagnostic_strip:
        os.makedirs(os.path.dirname(os.path.abspath(args.diagnostic_strip)), exist_ok=True)
        strip = Image.new("RGB", (args.width * 3, args.height * 2))
        for ri, (cname, cam) in enumerate([("Tactical", cam_tactical), ("Ground", cam_ground)]):
            for ci, (vm, vlabel) in enumerate([
                (1, "Raw HDR  /  shader + blend"),
                (2, "Bloom  /  preview pyramid"),
                (0, f"Composite  /  distortion + tone map ({args.env})"),
            ]):
                img, tel = _render_aa(args.diag_time, cam, vm)
                pan = annotate_panel(img, f"{cname} t={args.diag_time:.2f}s | {vlabel}")
                strip.paste(pan, (ci * args.width, ri * args.height))
        strip.save(args.diagnostic_strip)
        print(f"[WH3-L2] Saved Step1/Bloom/Step2 diagnostic strip -> {args.diagnostic_strip}")

    # Optional MP4 video (side-by-side Tactical + Ground Close-Up)
    if args.video:
        os.makedirs(os.path.dirname(os.path.abspath(args.video)), exist_ok=True)
        nframes = int(round(args.video_duration * args.video_fps))
        vw, vh = args.width * 2, args.height
        cmd = [
            FFMPEG_PATH, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{vw}x{vh}", "-r", str(args.video_fps), "-i", "-",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", args.video,
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for fi in range(nframes):
            t = fi / float(args.video_fps)
            img1, _ = _render_aa(t, cam_tactical, 0)
            img2, _ = _render_aa(t, cam_ground, 0)
            frame = np.concatenate([img1, img2], axis=1)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        if proc.wait() != 0: raise RuntimeError(f"ffmpeg failed: {proc.returncode}")
        print(f"[WH3-L2] Rendered {nframes} frames ({args.video_fps}fps) -> {args.video}")

    if args.telemetry_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.telemetry_out)), exist_ok=True)
        summary = {
            "pack": os.path.abspath(args.pack),
            "wrappers": wkeys,
            "env_preset": args.env,
            "env_params": renderer.env, "test_slope_degrees":args.slope_deg,
            "emitter_count": len(emitters), "uploaded_textures":len(renderer.gpu_textures),
            "omissions": sorted(resolver.omissions), "vat_vertex_fetch":resolver.vat_diagnostics,
            "rmv2_pivots": {k: v["gpu_mesh"]["pivot"] for k, v in ws_cache.items()},
            "phases": telem_records,
        }
        with open(args.telemetry_out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"[WH3-L2] Wrote telemetry -> {args.telemetry_out}")


if __name__ == "__main__":
    main()
