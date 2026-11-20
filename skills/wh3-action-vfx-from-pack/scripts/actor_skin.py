"""Actor-space 4-influence linear-blend skinning. Shared by Houdini and pack L2."""
from pathlib import Path
import json,numpy as np
from motion_math import qmat

class Skin:
    def __init__(self,path,motion):
        self.path=Path(path).resolve();self.info=json.loads(self.path.with_suffix('.json').read_text('utf8'))
        if self.info['bind_sha256']!=motion['bind_sha256'] or self.info['unit_scale']!=motion['unit_scale']:raise ValueError('Model bind pose/scale differs from action')
        with np.load(self.path,allow_pickle=False) as z:
            if z['names'].tolist()!=[b['name'] for b in motion['bones']]:raise ValueError('Model and action bone order differs')
            self.bp=z['bind_pos'];self.bq=z['bind_quat'];self.inv=np.array([qmat(q).T for q in self.bq])
            self.parts=[{k:z[k+str(i)] for k in ('p','n','uv','ids','w','tri','Cd')} for i in range(len(self.info['parts']))]
    def deform(self,pos,quat):
        r=(np.array([qmat(q) for q in quat])@self.inv).astype('f');t=pos-np.einsum('bij,bj->bi',r,self.bp)
        output=[]
        for part in self.parts:
            w,ids=part['w'],part['ids'];mix=np.einsum('nk,nkij->nij',w,r[ids])
            p=np.einsum('nij,nj->ni',mix,part['p'])+np.einsum('nk,nki->ni',w,t[ids])
            n=np.einsum('nij,nj->ni',mix,part['n']);n/=np.maximum(np.linalg.norm(n,axis=1,keepdims=True),1e-8)
            output.append((p.astype('f'),n))
        return output

