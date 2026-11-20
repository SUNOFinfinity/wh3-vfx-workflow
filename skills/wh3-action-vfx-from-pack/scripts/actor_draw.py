"""Textured, depth-writing diagnostic actor pass; no game shader or assets are edited."""
import ctypes,numpy as np
from PIL import Image
import wh3_true_preview as gl
from actor_skin import Skin
VS='''#version 300 es
layout(location=0) in vec3 P;layout(location=1) in vec2 uv;layout(location=2) in vec3 n;
uniform mat4 vp;out vec2 tc;out vec3 N;
void main(){tc=uv;N=n;gl_Position=vp*vec4(P,1.);}'''
FS='''#version 300 es
precision highp float;uniform sampler2D albedo;in vec2 tc;in vec3 N;
layout(location=0) out vec4 hdr;layout(location=1) out vec4 particle;
void main(){vec3 n=normalize(N);float sun=max(dot(n,normalize(vec3(.45,.85,.65))),0.);
vec3 c=texture(albedo,tc).rgb*(vec3(.48,.51,.56)+sun*vec3(1.24,1.15,1.0));hdr=vec4(c,1.);particle=vec4(0.);}'''
class ActorDraw:
    def __init__(self,ren,path,m):
        self.skin=Skin(path,m);self.ren=ren;self.program=ren._compile_prog(VS,FS);g=ren.gl;self.buffers=[]
        for p,info in zip(self.skin.parts,self.skin.info['parts']):
            vao=ctypes.c_uint();vbo=ctypes.c_uint();ebo=ctypes.c_uint()
            g.glGenVertexArrays(1,ctypes.byref(vao));g.glGenBuffers(1,ctypes.byref(vbo));g.glGenBuffers(1,ctypes.byref(ebo))
            g.glBindVertexArray(vao.value);g.glBindBuffer(gl.GL_ARRAY_BUFFER,vbo.value)
            for index,size,offset in ((0,3,0),(1,2,12),(2,3,20)):
                g.glEnableVertexAttribArray(index);g.glVertexAttribPointer(index,size,gl.GL_FLOAT,gl.GL_FALSE,32,ctypes.c_void_p(offset))
            tri=np.ascontiguousarray(p['tri'],'<u2');g.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER,ebo.value);g.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER,tri.nbytes,tri.ctypes.data_as(ctypes.c_void_p),gl.GL_STATIC_DRAW)
            with Image.open(self.skin.path.parent/info['texture']) as im:tex=ren.upload_texture('actor:'+info['albedo'],np.asarray(im.convert('RGBA')),True)
            self.buffers.append((vao.value,vbo.value,tex,tri.size))
        self.set_pose(self.skin.bp,self.skin.bq);ren.draw_opaque_actor=self.draw
    def set_pose(self,pos,q):
        pts=self.skin.deform(pos,q);g=self.ren.gl
        for (p,n),part,(_,vb,_,_) in zip(pts,self.skin.parts,self.buffers):
            data=np.ascontiguousarray(np.column_stack((p,part['uv'],n)),dtype='f')
            g.glBindBuffer(gl.GL_ARRAY_BUFFER,vb);g.glBufferData(gl.GL_ARRAY_BUFFER,data.nbytes,data.ctypes.data_as(ctypes.c_void_p),0x88e8)
        return pts
    def draw(self,VP):
        r=self.ren;g=r.gl;p=self.program;g.glUseProgram(p);r._um4(p,'vp',VP);r._u1i(p,'albedo',0)
        g.glDisable(gl.GL_CULL_FACE);g.glActiveTexture(gl.GL_TEXTURE0)
        for vao,_,tex,count in self.buffers:
            g.glBindTexture(gl.GL_TEXTURE_2D,tex);g.glBindVertexArray(vao);g.glDrawElements(gl.GL_TRIANGLES,count,gl.GL_UNSIGNED_SHORT,ctypes.c_void_p(0))

