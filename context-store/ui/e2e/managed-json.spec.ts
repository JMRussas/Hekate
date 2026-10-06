// Node-side tests of the integer-safe contract JSON parser (plan 021 §3). No browser.
import { expect, test } from '@playwright/test';
import { ContractJsonError, parseContractJson } from '../src/planContract/json';

function code(text: string): string {
  try {
    parseContractJson(text);
    return 'ok';
  } catch (e) {
    return e instanceof ContractJsonError ? e.code : `unexpected:${String(e)}`;
  }
}

test('safe integers, negatives, zero and the max-safe boundary parse exactly', () => {
  const { value } = parseContractJson<{ a: number; b: number; c: number; d: number; e: number }>(
    '{"a": 0, "b": -0, "c": -17, "d": 9007199254740991, "e": -9007199254740991}');
  expect(value).toEqual({ a: 0, b: -0, c: -17, d: 9007199254740991, e: -9007199254740991 });
});

test('digits inside strings (including escaped quotes) are not numbers', () => {
  const text = '{"value": "12345678901234567890", "tricky": "a\\"9007199254740993\\"b", "esc": "\\u0031e999"}';
  const { value } = parseContractJson<Record<string, string>>(text);
  expect(value.value).toBe('12345678901234567890');
  expect(value.tricky).toBe('a"9007199254740993"b');
});

test('plain unsafe integers fail closed', () => {
  expect(code('{"seq": 9007199254740992}')).toBe('unsafe_integer');
  expect(code('{"seq": 9007199254740993}')).toBe('unsafe_integer');
  expect(code('{"seq": -9007199254740993}')).toBe('unsafe_integer');
  expect(code('[1, 2, [3, {"deep": 123456789012345678901234567890}]]')).toBe('unsafe_integer');
});

test('fractions and exponents are rejected, including an exponent-encoded unsafe value', () => {
  expect(code('{"seq": 9.007199254740993e15}')).toBe('unsupported_number');
  expect(code('{"seq": 1e3}')).toBe('unsupported_number');
  expect(code('{"seq": 1E+2}')).toBe('unsupported_number');
  expect(code('{"seq": 1.5}')).toBe('unsupported_number');
  expect(code('{"seq": -0.0}')).toBe('unsupported_number');
});

test('malformed documents are malformed_json', () => {
  expect(code('{"a": "unterminated}')).toBe('malformed_json');
  expect(code('{"a": 1')).toBe('malformed_json');
  expect(code('{"a": 01}')).toBe('malformed_json');
  expect(code('{"a": tru}')).toBe('malformed_json');
  expect(code('{"a": "bad \\x escape"}')).toBe('malformed_json');
  expect(code('{"a": 1,}')).toBe('malformed_json');
  expect(code('')).toBe('malformed_json');
  expect(code('<html>proxy error</html>')).toBe('malformed_json');
});

test('the top-level cursor is captured as its exact checked source text', () => {
  const text = '{"events": [{"seq": 5, "nextAfterSeq": 1}], "nextAfterSeq": 9007199254740991}';
  const { value, captured } = parseContractJson<{ nextAfterSeq: number }>(text, ['nextAfterSeq']);
  expect(captured.nextAfterSeq).toBe('9007199254740991');   // top level only, not the nested field
  expect(value.nextAfterSeq).toBe(9007199254740991);
  expect(parseContractJson('{"nextAfterSeq": null}', ['nextAfterSeq']).captured).toEqual({});
});

test('escaped and duplicate cursor keys are captured exactly as JSON.parse resolves them (last wins)', () => {
  const dup = parseContractJson<{ nextAfterSeq: number }>('{"nextAfterSeq": 2, "next\\u0041fterSeq": 3}', ['nextAfterSeq']);
  expect(dup.value.nextAfterSeq).toBe(3);
  expect(dup.captured.nextAfterSeq).toBe('3');
  const sole = parseContractJson<{ nextAfterSeq: number }>('{"\\u006eextAfterSeq": 41}', ['nextAfterSeq']);
  expect(sole.captured.nextAfterSeq).toBe('41');
  const thenNull = parseContractJson<{ nextAfterSeq: null }>('{"nextAfterSeq": 2, "nextAfterSeq": null}', ['nextAfterSeq']);
  expect(thenNull.value.nextAfterSeq).toBeNull();
  expect(thenNull.captured).toEqual({});
});
