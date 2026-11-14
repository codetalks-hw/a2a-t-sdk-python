// Canonical-form conformance harness for the a2at-canonical/1 profile.
//
// Run with: node tests/js/canonical.test.mjs
// The Python test suite shells out to this file and skips cleanly when Node is absent.
//
// This is a second implementation on purpose. The profile claims a JavaScript peer can recompute
// the same digest from the same plan; only an actual JavaScript implementation can check that
// claim, and it checks it against the same golden vectors the Python writer uses.

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = resolve(HERE, "../../../../tests/fixtures/canonical_vectors.json");

class CanonicalFormError extends Error {}

class Decimal {
  constructor(text) {
    this.text = text;
  }
}

class Opaque {}

function reject(message) {
  throw new CanonicalFormError(message);
}

function hasLoneSurrogate(text) {
  for (let index = 0; index < text.length; index += 1) {
    const unit = text.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return true;
    }
  }
  return false;
}

// JSON.stringify emits exactly the escaping Python's json.dumps does with ensure_ascii=False:
// the five shorthand escapes, \u00XX for other control characters, and raw non-ASCII text.
function canonicalString(text) {
  if (hasLoneSurrogate(text)) reject("string contains a lone surrogate");
  return JSON.stringify(text);
}

function canonicalNumber(value) {
  if (!Number.isFinite(value)) reject(`non-finite number: ${value}`);
  if (Number.isInteger(value)) {
    if (!Number.isSafeInteger(value)) reject(`integer outside the safe range: ${value}`);
    return String(value); // String(-0) is "0", matching the Python normalisation
  }
  // Python's repr switches to exponent notation below 1e-4; reject that band so this
  // implementation accepts exactly the values the profile accepts.
  if (Math.abs(value) < 1e-4) reject(`number needs exponent notation: ${value}`);
  const text = String(value);
  if (!/^-?\d+\.\d+$/.test(text)) reject(`number needs exponent notation: ${text}`);
  return text;
}

function canonicalValue(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return canonicalNumber(value);
  if (typeof value === "string") return canonicalString(value);
  if (Array.isArray(value)) return `[${value.map(canonicalValue).join(",")}]`;
  if (typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      reject(`${value.constructor?.name ?? "object"} is not a canonical value`);
    }
    // Array.prototype.sort on strings orders by UTF-16 code unit, which is the JCS rule.
    const keys = Object.keys(value).sort();
    const members = keys.map(
      (key) => `${canonicalString(key)}:${canonicalValue(value[key])}`,
    );
    return `{${members.join(",")}}`;
  }
  reject(`${typeof value} is not a canonical value`);
}

function canonicalJson(value) {
  return canonicalValue(value);
}

function digestFor(value) {
  return "sha256:" + createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function decode(value, tag) {
  switch (tag) {
    case undefined:
    case null:
      return value;
    case "nan":
      return NaN;
    case "inf":
      return Infinity;
    case "-inf":
      return -Infinity;
    case "set":
      return new Set(value);
    case "bytes":
      return Uint8Array.from(value);
    case "decimal":
      return new Decimal(value);
    case "opaque":
      return new Opaque();
    // JavaScript has no non-string object key. A Map is the closest analogue and, like the
    // Python integer key, is not a canonical mapping.
    case "non-string-key":
      return new Map([[1, value]]);
    case "nested-nan":
      return { x: NaN };
    case "array-with-set":
      return [new Set(value)];
    case "tuple":
      return value;
    default:
      throw new Error(`unknown fixture decode tag: ${tag}`);
  }
}

const vectors = JSON.parse(readFileSync(FIXTURE, "utf8"));
assert.equal(vectors.profile, "a2at-canonical/1");

let count = 0;
for (const testCase of vectors.valid) {
  const value = decode(testCase.value, testCase.decode);
  assert.equal(canonicalJson(value), testCase.canonical, `${testCase.name}: canonical text`);
  assert.equal(digestFor(value), testCase.digest, `${testCase.name}: digest`);
  count += 1;
}
for (const testCase of vectors.invalid) {
  const value = decode(testCase.value, testCase.decode);
  assert.throws(() => canonicalJson(value), CanonicalFormError, `${testCase.name}: must throw`);
  count += 1;
}

console.log(`canonical conformance ok: ${count} vectors`);
