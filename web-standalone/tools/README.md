# tools

Development tools for the estimator. Not shipped: `.dockerignore` keeps the
image to `server.py`, `index.html` and `changelog.json`.

## accuracy.mjs

Measures estimator error against known target positions (#68).

```
node web-standalone/tools/accuracy.mjs
```

Reports the error distribution, stratified by observer count, contamination,
geometry and hop, plus two naive baselines. Runs offline against the committed
fixture.

Cases carry 1st- and 2nd-hop observers. 2nd-hop nodes are prepared the way
`runCaseDiscovery()` and `applyHopRadii()` prepare them (weight times
`SECOND_HOP_WEIGHT_FACTOR`, the 2nd-hop km input as `hopRadiusKm`, that same
value as the wide clustering threshold), so #46, #65, #66 and #67 touch code
this harness actually runs.

```
node web-standalone/tools/accuracy.mjs --hop1-only   # drop 2nd-hop observers
```

Run it both ways to tell "this change did nothing" apart from "this change was
never exercised". Before the 2nd-hop half of the fixture existed those two were
indistinguishable, and a zero delta read as a green light.

To judge a change, capture a baseline before it and compare after:

```
node web-standalone/tools/accuracy.mjs --baseline /tmp/before.json
# ... make the change ...
node web-standalone/tools/accuracy.mjs --compare /tmp/before.json
```

The comparison names the worst regressions individually, because a summary
statistic hides exactly the cases worth looking at.

To measure a variant without touching the working tree, point the extraction at
another copy:

```
sed 's/COVERAGE_EDGE_SHARPNESS = 6/COVERAGE_EDGE_SHARPNESS = 3/' \
  web-standalone/index.html > /tmp/variant.html
node web-standalone/tools/accuracy.mjs --source /tmp/variant.html
```

That is how the parameter sweeps in #69 were run.

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

Ground truth is the target's own position. 1st-hop observers are the peers that
demonstrably received from it, per the link direction mc-radar reports. 2nd-hop
observers are the peers those observers in turn transmitted to, excluding the
target and anything already 1st-hop, each tagged with the parent it heard
(`via`). Limitations are recorded in #69.

The case shape:

```
targetId, target {lat, lon}
observers[]           hop 1, unchanged fields; maxObserverKm is over these only
secondHopObservers[]  hop 2, plus via (parent id) and viaKm (measured parent link)
secondHopEligible     how many 2nd-hop peers existed before the per-case cap
```

The two lists stay separate so anything reading only `observers` measures the
same 1st-hop case it measured before.

The 2nd-hop set is capped per case (`SECOND_HOP_PER_CASE`) and filled
round-robin over the parents. A hub observer can have hundreds of peers, and all
of them at once is a scenario no operator can produce: 2nd-hop prefixes come out
of packet paths, a handful at a time.
