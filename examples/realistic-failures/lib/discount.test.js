const test = require("node:test");
const assert = require("node:assert/strict");
const { applyDiscount } = require("./discount");

test("no discount keeps the price", () => {
  assert.equal(applyDiscount(80, 0), 80);
});

test("10% off 200 is 180", () => {
  assert.equal(applyDiscount(200, 10), 180);
});

test("rejects discounts over 100%", () => {
  assert.throws(() => applyDiscount(50, 120), RangeError);
});
