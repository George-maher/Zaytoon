import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve, sep } from 'node:path'

/**
 * Exact-case import guard.
 *
 * Vite resolves relative imports through the OS, so on Windows and macOS a
 * specifier whose casing disagrees with the directory entry — `./components/hud`
 * for a folder named `components/HUD` — works in development and breaks the
 * production build on any case-sensitive filesystem (Linux: CI, Docker, the
 * `docker compose` stack). It is also invisible to every test, because the four
 * files that import the HUD could not even be loaded.
 *
 * The fix has to be a check that does not ask the filesystem "does this exist?"
 * — that question is exactly the one a case-insensitive FS answers wrongly. It
 * asks instead "is there a directory entry whose name equals this string?",
 * which returns the same answer on every platform.
 */

// `import.meta.url` is a file URL only for modules Node loaded from disk; under
// Vitest's jsdom environment it is virtual, so fall back to the project root
// Vitest was pointed at (it reports that root as its cwd).
const PROJECT_ROOT =
  typeof import.meta.dirname === 'string' && import.meta.dirname
    ? join(import.meta.dirname, '..')
    : process.cwd()
const EXTENSIONS = ['', '.js', '.jsx', '.json', '.css']

function sourceFiles(dir, found = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (entry === 'node_modules' || entry.startsWith('.')) continue
    if (statSync(full).isDirectory()) sourceFiles(full, found)
    else if (/\.(js|jsx)$/.test(entry)) found.push(full)
  }
  return found
}

function entriesOf(dir) {
  try {
    return new Set(readdirSync(dir))
  } catch {
    return null
  }
}

/** Whether `spec` (relative to `fromFile`) names a real entry, casing included. */
function resolvesExactly(fromFile, spec) {
  const target = resolve(dirname(fromFile), spec)
  const base = target.split(sep).pop()
  const parent = entriesOf(dirname(target))
  if (!parent) return false

  if (EXTENSIONS.some((ext) => parent.has(base + ext))) return true

  // A directory specifier must contain an index file of the same casing.
  if (parent.has(base)) {
    const inner = entriesOf(target)
    if (inner) return EXTENSIONS.some((ext) => inner.has(`index${ext}`))
  }
  return false
}

describe('relative import casing', () => {
  const files = sourceFiles(PROJECT_ROOT)

  it('scans a non-trivial number of files', () => {
    // An empty scan would make the suite below vacuously green.
    expect(files.length).toBeGreaterThan(40)
  })

  it.each(files.map((file) => [file.slice(PROJECT_ROOT.length + 1), file]))(
    '%s resolves every relative import with exact casing',
    (_name, file) => {
      const source = readFileSync(file, 'utf8')
      const lines = source.split('\n')
      const bad = []
      for (const match of source.matchAll(
        /(?:from|import)\s*\(?\s*['"](\.[^'"]+)['"]/g,
      )) {
        // JSDoc examples (`Usage: import x from './y'`) are prose, not module
        // specifiers. A real import never sits on a comment-continuation line.
        const line = lines[source.slice(0, match.index).split('\n').length - 1].trim()
        if (/^(\*|\/\/|\/\*)/.test(line)) continue
        if (!resolvesExactly(file, match[1])) bad.push(match[1])
      }
      expect(
        bad,
        `${_name} imports ${bad.join(', ')} — no entry with that exact casing exists`,
      ).toEqual([])
    },
  )
})
