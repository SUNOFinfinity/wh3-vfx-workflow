"""WH3 RMV2 v7 animated rigid groups, one building root / exact CA ANIM v7 dynamic TR track."""
import math,struct
import numpy as np
from pathlib import Path
HEAD=struct.Struct('<IIfH')
def encode(t,q,duration,fps=120.):
    p=np.asarray(t,dtype='<f4');q=np.asarray(q,dtype=float)
    q=q/np.linalg.norm(q,axis=1,keepdims=True)
    d=np.empty(len(p),dtype=[('t','<f4',(3,)),('q','<i2',(4,))])
    d['t']=p;d['q']=np.rint(np.clip(q,-1,1)*32767).astype('<i2')
    name=b'building';return (HEAD.pack(7,1,fps,len(name))+name+struct.pack('<IfIH',0,duration,1,len(name))+name+
          struct.pack('<8i',-1,0,0,0,0,1,1,len(d))+d.tobytes())
def matches_profile(raw):
    # Restrict the optional decoder to one v7 animated building T/Q root with the known native channel map.
    return bool(raw and len(raw)>=76 and raw[:8]==struct.pack('<2I',7,1) and raw[12:22]==b'\x08\x00building'
                and raw[22:26]==bytes(4) and raw[30:44]==struct.pack('<IH',1,8)+b'building'
                and raw[44:72]==struct.pack('<7i',-1,0,0,0,0,1,1))
def decode(raw):
    ver,typ,fps,ln=HEAD.unpack_from(raw);o=HEAD.size
    if (ver,typ)!=(7,1) or raw[o:o+ln]!=b'building' or not 0<fps<=1000:raise ValueError('Expected v7 building motion')
    o+=ln;flags,dur,nb,ln=struct.unpack_from('<IfIH',raw,o);o+=14
    if flags or nb!=1 or raw[o:o+ln]!=b'building':raise ValueError('Expected one dynamic building root')
    o+=ln;parent,tm,rm,st,sr,nt,nr,n=struct.unpack_from('<8i',raw,o);o+=32
    if (parent,tm,rm,st,sr,nt,nr)!=(-1,0,0,0,0,1,1) or n<1 or len(raw)!=o+20*n:raise ValueError('Bad rigid-root mapping/frame extent')
    d=np.frombuffer(raw,offset=o,count=n,dtype=[('t','<f4',(3,)),('q','<i2',(4,))]);t=d['t'].astype(float);q=d['q'].astype(float)/32767.
    if not np.isfinite(t).all() or not np.allclose(np.linalg.norm(q,axis=1),1.,atol=.0001):raise ValueError('Invalid root pose')
    return fps,dur,t,q
def matrix(clip,t):
    fps,dur,ps,qs=clip;u=np.clip(t,0,dur)*fps;i=min(int(u),len(ps)-1);j=min(i+1,len(ps)-1);v=min(1.,u-i)
    a=qs[i];b=qs[j] if qs[i]@qs[j]>=0 else -qs[j];q=a+(b-a)*v;q/=np.linalg.norm(q);x,y,z,w=q
    m=np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w),0],[2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w),0],[2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y),0],[0,0,0,1]],np.float32)
    m[:3,3]=ps[i]+(ps[j]-ps[i])*v;return m
def bind_rmv(raw):
    # vfx_models.pack::vfx/models/rigid_anim/wh3_slaanesh_moon_shatter: v7 layout 68/32B,
    # group +640/+688/+736 identity 3x4, +784 group matrix ID, +788 parent. Root group = 0/-1.
    out=bytearray(raw);off=struct.unpack_from('<I',out,152)[0];out[12:140]=b'building'.ljust(128,b'\0')
    mat=struct.pack('<12f',1,0,0,0,0,1,0,0,0,0,1,0)
    for i in (640,688,736):out[off+i:off+i+48]=mat
    struct.pack_into('<ii',out,off+784,0,-1);struct.pack_into('<f',out,156,10000.);return bytes(out)
def source(ir,s):
    root=Path(ir).resolve();p=(root/s).resolve()
    if not p.is_relative_to(root):raise ValueError('Animation source outside IR')
    data=p.read_bytes();decode(data);return data

