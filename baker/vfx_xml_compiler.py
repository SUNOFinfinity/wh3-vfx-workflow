"""WH3 UTF-16LE multi-root VFX XML compiler with 40-emitter auto-paging, 3-tier quality sync,
exact CA curve serialization, and wrapper<->library reachability auditing.
"""
import copy
import math
import os
import xml.etree.ElementTree as ET
from pfh5 import PFH5Pack

from baker_config import WH3_VFX_DESC_PACK
DONOR_LIB_PATH = r"vfx\wh2_dlc12_lib_abilities_burning_alignment.xml"

AXIS_COLORS = {
    "x": (8405056, 16728128),
    "y": (4227136, 4259648),
    "z": (4210816, 4210943),
    "alpha": (8388608, 16711680),
    "r": (16711680, 16711680),
    "g": (65280, 65280),
    "b": (255, 255),
    "brightness": (16777215, 16777215),
    "inner_radius": (8405056, 16728128),
    "outer_radius": (4227136, 4259648),
}


def parse_wh3_vfx_xml(raw: bytes) -> ET.Element:
    txt = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    return ET.fromstring("<root>" + txt + "</root>")


def dump_wh3_vfx_xml(root: ET.Element) -> bytes:
    return "".join(ET.tostring(c, encoding="unicode") for c in root).encode("utf-16")


def encode_curve(axes: dict[str, list[tuple[float, float]] | tuple[list[tuple[float, float]], list[tuple[float, float]]]]) -> str:
    """Encode CA VFX XML curve string.
    Each axis value is either:
      - list[(t, v)] (same min & max)
      - (min_list, max_list)
    """
    parts = []
    maps = []
    for j, (axis, spec) in enumerate(axes.items()):
        if isinstance(spec, tuple) and len(spec) == 2 and isinstance(spec[0], list):
            k_min, k_max = spec
        else:
            k_min = k_max = spec
        anim_min = int(len(k_min) > 1)
        anim_max = int(len(k_max) > 1)
        is_ranged = int(k_min != k_max)
        col_min, col_max = AXIS_COLORS[axis]
        for side, col, keys, anim in (("min", col_min, k_min, anim_min), ("max", col_max, k_max, anim_max)):
            # Sort keys by t and deduplicate identical timestamps
            dedup = []
            for t, v in sorted(keys, key=lambda x: x[0]):
                if dedup and abs(t - dedup[-1][0]) < 1e-6:
                    dedup[-1] = (t, v)
                else:
                    dedup.append((t, v))
            pts = ";".join(f"{t:.6f},{v:.6f},linear,linear,-0.05,0.0,0.05,0.0,{1 - anim}" for t, v in dedup)
            parts.append(f"{axis}_{side},{col},{anim};{pts}")
        maps.append(f"{axis},{is_ranged};{2 * j},{2 * j + 1}")
    return "|".join(parts) + ":" + "|".join(maps)


def scalar_curve(v: float, vmax: float | None = None) -> str:
    if vmax is None or abs(vmax - v) < 1e-7:
        return encode_curve({"x": [(0.0, v)]})
    return encode_curve({"x": ([(0.0, v)], [(0.0, vmax)])})


def vec3_curve(x: float, y: float, z: float) -> str:
    return encode_curve({"x": [(0.0, x)], "y": [(0.0, y)], "z": [(0.0, z)]})


def colour_curve(r: float, g: float, b: float, brightness: float = 1.0,
                 r2: float | None = None, g2: float | None = None, b2: float | None = None,
                 brightness_keys: list[tuple[float, float]] | None = None) -> str:
    r2 = r if r2 is None else r2
    g2 = g if g2 is None else g2
    b2 = b if b2 is None else b2
    bk = brightness_keys if brightness_keys is not None else [(0.0, brightness)]
    return encode_curve({
        "r": ([(0.0, r)], [(0.0, r2)]),
        "g": ([(0.0, g)], [(0.0, g2)]),
        "b": ([(0.0, b)], [(0.0, b2)]),
        "brightness": bk,
    })


def set_prop(em: ET.Element, name: str, value: str | None = None, text: str | None = None,
             tag: str = "property", extra_attrs: dict | None = None, quality: str = "0"):
    # Remove any non-q0 overrides of this property so quality 0/1/2 stay unified
    for old in list(em.findall(f"./{tag}[@name='{name}']")):
        if old.get("quality") != quality:
            em.remove(old)
    node = em.find(f"./{tag}[@name='{name}'][@quality='{quality}']")
    if node is None:
        node = ET.SubElement(em, tag, {"name": name, "quality": quality})
    if value is not None:
        node.set("value", str(value))
    if extra_attrs:
        for k, v in extra_attrs.items():
            node.set(k, str(v))
    if text is not None:
        node.text = text
    return node


def ensure_all_quality_tiers(em: ET.Element, cull_far: float = 3000.0, cull_max: float = 3750.0):
    """Force Enable=true and long cull distances across quality tiers 0, 1, 2."""
    for p in list(em.findall("./property[@name='Enable']")):
        em.remove(p)
    for p in list(em.findall("./property[@name='CullDistances']")):
        em.remove(p)
    for p in list(em.findall("./property[@name='ParticleCullDistances']")):
        em.remove(p)
    for q in ("0", "1", "2"):
        ET.SubElement(em, "property", {"name": "Enable", "quality": q, "value": "true"})
        ET.SubElement(em, "property", {"name": "CullDistances", "quality": q, "x": "0.00", "y": "0.00", "z": f"{cull_far:.2f}", "w": f"{cull_max:.2f}"})
        ET.SubElement(em, "property", {"name": "ParticleCullDistances", "quality": q, "x": "0.00", "y": "0.00", "z": f"{cull_far:.2f}", "w": f"{cull_max:.2f}"})


def sync_bc_qualities(bc: ET.Element):
    """Clone quality=0 into quality=1 and quality=2 so all graphics settings render identically."""
    q0 = bc.find("./quality[@value='0']")
    if q0 is None:
        return
    for q in list(bc.findall("./quality")):
        if q.get("value") != "0":
            bc.remove(q)
    for qv in ("1", "2"):
        qc = copy.deepcopy(q0)
        qc.set("value", qv)
        bc.append(qc)


class DonorVfxBank:
    def __init__(self, library=DONOR_LIB_PATH):
        from vanilla_index import VanillaIndex
        from pathlib import Path
        base=Path(WH3_VFX_DESC_PACK)
        index=VanillaIndex([base.with_name("vfx_desc_bl.pack"),base])
        try: raw=index.read(library)
        finally: index.close()
        if raw is None: raise KeyError(library)
        root = parse_wh3_vfx_xml(raw)
        self.vfx = root.find("vfx")

    def clone_triple(self, donor_id: str, new_id: str) -> tuple[ET.Element, ET.Element, ET.Element]:
        em = copy.deepcopy(self.vfx.find(f"./emitters/emitter[@id='{donor_id}']"))
        bc = copy.deepcopy(self.vfx.find(f"./behaviour_chains/behaviour_chain[@id='{donor_id}']"))
        rs = copy.deepcopy(self.vfx.find(f"./effect_render_stacks/effect_render_stack[@id='{donor_id}']"))
        if em is None or bc is None or rs is None:
            raise KeyError(f"Donor emitter triple '{donor_id}' incomplete")
        em.set("id", new_id)
        bc.set("id", new_id)
        rs.set("id", new_id)
        # Re-link emitter -> behaviour_chain & effect_render_stack
        for p in em.findall("./property"):
            n = p.get("name", "")
            if n in ("BehaviourChain", "BehaviourChainId"):
                p.set("value", new_id)
            elif n in ("EffectRenderStack", "EffectRenderStackId", "RenderStack"):
                p.set("value", new_id)
        # Check attributes on emitter tag too
        if "behaviour_chain" in em.attrib:
            em.set("behaviour_chain", new_id)
        if "render_stack" in em.attrib:
            em.set("render_stack", new_id)
        # Also remove any quality=1/2 overrides on emitter properties except Enable/Cull (which we re-set cleanly)
        for p in list(em):
            q = p.get("quality")
            if q in ("1", "2"):
                em.remove(p)
        return em, bc, rs


def build_library_xml(triples: list[tuple[ET.Element, ET.Element, ET.Element]]) -> bytes:
    root = ET.Element("root")
    ET.SubElement(root, "shader_library", {"file": "fx/particle_vfx_library.hlsl"})
    vfx = ET.SubElement(root, "vfx", {
        "revision": "2",
        "construction_type": "1",
        "enable_in_ted": "false",
        "camera_following": "false",
        "ref_spawn_speed": "-1.00",
    })
    em_parent = ET.SubElement(vfx, "emitters")
    bc_parent = ET.SubElement(vfx, "behaviour_chains")
    rs_parent = ET.SubElement(vfx, "effect_render_stacks")
    for em, bc, rs in triples:
        ensure_all_quality_tiers(em)
        sync_bc_qualities(bc)
        em_parent.append(em)
        bc_parent.append(bc)
        rs_parent.append(rs)
    ET.SubElement(root, "vfx_references")
    return dump_wh3_vfx_xml(root)


def build_paged_libraries_and_wrappers(
    lib_prefix: str,
    sky_triples: list[tuple[ET.Element, ET.Element, ET.Element]],
    ground_triples: list[tuple[ET.Element, ET.Element, ET.Element]],
    max_per_page: int = 40,
) -> tuple[dict[str, bytes], list[str], bytes, bytes]:
    """Auto-page emitters at <=40 per library XML and generate minimal zero-transform
    SKY and GROUND wrapper XMLs with 100% verified emitter enablement.
    """
    all_triples = [(t, "sky") for t in sky_triples] + [(t, "ground") for t in ground_triples]
    pages: list[tuple[str, list[tuple[tuple[ET.Element, ET.Element, ET.Element], str]]]] = []
    for i in range(0, len(all_triples), max_per_page):
        chunk = all_triples[i:i + max_per_page]
        page_key = lib_prefix if i == 0 else f"{lib_prefix}_page_{i // max_per_page:02d}"
        pages.append((page_key, chunk))

    lib_files: dict[str, bytes] = {}
    lib_keys: list[str] = []
    for page_key, chunk in pages:
        lib_keys.append(page_key)
        lib_files[f"vfx\\{page_key}.xml"] = build_library_xml([c[0] for c in chunk])

    def _make_wrapper(target_side: str) -> bytes:
        root = ET.Element("root")
        ET.SubElement(root, "shader_library", {"file": "fx/particle_vfx_library.hlsl"})
        vfx = ET.SubElement(root, "vfx", {
            "revision": "2",
            "construction_type": "2",
            "enable_in_ted": "false",
            "camera_following": "false",
            "ref_spawn_speed": "-1.00",
        })
        ET.SubElement(vfx, "emitters")
        ET.SubElement(vfx, "behaviour_chains")
        ET.SubElement(vfx, "effect_render_stacks")
        vrefs = ET.SubElement(root, "vfx_references")

        for pi, (page_key, chunk) in enumerate(pages):
            side_ids = [c[0][0].get("id") for c in chunk if c[1] == target_side]
            if not side_ids:
                continue
            ref = ET.SubElement(vrefs, "vfx", {
                "inst_name": f"{target_side}_page_{pi:02d}",
                "vfx_ref": page_key,
                "notes": "",
                "scale_min": "1.00",
                "scale_max": "1.00",
                "pos_x": "0.00",
                "pos_y": "0.00",
                "pos_z": "0.00",
                "rot_x": "0.00",
                "rot_y": "0.00",
                "rot_z": "0.00",
                "start_time_min": "0.00",
                "start_time_max": "0.00",
                "infinite_life": "true",
                "simulation_speed_multiplier": "1.00",
            })
            for (em, _, _), side in chunk:
                eid = em.get("id")
                ET.SubElement(ref, "emitter", {
                    "name": eid,
                    "enable": "true" if side == target_side else "false",
                    "tint_min_r": "1.00", "tint_min_g": "1.00", "tint_min_b": "1.00", "tint_min_a": "1.00",
                    "tint_max_r": "1.00", "tint_max_g": "1.00", "tint_max_b": "1.00", "tint_max_a": "1.00",
                    "particle_lifetime_bias_min": "0.00", "particle_lifetime_bias_max": "0.00",
                    "emitter_lifetime_bias_min": "0.00", "emitter_lifetime_bias_max": "0.00",
                    "spawn_rate_scale_factor_min": "1.00", "spawn_rate_scale_factor_max": "1.00",
                    "sound_record": "",
                })
        return dump_wh3_vfx_xml(root)

    return lib_files, lib_keys, _make_wrapper("sky"), _make_wrapper("ground")


def audit_wrapper_reachability(wrapper_bytes_list: list[bytes], lib_files: dict[str, bytes]) -> dict:
    """Strictly verify every defined emitter across all library pages is enabled exactly once
    in the union of SKY and GROUND wrappers, with zero missing or dangling references.
    """
    defined = {}
    for lpath, lbytes in lib_files.items():
        lkey = os.path.splitext(os.path.basename(lpath))[0]
        r = parse_wh3_vfx_xml(lbytes)
        ids = {em.get("id") for em in r.findall("./vfx/emitters/emitter")}
        defined[lkey] = ids

    enabled_by_lib = {k: [] for k in defined}
    for wb in wrapper_bytes_list:
        r = parse_wh3_vfx_xml(wb)
        for ref in r.findall("./vfx_references/vfx"):
            lkey = ref.get("vfx_ref")
            if lkey not in defined:
                raise RuntimeError(f"Wrapper references unknown library '{lkey}'")
            for em in ref.findall("./emitter"):
                if em.get("enable") == "true":
                    name = em.get("name")
                    if name not in defined[lkey]:
                        raise RuntimeError(f"Wrapper enables undefined emitter '{name}' in '{lkey}'")
                    enabled_by_lib[lkey].append(name)

    for lkey, def_ids in defined.items():
        en_list = enabled_by_lib[lkey]
        if len(en_list) != len(set(en_list)):
            raise RuntimeError(f"Duplicate enabled emitter in '{lkey}'")
        missing = def_ids - set(en_list)
        if missing:
            raise RuntimeError(f"Defined but NEVER enabled emitters in '{lkey}': {sorted(missing)}")

    return {
        "libraries": {k: len(v) for k, v in defined.items()},
        "total_defined": sum(len(v) for v in defined.values()),
        "total_enabled": sum(len(v) for v in enabled_by_lib.values()),
        "reachability_ok": True,
    }

def animated_modifier(cols,rows,fps):
    """WH3 DLC27 native modifier, fixed cycles/s = requested frames/s / number of cells."""
    if min(cols,rows,fps)<=0: raise ValueError("Flipbook grid and fps must be positive")
    bank=DonorVfxBank(r"vfx\cine_wh3_dlc27_lib_nightmaw.xml")
    mod=copy.deepcopy(bank.vfx.find(".//modifier[@fragment='vs_animated_texture']"))
    for key,value in {"no_cells_xy":f"float2({cols},{rows})","no_loops":f"float2({fps/(cols*rows):.9g},{fps/(cols*rows):.9g})","time_mode":"fixed_animation_speed","cell_blend":"false","randomize_cell":"false","loop":"true","randomize_row":"false","row_restricted":"false"}.items():
        mod.find(f"./property[@name='{key}']").set("value",value)
    return mod
