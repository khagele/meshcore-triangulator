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

It also reports how FLAT the likelihood surface is: the log-likelihood range
across the whole searched grid, the share of that grid tied with the winning
cell, and how far the tied region reaches. Error alone cannot separate a model
that is right from one that had nothing to say and guessed the middle. On the
committed fixture the median case spans 28 nats with 6% of the grid tied, so
the argmax means something; the hard stratum (nearest observer >= 5 km) spans
3.8 nats with 42% tied and err med 18.8 km, so there it does not.

Before the 2nd-hop half of the fixture existed the whole grid spanned 0.34 nats
and was 100% tied, and #69's first kernel sweep read that as "kernel shape does
not matter". It was a property of that fixture. Check these numbers before
concluding anything from a flat sweep.

### Prefix mode (#78)

By default observers arrive by full 8-hex id, so `dedupeByPrefix` is never
entered and no change to it can be judged. `--prefix 2` resolves observers the
way the app does: every node in the fixture sharing a clue prefix with a true
observer, decoys included, then clustering, then one node per prefix. Hops keep
separate prefix spaces, matching the two inputs in the UI.

```
node web-standalone/tools/accuracy.mjs --prefix 2
node web-standalone/tools/accuracy.mjs --prefix 2 --pick-cluster oracle
```

At 2 hex that is 4407 decoys pulled in and 241 nodes dropped by dedupe across
50 of 92 cases, 179 of them decided by the circular nearest-the-centroid step,
93 of them nodes that really did hear the target.

`--pick-cluster oracle` locks the component nearest the known target instead of
rank 1, standing in for the operator choosing correctly in step 2. Keep the two
apart: they measure different failures, and the gap between them is the finding.

| n=82, oracle cluster | med | p75 | p90 | max |
|---|---|---|---|---|
| full id | 2.0 | 3.9 | 6.2 | 19.5 |
| 2-hex prefix | 2.1 | 4.8 | 6.9 | 19.5 |

| n=92, rank-1 cluster | med | p75 | p90 | max |
|---|---|---|---|---|
| full id | 2.4 | 5.8 | 12.2 | 93.8 |
| 2-hex prefix | 2.8 | 6.4 | 18.2 | 191.9 |

So the dedupe pick costs ~0.1 km on the median once the right region is locked,
while picking the region under prefix ambiguity costs over 100 km on single
cases. Dedupe runs identically in both oracle columns, and the worst rank-1
blow-ups collapse when only the cluster choice changes: 8938F8CF 155.9 -> 2.5
km, 29334479 191.9 -> 1.1 km. Cluster choice, not the tie-break, which is #85.

### What "rank 1" means here

Candidate regions are ranked with the shipped `componentScore` and cut to the
top 5, exactly as `runCaseDiscovery()` does. This harness used to sort them by
total observer weight instead, so its rank-1 was not the region the operator
sees and a ranking change could not be measured at all (#85). Absolute numbers
from before that fix do not reproduce; orderings were unaffected.

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

That is how the parameter sweeps in #69 were run. Note what those found: kernel
shape, range scale and grid extent all leave the error where it is, because the
surface is flat before any of them apply. Re-run the flatness numbers before
spending time on a change that only reshapes the kernel.

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
