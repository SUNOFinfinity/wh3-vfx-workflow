"""WH3 DLC23 lava-splat VAT contract, not normalized bounds or a vertex-ID texture.
VS 29d6d446 loads integer texels using UV0 + floor(time*fps)/num_frames; no frame lerp.
"""
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np
from baker_config import WH3_DATA_DIR
from pfh5 import PFH5Pack
DONOR=r'vfx\materials\wh3_dlc23_lava_splat_3_baked_anim.xml.material'

def material(name,spec,textures):
    root=ET.fromstring(PFH5Pack(Path(WH3_DATA_DIR)/'vfx.pack').read(DONOR))
    root.find('name').text=name+'.xml'
    root.find('sort_override').text=str(spec.get('sort_override',128))
    params={'fps':spec.get('vat_fps',30),'num_frames':spec.get('vat_frame_count',60),'emissive_colour':','.join(map(str,spec.get('glow_colour',[1,1,1]))),'emissive_brightness':spec.get('glow_strength',2)}
    slots={k:textures[spec[key]] for k,key in [('t_xml_baked_animation','vat_pos_tex'),('t_xml_baked_normal','vat_nrm_tex'),('t_xml_baked_uv','vat_uv_tex')]}
    if 'tex_a' in spec:slots['t_xml_diffuse_tex']=textures[spec['tex_a']]
    for p in root.findall('./params/param'):
        k=p.findtext('name')
        if k in params:p.find('value').text=str(params[k])
    for tex in root.findall('./textures/texture'):
        k=tex.findtext('slot')
        if k in slots:tex.find('source').text=slots[k].replace('\\','/')
    return ET.tostring(root,encoding='utf8')

def atlas(raw):
    if raw[:4]!=b'DDS ' or struct.unpack_from('<I',raw,84)[0]!=116:raise ValueError('Expected legacy float4 VAT DDS')
    h,w=struct.unpack_from('<II',raw,12)
    return np.frombuffer(raw,offset=128,count=h*w*4,dtype='<f4').reshape(h,w,4)
def texels(uv,w,h,n,frame):
    xy=np.floor((uv+np.array([0.,frame/n]))*[w,h]).astype(np.int64)
    if np.any(xy<0) or np.any(xy>=[w,h]):raise ValueError('VAT lookup UV outside atlas at frame '+str(frame))
    return xy[:,1],xy[:,0]
def diagnose(mat,mesh,resolver,geometry=None,name=""):
    slots=mat['textures'];keys=('t_xml_baked_animation','t_xml_baked_normal','t_xml_baked_uv')
    if not all(k in slots for k in keys):return {'status':'LEGACY_MATERIAL','missing':[k for k in keys if k not in slots]}
    arrays=[atlas(resolver.read(slots[k])) for k in keys];h,w=arrays[0].shape[:2];n=int(mat['params']['num_frames'])
    if not all(a.shape==arrays[0].shape for a in arrays) or n<1 or h%n:raise ValueError('VAT atlas sizes/frame bands differ')
    frames=[]
    for frame in dict.fromkeys((0,n//2,n-1)):
        y,x=texels(mesh['uv'],w,h,n,frame);xyz=arrays[0][y,x,:3]
        if geometry is not None:geometry.setdefault(name,[]).append((frame, xyz.copy(),mesh["idx"]))
        frames.append(dict(frame=frame,bbox=[xyz.min(0).tolist(),xyz.max(0).tolist()],first_position=xyz[0].tolist(),first_uv=arrays[2][y[0],x[0],:2].tolist()))
    return dict(status='VERTEX_FETCH_VERIFIED',dimensions=[w,h],vertices=len(mesh['uv']),frames=frames)



def save_wires(geometry,path):
    """Pack-derived object space, common projection/scale. Never composited into the beauty pass."""
    from PIL import Image,ImageDraw
    w,h=230,180
    image=Image.new("RGB",(w*3,h*len(geometry)+32),"#141b27");d=ImageDraw.Draw(image)
    d.text((12,9),"VAT  /  OBJECT-SPACE GEOMETRY    |    Not a shaded render",fill="#eaf2fc")
    project=np.array([[.88,.18],[0,-1],[-.48,.33]])
    for row,(name,frames) in enumerate(geometry.items()):
        flat=[xyz@project for _,xyz,_ in frames];points=np.vstack(flat);lo=points.min(0);hi=points.max(0)
        factor=min((w-40)/max(1e-6,hi[0]-lo[0]),(h-44)/max(1e-6,hi[1]-lo[1]))
        for col,((frame,_,idx),pts) in enumerate(zip(frames,flat)):
            x=col*w;y=32+row*h;pts=(pts-(hi+lo)/2)*factor+[x+w/2,y+(h-22)/2]
            d.rectangle([x,y,x+w-1,y+h-1],outline="#334253")
            for tri in idx.reshape(-1,3):
                a,b,c=[tuple(map(float,pts[i])) for i in tri];d.line([a,b,c,a],fill="#76d3c6",width=1)
            label=name.replace(chr(92),"/").split("/")[-1]
            d.text((x+10,y+h-18),f"{label}   /   frame {frame}",fill="#cad7e6")
    image.save(path)
