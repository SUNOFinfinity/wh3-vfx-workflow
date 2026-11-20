"""Validate only explicitly declared host scenes. The caller supplies a read-only baseline."""
from pathlib import Path
import json,subprocess,shutil
from baker_config import CSC_CODEC_PATH,WH3_DATA_DIR
from pfh5 import PFH5Pack
from vanilla_index import VanillaIndex

def check_hosts(manifest,baseline=None):
    declared=[w for w in manifest['host_binding']['wrappers'] if w.get('host_csc')]
    if not declared:return []
    custom=PFH5Pack(baseline) if baseline else None
    db=VanillaIndex([Path(WH3_DATA_DIR)/n for n in ('data_bl.pack','data.pack','data_maps.pack')]);found={}
    try:
        for wrapper in declared:
            path=wrapper['host_csc']; key=wrapper['vfx_key']
            if path not in found:
                raw=custom.read(path) if custom and custom.has(path) else db.read(path)
                if raw is None:raise ValueError(f"host_csc not found: {path}")
                node=shutil.which('node')
                if not node or not Path(CSC_CODEC_PATH).is_file():raise ValueError("host_csc needs node on PATH and the installed Assembly Kit bridge CSC_CODEC_PATH")
                res=subprocess.run([node,str(Path(__file__).with_name('csc_vfx_keys.mjs')),CSC_CODEC_PATH],input=raw,capture_output=True)
                if res.returncode:raise ValueError(f"CSC decode failed for {path}: {res.stderr.decode(errors='replace')[-700:]}")
                found[path]=json.loads(res.stdout)
            if key not in found[path]:raise ValueError(f"{path}: VFX_ELEMENT references {found[path]}, expected {key}")
    finally:db.close()
    return found

