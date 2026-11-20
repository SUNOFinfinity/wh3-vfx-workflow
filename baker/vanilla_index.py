"""Read-only game assets; the disposable SQLite index stores offsets, never payloads."""
import os
import sqlite3
import tempfile
from pathlib import Path
from baker_config import WH3_DATA_DIR
from pfh5 import PFH5Pack, norm_pack_path, decode_payload

class VanillaIndex:
    def __init__(self, packs, cache=None):
        self.paths=[str(Path(p).resolve()) for p in packs if Path(p).is_file()]
        folder=Path(cache or os.getenv('WH3_VFX_CACHE_DIR',Path(tempfile.gettempdir())/'wh3_vfx_baker'))
        folder.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(folder/'pfh5-index-v1.sqlite', timeout=60)
        self.db.executescript('CREATE TABLE IF NOT EXISTS packs(path TEXT PRIMARY KEY,mtime INTEGER,size INTEGER); CREATE TABLE IF NOT EXISTS entries(pack TEXT,key TEXT,off INTEGER,size INTEGER,flag INTEGER,PRIMARY KEY(pack,key)) WITHOUT ROWID;')
        self.rebuilt=[]
        for p in self.paths:
            stat=os.stat(p)
            sig=(stat.st_mtime_ns,stat.st_size)
            if self.db.execute('SELECT mtime,size FROM packs WHERE path=?',(p,)).fetchone()==sig:continue
            entries=PFH5Pack(p).entries
            with self.db:
                self.db.execute('DELETE FROM entries WHERE pack=?',(p,))
                self.db.executemany('INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?)',((p,norm_pack_path(n).lower(),o,s,c) for n,o,s,c,_ in entries))
                self.db.execute('INSERT OR REPLACE INTO packs VALUES(?,?,?)',(p,*sig))
            self.rebuilt.append(Path(p).name)
    def locate(self,name):
        key=norm_pack_path(name).lower()
        for p in self.paths:
            r=self.db.execute('SELECT off,size,flag FROM entries WHERE pack=? AND key=?',(p,key)).fetchone()
            if r:return (p,*r)
    def __contains__(self,name):
        return self.locate(name) is not None
    def read(self,name):
        rec=self.locate(name)
        if rec is None:return None
        p,o,s,flag=rec
        with open(p,'rb') as f:
            f.seek(o);data=f.read(s)
        if len(data)!=s:raise IOError('Short read: '+p+'::'+name)
        return decode_payload(data,flag)
    def close(self):
        self.db.close()

def game_packs():
    d=Path(WH3_DATA_DIR)
    # Only installed vanilla families used by the original closure audit.
    return sorted((p for p in d.glob('*.pack') if p.name.startswith(('vfx','data','models','textures','shaders','terrain','boot','commontextures'))),key=lambda p:(0 if p.stem.endswith('_bl') else 1,p.name))

