# Ground truth for the realistic failures

Used to score LLM analyses. Keep this file out of the test repository.

## Frontend dependencies (`deps.yml`, job `install`)
- Category: `dependency_failure`
- Root cause: `web/package.json` pins `react@17.0.2`, but `react-dom@18.3.1` declares a peer
  dependency on `react@^18.3.1`, so `npm install` stops with ERESOLVE.
- Correct fix: upgrade `react` to `18.3.1` (or downgrade `react-dom` to `17.0.2`).
  `--legacy-peer-deps` / `--force` only hide the conflict.

## Unit tests (`tests.yml`, job `test`)
- Category: `test_failure`
- Root cause: `applyDiscount` in `lib/discount.js` returns `price - percent` (subtracts the
  percentage as a flat amount) instead of `price * (1 - percent / 100)`. The test
  "10% off 200 is 180" gets 190. The other two tests pass.
- Correct fix: change the formula in `lib/discount.js`; the test is correct.
