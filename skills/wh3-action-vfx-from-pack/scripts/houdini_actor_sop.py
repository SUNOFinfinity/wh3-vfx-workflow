"""Embedded by build_action_scene.py. Geometry, normals and UV all use the same sampled pose as L2."""
import json,sys
from pathlib import Path
import hou,numpy as np
node=hou.pwd();geo=node.geometry();sys.path.insert(0,node.evalParm('scripts')) if node.evalParm('scripts') not in sys.path else None
from actor_skin import Skin
from motion_math import pose
mp=Path(node.evalParm('motion'));ap=Path(node.evalParm('model'));files=[mp,ap,mp.with_suffix('.json'),ap.with_suffix('.json')]
sig=tuple((str(f),f.stat().st_mtime_ns) for f in files);cached=node.cachedUserData('skin_cache')
if not cached or cached[0]!=sig:
    with np.load(mp,allow_pickle=False) as z:ts,pp,qq=(z[k] for k in ('times','bone_pos','bone_quat'))
    skin=Skin(ap,json.loads(mp.with_suffix('.json').read_text('utf8')));cached=(sig,skin,ts,pp,qq);node.setCachedUserData('skin_cache',cached)
_,skin,ts,pp,qq=cached
p,q=pose(ts,pp,qq,(hou.frame()-1)/hou.fps());posed=skin.deform(p,q)
geo.createPoints(np.vstack([p for p,n in posed]).tolist())
for name,arr in [('N',np.vstack([n for p,n in posed])),('Cd',np.vstack([b['Cd'] for b in skin.parts]))]:
    geo.addAttrib(hou.attribType.Point,name,(0.,0.,0.));geo.setPointFloatAttribValuesFromString(name,np.ascontiguousarray(arr,'f').tobytes())
uv=np.vstack([b['uv'] for b in skin.parts]);uv[:,1]=1-uv[:,1]
geo.addAttrib(hou.attribType.Point,'uv',(0.,0.,0.));geo.setPointFloatAttribValuesFromString('uv',np.ascontiguousarray(np.c_[uv,np.zeros(len(uv))],'f').tobytes())
geo.addAttrib(hou.attribType.Prim,'shop_materialpath','');mats=[];start=0
for i,b in enumerate(skin.parts):
    geo.createPolygons((b['tri'][:,::-1].astype('i4')+start).tolist());start+=len(b['p'])
    mats.extend([f'/mat/wh3_actor_part_{i:02d}']*len(b['tri']))
geo.setPrimStringAttribValues('shop_materialpath',mats)

