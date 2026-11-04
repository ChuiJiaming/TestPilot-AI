import test from "node:test";
import assert from "node:assert/strict";
import { pinnedLookup } from "./worker_dns.mjs";

test("pinnedLookup answers Happy Eyeballs lookups with an address array", () => {
  const lookup = pinnedLookup({ address: "203.0.113.10", family: 4 });
  let seen;
  lookup("target.example", { all: true }, (error, value) => { seen = { error, value }; });
  assert.equal(seen.error, null);
  assert.deepEqual(seen.value, [{ address: "203.0.113.10", family: 4 }]);
});

test("pinnedLookup keeps the single-address form for family-pinned lookups", () => {
  const lookup = pinnedLookup({ address: "203.0.113.10", family: 4 });
  let seen;
  lookup("target.example", { family: 4 }, (error, address, family) => {
    seen = { error, address, family };
  });
  assert.equal(seen.error, null);
  assert.equal(seen.address, "203.0.113.10");
  assert.equal(seen.family, 4);
});
