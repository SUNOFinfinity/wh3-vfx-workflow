"""Read-only action discovery. All virtual names are normalized for lookup, never for serialization."""
import argparse,csv,json,os,shutil,struct,subprocess,sys,tempfile
from pathlib import Path
import numpy as np
def baker():
    candidates=[
        os.environ.get('WH3_VFX_BAKER'),
        Path(__file__).resolve().parents[3]/'baker',
        Path.cwd(),
        Path.cwd()/'baker',
        Path.cwd()/'wh3_vfx_baker',
    ]
    for x in candidates:
        if x and (Path(x)/'baker_config.py').is_file() and (Path(x)/'pfh5.py').is_file():
            sys.path.insert(0,str(Path(x).resolve()));return
    raise SystemExit('Set WH3_VFX_BAKER to the directory containing baker_config.py and pfh5.py.')
baker()
from baker_config import (
    ASSETEDITOR_DIR,
    RPFM_CLI,
    RPFM_SCHEMA,
    WH3_DATA_DIR,
    WH3_GAME_DIR,
    WH3_SLOT_FILE,
)
from pfh5 import PFH5Pack,guard_output
import anim_format as fmt
def key(x):return x.lower().replace('\\','/').strip('/')
def dump(path,data):
    path=Path(path);guard_output(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf8')
def editor():
    return Path(ASSETEDITOR_DIR)
class Reader:
    def __init__(self,b):self.b=b;self.i=0
    def take(self,n):
        if n<0 or self.i+n>len(self.b):raise ValueError('truncated animation container')
        start=self.i;self.i+=n;return self.b[start:self.i]
    def get(self,fmt):
        s=struct.Struct('<'+fmt);x=s.unpack(self.take(s.size));return x[0] if len(x)==1 else x
    def string(self):return self.take(self.get('H')).decode('utf8')
    def finish(self):
        if self.i!=len(self.b):raise ValueError('trailing bytes in animation container')
def unpack(raw):
    r=Reader(raw);rows=[];seen=set()
    for _ in range(r.get('I')):
        n=r.string();b=r.take(r.get('I'))
        if key(n) in seen:raise ValueError('duplicate normalized path in animpack: '+n)
        seen.add(key(n));rows.append((n,b))
    r.finish();return rows
def pack(rows):
    out=bytearray(struct.pack('<I',len(rows)))
    for path,b in rows:
        n=path.encode('utf8');out.extend(struct.pack('<H',len(n))+n+struct.pack('<I',len(b))+b)
    return bytes(out)
def fragment(raw):
    r=Reader(raw);v,sv=r.get('II')
    if v!=4:raise ValueError(f'unsupported battle fragment v{v}')
    table,mount,unmount,sk,graph=[r.string() for _ in range(5)]
    _,_,count=r.get('??I');out=[]
    for _ in range(count):
        slot,blend,weight,weapon,single,refs=r.get('IffI?I')
        for variant in range(refs):
            a,m,s=[key(r.string()) for _ in range(3)]
            out.append(dict(slot_id=slot,variant=variant,anim=a,meta=m,sound=s,weapon_set=weapon,selection_weight=weight))
    r.finish();return table,sk,out
class Assets:
    def __init__(self,path,data=None):
        self.path=Path(path).resolve();self.target=PFH5Pack(str(self.path));self.data=Path(data or WH3_DATA_DIR)
        self.loose={key(n):n for n,*_ in self.target.entries};self.inner={};self.containers={};self.native={};self.native_inner={}
        self.native_paths=sorted(self.data.glob('anim*.pack'),key=lambda p:(p.name!='anim2.pack',p.name))
        if len(self.loose)!=len(self.target.entries):raise ValueError('Duplicate normalized outer PFH5 path')
        for n in self.loose:
            if n.endswith('.animpack'):
                raw=self.target.read(n);rows=unpack(raw);self.containers[n]=rows
                for p,b in rows:self.inner.setdefault(key(p),[]).append((n,b))
    def copies(self,n):
        k=key(n);return ([('loose',self.target.read(k))] if k in self.loose else [])+self.inner.get(k,[])
    def read(self,n,deep=True):
        k=key(n);copies=self.copies(k)
        if copies:
            if any(blob!=copies[0][1] for _,blob in copies[1:]):raise ValueError('loose/container variants disagree; resolve effective loading set before use: '+k)
            return copies[0][1],'MOD::'+'+'.join(a for a,_ in copies)
        for f in self.native_paths:
            if f not in self.native:self.native[f]=PFH5Pack(str(f))
            if self.native[f].has(k):return self.native[f].read(k),f.name+'::'+k
        if deep:
            # Culture archives can be large. Inspect one at a time only after loose lookup misses.
            hits=[]
            for f,p in self.native.items():
                for c,*_ in p.entries:
                    if c.endswith('.animpack'):
                        for name,b in unpack(p.read(c)):
                            if key(name)==k:hits.append((b,f.name+'::'+c+'::'+name))
            if hits:
                if any(b!=hits[0][0] for b,_ in hits):raise ValueError('Vanilla culture variants differ: '+k)
                return hits[0]
        return None,None
    def tables(self):
        want={'land_units_tables','battle_personalities_tables','battle_animations_table_tables','land_units_to_battle_personalities_junctions_tables','battle_entities_tables','variants_tables','unit_variants_tables'}
        paths=[n for n in self.loose if n.startswith('db/') and n.split('/')[1] in want]
        if not paths:return {}
        cli=Path(RPFM_CLI)
        schema=Path(RPFM_SCHEMA)
        if not cli.is_file():raise ValueError(f'rpfm_cli not found at {cli}; run install.ps1 -WithActionTools or set RPFM_CLI')
        if not schema.is_file():raise ValueError(f'RPFM WH3 schema not found at {schema}; run install.ps1 -WithActionTools or set RPFM_SCHEMA')
        with tempfile.TemporaryDirectory() as td:
            args=[str(cli),'-g','warhammer_3','pack','extract','-p',str(self.path),'-t',str(schema)]
            for n in paths:args+=['-f',n+';'+td]
            res=subprocess.run(args,capture_output=True)
            if res.returncode:raise ValueError('RPFM table export failed: '+res.stderr.decode(errors='replace')[-600:])
            out={}
            for n in paths:
                with (Path(td)/(n+'.tsv')).open(encoding='utf-8-sig',newline='') as f:out.setdefault(n.split('/')[1],[]).extend(row for row in csv.DictReader(f,delimiter='\t') if not list(row.values())[0].startswith('#'))
            return out
    def actions(self,unit=None,table=None):
        slotfile=Path(WH3_SLOT_FILE)
        if not slotfile.is_file():raise ValueError(f'WH3 slot file not found at {slotfile}; set ASSETEDITOR_DIR or WH3_SLOT_FILE')
        slots=slotfile.read_text('utf-8-sig').splitlines();db=self.tables();units=db.get('land_units_tables',[])
        matched=[u for u in units if unit and unit.lower() in u['key'].lower()]
        if unit and not matched:raise ValueError('No local land_units match '+unit+'; specify a fragment with --table for an inherited unit.')
        tabs={u['man_animation'] for u in matched}
        junc=db.get('land_units_to_battle_personalities_junctions_tables',[])
        personal={v for j in junc if any(u['key'] in j.values() for u in matched) for v in j.values()}
        tabs.update(r.get('man_animations_table','') for r in db.get('battle_personalities_tables',[]) if r.get('key') in personal)
        names={n for n in self.loose.keys()|self.inner.keys() if n.startswith('animations/') and n.endswith('.bin')}
        for t in tabs|({table} if table else set()):names.add(key('animations/database/battle/bin/'+t+'.bin'))
        result=[]
        for n in sorted(names):
            raw,src=self.read(n,deep=bool(unit or table) and n not in self.loose and n not in self.inner)
            if not raw:continue
            t,sk,rows=fragment(raw)
            if (unit and t not in tabs) or (table and t!=table):continue
            for r in rows:
                i=r['slot_id'];r.update(table=t,fragment=n,skeleton=sk,slot=slots[i].strip() if i<len(slots) else str(i),fragment_source=src);result.append(r)
        return result,db
    def select(self,value,unit=None,table=None,anim_meta=None,sound_meta=None):
        rows,_=self.actions(unit,table);v=key(value);matches=[r for r in rows if v in (r['anim'],str(r['slot_id']),r['slot'].lower())]
        uniq={(r['anim'],r['meta'],r['sound'],r['skeleton']):r for r in matches}
        if not uniq and v.endswith('.anim') and not (unit or table):
            blob,src=self.read(v)
            if blob is None:raise ValueError('Animation path not found: '+v)
            def companion(user,suffix):
                if user:return key(user)
                path=v[:-5]+suffix
                return path if self.read(path,False)[0] is not None else ''
            return dict(anim=v,meta=companion(anim_meta,'.anm.meta'),sound=companion(sound_meta,'.snd.meta'),skeleton=fmt.load(blob).skeleton_name,table=None,slot=Path(v).stem,slot_id=None,binding_evidence='direct_clip_only; event binding unproven')
        if len(uniq)!=1:raise ValueError(f'{value}: {len(uniq)} distinct references. Specify --table or an exact animation path: '+str(list(uniq)[:8]))
        row=next(iter(uniq.values()))
        if any(x is not None and key(x)!=row[k] for x,k in [(anim_meta,'meta'),(sound_meta,'sound')]):raise ValueError('Explicit metadata conflicts with fragment')
        return dict(**row,binding_evidence='fragment')
def base_args(p):
    p.add_argument('--pack',required=True);p.add_argument('--game-data');p.add_argument('--unit');p.add_argument('--table')
def meta_codec(raw,spec=None):
    with tempfile.TemporaryDirectory() as td:
        d=Path(td);src=d/'src.meta';src.write_bytes(raw);j=d/'req.json';res=d/'read.json';out=d/'out.meta'
        if spec:j.write_text(json.dumps(spec,allow_nan=False),encoding='utf8')
        shell=os.getenv('PWSH_EXE') or shutil.which('pwsh') or shutil.which('powershell')
        cmd=[shell,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(Path(__file__).with_name('meta_codec.ps1')),'-EditorDir',str(editor()),'-Src',str(src),'-Report',str(res)]
        if spec:cmd+=['-Request',str(j),'-Dst',str(out)]
        run=subprocess.run(cmd,capture_output=True)
        if run.returncode:raise ValueError('AssetEditor metadata codec: '+(run.stderr or run.stdout).decode('utf8',errors='replace')[-2000:])
        return json.loads(res.read_text('utf-8-sig')),out.read_bytes() if spec else None

