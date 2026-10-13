// Offline mirror of bestgaa/category_classifier.py. Scores are not probabilities.
const OPAQUE_HOSTS = new Set(['bit.ly', 'bitli.in', 'bitl.in', 'bitly.com', 'amzn.to',
  'amzn.in', 'a.co', 'ekaro.in', 'clnk.in', 't.me', 'telegram.me', 'tinyurl.com',
  'cutt.ly', 'fkrt.co', 'fkrt.cc', 'hypd.store'])
const URL_RE = /https?:\/\/[^\s<>\[\](){}"']+/giu
function decodeCategoryEntities(value) {
  return String(value || '')
    .replace(/&#(x[0-9a-f]+|[0-9]+);/giu, (_, code) => {
      const n = code[0].toLowerCase() === 'x' ? parseInt(code.slice(1), 16) : Number(code)
      return n > 0 && n <= 0x10ffff ? String.fromCodePoint(n) : ' '
    })
    .replace(/&(amp|quot|apos|lt|gt|nbsp|NewLine|Tab);/gu, (_, name) =>
      ({amp:'&',quot:'"',apos:"'",lt:'<',gt:'>',nbsp:' ',NewLine:'\n',Tab:'\t'})[name])
}
function categoryLines(value) {
  return String(value).split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/u)
}
function normalizeCategoryPhrase(value) {
  return decodeCategoryEntities(value)
    .normalize('NFKC')
    .toLocaleLowerCase('en-IN')
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim()
    .replace(/\s+/g, ' ')
}
const CATEGORY_QUERY_KEYS = new Set([
  'k', 'q', 'keyword', 'keywords', 'search', 'searchterm', 'query',
  'title', 'name', 'product', 'productname',
])
const CATEGORY_URL_STOP_SEGMENTS = new Set([
  '', 'dp', 'gp', 'aw', 's', 'b', 'p', 'product', 'products', 'item',
  'items', 'detail', 'details', 'buy', 'shop', 'offers', 'offer', 'ref',
])
function categoryWordsFromUrl(value) {
  try {
    const input = decodeCategoryEntities(value).trim()
    if (input.includes('\\') || /%(?![a-fA-F0-9]{2})/u.test(input)) return ''
    const rawParts = input.match(/^https?:\/\/([^/?#]+)([^?#]*)/iu)
    if (!rawParts || rawParts[1].includes('%')) return ''
    const url = new URL(input)
    if (url.username || url.password) return ''
    const decodedPath = decodeURIComponent(rawParts[2])
    if (decodedPath.split('/').some(part => part === '.' || part === '..')
      || /[\t-\r\x1c-\x1f\x85\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u.test(decodedPath)) return ''
    if (!['http:', 'https:'].includes(url.protocol) || [...OPAQUE_HOSTS].some(host =>
      url.hostname === host || url.hostname.endsWith('.'+host))) return ''
    const words = []
    let previous = ''
    for (const raw of decodedPath.split('/')) {
      const isIdentifier = ['p', 'dp', 'product', 'item'].includes(previous.toLowerCase())
      previous = raw
      if (isIdentifier) continue
      const segment = raw
      const normalized = normalizeCategoryPhrase(segment.replace(/[-_]+/gu, ' '))
      if (!normalized || CATEGORY_URL_STOP_SEGMENTS.has(normalized)) continue
      if (/^\d{4,}$/u.test(segment)) continue
      if (/^(?:itm|pid|sku|asin|product)[-_]?[a-z0-9]{5,}$/iu.test(raw)) continue
      if (/^(?=[A-Z0-9]{8,}$)(?=.*[A-Z])(?=.*[0-9])[A-Z0-9]+$/u.test(raw)) continue
      words.push(normalized)
    }
    for (const [key, val] of url.searchParams.entries()) {
      if (CATEGORY_QUERY_KEYS.has(key.toLowerCase())) {
        for (const part of categoryLines(decodeCategoryEntities(val))) {
          const normalized = normalizeCategoryPhrase(part)
          if (normalized) words.push(normalized)
        }
      }
    }
    return words.join('\n')
  } catch {
    return ''
  }
}

export function classifyCategory(text, linkContext = [], rules = {}) {
  const result = { category: null, reason: 'no_evidence', scores: {}, evidence: [],
    rules_version: rules.version || 0 }
  if (typeof text !== 'string' || !text.trim()) return result
  if (typeof linkContext === 'string') linkContext = [linkContext]
  const urls = [...(text.match(URL_RE) || [])]
  for (const value of linkContext || []) {
    if (typeof value === 'string') urls.push(...(value.match(URL_RE) || []))
  }
  const segments = categoryLines(text.replace(URL_RE, '\n'))
    .flatMap(line => categoryLines(decodeCategoryEntities(line)).map(part => ['text', part]))
  for (const url of new Set(urls)) {
    for (const part of categoryWordsFromUrl(url).split('\n')) segments.push(['link', part])
  }
  const seen = new Set()
  for (const [source, segment] of segments) {
    const normalized = ` ${normalizeCategoryPhrase(segment)} `
    const matches = []
    for (const definition of rules.categories || []) {
      for (const [rawAlias, weight] of definition.aliases || []) {
        const alias = normalizeCategoryPhrase(rawAlias)
        if (!alias) continue
        let from = 0
        while (from < normalized.length) {
          const pos = normalized.indexOf(` ${alias} `, from)
          if (pos < 0) break
          matches.push({ start: pos + 1, end: pos + 1 + alias.length,
            key: definition.key, alias, weight })
          from = pos + 1
        }
      }
    }
    for (const m of matches) {
      if (matches.some(n => n.start <= m.start && n.end >= m.end
        && (n.start < m.start || n.end > m.end) && n.weight >= m.weight)) continue
      const id = `${m.key}:${m.alias}`
      if (seen.has(id)) continue
      seen.add(id)
      result.scores[m.key] = (result.scores[m.key] || 0) + m.weight
      result.evidence.push({ category: m.key, phrase: m.alias, weight: m.weight, source })
    }
  }
  const scores = Object.entries(result.scores).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  if (!scores.length) return result
  if (scores.length > 1) { result.reason = 'mixed_or_conflicting'; return result }
  const [key, score] = scores[0]
  const minimum = rules.min_score ?? 3
  if (score < minimum || !result.evidence.some(e => e.weight >= minimum)) {
    result.reason = 'insufficient_product_evidence'; return result
  }
  result.category = key
  result.reason = 'matched'
  return result
}
