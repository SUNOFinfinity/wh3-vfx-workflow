"""Offline registration diagnostic: native pack renderer + model-space FK guides in its VP matrix."""
import argparse,hashlib,json,math,subprocess
from pathlib import Path
from action_io import dump,guard_output,np
from action_gates import gates
from motion_math import pose,qmat,eulerq,yeuler
import wh3_true_preview as l2
from baker_config import FFMPEG_PATH
from PIL import Image,ImageDraw,ImageFont
def triple(s):
 x=[float(v) for v in s.split(',')]
 if len(x)!=3 or not np.isfinite(x).all():raise argparse.ArgumentTypeError('Expected finite x,y,z')
 return x
def boolean(s):
 if str(s).lower() not in ('true','false'):raise argparse.ArgumentTypeError('Use true or false')
 return str(s).lower()=='true'
def digest(path):
 with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def run(a):
 initial_digest=digest(a.pack)
 d=guard_output(a.out_dir);d.mkdir(parents=True,exist_ok=True);npz=Path(a.motion);m=json.loads(npz.with_name('action_motion.json').read_text('utf8'))
 with np.load(npz,allow_pickle=False) as z:ts,pos,quat,names,parents,weapons,feet=[z[k] for k in ('times','bone_pos','bone_quat','bone_names','parents','weapon_indices','foot_indices')]
 names=list(names);world=a.attach_bone=='world';idx=-1 if world else names.index(a.attach_bone);start=a.vfx_start;end=start if a.vfx_end is None else a.vfx_end
 if a.tracking is None:a.tracking=not world
 if world and a.tracking:raise ValueError('world has no tracking bone; use --tracking false')
 if not np.isfinite(a.camera_distance) or a.camera_distance<=0 or (a.tail is not None and (not np.isfinite(a.tail) or a.tail<=0)):raise ValueError('Invalid camera distance/observation window')
 if not (0<=start<=end<=float(m['duration_sec'])+1e-5 and a.fps>0 and a.width>=320 and a.height>=180 and a.width%2==a.height%2==0):raise ValueError('invalid event interval, FPS or output dimensions')
 rr,es=l2.load_vfx_scene_from_pack(a.pack,[a.wrapper])
 if not es or not rr.target.has('vfx/'+a.wrapper+'.xml'):raise ValueError('Expected a reviewed wrapper in this sandbox, with reachable emitters')
 ren=l2.WH3TrueRenderer(a.width,a.height,a.env);cache=l2.preload_wsmodels(ren,rr,es)
 gate=gates(rr,a.wrapper,es,cache,a.looping,a.sort_pair);actor=None
 if a.model:
  import inspect
  if 'draw_opaque_actor' not in inspect.getsource(ren.render_frame):raise ValueError('Update the baker renderer: missing optional opaque actor pass')
  from actor_draw import ActorDraw
  actor=ActorDraw(ren,a.model,m)
 guides=not actor if a.guides is None else a.guides
 if any(e.wrapper_infinite_life for e in es) and a.tail is None:raise ValueError('For looping effects pass --tail as explicit observation window')
 src=[l2.mat4_trs(e.wrapper_pos,e.wrapper_rot_deg,np.full(3,e.wrapper_scale)) for e in es];sims=[e.simulate_particles for e in es]
 # Camera is stationary in actor space: no recentering on the moving attachment.
 lo=pos.min((0,1));hi=pos.max((0,1));# Include the deformed silhouette, not just joint tips, in framing.
 if actor:
  for j in np.linspace(0,len(ts)-1,min(8,len(ts))).astype(int):
   for p,_ in actor.skin.deform(pos[j],quat[j]):lo=np.minimum(lo,p.min(0));hi=np.maximum(hi,p.max(0))
 center=(lo+hi)/2;span=max(.5,float(np.linalg.norm(hi-lo)));aim=np.asarray(a.camera_dir,float);aim/=np.linalg.norm(aim)
 if not np.isfinite(aim).all() or abs(aim[1])>.995:raise ValueError('camera direction cannot be zero or parallel to up')
 eye=(center+aim*span*a.camera_distance).astype('f');center=center.astype('f');fov=36.
 V=l2.mat4_lookat(eye,center,np.array([0,1,0],'f'));P=l2.mat4_perspective(fov,a.width/a.height,.25,1200);VP=P@V
 def project(x):
  v=np.append(x,1.)@VP.T
  if v[3]<=.25:return None
  uv=v[:2]/v[3];return ((uv[0]+1)*.5*a.width,(1-uv[1])*.5*a.height)
 def binding(t):
  b=np.eye(4);pp,qq=pose(ts,pos,quat,t if a.tracking else start)
  if not world:b[:3,3]=pp[idx];b[:3,:3]=qmat(qq[idx])
  off=np.eye(4);off[:3,3]=np.array(a.offset_pos)*m['unit_scale'];off[:3,:3]=qmat(eulerq(a.offset_rot))
  return b@off
 def drawframe(t):
  pp,qq=pose(ts,pos,quat,t);root=binding(t)
  if actor:actor.set_pose(pp,qq)
  for e,orig,sim in zip(es,src,sims):
   M=root@orig;sc=np.linalg.norm(M[:3,0]);R=M[:3,:3]/max(sc,1e-9)
   if e.render_pass in ('DEFERRED_DYNAMIC_LIGHT','DEFERRED_DECAL','DEFERRED_DECAL_EMISSIVE'):
    # These three existing renderer passes consume particle positions directly, not wrapper_pos.
    def transformed(now,resolver=None,sim=sim,M=M,R=R,sc=sc):
     particles=sim(now,resolver=resolver);out=[]
     for orig_p in particles:
      p=dict(orig_p);p['pos']=(M@np.append(p['pos'],1))[:3];p['scale']=p['scale']*sc
      p['rot_deg']=yeuler(R@l2.mat4_trs(np.zeros(3),p['rot_deg'],np.ones(3))[:3,:3]);out.append(p)
     return out
    e.simulate_particles=transformed
   else:e.wrapper_pos=M[:3,3].astype('f');e.wrapper_rot_deg=yeuler(R);e.wrapper_scale=sc
  tv=t-start;rgb,_=ren.render_frame(max(0.,tv),eye,center,fov,es if tv>=-1e-9 else [],rr,cache)
  base=ren.render_frame(0.,eye,center,fov,[],rr,cache)[0].astype('i2') if actor else background
  delta=int(np.count_nonzero(np.max(np.abs(rgb.astype('i2')-base),axis=2)>3))
  im=Image.fromarray(rgb);dr=ImageDraw.Draw(im);pts=[project(p) for p in pp]
  for i,parent in enumerate(parents):
   if guides and parent>=0 and pts[i] and pts[parent]:dr.line([pts[parent],pts[i]],fill='#65b9c7',width=1)
  for i in feet if guides else ():
   p=project(pp[i]*[1,0,1])
   if p:dr.ellipse((p[0]-2,p[1]-1,p[0]+2,p[1]+1),fill='#a0b7bd')
  for i in weapons if guides else ():
   p=pts[i];par=parents[i]
   if p and par>=0 and pts[par]:dr.line([pts[par],p],fill='#ffd780',width=3)
  mark=project(root[:3,3])
  if mark and guides:
   x,y=mark;dr.ellipse((x-6,y-6,x+6,y+6),outline='#ffb74b',width=2)
  dr.rectangle((0,0,a.width,26),fill='#17232e');dr.text((10,7),f'{m["slot"]}   /   {a.attach_bone}   /   '+('BONE FOLLOW' if a.tracking else 'WORLD LOCK'),fill='#f3f7fb')
  dr.rectangle((0,a.height-22,a.width,a.height),fill='#17232e');dr.text((10,a.height-17),f'ANIM {t:5.3f} s    FX {tv:+6.3f} s    |    L2  {"MODEL" if actor else "BONES"}  |  EVENT END {end:.3f}',fill='#c6e5e9')
  return im,delta,root
 background=None if actor else ren.render_frame(0.,eye,center,fov,[],rr,cache)[0].astype('i2')
 life=max((e.wrapper_delay+e.start_time+e.life_time+e.particle_life_max for e in es),default=.6)
 until=max(m['duration_sec'], start+(a.tail if a.tail is not None else life)+.12);N=math.ceil(until*a.fps)+1;video=d/'action_vfx.mp4'
 cmd=[FFMPEG_PATH,'-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','rgb24','-s',f'{a.width}x{a.height}','-r',str(a.fps),'-i','-','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(video)]
 peak=-1;peak_t=start;rec=[]
 with subprocess.Popen(cmd,stdin=subprocess.PIPE) as proc:
  try:
   for k in range(N):
    t=k/a.fps;im,changed,root=drawframe(t);proc.stdin.write(im.tobytes());rec.append([round(t,6),changed,root[:3,3].tolist()])
    if t>=start and changed>peak:peak,peak_t=changed,t
  finally:proc.stdin.close()
  if proc.wait():raise RuntimeError('ffmpeg encode failed')
 phases=[('BEFORE',max(0.,start-1/a.fps)),('TRIGGER',start),('PEAK / SCREEN COVERAGE',peak_t),('TAIL',until)]
 sheet=Image.new('RGB',(a.width*2,(a.height+22)*2),'#101820');dr=ImageDraw.Draw(sheet)
 for i,(tag,t) in enumerate(phases):
  im,_,_=drawframe(t);x=i%2*a.width;y=i//2*(a.height+22);sheet.paste(im,(x,y+22));dr.text((x+10,y+4),tag,fill='#ffc676')
 sheet.save(d/'action_vfx_sheet.png')
 known={'FULLRES_DEFERRED_ALPHA','HALF_RES_DEFERRED_ALPHA','DISTORTION','DEFERRED_DYNAMIC_LIGHT','DEFERRED_DECAL','DEFERRED_DECAL_EMISSIVE'}
 missing=sorted(set(rr.omissions)|{f'{e.id}: unsupported pass {e.render_pass!r}' for e in es if not e.wsmodel and e.render_pass not in known})
 if not m.get('scale_source') or m['scale_source']=='unconfirmed model-space scale':missing.append('Unit/variant scale not verified; extract with --unit-scale and --scale-source before release')
 sandbox_digest=digest(a.pack)
 if sandbox_digest!=initial_digest:raise ValueError('Sandbox changed during rendering')
 result=dict(verified_scope='motion, trigger time and spatial registration',l3_only=['EFFECT end/playback lifetime and transitions','blends, sync kills and unit shader lighting'],engineering_gates=gate,model=(actor.skin.info if actor else None),scale_source=m.get('scale_source'),guides=guides,sandbox_sha256=sandbox_digest,status='PARTIAL' if missing or peak<=0 else 'PASS',omissions=missing,peak_changed_pixels=peak,peak_time=peak_t,frames=N,environment=a.env,wrapper=a.wrapper,sandbox=str(Path(a.pack).resolve()),motion=str(npz.resolve()),anim=m['anim'],meta=m['meta'],anim_sha256=m['anim_sha256'],bind_sha256=m['bind_sha256'],bone=idx,bone_name=a.attach_bone,tracking=a.tracking,start=start,end=end,offset_pos=a.offset_pos,offset_rot=a.offset_rot,unit_scale=m['unit_scale'],frame_log=rec)
 dump(d/'action_review.json',result);print(result['status'],N,'frames; peak changed pixels:',peak,'; omissions:',missing)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for opt in ('pack','wrapper','motion','attach-bone','out-dir'):p.add_argument('--'+opt,required=True)
 p.add_argument('--vfx-start',type=float,required=True);p.add_argument('--vfx-end',type=float);p.add_argument('--tracking',type=boolean,default=None)
 for x in ('pos','rot'):p.add_argument('--offset-'+x,type=triple,default=[0.,0.,0.])
 p.add_argument('--fps',type=int,default=24);p.add_argument('--width',type=int,default=640);p.add_argument('--height',type=int,default=360);p.add_argument('--env',choices=l2.ENV_NAMES,default='daylight')
 p.add_argument('--model',help='actor_model.npz exported for this skeleton/scale');p.add_argument('--guides',type=boolean,help='show bones over the model (default off with model, on otherwise)');p.add_argument('--looping',action='store_true');p.add_argument('--sort-pair',action='append',default=[],metavar='MASK:GLOW')
 p.add_argument('--camera-dir',type=triple,default=[.9,.21,.65]);p.add_argument('--camera-distance',type=float,default=1.30);p.add_argument('--tail',type=float);run(p.parse_args())


