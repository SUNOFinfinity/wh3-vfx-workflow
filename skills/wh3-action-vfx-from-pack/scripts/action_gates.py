"""Action-specific gates on compiled emitter settings (before rendering the joint L2)."""
import xml.etree.ElementTree as ET
import numpy as np
import wh3_true_preview as l2

def values(curve):
    return [v for pts in curve.values() for _,v in pts]

def varying(curve):
    return any(len(pts)>1 and np.ptp([v for _,v in pts])>1e-7 for pts in curve.values())

def tiers(em):
    fields={}
    for prop in em:
        if prop.tag in ('property','ranged_property'):
            fields.setdefault(prop.get('quality','0'),{})[prop.get('name')]=prop
    for q in sorted(fields):
        yield q,{**fields.get('0',{}),**fields[q]}

def check_emitter(em):
    checks=[]
    for quality,p in tiers(em):
        tag=f"{em.get('id')} [quality {quality}]"
        def curve(name):
            if name not in p:return {}
            data=l2.parse_ca_curve(p[name].text or '')
            if not data or not values(data) or not np.isfinite(values(data)).all():raise ValueError(f'{tag}: unreadable {name}')
            return data
        rot=curve('EmitterOrientation');pos=curve('EmitterPosition');rigid=em.get('type')=='continuous_rigid'
        single=int(p['MaximumParticles'].get('value'))==1 if 'MaximumParticles' in p else True
        if single:
            if 'LifeTime' not in p or 'ParticleLifeTime' not in p:raise ValueError(f'{tag}: missing one-particle lifetime evidence')
            emlife=float(p['LifeTime'].attrib['max']);lc=curve('ParticleLifeTime')
            if not lc.get('x_min'):raise ValueError(f'{tag}: missing minimum particle lifespan')
            plife=min(v for _,v in lc['x_min']);limit=plife-.15 if varying(rot) else min(.05,.4*plife)
            if not (np.isfinite(emlife) and 0<=emlife<plife and emlife<=limit+1e-7):raise ValueError(f'{tag}: EmLife {emlife:g} must be < PLife {plife:g} and <= {limit:g} s')
            if rigid and varying(pos):raise ValueError(f'{tag}: EmitterPosition is sampled only at birth; use tracking or AnimFile for an existing rigid')
        space=p['ParticleSimulationSpace'].get('value') if 'ParticleSimulationSpace' in p else ''
        if rigid and space=='LOCAL_SPACE_POSITION_ROTATION' and any(abs(v)>1e-7 for v in values(pos)) and any(abs(v)>1e-7 for v in values(rot)):
            raise ValueError(f'{tag}: off-axis EmitterPosition with carrier rotation; center pivot or move spin to particle/AnimFile')
        checks.append(dict(emitter=em.get('id'),quality=quality,single_particle=single))
    return checks

def sort_gate(rr,cache,emitters,pairs):
    from collections import defaultdict
    ids=defaultdict(list)
    for e in emitters:ids[e.id].append(e)
    masks={e.id for e in emitters if cache.get(e.wsmodel,{}).get('native_kind')=='opaque'}
    glow={e.id for e in emitters if e.wsmodel in cache and not cache[e.wsmodel].get('native_kind')}
    edges=[]
    for pair in pairs:
        parts=pair.split(':')
        if len(parts)!=2 or parts[0]==parts[1]:raise ValueError('Use --sort-pair mask_id:glow_id')
        edges.append(tuple(parts))
    if masks and glow and len(masks)==len(glow)==1 and not edges:edges.append((next(iter(masks)),next(iter(glow))))
    if glow and masks-{m for m,g in edges}:raise ValueError('Identify the participating rigid emitters with --sort-pair mask_id:glow_id (repeat for additional layers)')
    def layer(name):
        if len(ids[name])!=1 or ids[name][0].wsmodel not in cache:raise ValueError('Missing/ambiguous rigid sort layer: '+name)
        e=ids[name][0];m=ET.fromstring(rr.read(e.wsmodel));mp=m.findtext('.//material');root=ET.fromstring(rr.read(mp))
        if root.find('sort_override') is None:raise ValueError(name+': explicit material sort_override required')
        M=l2.mat4_trs(e.wrapper_pos,e.wrapper_rot_deg,np.full(3,e.wrapper_scale))
        # Emission offset + local RMV2 pivot. Compare the full range, not just its midpoint.
        pivot=np.array(cache[e.wsmodel]['gpu_mesh']['pivot'],float)
        def origin(side):
            xyz=[]
            for axis in 'xyz':
                pts=e.pos_curves.get(axis+'_'+side,[(0.,0.)])
                if varying({'axis':pts}):raise ValueError(name+': moving sort origin must be baked to a common pivot')
                xyz.append(pts[0][1])
            return (M@np.append(pivot+xyz,1))[:3]
        return int(root.findtext('sort_override')),[origin('min'),origin('max')]
    for m,g in edges:
        sm,pm=layer(m);sg,pg=layer(g)
        if sm>=sg or not np.allclose(pm,pg,atol=1e-5,rtol=0):raise ValueError(f'{m}:{g}: mask and glow need coincident pivots and increasing explicit sort_override')
    return edges

def gates(rr,wrapper,es,cache,looping,sort_pairs):
    name='vfx/'+wrapper+'.xml';root=l2.parse_wh3_vfx_xml(rr.read(name))
    checks=[]
    for ref in root.findall('./vfx_references/vfx'):
        ids={x.get('name') for x in ref.findall('emitter') if x.get('enable','').lower()=='true'}
        if not ids:continue
        if not looping and ref.get('infinite_life','').lower()!='false':raise ValueError('One-shot wrapper must explicitly set infinite_life="false"')
        lib=ref.get('vfx_ref','');path=lib if lib.lower().endswith('.xml') else 'vfx/'+lib+'.xml'
        buf=rr.read(path)
        if buf is None:raise ValueError('Missing active library: '+path)
        nodes=[e for e in l2.parse_wh3_vfx_xml(buf).findall('./vfx/emitters/emitter') if e.get('id') in ids]
        if {e.get('id') for e in nodes}!=ids:raise ValueError('Missing enabled emitter in '+path)
        for e in nodes:checks.extend(check_emitter(e))
    for e in root.findall('./vfx/emitters/emitter'):checks.extend(check_emitter(e))
    return dict(tiers=checks,sort_pairs=sort_gate(rr,cache,es,sort_pairs),looping=looping)

