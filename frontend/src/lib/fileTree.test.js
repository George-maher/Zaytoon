import { describe, it, expect } from "vitest";
import { ancestorsOf, directoriesOf, filterTree, findByPath, flatten, glyphFor } from "./fileTree";

const TREE = [
  {
    name: "src",
    path: "src",
    is_dir: true,
    children: [
      { name: "app.py", path: "src/app.py", is_dir: false },
      {
        name: "api",
        path: "src/api",
        is_dir: true,
        children: [{ name: "routes.py", path: "src/api/routes.py", is_dir: false }],
      },
    ],
  },
  { name: "README.md", path: "README.md", is_dir: false },
];

describe("flatten", () => {
  it("shows only the top level when nothing is open", () => {
    const rows = flatten(TREE, new Set());
    expect(rows.map((r) => r.node.path)).toEqual(["src", "README.md"]);
    expect(rows.every((r) => r.depth === 0)).toBe(true);
  });

  it("hides a collapsed directory's contents", () => {
    // Not "renders them greyed out" — hides them. An explorer showing the
    // contents of a collapsed folder is a file listing with indentation.
    const rows = flatten(TREE, new Set());
    expect(rows.some((r) => r.node.path === "src/app.py")).toBe(false);
  });

  it("reveals one level when only that folder is open", () => {
    const rows = flatten(TREE, new Set(["src"]));
    expect(rows.map((r) => r.node.path)).toEqual([
      "src",
      "src/app.py",
      "src/api",
      "README.md",
    ]);
  });

  it("indents deeper levels", () => {
    const rows = flatten(TREE, new Set(["src", "src/api"]));
    const api = rows.find((r) => r.node.path === "src/api/routes.py");
    expect(api.depth).toBe(2);
  });

  it("walks the whole chain when everything is open", () => {
    const rows = flatten(TREE, new Set(["src", "src/api"]));
    expect(rows.map((r) => r.node.path)).toContain("src/api/routes.py");
  });

  it("never walks a skipped directory", () => {
    // node_modules and friends are listed so the user knows they exist, but
    // their contents are thousands of files the assistant has no business
    // reading.
    const tree = [
      { name: "node_modules", path: "node_modules", is_dir: true, skipped: true, children: [] },
    ];
    expect(flatten(tree, new Set(["node_modules"]))).toHaveLength(1);
  });

  it("handles no entries", () => {
    expect(flatten(null, new Set())).toEqual([]);
    expect(flatten(undefined, new Set())).toEqual([]);
  });
});

describe("directoriesOf", () => {
  it("lists every directory, open or not", () => {
    expect(directoriesOf(TREE)).toEqual(["src", "src/api"]);
  });

  it("handles a leaf", () => {
    expect(directoriesOf([{ name: "a.txt", path: "a.txt", is_dir: false }])).toEqual([]);
  });
});

describe("ancestorsOf", () => {
  it("returns the parent chain, without the entry itself", () => {
    // A file lives inside a folder: the folder is what has to be open. Returning
    // the file's own path would open the parent one level short.
    expect(ancestorsOf("src/api/routes.py")).toEqual(["src", "src/api"]);
  });

  it("returns nothing for a root-level file", () => {
    expect(ancestorsOf("README.md")).toEqual([]);
  });

  it("returns nothing for nothing", () => {
    expect(ancestorsOf("")).toEqual([]);
    expect(ancestorsOf(null)).toEqual([]);
  });

  it("does not open the entry itself", () => {
    const chain = ancestorsOf("src/app.py");
    expect(chain).not.toContain("src/app.py");
  });
});

describe("findByPath", () => {
  it("finds a top-level entry", () => {
    expect(findByPath(TREE, "README.md")?.name).toBe("README.md");
  });

  it("finds a deeply nested entry", () => {
    expect(findByPath(TREE, "src/api/routes.py")?.name).toBe("routes.py");
  });

  it("finds inside a collapsed folder", () => {
    // A collapsed folder is a *view* state, not a reason to claim a file does
    // not exist.
    expect(findByPath(TREE, "src/app.py")).toBeTruthy();
  });

  it("returns null for something absent", () => {
    expect(findByPath(TREE, "nope.txt")).toBeNull();
  });
});

describe("filterTree", () => {
  it("keeps only matches at the top level", () => {
    const { entries, matches } = filterTree(TREE, "readme");
    expect(matches).toEqual(["README.md"]);
    expect(entries.map((e) => e.path)).toEqual(["README.md"]);
  });

  it("keeps the ancestors of a nested match", () => {
    // Otherwise "1 match in src/api" is a dead end — the hit is three folders
    // down with none of them shown.
    const { entries, matches } = filterTree(TREE, "routes");
    expect(matches).toEqual(["src/api/routes.py"]);
    expect(entries).toHaveLength(1);
    expect(entries[0].path).toBe("src");
    expect(entries[0].children[0].path).toBe("src/api");
    expect(entries[0].children[0].children[0].path).toBe("src/api/routes.py");
  });

  it("keeps a matching directory's whole subtree", () => {
    // The user asked for `api`; showing what is inside it is the answer, not a
    // narrower one they did not ask for.
    const { entries } = filterTree(TREE, "api");
    const api = entries[0].children[0];
    expect(api.path).toBe("src/api");
    expect(api.children).toHaveLength(1);
  });

  it("matches case-insensitively", () => {
    expect(filterTree(TREE, "routes.py").matches).toEqual(["src/api/routes.py"]);
  });

  it("returns everything for an empty query", () => {
    const { entries, matches } = filterTree(TREE, "   ");
    expect(entries).toBe(TREE);
    expect(matches).toEqual([]);
  });

  it("returns nothing when nothing matches", () => {
    const { entries, matches } = filterTree(TREE, "zzzz");
    expect(entries).toEqual([]);
    expect(matches).toEqual([]);
  });

  it("the kept entries and the reported matches agree", () => {
    // A filter that keeps different files than it reports is worse than none.
    const { entries, matches } = filterTree(TREE, "e");
    const kept = flatten(entries, new Set(["src", "src/api"]));
    const keptPaths = kept.map((r) => r.node.path).filter((p) => p !== "src" && p !== "src/api");
    expect(new Set(keptPaths)).toEqual(new Set(matches));
  });
});

describe("glyphFor", () => {
  it("marks directories", () => {
    expect(glyphFor({ is_dir: true })).toBe("▸");
    expect(glyphFor({ is_dir: true, expanded: true })).toBe("▾");
  });

  it("shows an extension for a file", () => {
    expect(glyphFor({ name: "app.py", is_dir: false })).toBe("PY");
    expect(glyphFor({ name: "routes.tsx", is_dir: false })).toBe("TSX");
  });

  it("marks a dotfile quietly", () => {
    expect(glyphFor({ name: ".gitignore", is_dir: false })).toBe("·");
  });

  it("falls back for an extensionless file", () => {
    expect(glyphFor({ name: "Makefile", is_dir: false })).toBe("—");
  });

  it("handles nothing", () => {
    expect(glyphFor(null)).toBe("");
  });
});