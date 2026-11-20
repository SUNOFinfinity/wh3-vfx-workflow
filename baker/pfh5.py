import ctypes
import hashlib
import os
import struct
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path,PureWindowsPath
from baker_config import WH3_GAME_DIR,WH3_DATA_DIR

from baker_config import ZSTD_DLL_PATH
_zstd = ctypes.CDLL(ZSTD_DLL_PATH) if ZSTD_DLL_PATH and os.path.exists(ZSTD_DLL_PATH) else None
_zstd_py = None
if _zstd is not None:
    _zstd.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    _zstd.ZSTD_decompress.restype = ctypes.c_size_t
    _zstd.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    _zstd.ZSTD_compress.restype = ctypes.c_size_t
    _zstd.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    _zstd.ZSTD_compressBound.restype = ctypes.c_size_t
    _zstd.ZSTD_isError.argtypes = [ctypes.c_size_t]
    _zstd.ZSTD_isError.restype = ctypes.c_uint
else:
    try:
        import zstandard as _zstd_py
    except ImportError:
        _zstd_py = None


def zstd_backend() -> str:
    if _zstd is not None:
        return ZSTD_DLL_PATH
    if _zstd_py is not None:
        return f"python-zstandard ({getattr(_zstd_py, '__version__', 'installed')})"
    return ""


def zstd_decompress(src: bytes, uncomp_size: int) -> bytes:
    if _zstd is not None:
        dst = ctypes.create_string_buffer(uncomp_size)
        res = _zstd.ZSTD_decompress(dst, uncomp_size, src, len(src))
        if _zstd.ZSTD_isError(res):
            raise RuntimeError(f"ZSTD_decompress failed: code={res}")
        return dst.raw[:res]
    if _zstd_py is not None:
        return _zstd_py.ZstdDecompressor().decompress(src, max_output_size=uncomp_size)
    raise RuntimeError(f"Neither zstd.dll ({ZSTD_DLL_PATH}) nor Python package 'zstandard' is available")


def zstd_compress(src: bytes, level: int = 3) -> bytes:
    if _zstd is not None:
        bound = _zstd.ZSTD_compressBound(len(src))
        dst = ctypes.create_string_buffer(bound)
        res = _zstd.ZSTD_compress(dst, bound, src, len(src), level)
        if _zstd.ZSTD_isError(res):
            raise RuntimeError(f"ZSTD_compress failed: code={res}")
        return struct.pack("<I", len(src)) + dst.raw[:res]
    if _zstd_py is not None:
        comp = _zstd_py.ZstdCompressor(level=level).compress(src)
        return struct.pack("<I", len(src)) + comp
    raise RuntimeError(f"Neither zstd.dll ({ZSTD_DLL_PATH}) nor Python package 'zstandard' is available")


def lz4_frame_decompress(src: bytes) -> bytes:
    if src[:4] != b"\x04\x22\x4d\x18": raise ValueError("Invalid LZ4 frame magic")
    flg = src[4]
    pos = 6
    if flg & 0x08:
        pos += 8
    if flg & 0x01:
        pos += 4
    pos += 1
    out = bytearray()
    while pos + 4 <= len(src):
        bsize = struct.unpack_from("<I", src, pos)[0]
        pos += 4
        if bsize == 0:
            break
        uncomp = bool(bsize & 0x80000000)
        bsize &= 0x7FFFFFFF
        block = src[pos:pos + bsize]
        pos += bsize
        if flg & 0x10:
            pos += 4
        if uncomp:
            out.extend(block)
        else:
            ip = 0
            blen = len(block)
            while ip < blen:
                token = block[ip]
                ip += 1
                lit_len = token >> 4
                if lit_len == 15:
                    while True:
                        b = block[ip]
                        ip += 1
                        lit_len += b
                        if b != 255:
                            break
                out.extend(block[ip:ip + lit_len])
                ip += lit_len
                if ip >= blen:
                    break
                offset = block[ip] | (block[ip + 1] << 8)
                ip += 2
                match_len = (token & 0x0F) + 4
                if (token & 0x0F) == 15:
                    while True:
                        b = block[ip]
                        ip += 1
                        match_len += b
                        if b != 255:
                            break
                start = len(out) - offset
                if not 0 < offset <= len(out):raise ValueError("Invalid LZ4 match offset")
                for k in range(match_len):
                    out.append(out[start + k])
    return bytes(out)


def norm_pack_path(path: str) -> str:
    return path.replace("/", "\\").lstrip("\\")


def decode_payload(raw, comp):
    if not comp:
        return raw
    uncomp_size = struct.unpack_from("<I", raw, 0)[0]
    def checked(data):
        if len(data) != uncomp_size: raise ValueError("PFH5 decoded size mismatch")
        return data
    if raw[4:8] == b"\x28\xb5\x2f\xfd":
        return checked(zstd_decompress(raw[4:], uncomp_size))
    if raw[4:8] == b"\x04\x22\x4d\x18":
        return checked(lz4_frame_decompress(raw[4:]))
    import lzma
    props = raw[4:9]
    lc_lp_pb = props[0]
    pb = lc_lp_pb // 45
    rem = lc_lp_pb % 45
    lp = rem // 9
    lc = rem % 9
    dict_size = struct.unpack_from("<I", props, 1)[0]
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
        "id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": dict_size
    }])
    return checked(dec.decompress(raw[9:], max_length=uncomp_size+1))


def guard_output(out):
    out=Path(out).resolve()
    if any(out.is_relative_to(Path(d).resolve()) for d in (WH3_GAME_DIR,WH3_DATA_DIR)):
        raise ValueError("Refusing to write a game asset: "+str(out))
    return out

@contextmanager
def candidate(out):
    out=guard_output(out)
    out.parent.mkdir(parents=True,exist_ok=True)
    fd,path=tempfile.mkstemp(dir=out.parent,prefix=out.name+".",suffix=".tmp");os.close(fd)
    try:
        yield path
        os.replace(path,out)
    finally:
        Path(path).unlink(missing_ok=True)

def validate_keys(mapping):
    seen=set()
    for name in mapping:
        path=PureWindowsPath(name);key=norm_pack_path(name).lower()
        if not key or path.drive or path.root or ".." in path.parts or "\x00" in name or key in seen:
            raise ValueError("Invalid or duplicate PFH5 virtual path: "+repr(name))
        seen.add(key)

class PFH5Pack:
    def __init__(self, path: str):
        self.path = path
        self.bitmask = 0x3
        self.timestamp = 0
        self.pf_index_bytes = b""
        self.pf_index_count = 0
        self.ext_header = b""
        self.entries = []  # list of (name, offset, size, compressed, ts)
        self._lookup = {}
        self._parse()

    def _parse(self):
        with open(self.path, "rb") as f:
            magic = f.read(4)
            if magic != b"PFH5":
                raise ValueError(f"Not a PFH5 pack: {magic!r}")
            bitmask, pf_cnt, pf_sz, file_cnt, file_idx_sz, ts = struct.unpack("<IIIIII", f.read(24))
            self.bitmask = bitmask
            self.pf_index_count = pf_cnt
            self.timestamp = ts
            if bitmask & 0x10:
                self.ext_header = f.read(20)
            self.pf_index_bytes = f.read(pf_sz)
            idx_bytes = f.read(file_idx_sz)
            data_off = f.tell()

            has_ts = bool(bitmask & 0x40)
            pos = 0
            cur_off = data_off
            for _ in range(file_cnt):
                size = struct.unpack_from("<I", idx_bytes, pos)[0]
                pos += 4
                ets = 0
                if has_ts:
                    ets = struct.unpack_from("<I", idx_bytes, pos)[0]
                    pos += 4
                comp = idx_bytes[pos]
                pos += 1
                end = idx_bytes.index(b"\x00", pos)
                name = idx_bytes[pos:end].decode("utf-8", errors="replace")
                pos = end + 1
                rec = (name, cur_off, size, comp, ets)
                self.entries.append(rec)
                self._lookup[name.replace("/", "\\").lower()] = rec
                cur_off += size
            if pos != len(idx_bytes) or cur_off > os.fstat(f.fileno()).st_size:
                raise ValueError(f"Invalid PFH5 index or payload bounds: {self.path}")

    def has(self, path: str) -> bool:
        return norm_pack_path(path).lower() in self._lookup

    def read_raw(self, path: str) -> tuple[bytes, int]:
        rec = self._lookup[norm_pack_path(path).lower()]
        with open(self.path, "rb") as f:
            f.seek(rec[1])
            return f.read(rec[2]), rec[3]

    def read(self, path: str) -> bytes:
        raw, comp = self.read_raw(path)
        return decode_payload(raw, comp)

    def write_updated(self, out_path: str, replacements: dict[str, bytes], compress_threshold: int = 0) -> dict:
        """Write a new PFH5 pack preserving all untouched entries byte-for-byte,
        replacing or inserting entries from `replacements` (keys normalized to backslashes).
        """
        validate_keys(replacements)
        norm_rep = {norm_pack_path(k).lower(): (norm_pack_path(k), v) for k, v in replacements.items()}
        has_ts = bool(self.bitmask & 0x40)
        now_ts = int(time.time())

        final_items = []  # (path_str, raw_bytes_to_write, comp_flag, ts, expected_sha256_uncomp)
        seen = set()

        with open(self.path, "rb") as src_f:
            for name, off, size, comp, ets in self.entries:
                key = norm_pack_path(name).lower()
                seen.add(key)
                if key in norm_rep:
                    rep_name, rep_data = norm_rep[key]
                    if compress_threshold > 0 and len(rep_data) >= compress_threshold:
                        payload = zstd_compress(rep_data, level=3)
                        cflag = 1
                    else:
                        payload = rep_data
                        cflag = 0
                    final_items.append((rep_name, payload, cflag, now_ts if has_ts else 0, hashlib.sha256(rep_data).hexdigest()))
                else:
                    src_f.seek(off)
                    raw = src_f.read(size)
                    final_items.append((name, raw, comp, ets, None))

        for key, (rep_name, rep_data) in sorted(norm_rep.items()):
            if key not in seen:
                if compress_threshold > 0 and len(rep_data) >= compress_threshold:
                    payload = zstd_compress(rep_data, level=3)
                    cflag = 1
                else:
                    payload = rep_data
                    cflag = 0
                final_items.append((rep_name, payload, cflag, now_ts if has_ts else 0, hashlib.sha256(rep_data).hexdigest()))

        # Build file index
        idx_buf = bytearray()
        for name, payload, cflag, ets, _ in final_items:
            idx_buf.extend(struct.pack("<I", len(payload)))
            if has_ts:
                idx_buf.extend(struct.pack("<I", ets))
            idx_buf.append(cflag)
            idx_buf.extend(name.encode("utf-8") + b"\x00")

        with candidate(out_path) as tmp_path:
            with open(tmp_path, "wb") as out_f:
                out_f.write(b"PFH5")
                out_f.write(struct.pack(
                    "<IIIIII",
                    self.bitmask,
                    self.pf_index_count,
                    len(self.pf_index_bytes),
                    len(final_items),
                    len(idx_buf),
                    now_ts if self.timestamp else 0,
                ))
                if self.bitmask & 0x10:
                    out_f.write(self.ext_header)
                out_f.write(self.pf_index_bytes)
                out_f.write(idx_buf)
                for _, payload, _, _, _ in final_items:
                    out_f.write(payload)



            # Mandatory reopen & readback SHA-256 verification!
            rb = PFH5Pack(tmp_path)
            assert len(rb.entries) == len(final_items), f"Entry count mismatch: {len(rb.entries)} != {len(final_items)}"
            verified = 0
            for name, _, _, _, expected_sha in final_items:
                if expected_sha is not None:
                    got = rb.read(name)
                    got_sha = hashlib.sha256(got).hexdigest()
                    if got_sha != expected_sha:
                        raise RuntimeError(f"Readback SHA mismatch for {name}: {got_sha} != {expected_sha}")
                    verified += 1
        return {
            "out_path": out_path,
            "total_entries": len(rb.entries),
            "verified_written": verified,
            "pack_bytes": os.path.getsize(out_path),
        }


def write_standalone_pfh5(out_path: str, assets: dict[str, bytes]) -> dict:
    """Write a clean standalone PFH5 MOD pack (uncompressed, zero external baseline needed)
    with immediate reopen & SHA-256 readback verification. Ideal for L1+L2 IR sandbox previews
    and brand-new standalone VFX test packs.
    """
    bitmask = 0x3  # Mod pack, no ext_header, no per-entry timestamps
    now_ts = int(time.time())
    validate_keys(assets)
    items = []
    for k, data in sorted(assets.items(), key=lambda kv: norm_pack_path(kv[0]).lower()):
        np_name = norm_pack_path(k)
        items.append((np_name, data, hashlib.sha256(data).hexdigest()))

    idx_buf = bytearray()
    for name, payload, _ in items:
        idx_buf.extend(struct.pack("<I", len(payload)))
        idx_buf.append(0)  # uncompressed
        idx_buf.extend(name.encode("utf-8") + b"\x00")

    with candidate(out_path) as tmp_path:
        with open(tmp_path, "wb") as out_f:
            out_f.write(b"PFH5")
            out_f.write(struct.pack("<IIIIII", bitmask, 0, 0, len(items), len(idx_buf), now_ts))
            out_f.write(idx_buf)
            for _, payload, _ in items:
                out_f.write(payload)


        rb = PFH5Pack(tmp_path)
        assert len(rb.entries) == len(items)
        for name, _, expected_sha in items:
            got_sha = hashlib.sha256(rb.read(name)).hexdigest()
            if got_sha != expected_sha:
                raise RuntimeError(f"Standalone PFH5 SHA mismatch for {name}")
    return {
        "out_path": out_path,
        "total_entries": len(rb.entries),
        "verified_written": len(items),
        "pack_bytes": os.path.getsize(out_path),
    }
