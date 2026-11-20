import argparse,hashlib
from action_io import *
from motion_math import fk
def extract(a):
 s=Assets(a.pack,a.game_data);r=s.select(a.anim,a.unit,a.table,a.anim_meta,a.sound_meta);raw,src=s.read(r['anim']);skpath='animations/skeletons/'+r['skeleton']+'.anim';rest,restsrc=s.read(skpath)
 if not raw or not rest:raise ValueError('Missing clip or bind skeleton: '+r['anim']+' / '+skpath)
 clip,bind=fmt.load(raw),fmt.load(rest)
 if clip.version not in (6,7,8) or clip.skeleton_name.lower()!=r['skeleton'].lower():raise ValueError('Unsupported or mismatched clip skeleton')
 bones=bind.bones;names=[b.name for b in bones];ids={n:i for i,n in enumerate(names)};parents=np.array([b.parent for b in bones],dtype='i4')
 if len(ids)!=len(bones):raise ValueError('Duplicate bind-pose bone names')
 dense=fmt.resolve(bind);all_t=[];all_q=[]
 for part_id,part in enumerate(clip.parts):
  track=fmt.resolve(clip,part_id);F=len(track.translations) if part.dynamic_frames else max(1,part.declared_frame_count or round(clip.duration*clip.frame_rate)+1)
  t=np.tile(dense.translations[0],(F,1,1));q=np.tile(dense.rotations[0],(F,1,1))
  for j,b in enumerate(clip.bones):
   i=ids[b.name]
   if (clip.bones[b.parent].name if b.parent!=-1 else None)!=(bones[parents[i]].name if parents[i]!=-1 else None):raise ValueError('Clip/rig hierarchy disagrees at '+b.name)
   if track.has_translation[j]:t[:,i]=track.translations[:,j]
   if track.has_rotation[j]:q[:,i]=track.rotations[:,j]
  all_t.append(t);all_q.append(q)
 if not np.isfinite(a.unit_scale) or a.unit_scale<=0:raise ValueError('unit-scale must be positive finite')
 pp,qq=fk(np.concatenate(all_t).astype('f8')*a.unit_scale,np.concatenate(all_q),parents)
 if not np.isfinite(pp).all() or clip.frame_rate<=0:raise ValueError('Invalid pose data or FPS')
 F=len(pp);times=np.arange(F)/clip.frame_rate;vel=np.gradient(pp,1/clip.frame_rate,axis=0) if F>1 else pp*0
 roots=np.flatnonzero(parents<0);root=int(roots[0]);wanted=[x.strip() for x in a.weapon_bones.split(',') if x.strip()] if a.weapon_bones else [n for n in names if any(v in n.lower() for v in ('weapon','be_prop','muzzle'))]
 weapon=[ids[n] for n in wanted];events=[];metas={}
 for role,path in [('animation',r['meta']),('sound',r['sound'])]:
  if not path:continue
  b,provenance=s.read(path)
  if b is None:raise ValueError('Referenced metadata missing: '+path)
  info,_=meta_codec(b);metas[role]=dict(path=path,source=provenance,roundtrip=info['roundtrip'],sha256=hashlib.sha256(b).hexdigest())
  for ev in info['events']:
   idx=ev.get('NodeIndex',ev.get('BoneIndex',ev.get('BoneId')));ev.update(source=role,bone_name=names[idx] if isinstance(idx,int) and 0<=idx<len(names) else None);events.append(ev)
 anchors={}
 for i in weapon:
  speed=np.linalg.norm(vel[:,i],axis=-1);j=int(speed.argmax());anchors[names[i]]=dict(peak_speed_frame=j,peak_speed_mps=float(speed[j]),peak_speed_sec=float(times[j]),highest_frame=int(pp[:,i,1].argmax()),lowest_frame=int(pp[:,i,1].argmin()))
 feet=[i for i,n in enumerate(names) if 'foot' in n.lower() or 'ankle' in n.lower()]
 meta=dict(**r,anim_path=r['anim'],anim_source=src,anim_sha256=hashlib.sha256(raw).hexdigest(),meta_sources=metas,skeleton_source=restsrc,bind_sha256=hashlib.sha256(rest).hexdigest(),anim_version=clip.version,fps=clip.frame_rate,frame_count=F,duration_sec=clip.duration,sampled_end_sec=float(times[-1]),unit_scale=a.unit_scale,scale_source=a.scale_source or 'unconfirmed model-space scale',space='actor model space, CA right-handed Y-up metres; original X/Z signs preserved. No campaign/battle actor transform.',bones=[dict(name=n,index=i,parent=int(parents[i])) for i,n in enumerate(names)],events=events,key_anchors=anchors,weapon_bones=wanted,root_bone=names[root],foot_low_candidates={names[i]:int(pp[:,i,1].argmin()) for i in feet})
 d=guard_output(a.out_dir);d.mkdir(parents=True,exist_ok=True);dump(d/'action_motion.json',meta)
 np.savez_compressed(d/'action_motion.npz',times=times,bone_pos=pp.astype('f'),bone_quat=qq.astype('f'),bone_vel=vel.astype('f'),parents=parents,bone_names=np.array(names),root_motion=(pp[:,root]-pp[0,root]).astype('f'),weapon_indices=np.array(weapon,dtype='i4'),foot_indices=np.array(feet,dtype='i4'))
 dump(d/'houdini_motion_guides.json',dict(npz='action_motion.npz',fps=clip.frame_rate,frame_origin=1,units='metres',up='Y',attributes={'P':'bone_pos','orient':'bone_quat (xyzw)','v':'bone_vel','u':'normalized clip progress'},weapon_bones=wanted,anchors=anchors))
 print(f'{r["slot"]}: {F} frames @ {clip.frame_rate:g} fps, {len(names)} bones, {len(events)} events -> {d}')
if __name__=='__main__':
 p=argparse.ArgumentParser(description='Decode a real action with its bind pose; export model-space Y-up FK.');base_args(p);p.add_argument('--anim',required=True);p.add_argument('--out-dir',required=True);p.add_argument('--weapon-bones');p.add_argument('--scale-source',help='Evidence for resolved unit/variant scale');p.add_argument('--anim-meta');p.add_argument('--sound-meta');p.add_argument('--unit-scale',type=float,default=1.);extract(p.parse_args())

