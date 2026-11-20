"""Two vanilla rigid channels. No changes to game shaders or game files.

Opaque zero-radiance profile: wh3_dlc25_garagrim_axe_ethereal instance, SVT_GBUFFER
ZWrite ALL. Its ps_main_gbuffer sums diffuse*tint and masked emissive; both set to zero.
Accept only this reduction in the offline unlit path, not general PBR materials.
Howling Citadel: wh3_vfx_distortion_mesh.xml.hlsl ps_main_distortion; DXBC register
assignments: t0=opaque depth, t1=outside mask, t2=panning noise (R-B), t3=24B params.
"""
import os
import xml.etree.ElementTree as ET
from pfh5 import PFH5Pack
from vfx_xml_compiler import WH3_VFX_DESC_PACK
BLACK='vfx_rigid_unlit_emissive_fresnel'
LENS='wh3_vfx_distortion_mesh'
MASK='t_xml_24dfd100_83d2_4215_8ecc_e510422b4e5e'
def material(shader,name,spec):
    src='wh3_dlc25_garagrim_axe_ethereal' if shader==BLACK else 'wh3_hemisphere_howling_citadel'
    p=PFH5Pack(os.path.join(os.path.dirname(WH3_VFX_DESC_PACK),'vfx.pack'))
    root=ET.fromstring(p.read('vfx/materials/'+src+'.xml.material'));root.find('name').text=name+'.xml'
    assert root.findtext('shader')=='shaders/'+shader+'.xml.shader'
    root.find('sort_override').text=str(spec.get('sort_override',255))
    def setp(key,value):
        node=next(e for e in root.findall('./params/param') if e.findtext('name')==key)
        node.find('value').text=','.join(map(str,value)) if isinstance(value,(list,tuple)) else str(value)
    if shader==BLACK:
        if spec.get('glow_strength')!=0 or spec.get('glow_colour')!=[0,0,0]:raise ValueError('Native unlit profile requires zero diffuse tint and zero emissive')
        for tx in root.findall('./textures/texture'):
            name=tx.findtext('slot')
            tx.find('source').text='commontextures/default_normal.dds' if name=='t_xml_normal_map' else 'commontextures/default_black.dds'
        for key,val in [('diffuse_tint_colour',[0,0,0]),('emissive_colour',[0,0,0]),('fresnel_multiplier',0),('roughness_metalness',[1,0])]:setp(key,val)
    else:
        for key in ('distortion_tex_strength','panning_tex_direction','panning_texture_scale'):
            if key in spec:setp(key,spec[key])
        if 'depth_fade_distance' in spec:setp('depth_fade',spec['depth_fade_distance'])
    return ET.tostring(root,encoding='utf-8')
def kind(mat):
    shader=mat['shader'].replace('\\','/').lower()
    if shader=='shaders/'+LENS+'.xml.shader':return 'lens'
    if shader=='shaders/'+BLACK+'.xml.shader':
        pm=mat['params']
        if any(pm.get('diffuse_tint_colour',[1]*3)) or any(pm.get('emissive_colour',[1]*3)) or pm.get('fresnel_multiplier',1)!=0 or 'default_black' not in mat['textures'].get('t_xml_dither',''):
            return None  # not the supported zero-radiance, non-dithered profile
        return 'opaque'
    return None
NATIVE_VS="""#version 300 es
precision highp float;
layout(location=0) in vec3 p; layout(location=1) in vec2 uv; layout(location=2) in vec3 n;
uniform mat4 u_model,u_vp; out vec3 wp,wn; out vec2 tc;
void main(){vec4 w=u_model*vec4(p,1.);wp=w.xyz;wn=normalize(mat3(u_model)*n);tc=uv;gl_Position=u_vp*w;}
"""
BLACK_FS="""#version 300 es
precision highp float;
layout(location=0) out vec4 hdr; layout(location=1) out vec4 particle;
void main(){hdr=vec4(0,0,0,1);particle=vec4(0);}
"""
LENS_FS="""#version 300 es
precision highp float;
in vec3 wp,wn; in vec2 tc;
uniform highp sampler2D u_depth,u_mask,u_noise;
uniform mat4 u_invvp;uniform vec3 u_eye;uniform vec2 u_pan,u_tile,u_invres;
uniform float u_time,u_strength,u_fade,u_opacity;
layout(location=0) out vec4 outD;
void main(){
vec2 s=gl_FragCoord.xy*u_invres;
float z=texelFetch(u_depth,ivec2(gl_FragCoord.xy),0).r;
vec4 q=u_invvp*vec4(s*2.-1.,z*2.-1.,1.);
float fade=clamp(length(wp-q.xyz/q.w)/u_fade,0.,1.);
vec3 nx=texture(u_noise,(tc+u_time*u_pan)*u_tile).rgb;
float outside=texture(u_mask,vec2(s.x,1.-s.y)).r;
float v=dot(normalize(u_eye-wp),wn)*outside*2.*(nx.r-nx.b)*u_strength*floor(1000.*clamp(u_opacity,0.,1.))*.001*fade;
// DXBC writes identical XY in top-left D3D space; UV of GL colour targets has reversed Y.
outD=vec4(v,-v,0.,0.);
}
"""

