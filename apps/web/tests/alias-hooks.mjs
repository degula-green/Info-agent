// The api modules use the "@/" alias from vite.config.ts. Plain `node --test`
// has no alias support, so map it to src/ (with the TypeScript extensions the
// source files use) before handing the specifier to the default resolver.
import { existsSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const sourceRoot = new URL('../src/', import.meta.url)

function firstExisting(base) {
  for (const candidate of [base, new URL(`${base.href}.ts`), new URL(`${base.href}.tsx`), new URL(`${base.href}/index.ts`)]) {
    if (existsSync(fileURLToPath(candidate))) return candidate
  }
  return null
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    // "@/x" -> src/x，以及 "./x" / "../x" 这种没写扩展名的相对导入。
    const isAlias = specifier.startsWith('@/')
    const isExtensionlessRelative = /^\.{1,2}\//.test(specifier) && extname(specifier) === ''
    if (isAlias || isExtensionlessRelative) {
      const base = isAlias ? new URL(specifier.slice(2), sourceRoot) : new URL(specifier, context.parentURL)
      const resolved = firstExisting(base)
      if (resolved) return nextResolve(resolved.href, context)
    }
    return nextResolve(specifier, context)
  },
})
