// Integer-safe JSON parsing for the plan contract API (plan 021 §3, codex-hekate msgs 592/615)
//
// The API emits Int64 counters as JSON numbers; a JS Number silently rounds above 2^53-1.
// parseContractJson scans the WHOLE raw text before JSON.parse and fails closed:
//   - digits inside strings (escape-aware) are ignored;
//   - every numeric token outside strings must be a plain JSON integer within +-(2^53-1),
//     checked with BigInt (unsafe_integer otherwise);
//   - any fraction or exponent token is rejected (unsupported_number) — the contract has no
//     non-integer numbers, and this also stops exponent-encoded unsafe values;
//   - anything else that is not well-formed JSON is malformed_json.
// Top-level numeric fields named in `captureKeys` are returned as their checked SOURCE TEXT,
// so cursors are passed back byte-for-byte and never round-trip through a float.
//
// Depends on: nothing
// Used by: planContract/api.ts, e2e/managed-json.spec.ts

export type ContractJsonErrorCode = 'unsafe_integer' | 'unsupported_number' | 'malformed_json';

export class ContractJsonError extends Error {
  readonly code: ContractJsonErrorCode;
  constructor(code: ContractJsonErrorCode, message: string) {
    super(message);
    this.name = 'ContractJsonError';
    this.code = code;
  }
}

export interface CheckedJson<T> {
  value: T;
  /** Checked source text of top-level numeric fields named in captureKeys (absent if null/missing). */
  captured: Record<string, string>;
}

const MAX_SAFE = 9007199254740991n;
const NUMBER = /-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/y;
const HEX4 = /^[0-9a-fA-F]{4}$/;

export function parseContractJson<T = unknown>(text: string, captureKeys: readonly string[] = []): CheckedJson<T> {
  const captured: Record<string, string> = {};
  const stack: string[] = [];        // '{' or '['
  let lastKey: string | null = null; // key awaiting its value at the current object level
  let i = 0;
  const n = text.length;

  const fail = (code: ContractJsonErrorCode, msg: string): never => {
    throw new ContractJsonError(code, `${msg} (at offset ${i})`);
  };

  while (i < n) {
    const c = text[i];
    if (c === ' ' || c === '\t' || c === '\n' || c === '\r') { i++; continue; }
    if (c === '"') {
      const start = i + 1;
      i++;

      for (;;) {
        if (i >= n) fail('malformed_json', 'Unterminated string');
        const ch = text[i];
        if (ch === '"') break;
        if (ch === '\\') {
          const esc = text[i + 1];
          if (esc === 'u') {
            if (!HEX4.test(text.slice(i + 2, i + 6))) fail('malformed_json', 'Invalid \\u escape');
            i += 6;
          } else if (esc !== undefined && '"\\/bfnrt'.includes(esc)) {
            i += 2;
          } else {
            fail('malformed_json', 'Invalid escape');
          }
          continue;
        }
        if (ch < ' ') fail('malformed_json', 'Control character in string');
        i++;
      }
      const raw = text.slice(start, i);
      i++; // closing quote
      // A string directly followed by ':' inside an object is a key.
      let j = i;
      while (j < n && (text[j] === ' ' || text[j] === '\t' || text[j] === '\n' || text[j] === '\r')) j++;
      // Keys are decoded exactly as JSON.parse will see them (\uXXXX escapes included; a
      // string-only parse, so no number coercion), and a repeated key is captured last-wins,
      // matching JSON.parse.
      if (text[j] === ':' && stack[stack.length - 1] === '{') {
        try { lastKey = raw.includes('\\') ? (JSON.parse(`"${raw}"`) as string) : raw; }
        catch { fail('malformed_json', 'Invalid property name'); }
        // A later duplicate replaces an earlier capture; a non-number value leaves none.
        if (stack.length === 1 && lastKey !== null && captureKeys.includes(lastKey)) delete captured[lastKey];
      } else {
        lastKey = null;
      }
      continue;
    }
    if (c === '-' || (c >= '0' && c <= '9')) {
      NUMBER.lastIndex = i;
      const m = NUMBER.exec(text);
      if (!m) fail('malformed_json', 'Invalid number');
      const token = m![0];
      if (m![1] !== undefined || m![2] !== undefined) fail('unsupported_number', `Non-integer number token '${token}'`);
      const big = BigInt(token);
      if (big > MAX_SAFE || big < -MAX_SAFE) fail('unsafe_integer', `Integer ${token} exceeds the browser-safe range`);
      if (stack.length === 1 && stack[0] === '{' && lastKey !== null && captureKeys.includes(lastKey)) captured[lastKey] = token;
      lastKey = null;
      i += token.length;
      continue;
    }
    if (c === '{' || c === '[') { stack.push(c); lastKey = null; i++; continue; }
    if (c === '}' || c === ']') {
      const open = stack.pop();
      if ((c === '}' && open !== '{') || (c === ']' && open !== '[')) fail('malformed_json', 'Unbalanced brackets');
      lastKey = null;
      i++;
      continue;
    }
    if (c === ':') { i++; continue; }
    if (c === ',') { lastKey = null; i++; continue; }
    if (text.startsWith('true', i)) { i += 4; lastKey = null; continue; }
    if (text.startsWith('false', i)) { i += 5; lastKey = null; continue; }
    if (text.startsWith('null', i)) { i += 4; lastKey = null; continue; }
    fail('malformed_json', `Unexpected character '${c}'`);
  }
  if (stack.length !== 0) fail('malformed_json', 'Unbalanced brackets');

  let value: T;
  try {
    value = JSON.parse(text) as T;
  } catch (e) {
    throw new ContractJsonError('malformed_json', `Invalid JSON: ${(e as Error).message}`);
  }
  return { value, captured };
}
