// Checks that en.json and nl.json have the same keys and that every static t("...") key exists.
import fs from "node:fs";
import path from "node:path";
const root = path.resolve(import.meta.dirname, "../src");
const flat = (o, p = "") => Object.entries(o).flatMap(([k, v]) => (typeof v === "object" ? flat(v, p + k + ".") : [p + k]));
const en = new Set(flat(JSON.parse(fs.readFileSync(path.join(root, "i18n/en.json"), "utf8"))));
const nl = new Set(flat(JSON.parse(fs.readFileSync(path.join(root, "i18n/nl.json"), "utf8"))));
let bad = 0;
for (const k of en) if (!nl.has(k)) { console.error("missing in nl:", k); bad++; }
for (const k of nl) if (!en.has(k)) { console.error("missing in en:", k); bad++; }
const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : /\.tsx?$/.test(e.name) ? [path.join(d, e.name)] : []));
const re = /(?:\bt\(|i18nKey=)\s*["'`]([a-z_]+(?:\.[a-z0-9_]+)+)["'`]/g;
const prefixes = new Set();
for (const f of walk(root)) {
  const src = fs.readFileSync(f, "utf8");
  for (const m of src.matchAll(re)) if (!en.has(m[1])) { console.error(`unknown key ${m[1]} in ${path.relative(root, f)}`); bad++; }
  for (const m of src.matchAll(/t\(\s*`([a-z_.]+)\$\{/g)) prefixes.add(m[1]);
}
for (const p of prefixes) if (![...en].some((k) => k.startsWith(p))) { console.error("no keys for dynamic prefix", p); bad++; }
console.log(`${en.size} keys, dynamic prefixes: ${[...prefixes].join(", ")}`);
process.exit(bad ? 1 : 0);
