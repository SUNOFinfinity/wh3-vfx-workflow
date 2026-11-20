"""Generate a minimal, self-contained WH3 Route-A IR for end-to-end smoke testing."""
import argparse
import json
import sys
from pathlib import Path

_BAKER = Path(__file__).resolve().parents[1] / "baker"
if _BAKER.is_dir():
    sys.path.insert(0, str(_BAKER))
    import baker_config  # noqa: F401 - ensures Houdini/custom DLL search paths are registered

import numpy as np
from PIL import Image


def make_radial_glow(path: Path, size: int = 256) -> None:
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    nx = (x + 0.5) / size * 2.0 - 1.0
    ny = (y + 0.5) / size * 2.0 - 1.0
    r = np.sqrt(nx * nx + ny * ny)
    # Smooth falloff reaching exact zero well before texture border (safe for BC7 mipmaps).
    mask = np.clip((0.82 - r) / 0.82, 0.0, 1.0)
    alpha = (mask ** 2.4)
    core = np.clip((0.45 - r) / 0.45, 0.0, 1.0) ** 2.0
    rgb = np.zeros((size, size, 3), dtype=np.float32)
    rgb[..., 0] = np.clip(0.95 * alpha + 0.35 * core, 0.0, 1.0)
    rgb[..., 1] = np.clip(0.55 * alpha + 0.45 * core, 0.0, 1.0)
    rgb[..., 2] = np.clip(0.18 * alpha + 0.35 * core, 0.0, 1.0)
    rgba = np.concatenate([rgb, alpha[..., None]], axis=-1)
    # Zero out outer 8px guard border explicitly
    rgba[:8, :, :] = 0.0
    rgba[-8:, :, :] = 0.0
    rgba[:, :8, :] = 0.0
    rgba[:, -8:, :] = 0.0
    Image.fromarray(np.clip(rgba * 255.0 + 0.5, 0, 255).astype(np.uint8), mode="RGBA").save(path)


def make_decal_ring(path: Path, size: int = 256) -> None:
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    nx = (x + 0.5) / size * 2.0 - 1.0
    ny = (y + 0.5) / size * 2.0 - 1.0
    r = np.sqrt(nx * nx + ny * ny)
    ring = np.exp(-((r - 0.56) / 0.14) ** 2)
    inner = np.exp(-((r - 0.20) / 0.18) ** 2) * 0.55
    guard = np.clip((0.84 - r) / 0.16, 0.0, 1.0)
    alpha = np.clip((ring + inner) * guard, 0.0, 1.0)
    rgb = np.zeros((size, size, 3), dtype=np.float32)
    rgb[..., 0] = 1.0 * alpha
    rgb[..., 1] = 0.62 * alpha
    rgb[..., 2] = 0.22 * alpha
    rgba = np.concatenate([rgb, alpha[..., None]], axis=-1)
    rgba[:8, :, :] = 0.0
    rgba[-8:, :, :] = 0.0
    rgba[:, :8, :] = 0.0
    rgba[:, -8:, :] = 0.0
    Image.fromarray(np.clip(rgba * 255.0 + 0.5, 0, 255).astype(np.uint8), mode="RGBA").save(path)


def build_smoke_ir(out_dir: Path) -> None:
    tex_dir = out_dir / "textures"
    tex_dir.mkdir(parents=True, exist_ok=True)
    make_radial_glow(tex_dir / "smoke_glow.png", 256)
    make_decal_ring(tex_dir / "smoke_ring.png", 256)

    manifest = {
        "schema_version": "1.0.0",
        "meta": {
            "effect_id": "smoke_golden_burst",
            "prefix": "smk",
            "display_name": "Smoke Test Golden Burst",
            "author": "wh3-vfx-workflow",
            "source_dcc": "Procedural Python",
            "duration_sec": 2.0,
            "looping": False,
            "cull_distances": [1200, 1800],
            "preview_framing_m": [0.0, 5.0, 6.0],
            "preview_phases_sec": [0.15, 0.55, 1.15, 1.75],
        },
        "archetype": "explosion_shockwave",
        "host_binding": {
            "wrappers": [
                {
                    "vfx_key": "smk_smoke_golden_burst",
                    "library_base": "smk_smoke_golden_burst_lib",
                    "terrain_attach": "map_zero_y_to_terrain",
                    "delay_sec": 0.0,
                    "scale": 1.0,
                }
            ]
        },
        "textures": {
            "glow_orb": {
                "source_path": "textures/smoke_glow.png",
                "pack_path": "vfx/textures/smk_smoke_glow.dds",
                "usage": "particle_optical",
                "format": "BC7_UNORM_SRGB",
                "target_size": [256, 256],
                "tileable": False,
                "source_color_space": "srgb",
                "premultiply_alpha": False,
                "require_zero_border": True,
            },
            "ground_ring": {
                "source_path": "textures/smoke_ring.png",
                "pack_path": "vfx/textures/smk_smoke_ring.dds",
                "usage": "decal_emissive",
                "format": "BC7_UNORM_SRGB",
                "target_size": [256, 256],
                "tileable": False,
                "source_color_space": "srgb",
                "premultiply_alpha": False,
                "require_zero_border": True,
            },
        },
        "meshes": {},
        "materials": {},
        "particle_emitters": [
            {
                "name": "smk_core_flash",
                "wrapper_vfx_key": "smk_smoke_golden_burst",
                "texture_id": "glow_orb",
                "render_pass": "FULLRES_DEFERRED_ALPHA",
                "pre_multiplied_alpha": False,
                "omit_ps_lighting": True,
                "start_time": 0.0,
                "duration": 0.4,
                "particle_life": [0.9, 1.4],
                "emission_rate": 0.0,
                "burst_count": 28,
                "max_particles": 48,
                "billboard_orientation": "camera",
                "emitter_shape": "sphere",
                "emitter_radius": 0.6,
                "emitter_height": 0.4,
                "offset_xyz": [0.0, 0.8, 0.0],
                "velocity_xyz": [0.0, 3.2, 0.0],
                "velocity_radial": 3.5,
                "gravity": -1.8,
                "drag": 0.8,
                "size_curve": [[0.0, 0.4], [0.25, 1.3], [1.0, 0.25]],
                "colour_curve": [
                    [0.0, 1.0, 0.92, 0.65],
                    [0.45, 1.0, 0.58, 0.18],
                    [1.0, 0.65, 0.20, 0.04],
                ],
                "alpha_curve": [[0.0, 0.0], [0.12, 0.95], [0.75, 0.45], [1.0, 0.0]],
                "colour_multiplier": 1.6,
                "emissive": 3.2,
                "additive_blend_mul": 0.85,
                "soft_factor": 1.5,
                "velocity_stretch_mul": 0.25,
                "velocity_stretch_cap": 3.0,
            }
        ],
        "decal_emitters": [
            {
                "name": "smk_ground_ring",
                "wrapper_vfx_key": "smk_smoke_golden_burst",
                "texture_id": "ground_ring",
                "render_pass": "DEFERRED_DECAL_EMISSIVE",
                "pre_multiplied_alpha": False,
                "keep_ps_lighting": False,
                "start_time": 0.0,
                "life_time": 1.8,
                "radius_curve": [[0.0, 0.8], [0.35, 3.6], [1.0, 4.4]],
                "rotation_speed_deg": 18.0,
                "colour_curve": [[0.0, 1.0, 0.78, 0.35], [0.6, 0.95, 0.45, 0.12], [1.0, 0.4, 0.12, 0.02]],
                "alpha_curve": [[0.0, 0.0], [0.15, 0.9], [0.7, 0.5], [1.0, 0.0]],
                "colour_multiplier": 2.0,
            }
        ],
        "lights": [
            {
                "name": "smk_burst_light",
                "wrapper_vfx_key": "smk_smoke_golden_burst",
                "start_time": 0.0,
                "life_time": 1.5,
                "offset_xyz": [0.0, 1.4, 0.0],
                "radius_curve": [[0.0, 2.0], [0.25, 8.0], [1.0, 4.0]],
                "colour_curve": [[0.0, 1.0, 0.75, 0.35], [1.0, 0.8, 0.3, 0.08]],
                "intensity_curve": [[0.0, 0.0], [0.15, 5.0], [1.0, 0.0]],
            }
        ],
    }

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Generated smoke IR -> {out_dir.resolve()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("work/smoke_ir"), help="Output IR directory")
    args = ap.parse_args()
    build_smoke_ir(args.out)
