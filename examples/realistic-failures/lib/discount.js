// Returns the price after a percentage discount, e.g. applyDiscount(200, 10) === 180.
function applyDiscount(price, percent) {
  if (percent < 0 || percent > 100) {
    throw new RangeError("percent must be between 0 and 100");
  }
  return price - percent;
}

module.exports = { applyDiscount };
