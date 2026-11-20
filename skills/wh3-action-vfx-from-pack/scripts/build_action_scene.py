"""Create a new Houdini scene with textured actor and separately switchable guides. Run with hython."""
import argparse,json,os
from pathlib import Path
import hou
from action_io import guard_output

def build(a):
    if hou.isUIAvailable():raise ValueError('Use a fresh hython process; never replace an open interactive scene')
    hip=guard_output(a.out)
    if hip.exists():raise ValueError('Scene already exists; choose a new output name')
    hip.parent.mkdir(parents=True,exist_ok=True);mp=Path(a.motion).resolve();meta=json.loads(mp.with_suffix('.json').read_text('utf8'));s=Path(__file__).resolve().parent
    def hipfile(p):return '$HIP/'+Path(os.path.relpath(p,hip.parent)).as_posix() if p.drive==hip.drive else p.as_posix()
    # Set HIP first so relative inputs resolve on their very first cook.
    hou.hipFile.setName(str(hip));hou.putenv('HIP',str(hip.parent));hou.setFps(meta['fps']);hou.playbar.setFrameRange(1,meta['frame_count']);hou.playbar.setPlaybackRange(1,meta['frame_count']);hou.setFrame(1)
    def sop(name,code,params):
        obj=hou.node('/obj').createNode('geo',name);n=obj.createNode('python','from_pack');g=n.parmTemplateGroup()
        for k in params:g.append(hou.StringParmTemplate(k,k.title(),1))
        n.setParmTemplateGroup(g)
        for k,v in params.items():n.parm(k).set(v)
        n.parm('python').set((s/code).read_text('utf8'));n.setDisplayFlag(True);n.setRenderFlag(True);obj.layoutChildren();return obj,n
    guides,gn=sop('action_guides','houdini_motion_sop.py',dict(motion=hipfile(mp)))
    if a.model:
        ap=Path(a.model).resolve();info=json.loads(ap.with_suffix('.json').read_text('utf8'))
        for i,part in enumerate(info['parts']):
            mat=hou.node('/mat').createNode('principledshader::2.0',f'wh3_actor_part_{i:02d}');mat.parm('basecolor_useTexture').set(1);mat.parm('basecolor_texture').set(hipfile(ap.parent/part['texture']))
        actor,an=sop('action_model','houdini_actor_sop.py',dict(model=hipfile(ap),motion=hipfile(mp),scripts=s.as_posix()))
        gm=an.geometry();expected=sum(v['triangles'] for v in info['parts'])
        if an.errors() or gm is None or gm.intrinsicValue('primitivecount')!=expected:raise ValueError(f'{an.errors()} / expected {expected} triangles')
        guides.setDisplayFlag(False)
    if gn.geometry() is None or gn.errors():raise ValueError(str(gn.errors()))
    hou.node('/obj').layoutChildren();hou.hipFile.save(str(hip));print(hip)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--motion',required=True);p.add_argument('--model');p.add_argument('--out',required=True);build(p.parse_args())

