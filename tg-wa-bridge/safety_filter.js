/*
 * Public-channel content safety — the WhatsApp side of the same policy.
 *
 * `bestgaa/safety_rules.json` is the single source of truth; this file is a
 * line-for-line port of `bestgaa/safety_filter.py` so that the bot (Telegram)
 * and the bridge (WhatsApp) reach the SAME verdict for the same text.
 * `test_safety_filter.py` loads both and compares them on a shared corpus —
 * if anyone edits one side only, that test fails.
 *
 * The normalisation steps, in order, are identical on both sides:
 *   NFKC -> lowercase -> leet fold (0o 1i 3e 4a 5s 7t @a $s)
 *        -> join dotted/spaced single characters ("s.e.x" -> "sex")
 *        -> blank out the allow-list words -> match the policy patterns.
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const RULES_PATH = path.join(HERE, '..', 'bestgaa', 'safety_rules.json')

const URL_RE = /https?:\/\/[^\s<>\[\](){}"']+/gi;
// Captions often name a site without a scheme ("1xbet.com"). Only blocklisted
// domains are acted on, so collecting them broadly is safe.
const DOMAIN_RE = /(?<![@A-Za-z0-9_.-])(?:[A-Za-z0-9][A-Za-z0-9-]*\.)+[A-Za-z]{2,24}(?![A-Za-z0-9_-])/g;
const LEET = { '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '@': 'a', $: 's' };
// ASCII-only classes, matching the Python side (`re.ASCII`).
const JOIN_EVASION = /(?<![A-Za-z0-9_])(?:[A-Za-z0-9_][^A-Za-z0-9]+){2,}[A-Za-z0-9_](?![A-Za-z0-9_])/g;

function flag(name, dflt) {
  const v = process.env[name];
  if (v === undefined || v === null || v === '') return dflt;
  return ['1', 'true', 'yes', 'on'].includes(String(v).trim().toLowerCase());
}

// Read at CALL time (like the Python side) so a switch change never needs a
// process restart to take effect.
const safetyEnabled = () => !flag('SAFETY_FILTER_DISABLED', false);
const allowIntimate = () => flag('SAFETY_ALLOW_INTIMATE_PRODUCTS', false);
const strictReview = () => flag('SAFETY_STRICT_REVIEW', false);
const allowSuggestive = () => flag('SAFETY_ALLOW_SUGGESTIVE', false);
const SAFETY_ENABLED = !flag('SAFETY_FILTER_DISABLED', false);
const ALLOW_INTIMATE = flag('SAFETY_ALLOW_INTIMATE_PRODUCTS', false);
const STRICT_REVIEW = flag('SAFETY_STRICT_REVIEW', false);
const ALLOW_SUGGESTIVE = flag('SAFETY_ALLOW_SUGGESTIVE', false);

let CACHE = null;

function loadRules(file) {
  const rulesPath = file || process.env.SAFETY_RULES_PATH || RULES_PATH;
  const data = JSON.parse(fs.readFileSync(rulesPath, 'utf8'));
  if (!Array.isArray(data.categories) || !data.categories.length) {
    throw new Error(`${rulesPath}: no categories`);
  }
  return {
    version: String(data.version || ''),
    categories: data.categories.map((c) => ({
      key: String(c.key),
      label: String(c.label || c.key),
      severity: String(c.severity || 'block'),
      why: String(c.why || ''),
      patterns: (c.patterns || []).map((p) => new RegExp(p, 'g')),
      domains: (c.domains || []).map((d) => String(d).toLowerCase().replace(/^\.+|\.+$/g, '')),
    })),
    allow: (data.allow || []).map((p) => new RegExp(p, 'g')),
  };
}

function rules() {
  if (CACHE === null) CACHE = loadRules();
  return CACHE;
}

function resetCache() { CACHE = null; }

function normalize(text) {
  const raw = String(text || '').normalize('NFKC').toLowerCase();
  const folded = raw.replace(/[013457@$]/g, (ch) => LEET[ch]);
  const joined = folded.replace(JOIN_EVASION, (m) => m.replace(/[^A-Za-z0-9]+/g, ''));
  const variants = [folded];
  if (joined !== folded) variants.push(joined);
  return variants;
}

function applyAllow(text, allow) {
  let out = text;
  for (const pattern of allow) {
    pattern.lastIndex = 0;
    out = out.replace(pattern, (m) => ' '.repeat(Math.max(1, m.length)));
  }
  return out;
}

function hosts(urls) {
  const out = [];
  for (const url of urls) {
    try {
      let body = String(url).trim().replace(/^[a-z]+:\/\//i, '');
      body = body.split('/')[0].split('?')[0].split('#')[0];
      body = body.split('@').pop().split(':')[0].replace(/^\.+|\.+$/g, '').toLowerCase();
      if (body) out.push(body);
    } catch (e) { /* a junk URL is not a crash */ }
  }
  return out;
}

function domainHit(host, domains) {
  for (const domain of domains) {
    if (host === domain || host.endsWith('.' + domain)) return domain;
  }
  return '';
}

function operatorExtra() {
  const extra = String(process.env.SAFETY_EXTRA_BLOCK_TERMS || '')
    .split(',').map((t) => t.trim()).filter(Boolean);
  const patterns = [];
  for (const term of extra) {
    const escaped = term.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try { patterns.push(new RegExp('\\b' + escaped + '\\b', 'g')); } catch (e) { /* skip */ }
  }
  const allowTerms = String(process.env.SAFETY_ALLOW_TERMS || '')
    .split(',').map((t) => t.trim().toLowerCase()).filter(Boolean);
  return { patterns, allowTerms };
}

function search(pattern, text) {
  pattern.lastIndex = 0;
  const m = pattern.exec(text);
  return m ? m[0] : '';
}

function scan(text, urls) {
  const verdict = {
    blocked: false, severity: 'clean', category: '', label: '', matched: '',
    reason: '', version: '',
  };
  if (!safetyEnabled()) {
    verdict.reason = 'filter disabled (SAFETY_FILTER_DISABLED)';
    return verdict;
  }
  const data = rules();
  verdict.version = data.version;
  const source = String(text || '');
  const links = urls === undefined || urls === null
    ? (source.match(URL_RE) || []).concat(source.match(DOMAIN_RE) || [])
    : urls.map(String);
  const hostList = hosts(links);
  const variants = normalize(source).map((v) => applyAllow(v, data.allow));
  const { patterns: extraPatterns, allowTerms } = operatorExtra();

  function finding(category, match, reason) {
    let severity = category.severity;
    if (category.key === 'suggestive' && allowSuggestive()) severity = 'review';
    if (severity === 'review' && strictReview()) severity = 'block';
    return {
      blocked: severity === 'block',
      severity,
      category: category.key,
      label: category.label,
      matched: String(match).slice(0, 80),
      reason: reason || category.why || '',
      version: data.version,
    };
  }

  for (const pattern of extraPatterns) {
    for (const variant of variants) {
      const hit = search(pattern, variant);
      if (hit && !allowTerms.includes(hit.trim().toLowerCase())) {
        return finding({
          key: 'operator_term', label: 'Operator block term', severity: 'block',
          why: 'an operator-configured blocked term (SAFETY_EXTRA_BLOCK_TERMS) appears in this post',
        }, hit, '');
      }
    }
  }

  for (const category of data.categories) {
    if (category.key === 'intimate_product' && allowIntimate()) continue;
    for (const host of hostList) {
      const domain = domainHit(host, category.domains);
      if (domain) return finding(category, host, category.why);
    }
    for (const pattern of category.patterns) {
      for (const variant of variants) {
        const hit = search(pattern, variant);
        if (!hit) continue;
        if (allowTerms.includes(hit.trim().toLowerCase())) continue;
        return finding(category, hit, category.why);
      }
    }
  }
  return verdict;
}

function isSafe(text, urls) { return !scan(text, urls).blocked; }

function policySummary() {
  const data = safetyEnabled() ? rules() : { version: '', categories: [] };
  return {
    version: data.version,
    enabled: safetyEnabled(),
    strict_review: strictReview(),
    allow_suggestive: allowSuggestive(),
    allow_intimate: allowIntimate(),
    categories: data.categories.map((c) => ({
      key: c.key, label: c.label, severity: c.severity, why: c.why,
      patterns: c.patterns.length, domains: c.domains.length,
    })),
  };
}

export {
  RULES_PATH, loadRules, rules, resetCache, normalize, scan, isSafe,
  policySummary, safetyEnabled, allowIntimate, allowSuggestive, strictReview,
  SAFETY_ENABLED, ALLOW_INTIMATE, STRICT_REVIEW, ALLOW_SUGGESTIVE,
};
export default scan;

const RUN_AS_SCRIPT = process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (RUN_AS_SCRIPT) {
  const args = process.argv.slice(2);
  if (!args.length) {
    console.log(JSON.stringify(policySummary(), null, 2));
    process.exit(0);
  }
  let bad = false;
  for (const text of args) {
    const v = scan(text);
    if (v.blocked) bad = true;
    console.log(`${v.severity.toUpperCase().padEnd(7)} ${(v.category || '-').padEnd(16)} ${JSON.stringify(text.slice(0, 70))}`);
    if (v.reason) console.log(`        reason: ${v.reason}`);
  }
  process.exit(bad ? 1 : 0);
}
