// Loads plugin/local.munin/Model.js outside QML.
//
// A QML `.pragma library` file is ordinary ECMAScript with two QML-only
// directives bolted on the front (`.pragma`, `.import`), which plain JS
// refuses to parse. Stripping those two line forms is most of the shim; the
// rest is collecting the top-level declarations, which is how the QML engine
// exposes a library to the files that import it.
//
// The body is evaluated with `new Function` rather than in a `node:vm`
// context on purpose: a vm context has its own intrinsics, so every array and
// object the module returned would fail a strict deep-equality check against
// one built in the test file, for no reason a reader would ever guess.
//
// Doing it this way rather than converting Model.js to an ES module means the
// file the shell loads and the file the test loads are byte-for-byte the same
// one. A copy would drift.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const MODEL_PATH = join(here, "..", "..", "plugin", "local.munin", "Model.js");

// Only whole lines whose first non-space character starts a QML directive.
const DIRECTIVE = /^[ \t]*\.(pragma|import)\b.*$/gm;

// Top-level `function f(` / `var v =` declarations, at column zero. Anything
// indented is inside another function and is none of the importer's business,
// which is also how QML scopes a library.
const TOP_LEVEL = /^(?:function\s+([A-Za-z_$][\w$]*)\s*\(|var\s+([A-Za-z_$][\w$]*)\s*=)/gm;

export function loadModel(path = MODEL_PATH) {
  const source = readFileSync(path, "utf8").replace(DIRECTIVE, "");

  const names = [];
  for (const match of source.matchAll(TOP_LEVEL)) {
    const name = match[1] || match[2];
    if (name && !names.includes(name)) names.push(name);
  }
  if (names.length === 0) throw new Error(`no top-level declarations found in ${path}`);

  const body = `${source}\nreturn { ${names.map((n) => `${n}: ${n}`).join(", ")} };`;
  // eslint-disable-next-line no-new-func
  return new Function(body)();
}
