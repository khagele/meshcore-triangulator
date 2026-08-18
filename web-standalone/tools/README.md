# tools

Development tools for the estimator. Not shipped: `.dockerignore` keeps the
image to `server.py`, `index.html` and `changelog.json`.

## accuracy.mjs

Measures estimator error against known target positions (#68).

```
node web-standalone/tools/accuracy.mjs
```

Reports the error distribution, stratified by observer count, contamination and
geometry, plus two naive baselines. Runs offline against the committed fixture.

To judge a change, capture a baseline before it and compare after:

```
node web-standalone/tools/accuracy.mjs --baseline /tmp/before.json
# ... make the change ...
node web-standalone/tools/accuracy.mjs --compare /tmp/before.json
```

The comparison names the worst regressions individually, because a summary
statistic hides exactly the cases worth looking at.

The scoring functions are extracted from `index.html` at runtime rather than
reimplemented, so the harness cannot drift from what ships. If a rename breaks
an extraction the tool fails loudly rather than silently measuring nothing.

## build-fixture.py

Regenerates `fixtures/accuracy-cases.json` from mc-radar. Only needed to refresh
the fixture.

```
python3 web-standalone/tools/build-fixture.py
```

Responses are cached under `.cache/`, so an interrupted run resumes. The
upstream needs an explicit User-Agent (a default urllib one gets 403) and
rate-limits with 429 under sustained querying; both are handled.

Ground truth is the target's own position. Observers are the peers that
demonstrably received from it, per the link direction mc-radar reports.
Limitations are recorded in #69.
