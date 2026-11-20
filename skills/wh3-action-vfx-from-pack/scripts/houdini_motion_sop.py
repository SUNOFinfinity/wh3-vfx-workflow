"""Run inside a Houdini Python SOP with a string parm 'motion' pointing at the NPZ.
An optional float parm 'start_frame' defaults to 1. Scrubbing uses the scene's FPS.
"""
import hou,numpy as np,json
from pathlib import Path
node=hou.pwd();geo=node.geometry();path=Path(node.evalParm('motion'));data=np.load(path,allow_pickle=False)
meta=json.loads(path.with_name('action_motion.json').read_text('utf8'));times=data['times'];origin=node.evalParm('start_frame') if node.parm('start_frame') else 1
t=float(np.clip((hou.frame()-origin)/hou.fps(),0,times[-1]));j=min(len(times)-1,int(np.searchsorted(times,t)));i=max(0,j-1);u=0 if i==j else (t-times[i])/(times[j]-times[i])
pos=(1-u)*data['bone_pos'][i]+u*data['bone_pos'][j];vel=(1-u)*data['bone_vel'][i]+u*data['bone_vel'][j]
a,b=data['bone_quat'][i],data['bone_quat'][j];b=np.where((a*b).sum(1)[:,None]<0,-b,b);quat=a*(1-u)+b*u;quat/=np.linalg.norm(quat,axis=1)[:,None]
name=geo.addAttrib(hou.attribType.Point,'name','');v=geo.addAttrib(hou.attribType.Point,'v',(0.,0.,0.));orient=geo.addAttrib(hou.attribType.Point,'orient',(0.,0.,0.,1.))
phase=geo.addAttrib(hou.attribType.Point,'u',0.);anchor=geo.addAttrib(hou.attribType.Point,'power_frame',0);live=geo.createPointGroup('pose');trail=geo.createPointGroup('weapon_trails')
def point(n,p,q,s,progress,mark,group):
 pt=geo.createPoint();pt.setPosition(tuple(map(float,p)));pt.setAttribValue(name,str(n));pt.setAttribValue(orient,tuple(map(float,q)));pt.setAttribValue(v,tuple(map(float,s)));pt.setAttribValue(phase,float(progress));pt.setAttribValue(anchor,int(mark));group.add(pt);return pt
names=data['bone_names'];current=int(np.abs(times-t).argmin());marks=meta['key_anchors']
pts=[point(n,p,q,s,t/max(times[-1],1e-8),marks.get(str(n),{}).get('peak_speed_frame')==current,live) for n,p,q,s in zip(names,pos,quat,vel)]
def line(points):
 poly=geo.createPolygon(is_closed=False)
 for pt in points:poly.addVertex(pt)
for k,par in enumerate(data['parents']):
 if par>=0:line([pts[par],pts[k]])
# Full sampled arcs carry normalized time and peak markers, separate from the live skeleton.
spos,squat,svel=data['bone_pos'],data['bone_quat'],data['bone_vel']
for k in data['weapon_indices']:
 n=str(names[k]);line([point(n,spos[j,k],squat[j,k],svel[j,k],j/max(len(times)-1,1),j==marks[n]['peak_speed_frame'],trail) for j in range(len(times))])
data.close()

