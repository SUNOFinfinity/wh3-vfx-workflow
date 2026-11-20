"""Resolve one unit variant and export its actual weighted LOD and texture bindings."""
import argparse,hashlib,json,struct,xml.etree.ElementTree as ET
from pathlib import Path
from action_io import Assets,key,fmt,guard_output,dump,np
from motion_math import fk
from PIL import Image

class Models:
    def __init__(self,assets):
        self.assets=assets;self.reads={};self.native={};self._tex_cache={};self.omissions=set()
    def get(self,n):
        from pfh5 import PFH5Pack
        n=key(n);copies=self.assets.copies(n)
        if copies:
            raw,source=self.assets.read(n,False)
        else:
            raw=None
            for p in sorted((p for p in self.assets.data.glob('*.pack') if p.name.startswith(('variants','models','textures','commontextures'))),key=lambda p:(not p.stem.endswith('_bl'),p.name)):
                if p not in self.native:self.native[p]=PFH5Pack(str(p))
                if self.native[p].has(n):raw,source=self.native[p].read(n),p.name;break
            if raw is None:raise ValueError('Model asset unavailable: '+n)
        self.reads[n]=dict(source=source,sha256=hashlib.sha256(raw).hexdigest());return raw

    read=get
    def albedo(self,tex):
        from wh3_true_preview import PackAssetResolver
        return Image.fromarray(PackAssetResolver.decode_dds_rgba_u8(self,tex)).convert('RGB')

def choose(src,a):
    if a.wsmodel:return a.wsmodel
    variant=a.variant
    if not variant:
        db=src.assets.tables()
        ids={r['variant'] for r in db.get('unit_variants_tables',[]) if r['unit']==a.unit}
        if len(ids)!=1:raise ValueError(f'{a.unit}: select one --variant for this preview: {sorted(ids)}')
        k=ids.pop();rows=[r for r in db.get('variants_tables',[]) if r['variant_name']==k]
        if len(rows)>1:raise ValueError('Ambiguous variant filename: '+k)
        variant=rows[0]['variant_filename'] if rows else k
    v=variant if variant.lower().endswith('.variantmeshdefinition') else 'variantmeshes/variantmeshdefinitions/'+variant+'.variantmeshdefinition'
    root=ET.fromstring(src.get(v));found=[]
    def walk(node):
        if node.get('attach_point') or node.get('attachment_point') or node.get('transform') or node.tag.upper() in ('REFERENCE','ATTACH_POINT','TRANSFORM'):
            raise ValueError('Attachment/reference needs explicit assembly: '+v+'; use --wsmodel to inspect selected components')
        if node.get('model'):found.append(node.get('model'))
        if node.tag.upper()=='SLOT' and len([x for x in node if x.tag.upper()=='VARIANT_MESH'])>1:raise ValueError('Variant SLOT has alternatives; choose intended --wsmodel component(s)')
        for child in node:walk(child)
    walk(root)
    if not found:raise ValueError('No model reference in '+v)
    return found

def blocks(raw,sk,bones,lod):
    if raw[:4]!=b'RMV2' or struct.unpack_from('<I',raw,4)[0]!=8:raise ValueError('Expected RMV2 v8 weighted4')
    if not 0<=lod<struct.unpack_from('<I',raw,8)[0]:raise ValueError('LOD not present')
    if raw[12:140].split(b'\0')[0].decode().lower()!=sk.lower():raise ValueError('Mesh is bound to a different skeleton')
    count,total_v,total_i,p=struct.unpack_from('<4I',raw,140+28*lod);verts=inds=0
    for k in range(count):
        size,vo,nv,ioff,ni=struct.unpack_from('<5I',raw,p+4)
        if struct.unpack_from('<H',raw,p+80)[0]!=4 or ioff-vo!=nv*36 or not nv or not ni or ni%3 or ioff+ni*2!=size or p+size>len(raw):raise ValueError('Unsupported RMV2 vertex layout; need weighted4/36-byte vertices')
        dt=np.dtype(dict(names=['p','ids','w','n','uv'],formats=[('<f2',4),('u1',4),('u1',4),('u1',4),('<f2',2)],offsets=[0,8,12,16,20],itemsize=36))
        x=np.frombuffer(raw,dtype=dt,offset=p+vo,count=nv);tri=np.frombuffer(raw,'<u2',ni,p+ioff).copy().reshape(-1,3)
        ids=x['ids'].copy();w=x['w'].copy();ids[w==0]=0
        if not (tri.max()<nv and ids.max()<bones and (w.sum(1)==255).all()):raise ValueError('Invalid index or bone weights')
        yield dict(p=x['p'][:,:3].astype('f'),n=x['n'][:,:3].astype('f')/127.5-1,uv=x['uv'].astype('f'),ids=ids,w=w.astype('f')/255,tri=tri)
        p+=size;verts+=nv*36;inds+=ni*2
    if (verts,inds)!=(total_v,total_i):raise ValueError('LOD byte counts disagree')

def export(a):
    src=Models(Assets(a.pack,a.game_data));d=guard_output(a.out_dir);d.mkdir(parents=True,exist_ok=True)
    meta=json.loads(Path(a.motion).with_name('action_motion.json').read_text('utf8'));sk=meta['skeleton'];bind,_=src.assets.read('animations/skeletons/'+sk+'.anim')
    if hashlib.sha256(bind).hexdigest()!=meta['bind_sha256']:raise ValueError('Motion bind pose changed')
    b=fmt.load(bind);dense=fmt.resolve(b);scale=meta['unit_scale'];names=[v.name for v in b.bones]
    if names!=[v['name'] for v in meta['bones']]:raise ValueError('Bone order differs from motion')
    bp,bq=fk(dense.translations[:1].astype('f8')*scale,dense.rotations[:1],[v.parent for v in b.bones])
    arrays=dict(bind_pos=bp[0],bind_quat=bq[0],names=np.array(names));materials=[]
    for ws in choose(src,a):
        if not ws.lower().endswith('.wsmodel'):raise ValueError('Expected a .wsmodel, got '+ws)
        w=ET.fromstring(src.get(ws));mesh=w.findtext('geometry');mats={int(x.get('part_index')):x.text for x in w.findall('./materials/material') if int(x.get('lod_index'))==a.lod}
        for i,part in enumerate(blocks(src.get(mesh),sk,len(names),a.lod)):
            material=mats[i];doc=ET.fromstring(src.get(material));shader=doc.findtext('shader','')
            if 'weighted4' not in shader.lower():raise ValueError('Unsupported material on weighted model: '+shader)
            texs={t.findtext('slot'):t.findtext('source') for t in doc.findall('./textures/texture')};tex=texs.get('t_xml_base_colour') or texs.get('t_xml_diffuse')
            if not tex:raise ValueError('No base colour slot in '+material)
            j=len(materials);im=src.albedo(tex);png=f'part_{j:02d}_albedo.png';im.save(d/png)
            part['p']*=scale;uv=part['uv']%1;rgb=np.asarray(im);h,wid=rgb.shape[:2];part['Cd']=rgb[(uv[:,1]*h).astype(int),(uv[:,0]*wid).astype(int)].astype('f')/255
            arrays.update({f'{k}{j}':v for k,v in part.items()})
            materials.append(dict(wsmodel=ws,geometry=mesh,material=material,shader=shader,albedo=tex,texture=png,vertices=len(part['p']),triangles=len(part['tri'])))
    if not materials:raise ValueError('No weighted mesh parts selected')
    np.savez_compressed(d/'actor_model.npz',**arrays)
    dump(d/'actor_model.json',dict(skeleton=sk,bind_sha256=meta['bind_sha256'],unit_scale=scale,lod=a.lod,parts=materials,assets=src.reads,appearance='weighted geometry + base colour, diagnostic lighting; not game character shading'))
    print(len(materials),'weighted parts;',sum(p['triangles'] for p in materials),'triangles ->',d)
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for k in ('pack','motion','out-dir'):p.add_argument('--'+k,required=True)
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--unit');g.add_argument('--variant');g.add_argument('--wsmodel',action='append')
    p.add_argument('--game-data');p.add_argument('--lod',type=int,default=0);export(p.parse_args())

