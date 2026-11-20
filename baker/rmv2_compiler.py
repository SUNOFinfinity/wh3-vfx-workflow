import os
import struct
import numpy as np
from pfh5 import PFH5Pack

from baker_config import WH3_VFX_MODELS_PACK
DONOR_RMV2_ENTRY = r"vfx\models\cylinder_wide_top.rigid_model_v2"

from functools import lru_cache
DONORS = {"cylinder": DONOR_RMV2_ENTRY, "sphere": r"vfx\models\1m_sphere.rigid_model_v2", "plane": r"vfx\models\portal_plane.rigid_model_v2"}

@lru_cache(maxsize=3)
def _get_donor_template(profile="cylinder"):
    raw = PFH5Pack(WH3_VFX_MODELS_PACK).read(DONORS[profile])
    sh, _, vo, count, io, _ = struct.unpack_from("<6I", raw,168)
    if struct.unpack_from("<2I",raw,4)!=(7,1) or struct.unpack_from("<I",raw,152)[0]!=168 or (sh,vo,io-vo)!=(68,2248,count*32):
        raise ValueError("Installed donor layout changed: "+DONORS[profile])
    return raw


def _pack_bgra8_vec(vecs: np.ndarray) -> np.ndarray:
    """Pack unit vectors (N, 3) [x, y, z] into WH3 RMV2 DXGI_FORMAT_B8G8R8A8_UNORM (nz, ny, nx, 0).
    Verified directly against vanilla cylinder_wide_top.rigid_model_v2 and wh_vfx_mesh_vertex_anim VS.
    """
    nrm = vecs / np.maximum(1e-8, np.linalg.norm(vecs, axis=-1, keepdims=True))
    enc = np.clip(np.round((nrm * 0.5 + 0.5) * 255.0), 0, 255).astype(np.uint8)
    out = np.zeros((len(vecs), 4), dtype=np.uint8)
    out[:, 0] = enc[:, 2]  # B = Z
    out[:, 1] = enc[:, 1]  # G = Y
    out[:, 2] = enc[:, 0]  # R = X
    out[:, 3] = 0
    return out


def build_rmv2_v7(
    positions: np.ndarray,
    uvs: np.ndarray,
    normals: np.ndarray,
    tangents: np.ndarray,
    bitangents: np.ndarray,
    coverage_alpha: np.ndarray,
    faces: np.ndarray,
    group_name: str = "shb_mesh_LOD01",
    sqrt_compensate_alpha: bool = True,
    flip_winding: bool = False,
    vertex_colors_rgb: np.ndarray | None = None,
    donor_profile: str = "cylinder",
) -> bytes:
    """Compile geometry into a 100% WH3-native RMV2 v7 (32-byte static vertex + uint16 indices)
    by cloning a validated 2248-byte group header from the selected static donor.

    If sqrt_compensate_alpha=True, stores sqrt(coverage_alpha) in COLOR0.a so that
    wh_vfx_mesh_vertex_anim's |v13.w|^2 recovers exact linear coverage!
    """
    positions = np.asarray(positions, dtype=np.float32)
    uvs = np.asarray(uvs, dtype=np.float32)
    normals = np.asarray(normals, dtype=np.float32)
    tangents = np.asarray(tangents, dtype=np.float32)
    bitangents = np.asarray(bitangents, dtype=np.float32)
    coverage_alpha = np.asarray(coverage_alpha, dtype=np.float32)
    faces = np.asarray(faces, dtype=np.uint32)

    vcnt = len(positions)
    if vcnt == 0 or vcnt > 65535:
        raise ValueError(f"RMV2 uint16 vertex count out of range: {vcnt}")
    if not np.all(np.isfinite(positions)):
        raise ValueError("Non-finite vertex positions in RMV2 build")

    if np.abs(positions).max() > 65504 or not np.isfinite(uvs).all() or np.abs(uvs).max() > 65504:
        raise ValueError("Position/UV exceeds finite binary16 range")
    if faces.size == 0 or faces.max() >= vcnt: raise ValueError("Invalid uint16 triangle index")
    if flip_winding:
        faces = faces[:, ::-1]
    indices = np.ascontiguousarray(faces.reshape(-1), dtype=np.uint16)
    icnt = len(indices)

    # Build 32-byte structured vertex array
    vbuf = np.zeros((vcnt, 32), dtype=np.uint8)
    # 0..8: pos (4 * f16: x, y, z, 1.0)
    pos4 = np.ones((vcnt, 4), dtype=np.float16)
    pos4[:, :3] = positions.astype(np.float16)
    vbuf[:, 0:8] = pos4.view(np.uint8).reshape(vcnt, 8)
    # 8..12: uv0 (2 * f16)
    uv0 = uvs[:, :2].astype(np.float16)
    vbuf[:, 8:12] = uv0.view(np.uint8).reshape(vcnt, 4)
    # 12..16: uv1 (0, 0) already zero
    # 16..20: normal BGRA8
    vbuf[:, 16:20] = _pack_bgra8_vec(normals)
    # 20..24: tangent BGRA8
    vbuf[:, 20:24] = _pack_bgra8_vec(tangents)
    # 24..28: bitangent BGRA8
    vbuf[:, 24:28] = _pack_bgra8_vec(bitangents)
    # 28..32: COLOR0 BGRA8 (B, G, R, alpha_u8) — unlocks rich per-vertex RGB gradients in wh_vfx_mesh_vertex_anim!
    alpha = np.clip(coverage_alpha, 0.0, 1.0)
    if sqrt_compensate_alpha:
        alpha = np.sqrt(alpha)
    alpha_u8 = np.clip(np.round(alpha * 255.0), 0, 255).astype(np.uint8)
    if vertex_colors_rgb is not None:
        rgb = np.clip(np.asarray(vertex_colors_rgb, dtype=np.float32)[:, :3], 0.0, 1.0)
        rgb_u8 = np.clip(np.round(rgb * 255.0), 0, 255).astype(np.uint8)
        vbuf[:, 28] = rgb_u8[:, 2]  # B
        vbuf[:, 29] = rgb_u8[:, 1]  # G
        vbuf[:, 30] = rgb_u8[:, 0]  # R
    else:
        vbuf[:, 28] = 255
        vbuf[:, 29] = 255
        vbuf[:, 30] = 255
    vbuf[:, 31] = alpha_u8

    vbytes = vbuf.tobytes()
    ibytes = indices.tobytes()

    donor = _get_donor_template(donor_profile)
    # Header (0..140) + LOD0 Header (140..168) + Group0 Header (168..168+2248)
    g_off = 168
    donor_voff = struct.unpack_from("<I", donor, g_off + 8)[0]  # 2248
    ghdr = bytearray(donor[g_off:g_off + donor_voff])

    new_ioff = donor_voff + len(vbytes)
    new_glen = new_ioff + len(ibytes)

    bb_min = np.minimum(positions.min(axis=0),pos4[:, :3].min(axis=0))
    bb_max = np.maximum(positions.max(axis=0),pos4[:, :3].max(axis=0))
    # Expand degenerate axes slightly to avoid frustum culling bugs
    for a in range(3):
        if bb_max[a] - bb_min[a] < 0.02:
            bb_min[a] -= 0.01
            bb_max[a] += 0.01

    struct.pack_into("<6I6f", ghdr, 0,
                     68, new_glen, donor_voff, vcnt, new_ioff, icnt,
                     float(bb_min[0]), float(bb_min[1]), float(bb_min[2]),
                     float(bb_max[0]), float(bb_max[1]), float(bb_max[2]))

    # CRITICAL: Zero RMV2 Group0 pivot_xyz at ghdr+628..640!
    # Donor cylinder_wide_top.rigid_model_v2 stores pivot=(0.0, +7.238033, 0.0) because its vertices
    # span [-7.238, +7.238]. Leaving that non-zero shifts custom meshes by +7.238 * ParticleScale.y
    # (teleporting 284x-scaled inverted beams 2 km underground and floating apertures +47m!).
    struct.pack_into("<3f", ghdr, 628, 0.0, 0.0, 0.0)

    # Write clean ASCII group name at offset 82 (fixed-length slot where 'cylinder_beam_LOD01' lives)
    name_pos = 82  # verified in all three static donors
    if name_pos >= 0:
        gname_b = group_name.encode("ascii", errors="ignore")[:31].ljust(32, b"\x00")
        ghdr[name_pos:name_pos + 32] = gname_b

    file_hdr = bytearray(donor[:140])  # RMV2 v7, 1 LOD, empty skeleton
    lod_hdr = bytearray(donor[140:168])  # Exact 28-byte LOD0 header cloned from donor!
    struct.pack_into("<4I", lod_hdr, 0, 1, len(vbytes), len(ibytes), 168)

    out_bytes = bytes(file_hdr) + bytes(lod_hdr) + bytes(ghdr) + vbytes + ibytes
    # Built-in round-trip structural assertions
    chk_g = struct.unpack_from("<6I", out_bytes, 168)
    assert chk_g[0] == 68 and chk_g[2] == donor_voff and chk_g[3] == vcnt and chk_g[4] == new_ioff and chk_g[5] == icnt
    assert 168 + chk_g[1] == len(out_bytes)
    assert struct.unpack_from("<3f", out_bytes, 168 + 628) == (0.0, 0.0, 0.0), "RMV2 pivot must be zeroed!"
    return out_bytes


def build_wsmodel(rmv2_vpath: str, material_vpath: str) -> bytes:
    txt = (
        '<model version="1">\r\n'
        f'  <geometry>{rmv2_vpath}</geometry>\r\n'
        '  <materials>\r\n'
        f'    <material lod_index="0" part_index="0">{material_vpath}</material>\r\n'
        '  </materials>\r\n'
        '</model>\r\n'
    )
    return txt.encode("utf-8")


def build_wh_vfx_mesh_vertex_anim_material(
    name: str,
    tex_a: str,
    tex_b: str,
    tex_c: str,
    disp_tex: str,
    tile_a: tuple[float, float],
    scroll_a: tuple[float, float],
    tile_b: tuple[float, float],
    scroll_b: tuple[float, float],
    tile_c: tuple[float, float],
    scroll_c: tuple[float, float],
    glow_colour: tuple[float, float, float],
    glow_strength: float,
    total_alpha_strength: float = 1.0,
    normal_fade_in_out: float = 0.5,
    normal_fade_power: float = 1.0,
    depth_fade_switch: float = 1.0,
    pixel_depth_fade: float = 1.0,
    vertex_displacement: float = 1.0,
    vertex_disp_scale: float = 0.2,
    vertex_disp_tile: tuple[float, float] = (2.0, 4.0),
    vertex_disp_scroll: tuple[float, float] = (0.0, 1.5),
    brightness_abc: tuple[float, float, float] = (1.0, 1.0, 1.0),
    global_mask: str = "commontextures/default_white.dds",
    global_mask_tile: tuple[float, float] = (1.0, 1.0),
    global_mask_pan: tuple[float, float] = (0.0, 0.0),
    colour_overlay: str = "commontextures/default_white.dds",
    colour_overlay_tile: tuple[float, float] = (1.0, 1.0),
    colour_overlay_pan: tuple[float, float] = (0.0, 0.0),
    colour_overlay_strength: float = 1.0,
    sort_override: int = 255,
    tex_a_channels: str = "1,0,0,0",
    tex_b_channels: str = "1,0,0,0",
    tex_c_channels: str = "1,0,0,0",
    vertex_oscilation_scale: float = 0.0,
    vertex_amp_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0),
    vertex_freq_xyz: tuple[float, float, float] = (1.0, 1.0, 1.0),
    vertex_anim: float = 0.0, vertex_anim_speed: float = 0.0,
) -> bytes:
    """Build a verified WH3 shaders/wh_vfx_mesh_vertex_anim.xml.shader material.
    All parameter names, types, and order match vanilla wh2_dlc12_abilities_burning_beam.xml.material.
    """
    params = [
        ("Texture_A_channels", "float4", tex_a_channels),
        ("Texture_B_Channels", "float4", tex_b_channels),
        ("Texture_C_Channels", "float4", tex_c_channels),
        ("colour_overlay_pan", "float2", f"{colour_overlay_pan[0]:.5f},{colour_overlay_pan[1]:.5f}"),
        ("colour_overlay_strength", "float", f"{colour_overlay_strength:.4f}"),
        ("colour_overlay_tile", "float2", f"{colour_overlay_tile[0]:.4f},{colour_overlay_tile[1]:.4f}"),
        ("depth_fade_switch", "float", f"{depth_fade_switch:.4f}"),
        ("global_mask_pan", "float2", f"{global_mask_pan[0]:.5f},{global_mask_pan[1]:.5f}"),
        ("global_mask_tiling", "float2", f"{global_mask_tile[0]:.4f},{global_mask_tile[1]:.4f}"),
        ("glow_colour", "float3", f"{glow_colour[0]:.5f},{glow_colour[1]:.5f},{glow_colour[2]:.5f}"),
        ("glow_strength", "float", f"{glow_strength:.4f}"),
        ("normal_fade_in_out", "float", f"{normal_fade_in_out:.4f}"),
        ("normal_fade_power", "float", f"{normal_fade_power:.4f}"),
        ("pixel_depth_fade", "float", f"{pixel_depth_fade:.4f}"),
        ("scrolling_brightness", "float", f"{brightness_abc[0]:.4f}"),
        ("scrolling_brightness2", "float", f"{brightness_abc[1]:.4f}"),
        ("scrolling_brightness3", "float", f"{brightness_abc[2]:.4f}"),
        ("scrolling_colour_scroll", "float2", f"{scroll_a[0]:.5f},{scroll_a[1]:.5f}"),
        ("scrolling_colour_scroll2", "float2", f"{scroll_b[0]:.5f},{scroll_b[1]:.5f}"),
        ("scrolling_colour_scroll3", "float2", f"{scroll_c[0]:.5f},{scroll_c[1]:.5f}"),
        ("scrolling_colour_tiling", "float2", f"{tile_a[0]:.4f},{tile_a[1]:.4f}"),
        ("scrolling_colour_tiling2", "float2", f"{tile_b[0]:.4f},{tile_b[1]:.4f}"),
        ("scrolling_colour_tiling3", "float2", f"{tile_c[0]:.4f},{tile_c[1]:.4f}"),
        ("time_source", "float", "0"),
        ("total_alpha_strength", "float", f"{total_alpha_strength:.4f}"),
        ("vertex_anim", "float", str(vertex_anim)),
        ("vertex_anim_speed", "float", str(vertex_anim_speed)),
        ("vertex_colour", "float", "1"),
        ("vertex_disp_scale", "float", f"{vertex_disp_scale:.4f}"),
        ("vertex_disp_scroll", "float2", f"{vertex_disp_scroll[0]:.5f},{vertex_disp_scroll[1]:.5f}"),
        ("vertex_disp_tiling", "float2", f"{vertex_disp_tile[0]:.4f},{vertex_disp_tile[1]:.4f}"),
        ("vertex_displacement", "float", f"{vertex_displacement:.4f}"),
        ("vertex_oscilation_scale", "float", f"{vertex_oscilation_scale:.4f}"),
        ("vertex_x_amp", "float", f"{vertex_amp_xyz[0]:.4f}"),
        ("vertex_x_freq", "float", f"{vertex_freq_xyz[0]:.4f}"),
        ("vertex_y_amp", "float", f"{vertex_amp_xyz[1]:.4f}"),
        ("vertex_y_freq", "float", f"{vertex_freq_xyz[1]:.4f}"),
        ("vertex_z_amp", "float", f"{vertex_amp_xyz[2]:.4f}"),
        ("vertex_z_freq", "float", f"{vertex_freq_xyz[2]:.4f}"),
        ("vfx_mesh_alpha", "float", "1"),
    ]
    texs = [
        ("t_xml_Texture_A", tex_a),
        ("t_xml_Texture_B", tex_b),
        ("t_xml_Texture_C", tex_c),
        ("t_xml_colour_overlay", colour_overlay),
        ("t_xml_global_mask", global_mask),
        ("t_xml_vertex_disp_texture", disp_tex),
    ]
    lines = [
        "<material>",
        f" <name>{name}.xml</name>",
        " <shader>shaders/wh_vfx_mesh_vertex_anim.xml.shader</shader>",
        f" <sort_override>{int(sort_override)}</sort_override>",
        " <textures>",
    ]
    for slot, src in texs:
        lines.extend([
            "  <texture>",
            f'   <slot version="2">{slot}</slot>',
            f"   <source>{src}</source>",
            "  </texture>",
        ])
    lines.append(" </textures>")
    lines.append(" <params>")
    for pname, ptype, pval in params:
        lines.extend([
            "  <param>",
            f"   <name>{pname}</name>",
            f"   <type>{ptype}</type>",
            f"   <value>{pval}</value>",
            "  </param>",
        ])
    lines.append(" </params>")
    lines.append("</material>")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def build_wh3_vat_fluid_material(
    name: str,
    vat_pos_tex: str,
    vat_nrm_tex: str,
    base_colour_tex: str = "commontextures/default_white.dds",
    glow_colour: tuple[float, float, float] = (1.0, 1.0, 1.0),
    glow_strength: float = 2.0,
    vat_fps: float = 30.0,
    vat_frame_count: int = 60,
    bounds_min: tuple[float, float, float] = (-5.0, -5.0, -5.0),
    bounds_max: tuple[float, float, float] = (5.0, 5.0, 5.0),
    sort_override: int = 128,
) -> bytes:
    """Build a Route-A WH3 shaders/wh3_rigid_baked_fluid_simulation.xml.shader VAT material."""
    params = [
        ("vat_fps", "float", f"{vat_fps:.3f}"),
        ("vat_frame_count", "float", f"{float(vat_frame_count):.1f}"),
        ("vat_bounds_min", "float3", f"{bounds_min[0]:.5f},{bounds_min[1]:.5f},{bounds_min[2]:.5f}"),
        ("vat_bounds_max", "float3", f"{bounds_max[0]:.5f},{bounds_max[1]:.5f},{bounds_max[2]:.5f}"),
        ("glow_colour", "float3", f"{glow_colour[0]:.5f},{glow_colour[1]:.5f},{glow_colour[2]:.5f}"),
        ("glow_strength", "float", f"{glow_strength:.4f}"),
    ]
    texs = [
        ("t_xml_base_colour", base_colour_tex),
        ("t_xml_vat_position", vat_pos_tex),
        ("t_xml_vat_normal", vat_nrm_tex),
    ]
    lines = [
        "<material>",
        f" <name>{name}.xml</name>",
        " <shader>shaders/wh3_rigid_baked_fluid_simulation.xml.shader</shader>",
        f" <sort_override>{int(sort_override)}</sort_override>",
        " <textures>",
    ]
    for slot, src in texs:
        lines.extend([
            "  <texture>",
            f'   <slot version="2">{slot}</slot>',
            f"   <source>{src}</source>",
            "  </texture>",
        ])
    lines.append(" </textures>")
    lines.append(" <params>")
    for pname, ptype, pval in params:
        lines.extend([
            "  <param>",
            f"   <name>{pname}</name>",
            f"   <type>{ptype}</type>",
            f"   <value>{pval}</value>",
            "  </param>",
        ])
    lines.append(" </params>")
    lines.append("</material>")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
