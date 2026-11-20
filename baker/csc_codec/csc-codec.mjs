// Lossless value representation and isolated ESF codec worker.
import fs from 'node:fs';
import { isDeepStrictEqual } from 'node:util';
import { initSync, decode, encode } from './vendor/esf/rpfm_lib_esf_wasm.js';

export const stringify = value => JSON.stringify(value, (_key, v) => {
  if (typeof v === 'bigint') return { $bigint: v.toString() };
  if (typeof v === 'number' && (!Number.isFinite(v) || Object.is(v, -0))) return { $number: Object.is(v, -0) ? '-0' : String(v) };
  return v;
});
export const parse = text => JSON.parse(text, (_key, v) => {
  if (v && typeof v === 'object' && Object.keys(v).length === 1) {
    if (typeof v.$bigint === 'string' && /^-?\d{1,20}$/.test(v.$bigint)) return BigInt(v.$bigint);
    if (['-0', 'NaN', 'Infinity', '-Infinity'].includes(v.$number)) return Number(v.$number);
  }
  return v;
});
export const jsonValue = v => JSON.parse(stringify(v));
const esc = v => String(v).replaceAll('~', '~0').replaceAll('/', '~1');

function semantic(value) {
  if (Array.isArray(value)) return value.map(semantic);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).filter(([k]) => k !== 'optimized').map(([k, v]) => [k, k === 'record_flags' ? v.split(' | ').filter(f => f !== 'HAS_NON_OPTIMIZED_INFO').join(' | ') : semantic(v)]));
  return value;
}
export function leaves(value, base = '', record = '', out = []) {
  if (value && typeof value === 'object') {
    if (value.Record) record = value.Record.name;
    for (const [k, v] of Object.entries(value)) leaves(v, base + '/' + esc(k), record, out);
  } else out.push({ pointer: base, record, value: jsonValue(value), type: typeof value, editable: editable(base) });
  return out;
}
function editable(pointer) {
  return pointer.startsWith('/root_node/') && !/\/(optimized|record_flags|version|name)$/.test(pointer);
}
function setValue(tree, pointer, incoming) {
  if (!editable(pointer)) throw new Error('Only existing payload fields can be changed: ' + pointer);
  const keys = pointer.slice(1).split('/').map(k => k.replaceAll('~1', '/').replaceAll('~0', '~'));
  if (keys.some(k => ['__proto__', 'prototype', 'constructor'].includes(k))) throw new Error('Invalid pointer');
  let owner = tree;
  for (const key of keys.slice(0, -1)) {
    if (!owner || typeof owner !== 'object' || !Object.hasOwn(owner, key)) throw new Error('Pointer does not exist: ' + pointer);
    owner = owner[key];
  }
  const key = keys.at(-1);
  if (!owner || !Object.hasOwn(owner, key)) throw new Error('Pointer does not exist: ' + pointer);
  const old = owner[key];
  if (old === null || typeof old === 'object') throw new Error('Replace scalar values only');
  let value = incoming;
  if (typeof old === 'bigint') {
    if (typeof incoming !== 'string' || !/^-?\d{1,20}$/.test(incoming)) throw new Error('64-bit integers require a decimal string');
    value = BigInt(incoming);
  } else if (typeof old !== typeof incoming) throw new Error('Value type must remain ' + typeof old);
  if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('New numbers must be finite');
  if (typeof value === 'string' && (value.length > 65535 || value.includes('\0'))) throw new Error('Invalid string value');
  const kind = keys.findLast(k => /^(?:[UI](8|16|32|64)|F(32|64)|Coord[23]d|Ascii|Utf16|Unknown\d+)(?:Array)?$/.test(k));
  if (kind === 'Ascii' && /[^\x00-\x7f]/.test(value)) throw new Error('Ascii fields require ASCII text');
  const intMatch = /^(U|I)(8|16|32|64)(?:Array)?$/.exec(kind ?? '');
  if (intMatch) {
    if (typeof value === 'number' && !Number.isSafeInteger(value)) throw new Error('Expected an integer');
    const bits = BigInt(intMatch[2]), unsigned = intMatch[1] === 'U', num = BigInt(value);
    const min = unsigned ? 0n : -(1n << (bits - 1n)), max = unsigned ? (1n << bits) - 1n : (1n << (bits - 1n)) - 1n;
    if (num < min || num > max) throw new Error('Integer out of range for ' + kind);
  }
  if (/^(F32(?:Array)?|Coord[23]d)$/.test(kind ?? '')) {
    value = Math.fround(value);
    if (!Number.isFinite(value)) throw new Error('Float32 overflow');
  }
  owner[key] = value;
  return { pointer, before: jsonValue(old), after: jsonValue(value) };
}

export function processCsc(bytes, { edits = [], importedTree } = {}) {
  if (bytes.length < 16 || bytes.length > 16 * 1024 * 1024) throw new Error('CSC size must be 16 bytes to 16 MiB');
  const magic = Buffer.from(bytes).readUInt32LE(0);
  if (![0xabca, 0xabcb].includes(magic)) throw new Error('Only ABCA/ABCB CSC files are supported');
  initSync({ module: fs.readFileSync(new URL('./vendor/esf/rpfm_lib_esf_wasm_bg.wasm', import.meta.url)) });
  const original = decode(bytes);
  if (original.root_node?.Record?.name !== 'ROOT') throw new Error('Not a supported CSC ROOT tree');
  const baseline = decode(encode(original));
  if (!isDeepStrictEqual(semantic(original), semantic(baseline))) throw new Error('Codec cannot preserve this file; refusing edits');
  const tree = structuredClone(original);
  if (importedTree !== undefined) {
    const incoming = parse(importedTree);
    // Import edits existing payload fields only; do not guess scene counts or node schema.
    const before = leaves(original), after = leaves(incoming);
    if (before.length !== after.length) throw new Error('Structural edits are unsupported');
    edits = [];
    for (let i = 0; i < before.length; i++) {
      if (before[i].pointer !== after[i].pointer) throw new Error('Tree structure/order differs');
      if (!isDeepStrictEqual(before[i].value, after[i].value)) {
        const value = after[i].type === 'bigint' ? after[i].value.$bigint : after[i].value;
        edits.push({ pointer: before[i].pointer, value });
      }
    }
  }
  const seen = new Set();
  const changes = edits.map(e => {
    if (seen.has(e.pointer)) throw new Error('Duplicate edit pointer');
    seen.add(e.pointer);
    return setValue(tree, e.pointer, e.value);
  }).filter(c => !isDeepStrictEqual(c.before, c.after));
  const out = changes.length ? Buffer.from(encode(tree)) : Buffer.from(bytes);
  const readback = decode(out);
  if (!isDeepStrictEqual(semantic(tree), semantic(readback))) throw new Error('Encoded file failed full-tree readback verification');
  return { tree: stringify(readback), bytes: out, changes, leafCount: leaves(readback).length, signature: original.signature, unchanged: changes.length === 0 };
}
if (process.argv.includes('--csc-worker')) {
  process.once('message', data => {
    let response;
    try { response = { ok: true, result: processCsc(data.bytes, data.options) }; }
    catch (e) { response = { ok: false, error: e?.message ?? String(e) }; }
    process.send(response, () => process.disconnect());
  });
}
