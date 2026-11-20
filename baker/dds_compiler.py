from pathlib import Path
import io
import os
import struct
import subprocess
import tempfile
import baker_config  # retains the configured DLL search handle
import numpy as np
from PIL import Image

from baker_config import TEXCONV_PATH


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    """Exact IEC 61966-2-1 Linear -> sRGB OETF (inverse of WH3 linear_accurate_component)."""
    x = np.clip(x, 0.0, 1.0).astype(np.float32)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(np.maximum(x, 1e-12), 1.0 / 2.4) - 0.055)


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """Exact IEC 61966-2-1 sRGB -> Linear EOTF (matches WH3 particle_vfx_library.hlsl)."""
    x = np.clip(x, 0.0, 1.0).astype(np.float32)
    return np.where(x <= 0.04045, x / 12.92, np.power(np.maximum((x + 0.055) / 1.055, 0.0), 2.4))


def upsample_seamless(img_hw: np.ndarray, target_size: int | tuple[int,int]) -> np.ndarray:
    """Periodic-wrapped bicubic upsampling for seamless tiling textures (zero edge seam)."""
    h, w = img_hw.shape[:2]
    pad = 4
    if img_hw.ndim == 2:
        padded = np.pad(img_hw, ((pad, pad), (pad, pad)), mode="wrap")
    else:
        padded = np.pad(img_hw, ((pad, pad), (pad, pad), (0, 0)), mode="wrap")
    tw,th=(target_size,target_size) if isinstance(target_size,int) else target_size
    sx,sy=tw/float(w),th/float(h)
    new_w=int(round(padded.shape[1]*sx));new_h=int(round(padded.shape[0]*sy))
    if img_hw.ndim == 2:
        pil = Image.fromarray(np.ascontiguousarray(padded, dtype=np.float32), mode="F")
        up = np.clip(np.asarray(pil.resize((new_w, new_h), Image.Resampling.BICUBIC), dtype=np.float32), 0.0, 1.0)
    else:
        up = np.empty((new_h, new_w, img_hw.shape[2]), dtype=np.float32)
        for c in range(img_hw.shape[2]):
            pil = Image.fromarray(np.ascontiguousarray(padded[:, :, c], dtype=np.float32), mode="F")
            up[:, :, c] = np.clip(np.asarray(pil.resize((new_w, new_h), Image.Resampling.BICUBIC), dtype=np.float32), 0.0, 1.0)
    px,py=round(pad*sx),round(pad*sy)
    return up[py:py+th, px:px+tw]


def upsample_clamped(rgba_f32: np.ndarray, target_size: int | tuple[int,int], border_px: int = 8, neutral_rgba=(0.0, 0.0, 0.0, 0.0)) -> np.ndarray:
    """Smoothly upsample an isolated optical tile with BC 4x4 block-aligned safe border.
    CRITICAL: Resamples each channel independently as 32-bit float ('F' mode) so Pillow NEVER
    applies premultiplied-alpha RGB*A/A division (which turns low-alpha 64x64 tiles into 8x8 pixel blocks!).
    """
    tw,th=(target_size,target_size) if isinstance(target_size,int) else target_size
    border_px=min(border_px,(min(tw,th)-1)//2)
    iw,ih=tw-2*border_px,th-2*border_px
    up_inner = np.empty((ih, iw, 4), dtype=np.float32)
    for c in range(4):
        ch_pil = Image.fromarray(np.ascontiguousarray(rgba_f32[:, :, c], dtype=np.float32), mode="F")
        up_inner[:, :, c] = np.clip(
            np.asarray(ch_pil.resize((iw, ih), Image.Resampling.BICUBIC), dtype=np.float32),
            0.0, 1.0,
        )
    out = np.empty((th,tw,4), dtype=np.float32)
    for c in range(4):
        out[:, :, c] = neutral_rgba[c]
    out[border_px:border_px + ih, border_px:border_px + iw] = up_inner
    # Soft feather over 8px inside inner edge so zero hard border step
    if border_px > 0:
        def ramp(n):
            v=np.ones(n,dtype=np.float32);fade=min(12,n//8)
            if fade:
                edge=np.linspace(0.,1.,fade,dtype=np.float32);v[:fade]=edge;v[-fade:]=edge[::-1]
            return v
        mask=np.outer(ramp(ih),ramp(iw))[...,None]
        neut = np.array(neutral_rgba, dtype=np.float32)[None, None, :]
        out[border_px:border_px + ih, border_px:border_px + iw] = (
            neut * (1.0 - mask) + out[border_px:border_px + ih, border_px:border_px + iw] * mask
        )
    return out


def _run_texconv(
    src_png: str,
    out_dir: str,
    fmt: str,
    srgb_in: bool,
    mips: int = 0,
    tileable: bool = False,
    separate_alpha: bool = False,
) -> str:
    args = [
        TEXCONV_PATH,
        "-nologo",
        "-y",
        "-f", fmt,
        "-dx10",
        "-m", str(mips),
        "-if", "CUBIC",
        "-o", out_dir,
    ]
    if srgb_in:
        args.append("-srgbi")
    if tileable:
        args.append("-wrap")
    if separate_alpha:
        args.append("-sepalpha")
    args.append(src_png)
    res = subprocess.run(args, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"texconv failed ({res.returncode}): {res.stdout.decode('utf-8', 'replace')}\n{res.stderr.decode('utf-8', 'replace')}")
    base = os.path.splitext(os.path.basename(src_png))[0] + ".dds"
    return os.path.join(out_dir, base)


def audit_dds_border(dds_path: str, expected_neutral_u8=(0, 0, 0, 0), max_tol: int = 1, check_channels=(3,)) -> dict:
    """Decode DDS via texconv to PNG and verify border pixels across top mip have zero leak."""
    raw=Path(dds_path).read_bytes()
    # Both BC3 and BC7 use a 16-byte 4x4 block. Decode native mips in-process.
    if raw[84:88]==b"DX10" and struct.unpack_from("<I",raw,128)[0] in (77,78,98,99):
        h,w=struct.unpack_from("<II",raw,12);shape=[h,w,4];off=148;levels=[]
        for level in range(max(1,struct.unpack_from("<I",raw,28)[0])):
            size=max(1,(w+3)//4)*max(1,(h+3)//4)*16
            if off+size>len(raw):raise ValueError(f"{dds_path} truncated at mip {level}")
            head=bytearray(raw[:148]);struct.pack_into("<3I",head,12,h,w,size);struct.pack_into("<I",head,28,1)
            try:
                with Image.open(io.BytesIO(head+raw[off:off+size])) as im:
                    arr=np.asarray(im.convert("RGBA"),dtype=np.int16)
            except NotImplementedError:break # older Pillow: retain texconv top-mip contract
            border=np.concatenate((arr[0],arr[-1],arr[:,0],arr[:,-1]))
            errs={f"ch{c}":int(np.abs(border[:,c]-expected_neutral_u8[c]).max()) for c in check_channels}
            if level==0 and max(errs.values())>max_tol:raise ValueError(f"{dds_path} mip 0 border {errs}; tolerance {max_tol}")
            levels.append({"mip":level,"size":[w,h],"max_err":errs})
            off+=size;w=max(1,w//2);h=max(1,h//2)
        else:return {"file":str(dds_path),"shape":shape,"border_max_err":levels[0]["max_err"],"mips":levels}
    with tempfile.TemporaryDirectory() as td:
        args = [TEXCONV_PATH, "-nologo", "-y", "-ft", "png", "-f", "R8G8B8A8_UNORM", "-o", td, dds_path]
        res = subprocess.run(args, capture_output=True)
        if res.returncode != 0:
            raise RuntimeError(f"texconv decode audit failed: {res.stdout.decode('utf-8', 'replace')}\n{res.stderr.decode('utf-8', 'replace')}")
        png_p = os.path.join(td, os.path.splitext(os.path.basename(dds_path))[0] + ".png")
        arr = np.asarray(Image.open(png_p).convert("RGBA"), dtype=np.int32)
        border = np.concatenate([
            arr[0, :, :], arr[-1, :, :], arr[:, 0, :], arr[:, -1, :]
        ], axis=0)
        max_err = {}
        for ch in check_channels:
            err = int(np.max(np.abs(border[:, ch] - expected_neutral_u8[ch])))
            max_err[f"ch{ch}"] = err
            if err > max_tol:
                raise RuntimeError(f"DDS border leak in {os.path.basename(dds_path)} ch{ch}: max_err={err} > {max_tol}")
        return {"file": os.path.basename(dds_path), "shape": list(arr.shape), "border_max_err": max_err}


def encode_bc7_srgb_dds(
    rgba_f32: np.ndarray,
    out_dds_path: str,
    alpha_is_linear: bool = True,
    rgb_is_linear: bool = True,
    verify_zero_alpha_border: bool = False,
    tileable: bool = False,
    separate_alpha: bool = True,
    srgb_format: bool = True,
) -> dict:
    """Encode RGBA float32 [0..1] to WH3-native DX10:BC7_UNORM_SRGB with full mip chain.
    If alpha_is_linear=True, applies exact IEC 61966-2-1 Linear->sRGB OETF to alpha
    so WH3's ps_texture linear_accurate_component(alpha) recovers exact linear opacity!
    - tileable=True passes `-wrap` to texconv so mip levels wrap seamlessly with zero edge seam.
    - separate_alpha=True passes `-sepalpha` so straight-alpha emissive mipmaps avoid dark halo fringes.
    """
    out = np.empty_like(rgba_f32, dtype=np.float32)
    out[:, :, :3] = linear_to_srgb(rgba_f32[:, :, :3]) if rgb_is_linear else np.clip(rgba_f32[:, :, :3], 0.0, 1.0)
    if not srgb_format: out[:,:,:3]=np.clip(rgba_f32[:,:,:3],0,1) if rgb_is_linear else srgb_to_linear(rgba_f32[:,:,:3])
    out[:, :, 3] = linear_to_srgb(rgba_f32[:, :, 3]) if alpha_is_linear else np.clip(rgba_f32[:, :, 3], 0.0, 1.0)
    u8 = np.clip(np.round(out * 255.0), 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(out_dds_path), exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        src_png = os.path.join(td, "src.png")
        Image.fromarray(u8, mode="RGBA").save(src_png)
        dds_tmp = _run_texconv(
            src_png, td, fmt="BC7_UNORM_SRGB" if srgb_format else "BC7_UNORM", srgb_in=srgb_format, mips=0,
            tileable=tileable, separate_alpha=separate_alpha,
        )
        with open(dds_tmp, "rb") as f:
            data = f.read()
        with open(out_dds_path, "wb") as f:
            f.write(data)
    info = {"path": out_dds_path, "bytes": len(data), "format": "DX10:BC7_UNORM_SRGB" if srgb_format else "DX10:BC7_UNORM"}
    if verify_zero_alpha_border:
        info["border_audit"] = audit_dds_border(out_dds_path, expected_neutral_u8=(0, 0, 0, 0), max_tol=1, check_channels=(3,))
    return info


def encode_dxt5nm_distortion_dds(
    vx_norm: np.ndarray,
    vy_norm: np.ndarray,
    out_dds_path: str,
) -> dict:
    """Encode linear 2D distortion vectors (vx, vy in [-1..1], 0=neutral) into WH3 DXT5nm
    DX10:BC3_UNORM linear DDS (A=X, G=Y, neutral=0.5, R=0, B=1) with neutral border audit.
    """
    h, w = vx_norm.shape
    rgba = np.zeros((h, w, 4), dtype=np.float32)
    rgba[:, :, 0] = 0.0
    rgba[:, :, 1] = np.clip(vy_norm * 0.5 + 0.5, 0.0, 1.0)
    rgba[:, :, 2] = 1.0
    rgba[:, :, 3] = np.clip(vx_norm * 0.5 + 0.5, 0.0, 1.0)
    u8 = np.clip(np.round(rgba * 255.0), 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(out_dds_path), exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        src_png = os.path.join(td, "dist.png")
        Image.fromarray(u8, mode="RGBA").save(src_png)
        dds_tmp = _run_texconv(src_png, td, fmt="BC3_UNORM", srgb_in=False, mips=0)
        with open(dds_tmp, "rb") as f:
            data = f.read()
        with open(out_dds_path, "wb") as f:
            f.write(data)
    audit = audit_dds_border(out_dds_path, expected_neutral_u8=(0, 128, 255, 128), max_tol=2, check_channels=(1, 3))
    return {"path": out_dds_path, "bytes": len(data), "format": "DX10:BC3_UNORM", "border_audit": audit}


def encode_vectorfield_3d_dds(vec_zyx3: np.ndarray, out_dds_path: str) -> bytes:
    """Encode a 3D velocity field (shape [D, H, W, 3] float32) into WH3-native
    D3DFMT_A16B16G16R16F (FourCC=113) 3D Volume DDS matching vanilla vfx/vectorfields/*.dds.
    """
    d, h, w, c = vec_zyx3.shape
    assert c == 3
    rgba16 = np.zeros((d, h, w, 4), dtype=np.float16)
    rgba16[:, :, :, :3] = vec_zyx3.astype(np.float16)
    rgba16[:, :, :, 3] = np.float16(1.0)

    # DDS_HEADER (124 bytes after 'DDS ')
    # flags: CAPS(0x1)|HEIGHT(0x2)|WIDTH(0x4)|PIXELFORMAT(0x1000)|DEPTH(0x800000)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x800000
    pitch = w * 8  # 4 * 2 bytes per pixel
    pf_flags = 0x4  # DDPF_FOURCC
    fourcc_a16b16g16r16f = 113
    caps1 = 0x1000 | 0x8  # TEXTURE | COMPLEX
    caps2 = 0x200000  # DDSCAPS2_VOLUME

    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<7I", header, 4, 124, flags, h, w, pitch, d, 0)
    struct.pack_into("<4I", header, 76, 32, pf_flags, fourcc_a16b16g16r16f, 0)
    struct.pack_into("<2I", header, 108, caps1, caps2)

    blob = bytes(header) + rgba16.tobytes()
    os.makedirs(os.path.dirname(out_dds_path), exist_ok=True)
    with open(out_dds_path, "wb") as f:
        f.write(blob)
    return blob


def encode_vat_f32_dds(rgba_hw4: np.ndarray, out_dds_path: str) -> bytes:
    """Encode 2D float32 RGBA array [H, W, 4] into WH3-native D3DFMT_A32B32G32R32F (FourCC=116, mips=1)
    for shaders/wh3_rigid_baked_fluid_simulation.xml.shader.
    """
    h, w, c = rgba_hw4.shape
    assert c == 4
    arr = np.ascontiguousarray(rgba_hw4, dtype=np.float32)
    flags = 0x100F  # WH3 lava splat position/normal/UV: DDSD_PITCH, count field 1
    pitch = w * 16
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<7I", header, 4, 124, flags, h, w, pitch, 1, 1)
    struct.pack_into("<4I", header, 76, 32, 0x4, 116, 0)  # D3DFMT_A32B32G32R32F
    struct.pack_into("<2I", header, 108, 0x1000, 0)
    blob = bytes(header) + arr.tobytes()
    os.makedirs(os.path.dirname(out_dds_path), exist_ok=True)
    with open(out_dds_path, "wb") as f:
        f.write(blob)
    return blob

def exact_zero_dxt5nm_padding(path):
    # One verified BC3 block: texconv -> AG=(127,127), B=255, ignored R=129.
    # The original sprite PS reads only AG and adds 0.5/255 before the signed remap.
    import struct
    from pathlib import Path
    p=Path(path);data=bytearray(p.read_bytes());start=148 if data[84:88]==b'DX10' else 128
    h,w=struct.unpack_from('<II',data,12)
    if h<16 or w<16 or min(h,w)%4 or data[:4]!=b'DDS ':raise ValueError("Expected block-aligned DXT5nm")
    neutral=bytes((127,127,0,0,0,0,0,0))+struct.pack('<HHI',(10<<11)|(18<<5)|31,(27<<11)|(58<<5)|31,0xaaaaaaaa)
    old=bytes(data[start:start+16])
    # Correct every fully neutral block in all mips, including the flat interior of a ring-shaped field.
    for i in range(start,len(data),16):
        if data[i:i+16]==old:data[i:i+16]=neutral
    # Explicit top-mip eight-texel boundary. Other mips retain filtered detail.
    for y in range(h//4):
        for x in range(w//4):
            if min(x,y,w//4-1-x,h//4-1-y)<2:
                i=start+16*(y*(w//4)+x);data[i:i+16]=neutral
    p.write_bytes(data)
