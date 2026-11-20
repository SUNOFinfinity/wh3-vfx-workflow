"""Plan an animation EFFECT event; --apply additionally requires the exact reviewed inputs and a recoverable target."""
import argparse,hashlib,json,os,tempfile,shutil,struct
from pathlib import Path
from contextlib import contextmanager
from action_io import Assets,PFH5Pack,key,pack,unpack,meta_codec,np,guard_output,WH3_GAME_DIR,WH3_DATA_DIR
from motion_math import eulerq
def vec(s):
 v=[float(x) for x in s.split(',')]
 if len(v)!=3 or not np.isfinite(v).all():raise argparse.ArgumentTypeError('Expected three finite values')
 return v
def boolean(s):
 if s.lower() not in ('true','false'):raise argparse.ArgumentTypeError('Use true or false')
 return s.lower()=='true'
def digest(path):
 with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def backup(target,base,expected):
 if base.exists():
  if os.path.samefile(target,base) or digest(base)!=expected:raise ValueError('Baseline is not the current pre-event pack. Select a new, distinct backup after the VFX bake.')
 else:
  base.parent.mkdir(parents=True,exist_ok=True)
  with target.open('rb') as i,base.open('xb') as o:shutil.copyfileobj(i,o)
  if digest(base)!=expected:raise ValueError('Backup verification failed')
def verify_events(info,q):
 rows=[r for r in info['events'] if r['tag']=='EFFECT' and r.get('VfxName')==q['key']]
 if len(rows)!=1:raise ValueError('Expected exactly one matching EFFECT on readback')
 e=rows[0]
 for field,val in {'StartTime':q['start'],'EndTime':q['end'],'EffectStartTime':q['start'],'EffectEndTime':q['end'],'Tracking':q['tracking'],'NodeIndex':q['bone'],'Position':q['pos'],'Orientation':q['quat']}.items():
  if not np.allclose(e[field],val,atol=1e-6,rtol=1e-6):raise ValueError(f'Metadata readback mismatch: {field} {e[field]} != {val}')
def run(a):
 target=Path(a.pack).resolve(strict=True);base=Path(a.baseline_pack).resolve() if a.baseline_pack else None
 if a.apply and not (a.review and base):raise ValueError('--apply requires --review and --baseline-pack')
 if a.apply:
  for p in (target,base):
   guard_output(p)
   if p.is_relative_to(Path(a.game_data or WH3_DATA_DIR).resolve()):raise ValueError('Game data is read only')
  if target==base:raise ValueError('Target cannot be its backup')
 pre=digest(target) if a.apply else None;s=Assets(target,a.game_data);n=key(a.anim_meta);copies=s.copies(n)
 if not n.endswith('.anm.meta') or not copies:raise ValueError('Metadata must already exist in the target, loose or in an animpack. Do not patch a shared vanilla event implicitly.')
 raw,src=s.read(n)
 if a.bone_index < -1 or not 0<=a.start<=a.end or not np.isfinite([a.start,a.end]).all():raise ValueError('Invalid bone or event interval')
 if a.bone_index==-1 and a.tracking:raise ValueError('World anchor has no tracking bone; use --tracking false')
 q=dict(key=a.vfx_key,start=a.start,end=a.end,bone=a.bone_index,pos=a.offset_pos,quat=eulerq(a.offset_rot).tolist(),tracking=a.tracking)
 if not s.target.has('vfx/'+a.vfx_key+'.xml'):raise ValueError('Bake the reviewed wrapper into this working pack before installing the action event.')
 if a.review:
  rev=json.loads(Path(a.review).read_text('utf8'));required={'wrapper':a.vfx_key,'meta':n,'bone':a.bone_index,'start':a.start,'end':a.end,'tracking':a.tracking,'offset_pos':a.offset_pos,'offset_rot':a.offset_rot}
  if rev['status']!='PASS' and not (rev['status']=='PARTIAL' and a.accept_partial):raise ValueError('Review is not PASS. Resolve omissions, or explicitly accept PARTIAL with --accept-partial after manual review.')
  for k,v in required.items():
   if rev.get(k)!=v:raise ValueError('Reviewed event differs: '+k)
  motion=json.loads(Path(rev['motion']).with_name('action_motion.json').read_text('utf8'))
  if a.bone_index>=len(motion['bones']) or (a.bone_index>=0 and rev['bone_name']!=motion['bones'][a.bone_index]['name']) or a.end>motion['duration_sec']+1e-5:raise ValueError('Bone or time is outside reviewed animation')
  cur=s.select(rev['anim'],table=motion['table'])
  if cur['meta']!=n or cur['binding_evidence']!='fragment':raise ValueError('Fragment no longer references this metadata')
  clip,_=s.read(cur['anim']);bind,_=s.read('animations/skeletons/'+cur['skeleton']+'.anim')
  if hashlib.sha256(clip).hexdigest()!=rev['anim_sha256'] or hashlib.sha256(bind).hexdigest()!=rev['bind_sha256']:raise ValueError('Clip or bind pose changed after review')
  if rev.get('model'):
   from export_action_model import Models
   mr=Models(s)
   for path,e in rev['model']['assets'].items():
    if hashlib.sha256(mr.get(path)).hexdigest()!=e['sha256']:raise ValueError('Reviewed actor appearance changed: '+path)
  approved=PFH5Pack(rev['sandbox'])
  reviewed_digest=rev.get('sandbox_sha256')
  if not reviewed_digest or digest(rev['sandbox'])!=reviewed_digest:raise ValueError('Sandbox changed since the joint preview; render it again')
  for name,*_ in approved.entries:
   if not s.target.has(name) or s.target.read(name)!=approved.read(name):raise ValueError('Target is missing or differs from reviewed sandbox payload: '+name)
 info,edited=meta_codec(raw,q);verify_events(info,q)
 # Idempotent return is safe even with an existing original backup, which now differs from current target.
 changes={};same=(edited==raw)
 for label,_ in copies:
  if label=='loose':changes[s.loose[n]]=edited
  else:
   orig=s.target.read(label);rows=s.containers[label]
   if pack(rows)!=orig:raise ValueError('Unmodified animpack does not round-trip: '+label)
   blob=pack([(k,edited if key(k)==n else v) for k,v in rows])
   newrows=unpack(blob)
   if len(newrows)!=len(rows) or any(x!=u or (edited if key(x)==n else y)!=z for (x,y),(u,z) in zip(rows,newrows)):raise ValueError('Nested inventory changed unexpectedly')
   changes[label]=blob
 report=dict(mode='unchanged' if same else ('apply' if a.apply else 'dry-run'),target=str(target),metadata=n,copies=[c for c,_ in copies],effect=q,checked_metadata_roundtrip=True)
 if a.apply and not same:
  fd,p=tempfile.mkstemp(prefix='.action-',suffix='.pack',dir=target.parent);os.close(fd);candidate=Path(p)
  try:
   s.target.write_updated(str(candidate),{})
   # PFH5Pack normally stamps a build date. For the identity trial retain the source timestamp.
   with candidate.open('r+b') as noop:noop.seek(24);noop.write(struct.pack('<I',s.target.timestamp))
   if digest(candidate)!=pre:raise ValueError('Unmodified PFH5 is not byte-exact; target and baseline left untouched')
   backup(target,base,pre)
   stats=s.target.write_updated(str(candidate),changes)
   check=Assets(candidate,a.game_data)
   if set(check.loose)!=set(s.loose):raise ValueError('PFH5 inventory changed unexpectedly')
   changed={key(x):v for x,v in changes.items()}
   for k in s.loose:
    if k not in changed and s.target.read_raw(k)!=check.target.read_raw(k):raise ValueError('Unrelated raw payload changed: '+k)
    expected=changed[k] if k in changed else s.target.read(k)
    if hashlib.sha256(check.target.read(k)).digest()!=hashlib.sha256(expected).digest():raise ValueError('PFH5 entry SHA mismatch: '+k)
   rb=check.copies(n)
   if [x for x,_ in rb]!=[x for x,_ in copies] or any(v!=edited for _,v in rb):raise ValueError('Nested/loose readback differs')
   verify_events(meta_codec(rb[0][1])[0],q)
   if digest(target)!=pre:raise ValueError('Target changed concurrently; candidate was not published')
   os.replace(candidate,target)
   final=Assets(target,a.game_data)
   if final.copies(n)!=rb:raise IOError('Published target differs; retain backup and stop')
   report.update(backup=str(base),checked_pfh5_roundtrip=True,sha256_verified_entries=len(check.loose),changed_entries=stats['verified_written'])
  finally:candidate.unlink(missing_ok=True)
 print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pack',required=True);p.add_argument('--baseline-pack');p.add_argument('--anim-meta',required=True);p.add_argument('--vfx-key',required=True)
 p.add_argument('--start',type=float,required=True);p.add_argument('--end',type=float,required=True);p.add_argument('--bone-index',type=int,required=True);p.add_argument('--offset-pos',type=vec,default=[0.,0.,0.]);p.add_argument('--offset-rot',type=vec,default=[0.,0.,0.]);p.add_argument('--tracking',type=boolean,required=True)
 p.add_argument('--game-data');p.add_argument('--review');p.add_argument('--apply',action='store_true');p.add_argument('--accept-partial',action='store_true');run(p.parse_args())

