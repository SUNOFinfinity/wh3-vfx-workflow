// Read-only worker; binary stdin, provenance-preserving JSON stdout.
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const {processCsc,parse}=await import(pathToFileURL(process.argv[2]).href);
const tree=parse(processCsc(fs.readFileSync(0)).tree);
const keys=[];
function visit(v) {
    if (!v || typeof v!=='object') return;
    if (v.Record?.name==='VFX_ELEMENT') {
        const key=v.Record.children?.[0]?.[0]?.Ascii;
        if(typeof key!=='string') throw Error('Unknown VFX_ELEMENT key layout, version '+v.Record.version);
        keys.push(key);
    }
    for(const x of Object.values(v))visit(x);
}
visit(tree.root_node);console.log(JSON.stringify(keys));
