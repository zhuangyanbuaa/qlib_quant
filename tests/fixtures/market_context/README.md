# Market context fixtures

`cnn_contract.json` is a synthetic parser contract fixture, not a captured CNN response
or historical market fact. The endpoint probe on 2026-11-04 returned HTTP 418.
The candidate `cnn-graphdata-v1-unverified` adapter remains opt-in; production
activation requires a real successful response and schema review. Do not use these
fixture values as market data. Fixed tests intentionally retain this provenance.
