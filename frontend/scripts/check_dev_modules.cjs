/**
 * Headless runtime check of the Vite dev server.
 *
 * There is no browser attached to this session, so this covers the part of the
 * "does it load" question that can be answered without one: every source file
 * is requested from the dev server exactly as the browser would, and Vite
 * returns 500 with the transform error if a module has a syntax error or an
 * unresolvable import. That is the failure mode behind a blank screen.
 */
const BASE = "http://localhost:5173";
const fs = require("fs");
const path = require("path");

const SRC = path.resolve(__dirname, "..", "src");

function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (/\.(jsx?|css)$/.test(e.name)) out.push(p);
  }
  return out;
}

(async () => {
  const files = walk(SRC);
  console.log(`Checking ${files.length} source modules against ${BASE}\n`);
  let bad = 0;
  for (const f of files) {
    const rel = "/" + path.relative(SRC, f).split(path.sep).join("/");
    const url = BASE + rel;
    let res, body;
    try {
      res = await fetch(url);
      body = await res.text();
    } catch (e) {
      console.log(`  NETWORK  ${rel}\n           ${e.message}`);
      bad++;
      continue;
    }
    if (!res.ok) {
      const first = body.split("\n").find((l) => l.trim()) || "";
      console.log(`  ${res.status}     ${rel}`);
      console.log(`           ${first.slice(0, 160)}`);
      bad++;
    } else if (rel.endsWith(".css") && /Failed to resolve|error/i.test(body.slice(0, 400))) {
      console.log(`  CSS-ERR  ${rel}`);
      bad++;
    }
  }

  // Assets referenced by index.html must resolve too.
  const html = await (await fetch(BASE + "/")).text();
  const assets = [...html.matchAll(/(?:src|href)="([^"]+)"/g)]
    .map((m) => m[1])
    .filter((u) => u.startsWith("/") && !u.startsWith("//"));
  console.log(`\nindex.html references ${assets.length} local assets:`);
  for (const a of assets) {
    const r = await fetch(BASE + a);
    console.log(`  ${r.ok ? "OK  " : "MISS"} ${a}`);
    if (!r.ok) bad++;
  }

  console.log(`\n${bad === 0 ? "PASS" : "FAIL"}: ${bad} module(s) failed to load`);
  process.exit(bad === 0 ? 0 : 1);
})();
