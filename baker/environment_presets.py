"""Battle exposure records, distinct from the approximate L2 terrain and sky colours."""
from pathlib import Path
import xml.etree.ElementTree as ET
from baker_config import WH3_DATA_DIR
from pfh5 import PFH5Pack
NATIVE = {
    'snow_prologue':r'weather\battle\battle_prologue_02.environment',
    'chaos_arbaal':r'weather\battle\wh3_qb_arbaal_spooky_v04.environment',
    'dark_forge':r'weather\battle\wh_forge_02.environment',
    'middenheim':r'weather\battle\middenheim_01.environment_group',
}
NAMES=('daylight','dusk',*NATIVE)
def parameters(name):
    if name not in NATIVE:return {'source':'legacy studio exposure'}
    path=NATIVE[name]; root=ET.fromstring(PFH5Pack(Path(WH3_DATA_DIR)/'data.pack').read(path))
    if root.tag=='environment_group_file':root=root.find('environments/environment')
    bloom=root.find('bloom');tm=root.find('tone_mapping')
    return dict(source='data.pack::'+path,bloom_threshold=float(bloom.get('bloom_threshold')),bloom_strength=float(bloom.get('bloom_strength')),bloom_emissive_strength=float(bloom.get('bloom_emissive_strength')),bloom_water_strength=float(bloom.get('bloom_water_strength')),bloom_particle_strength=float(bloom.get('bloom_particle_strength')),tm_brightness=float(tm.get('brightness')),tm_burn=float(tm.get('burn')),sharpen=float(root.find('post_processing').get('sharpening_strength')),auto_exposure=tm.get('auto_exposure'))

