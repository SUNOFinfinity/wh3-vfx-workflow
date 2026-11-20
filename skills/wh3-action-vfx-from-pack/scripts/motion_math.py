import numpy as np
def unitq(q):
 q=np.asarray(q,dtype='f8');n=np.linalg.norm(q,axis=-1,keepdims=True)
 if np.any(n<1e-9) or not np.isfinite(q).all():raise ValueError('Invalid rotation quaternion')
 return q/n
def qm(a,b):
 a,b=np.asarray(a),np.asarray(b);x,y=a[...,:3],b[...,:3];w=a[...,3:];v=b[...,3:]
 return np.concatenate((w*y+v*x+np.cross(x,y),w*v-np.sum(x*y,axis=-1,keepdims=True)),axis=-1)
def qr(q,v):
 t=2*np.cross(q[...,:3],v);return v+q[...,3:]*t+np.cross(q[...,:3],t)
def eulerq(deg):
 """Intrinsic local X/Y/Z input in degrees; composition Ry @ Rx @ Rz (baker TRS)."""
 q=[]
 for i,a in enumerate(np.radians(deg)*.5):
  r=np.zeros(4);r[i]=np.sin(a);r[3]=np.cos(a);q.append(r)
 return unitq(qm(q[1],qm(q[0],q[2])))
def qmat(q):return qr(np.broadcast_to(unitq(q),(3,4)),np.eye(3)).T
def yeuler(r):
 x=np.arcsin(np.clip(-r[1,2],-1,1))
 if abs(np.cos(x))>1e-6:y=np.arctan2(r[0,2],r[2,2]);z=np.arctan2(r[1,0],r[1,1])
 else:y=np.arctan2(-r[2,0],r[0,0]);z=0.
 return np.degrees([x,y,z])
def fk(t,q,parents):
 p=t.copy();g=unitq(q);seen=set()
 def solve(i,vis):
  if i in seen:return
  if i in vis:raise ValueError('Cyclic skeleton')
  par=parents[i]
  if par< -1 or par>=len(parents):raise ValueError('Invalid parent index')
  if par!=-1:
   solve(par,vis|{i});p[:,i]=p[:,par]+qr(g[:,par],t[:,i]);g[:,i]=unitq(qm(g[:,par],g[:,i]))
  seen.add(i)
 for i in range(len(parents)):solve(i,set())
 return p,g
def pose(times,pos,quat,t):
 j=int(np.searchsorted(times,t,'right'));a=max(0,min(len(times)-1,j-1));b=min(len(times)-1,j)
 u=0 if a==b else np.clip((t-times[a])/(times[b]-times[a]),0,1)
 first,last=quat[a],quat[b];last=np.where(np.sum(first*last,axis=-1,keepdims=True)<0,-last,last)
 return (1-u)*pos[a]+u*pos[b],unitq((1-u)*first+u*last)

