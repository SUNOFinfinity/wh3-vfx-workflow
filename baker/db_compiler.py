import struct
import uuid


def registration_keys(manifest, library_keys):
    extra=(key for w in manifest["host_binding"]["wrappers"] for key in w.get("particle_effects_table_keys", []))
    return list(dict.fromkeys([*library_keys,*extra]))


def build_particle_effects_table(keys: list[str], guid_str: str | None = None) -> bytes:
    """Build a byte-exact WH3 db/particle_effects_tables binary file.
    Verified against WH3 9.0.5.0 db.pack::db/particle_effects_tables/data__ (2,084 rows; no version marker).
    """
    if guid_str is None:
        guid_str = str(uuid.uuid5(uuid.NAMESPACE_DNS, "wh3_vfx_baker:" + ",".join(keys)))
    guid_u16 = guid_str.encode("utf-16-le")
    out = bytearray()
    out.extend(b"\xfd\xfe\xfc\xff")
    out.extend(struct.pack("<H", len(guid_str)))
    out.extend(guid_u16)
    out.append(1)
    out.extend(struct.pack("<I", len(keys)))
    for k in keys:
        kb = k.encode("utf-8")
        out.extend(struct.pack("<H", len(kb)))
        out.extend(kb)
    return bytes(out)
