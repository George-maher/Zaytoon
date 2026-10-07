/**
 * The tree-folding model.
 *
 * Pure, and separate from the component, because "which nodes are open" is the
 * part with the interesting rules and the part a test can actually reach.
 *
 * The rules that matter:
 *
 * - **A collapsed parent hides its children from the flat list.** Not "renders
 *   them greyed out" — hides them. An explorer showing the contents of a
 *   collapsed folder is not a tree, it is a file listing with indentation.
 * - **Opening a directory loads its children lazily.** A depth-first render of
 *   an already-loaded tree is fine; discovering the whole project on first paint
 *   is what makes an explorer feel slow.
 * - **Searching reveals matches wherever they are.** Otherwise "search found 3
 *   matches" is a dead end when all three are inside collapsed folders.
 */

const isDir = (node) => Boolean(node && node.is_dir);

/**
 * Flatten the tree into render order, honouring what is open.
 *
 * @param {Array<object>} entries    root-level entries
 * @param {Set<string>} expanded     paths whose children should be shown
 * @param {number} depth             the level the entries are at (for the indent)
 * @returns {Array<{node: object, depth: number}>}
 */
export function flatten(entries, expanded, depth = 0) {
  const out = [];
  for (const node of entries || []) {
    out.push({ node, depth });
    // A node the server marked skipped (node_modules and friends) is listed so
    // the user can see it exists, but its contents are never walked — they are
    // thousands of files the assistant has no business reading.
    if (isDir(node) && expanded.has(node.path) && !node.skipped) {
      out.push(...flatten(node.children || [], expanded, depth + 1));
    }
  }
  return out;
}

/**
 * Every directory in the tree, whether open or not.
 *
 * Used to reveal a search match's parents: to *show* a node inside a collapsed
 * folder, every folder above it has to be open.
 */
export function directoriesOf(entries, acc = []) {
  for (const node of entries || []) {
    if (isDir(node)) {
      acc.push(node.path);
      if (!node.skipped) directoriesOf(node.children || [], acc);
    }
  }
  return acc;
}

/**
 * The set of directories that must be open for `path` to be visible.
 *
 * Every **ancestor** of `path`, with the final segment dropped: a file lives
 * inside a folder, so the folder is what has to be open. Returning `path`
 * itself would open the file's *parent* one level short.
 */
export function ancestorsOf(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  // Drop the last segment: it is the entry itself, not a folder containing it.
  parts.pop();
  const out = [];
  let current = "";
  for (const part of parts) {
    current = current ? `${current}/${part}` : part;
    out.push(current);
  }
  return out;
}

/**
 * Find one entry by path, searching the whole tree.
 *
 * Depth-first, and does not stop at collapsed nodes — a collapsed folder is a
 * *view* state, not a reason to claim a file does not exist.
 */
export function findByPath(entries, path) {
  for (const node of entries || []) {
    if (node.path === path) return node;
    if (isDir(node)) {
      const found = findByPath(node.children || [], path);
      if (found) return found;
    }
  }
  return null;
}

/**
 * Filter the tree to paths matching `query`, keeping the ancestors of each hit.
 *
 * A hit whose parent folder is kept but whose own children are not is the whole
 * point: "3 matches in src/api" is navigable, whereas a flat list of paths is
 * not.
 *
 * @returns {{entries: Array<object>, matches: string[]}}
 */
export function filterTree(entries, query) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return { entries: entries || [], matches: [] };

  // One recursive pass, collecting matches as a side effect. Written as a
  // single function rather than a helper plus a near-duplicate closure: the two
  // copies drifted the moment one of them changed, and a filter that reports
  // different matches than it keeps is worse than no filter.
  const matches = [];

  const walk = (nodes) => {
    const kept = [];
    for (const node of nodes || []) {
      const selfMatches = String(node.name || "").toLowerCase().includes(needle);

      if (isDir(node)) {
        // A directory is kept when it matches itself *or* when something inside
        // it matched — otherwise a hit three levels down has no path to the
        // root and "3 matches" is a dead end.
        const children = walk(node.children || []);
        if (selfMatches || children.length > 0) {
          // A self-matching directory keeps its whole subtree: the user asked
          // for `src`, so showing what is inside it is the answer, not a
          // narrower one they did not ask for.
          kept.push(selfMatches ? node : { ...node, children });
        }
      } else if (selfMatches) {
        kept.push(node);
      }

      if (selfMatches) matches.push(node.path);
    }
    return kept;
  };

  return { entries: walk(entries || []), matches };
}

/** An icon-ish glyph for an entry. Text, not colour — colour is not the only channel. */
export function glyphFor(node) {
  if (!node) return "";
  if (isDir(node)) return node.expanded ? "▾" : "▸";
  const name = String(node.name || "");
  if (name.startsWith(".")) return "·";
  return name.includes(".") ? name.split(".").pop().slice(0, 3).toUpperCase() : "—";
}

export default flatten;