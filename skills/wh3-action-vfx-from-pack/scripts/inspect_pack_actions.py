import argparse,json,struct
from action_io import Assets,base_args,dump,fmt,key
def main():
 p=argparse.ArgumentParser(description='Resolve unit → fragment → slot and report loose/container copies.');base_args(p);p.add_argument('--filter',default='');p.add_argument('--out');a=p.parse_args()
 assets=Assets(a.pack,a.game_data);rows,db=assets.actions(a.unit,a.table);refs={r['anim'] for r in rows};f=a.filter.lower();cache={}
 for r in rows:
  if f not in ' '.join(str(x) for x in (r['slot_id'],r['slot'],r['anim'])).lower():continue
  n=r['anim']
  if n not in cache:
   raw,src=assets.read(n,False)
   if raw:
    clip=fmt.load(raw);cache[n]=dict(source=src,fps=clip.frame_rate,duration_sec=clip.duration,anim_version=clip.version,clip_skeleton=clip.skeleton_name)
   else:cache[n]=dict(source=None,status='not found loose or in target; extraction also searches vanilla containers')
  r['clip']=cache[n]
 rows=[r for r in rows if 'clip' in r]
 duplicated=[dict(path=n,sources=[x for x,_ in assets.copies(n)],identical=all(x==assets.copies(n)[0][1] for _,x in assets.copies(n))) for n in sorted(assets.inner) if n in assets.loose or len(assets.inner[n])>1]
 doc=dict(pack=str(assets.path),units=[{'key':u['key'],'animation_table':u['man_animation'],'entity':u['man_entity']} for u in db.get('land_units_tables',[]) if not a.unit or a.unit.lower() in u['key'].lower()],battle_tables=db.get('battle_animations_table_tables',[]),personalities=db.get('battle_personalities_tables',[]),scale_records=[{k:v for k,v in r.items() if 'scale' in k or k in ('key','variant','unit','variant_name','variant_filename')} for t in ('battle_entities_tables','variants_tables','unit_variants_tables') for r in db.get(t,[])],duplicates=duplicated,source_policy='target first; within MOD require identical copies. Runtime precedence across culture/load-order is not inferred.',actions=rows,unreferenced_local_anims=sorted({n for n in assets.loose.keys()|assets.inner.keys() if n.endswith('.anim')} - refs))
 if a.out:dump(a.out,doc)
 else:print(json.dumps(doc,ensure_ascii=False,indent=2))
if __name__=='__main__':main()


