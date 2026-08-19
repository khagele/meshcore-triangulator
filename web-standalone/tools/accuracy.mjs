// Ground-truth accuracy harness for the estimator (#68).
//
// Runs the shipped scoring code against cases with a KNOWN target position and
// reports the error distribution. Without this, a change to the estimator can
// only be argued about, not measured.
//
//   node web-standalone/tools/accuracy.mjs
//   node web-standalone/tools/accuracy.mjs --baseline out.json   # write results
//   node web-standalone/tools/accuracy.mjs --compare out.json    # diff vs a run
//   node web-standalone/tools/accuracy.mjs --hop1-only           # drop 2nd hop
//   node web-standalone/tools/accuracy.mjs --prefix 2            # resolve
//     observers by 2-hex prefix, as the app does, not by full id (#78)
//   node web-standalone/tools/accuracy.mjs --prefix 2 --pick-cluster oracle
//     as above, but assume the operator locks the right region in step 2
//
// Cases carry 1st- AND 2nd-hop observers. 2nd-hop nodes are built the way
// runCaseDiscovery()/applyHopRadii() build them (SECOND_HOP_WEIGHT_FACTOR on the
// weight, the 2nd-hop km input as hopRadiusKm), so the paths #46, #65, #66 and
// #67 are about are actually entered. --hop1-only re-runs without them,
// which is how you tell "this change did nothing" from "this change was never
// exercised": the two runs must differ.
//
// The scoring functions are EXTRACTED FROM index.html rather than reimplemented,
// so this cannot drift from what ships. Only the grid sweep is mirrored here,
// because updateHeatmap() is bound to Leaflet layers and DOM status text; the
// sweep's constants are pulled from the file too.
//
// Fixture: tools/fixtures/accuracy-cases.json, built by build-fixture.py from
// mc-radar data. Committed so this runs offline, because the upstream
// rate-limits (429) after a few hundred calls.

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
// --source points the extraction at a different index.html, so a variant can be
// measured without editing the working tree. Used for parameter sweeps.
const argv = process.argv.slice(2);
const sourceArg = argv.indexOf("--source");
const sourcePath = sourceArg !== -1 && argv[sourceArg + 1]
  ? argv[sourceArg + 1]
  : join(here, "..", "index.html");
const source = readFileSync(sourcePath, "utf8");
const fixture = JSON.parse(readFileSync(join(here, "fixtures", "accuracy-cases.json"), "utf8"));

function grab(pattern, label) {
  const match = source.match(pattern);
  if (!match) throw new Error(`could not extract ${label} from index.html`);
  return match[0];
}

// Everything the scoring path needs, lifted verbatim.
const extracted = [
  grab(/function toRadians\(value\) \{[\s\S]*?\n    \}/, "toRadians"),
  grab(/function clamp\(value, min, max\) \{[\s\S]*?\n    \}/, "clamp"),
  grab(/function haversineKm\(a, b\) \{[\s\S]*?\n    \}/, "haversineKm"),
  grab(/function percentile\(values, p\) \{[\s\S]*?\n    \}/, "percentile"),
  grab(/const DEFAULT_ANCHOR_RANGE_KM = \d+;/, "DEFAULT_ANCHOR_RANGE_KM"),
  grab(/const PROVEN_RADIUS_PERCENTILE = [\d.]+;/, "PROVEN_RADIUS_PERCENTILE"),
  grab(/const MAX_PLAUSIBLE_LINK_KM = \d+;/, "MAX_PLAUSIBLE_LINK_KM"),
  grab(/const MEASURED_LINK_QUORUM = \d+;/, "MEASURED_LINK_QUORUM"),
  grab(/const MEASURED_CEILING_SLACK = [\d.]+;/, "MEASURED_CEILING_SLACK"),
  grab(/const RECEIVE_QUORUM = \d+;/, "RECEIVE_QUORUM"),
  grab(/function linkCeilingKm\(measuredDistances\) \{[\s\S]*?\n    \}/, "linkCeilingKm"),
  grab(/function provenRadiusFromLinks\(entries\) \{[\s\S]*?\n    \}/, "provenRadiusFromLinks"),
  grab(/function anchorRangeKm\(anchor\) \{[\s\S]*?\n    \}/, "anchorRangeKm"),
  grab(/const SECOND_HOP_WEIGHT_FACTOR = [\d.]+;/, "SECOND_HOP_WEIGHT_FACTOR"),
  grab(/const COVERAGE_EDGE_SHARPNESS = \d+;/, "COVERAGE_EDGE_SHARPNESS"),
  grab(/function coverageLikelihood\(distanceKm, rangeKm\) \{[\s\S]*?\n    \}/, "coverageLikelihood"),
  grab(/const MIN_OBSERVATION_LIKELIHOOD = [\de.-]+;/, "MIN_OBSERVATION_LIKELIHOOD"),
  grab(/const SUPPORT_NODE_SCORE_WEIGHT = \d+;/, "SUPPORT_NODE_SCORE_WEIGHT"),
  grab(/function scorePoint\(point, matchedNodes, supportNodes\) \{[\s\S]*?\n    \}/, "scorePoint"),
  grab(/function connectedComponents\(nodesList, thresholdKm, wideThresholdKm = thresholdKm\) \{[\s\S]*?\n    \}/, "connectedComponents"),
  // Only reached in --prefix mode, see below.
  grab(/function obsPrefix\(node\) \{[\s\S]*?\n    \}/, "obsPrefix"),
  grab(/function nodeId\(node\) \{[\s\S]*?\n    \}/, "nodeId"),
  grab(/function centroidOfNodes\(nodesList\) \{[\s\S]*?\n    \}/, "centroidOfNodes"),
  grab(/function dedupeByPrefix\(nodes, centroid, provenNodeIds = new Set\(\)\) \{[\s\S]*?\n    \}/, "dedupeByPrefix")
];

const estimator = new Function(`${extracted.join("\n")}
  return { scorePoint, anchorRangeKm, haversineKm, provenRadiusFromLinks, connectedComponents,
           SECOND_HOP_WEIGHT_FACTOR, dedupeByPrefix, centroidOfNodes, nodeId, obsPrefix };`)();

// --prefix <n> feeds observers the way the APP gets them: by n-hex prefix out
// of the whole node universe, not by full id (#78).
//
// Without it, observers arrive by their full 8-hex id, so dedupeByPrefix() is
// never entered and no change to it can be measured. That is the gap #78 exists
// to close, and it is why fix A from #54 (a mixture over candidates) could not
// be judged.
//
// What the mode reconstructs: an operator types a 2-hex clue prefix and the app
// resolves it against every node on the map. So a case's candidate pool is
// every node in the fixture sharing a clue prefix with one of its true
// observers, which is the true observers PLUS decoys that never heard the
// target. Clustering then drops the geographically implausible ones, as the app
// does, and dedupeByPrefix() keeps one node per surviving prefix.
//
// Hops keep their own prefix space, matching the two separate inputs in the UI:
// a 1st-hop clue only pulls 1st-hop candidates.
const prefixArg = argv.indexOf("--prefix");
const PREFIX_HEX = prefixArg !== -1 && argv[prefixArg + 1] ? Number(argv[prefixArg + 1]) : 0;
if (prefixArg !== -1 && !(PREFIX_HEX >= 1 && PREFIX_HEX <= 8)) {
  throw new Error("--prefix takes 1 to 8 hex characters");
}

// Every distinct node the fixture knows about, per hop, keyed by full id. The
// stand-in for "every node on the map" that a prefix resolves against.
const universe = { 1: new Map(), 2: new Map() };
for (const testCase of fixture.cases) {
  for (const observer of testCase.observers) {
    if (!universe[1].has(observer.id)) universe[1].set(observer.id, observer);
  }
  for (const observer of testCase.secondHopObservers || []) {
    if (!universe[2].has(observer.id)) universe[2].set(observer.id, observer);
  }
}
const shortOf = (id) => id.slice(0, PREFIX_HEX).toUpperCase();

// The nodes a case is scored on, per hop. In prefix mode the true set is
// widened to everything sharing a clue prefix, then narrowed again by
// clustering and dedupe, which is the pipeline the app runs.
function poolFor(trueObservers, hop) {
  if (!PREFIX_HEX) return { observers: trueObservers, decoys: 0 };
  const cluePrefixes = new Set(trueObservers.map((o) => shortOf(o.id)));
  const trueIds = new Set(trueObservers.map((o) => o.id));
  const observers = [];
  for (const [id, node] of universe[hop]) {
    if (cluePrefixes.has(shortOf(id))) observers.push(node);
  }
  return { observers, decoys: observers.filter((o) => !trueIds.has(o.id)).length };
}

// The app never estimates over a raw observer list. Discovery clusters the
// resolved candidates first and the operator locks ONE region, so the estimator
// only ever sees a connected cluster. Skipping that here would measure a
// pipeline the app does not run: one fixture case has 96 "observers" spanning
// 281 km, of which 49 are past 100 km, and feeding those to scorePoint together
// is not a scenario any operator can produce.
//
// Default cluster km from the UI. Chaining is transitive, so this is not a cap
// on observer spread: a line of observers 5 km apart still forms one component.
const CLUSTER_KM = Number(grab(/id="region-radius-input"[^>]*value="(\d+)"/, "cluster km").match(/value="(\d+)"/)[1]);
// Default 2nd-hop km from the UI, i.e. what hop2RadiusKm() returns untouched.
// It is both the coverage radius stamped on a 2nd-hop node and the wide
// clustering threshold any edge touching one is allowed (#65, #66).
const HOP2_KM = Number(grab(/id="hop2-radius-input"[^>]*value="(\d+)"/, "2nd-hop km").match(/value="(\d+)"/)[1]);

// Rank-1 region as the app would show it: most observer weight, then tightest.
// The wide threshold matches runCaseDiscovery(), which passes hop2RadiusKm().
// Weight rather than raw count because componentScore()'s leading term is the
// weighted one: a 2nd-hop node counts 0.3, so a loose knot of 2nd-hop nodes does
// not outrank the direct evidence. With 1st-hop only the two are identical.
const clusterWeight = (nodes) => nodes.reduce((sum, node) => sum + node.weight, 0);
// The app does NOT commit to rank 1: step 2 lists the candidate regions and the
// operator locks one. Scoring rank 1 automatically therefore charges the
// estimator for cluster choices a human would not make, which matters once
// --prefix pulls decoys in and rank 1 can be a cluster of them.
// --pick-cluster oracle locks the component nearest the known target instead,
// an upper bound standing in for an operator who chooses correctly. The gap
// between the two is the cost of cluster CHOICE, worth keeping separate from
// the cost of the dedupe step inside a chosen cluster.
const pickArg = argv.indexOf("--pick-cluster");
const PICK = pickArg !== -1 && argv[pickArg + 1] ? argv[pickArg + 1] : "rank1";
if (!["rank1", "oracle"].includes(PICK)) throw new Error("--pick-cluster takes rank1 or oracle");

function largestCluster(observers, target) {
  const components = estimator.connectedComponents(observers, CLUSTER_KM, HOP2_KM);
  if (PICK === "oracle") {
    const distance = (component) => estimator.haversineKm(
      estimator.centroidOfNodes(component.nodes), target);
    return components.slice().sort((a, b) => distance(a) - distance(b))[0].nodes;
  }
  components.sort((a, b) =>
    clusterWeight(b.nodes) - clusterWeight(a.nodes) || a.maxPairKm - b.maxPairKm);
  return components[0].nodes;
}

// Grid sweep constants, read from updateHeatmap() so they track the app.
const latStep = Number(grab(/lat \+= (0\.\d+)/, "lat step").split("+= ")[1]);
const lonStep = Number(grab(/lon \+= (0\.\d+)/, "lon step").split("+= ")[1]);
const latPad = Number(grab(/Math\.min\(\.\.\.lats\) - (0\.\d+)/, "lat pad").split("- ")[1]);
const lonPad = Number(grab(/Math\.min\(\.\.\.lons\) - (0\.\d+)/, "lon pad").split("- ")[1]);

// How much better is the winning cell than the rest of the search area? A
// likelihood ratio under e^0.5 either way is not a distinction any operator
// could act on, so cells inside that band count as tied with the argmax (#69).
const TIED_NATS = 0.5;

function estimate(observers) {
  const lats = observers.map((o) => o.lat);
  const lons = observers.map((o) => o.lon);
  const minLat = Math.min(...lats) - latPad;
  const maxLat = Math.max(...lats) + latPad;
  const minLon = Math.min(...lons) - lonPad;
  const maxLon = Math.max(...lons) + lonPad;
  let best = null;
  const cells = [];
  for (let lat = minLat; lat <= maxLat; lat += latStep) {
    for (let lon = minLon; lon <= maxLon; lon += lonStep) {
      const point = { lat, lon };
      const score = estimator.scorePoint(point, observers, []);
      cells.push({ lat, lon, score });
      if (!best || score > best.score) best = { lat, lon, score };
    }
  }
  // Flatness diagnostics. An argmax is only meaningful if the surface it is
  // the max OF has structure; without these, a change can move the reported
  // point without anyone noticing the point was never pinned down.
  const worst = cells.reduce((low, cell) => Math.min(low, cell.score), Infinity);
  const tied = cells.filter((cell) => cell.score > best.score - TIED_NATS);
  const tiedRadiusKm = tied.reduce(
    (far, cell) => Math.max(far, estimator.haversineKm(cell, best)), 0);
  return {
    ...best,
    // Total log-likelihood range over the whole searched area, in nats.
    scoreSpanNats: best.score - worst,
    // Share of the searched area that is tied with the winner.
    tiedShare: tied.length / cells.length,
    // How far the tied region reaches from the winning cell.
    tiedRadiusKm
  };
}

// Weight is times-heard. The fixture has one reception per observer, so 1,
// times SECOND_HOP_WEIGHT_FACTOR at hop 2 exactly as tagHop() does.
// provenRadiusKm is derived with the shipped helper where the observer's own
// links were cached, else anchorRangeKm() falls through to the link_count tier,
// which is what the app does for observers outside its fetch budget.
function prepare(observers, hop) {
  return observers.map((o) => {
    const node = {
      lat: o.lat, lon: o.lon, hop, link_count: o.link_count,
      weight: hop === 2 ? estimator.SECOND_HOP_WEIGHT_FACTOR : 1,
      // What the shipped nodeId() and obsPrefix() read. Set in both modes so
      // they differ only in which nodes arrive here, not in node shape.
      key: o.id, shortId: PREFIX_HEX ? shortOf(o.id) : o.id
    };
    if (o.links && o.links.length) {
      const { radiusKm } = estimator.provenRadiusFromLinks(o.links);
      if (Number.isFinite(radiusKm)) node.provenRadiusKm = radiusKm;
    }
    // applyHopRadii(): 2nd-hop nodes carry the hop input, 1st-hop nodes carry
    // none. anchorRangeKm() reads hopRadiusKm FIRST, so this also suppresses
    // any provenRadiusKm set just above. That precedence is #67's point 3, and
    // it is only visible here because the fixture now has both.
    if (hop === 2) node.hopRadiusKm = HOP2_KM;
    return node;
  });
}

function percentile(values, p) {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const rank = (s.length - 1) * p;
  const lo = Math.floor(rank);
  const hi = Math.min(lo + 1, s.length - 1);
  return s[lo] + (s[hi] - s[lo]) * (rank - lo);
}

const fmt = (n) => (n === null ? "  n/a" : n.toFixed(1).padStart(5));

function report(label, errors) {
  if (!errors.length) return console.log(`${label.padEnd(30)} no cases`);
  console.log(
    `${label.padEnd(30)} n=${String(errors.length).padEnd(4)} ` +
    `p25=${fmt(percentile(errors, 0.25))} med=${fmt(percentile(errors, 0.5))} ` +
    `p75=${fmt(percentile(errors, 0.75))} p90=${fmt(percentile(errors, 0.9))} ` +
    `max=${fmt(Math.max(...errors))}`
  );
}

const hop1Only = argv.includes("--hop1-only");
const results = [];
const skipped = [];
for (const testCase of fixture.cases) {
  const secondHop = hop1Only ? [] : (testCase.secondHopObservers || []);
  const hop1Pool = poolFor(testCase.observers, 1);
  const hop2Pool = poolFor(secondHop, 2);
  const clustered = largestCluster([
    ...prepare(hop1Pool.observers, 1),
    ...prepare(hop2Pool.observers, 2)
  ], testCase.target);
  // A case whose rank-1 cluster is a lone node has no geometry to solve. The
  // app would not offer an estimate either, so scoring it would flatter or
  // punish the estimator for something it never sees.
  if (clustered.length < 2) {
    skipped.push(testCase.targetId);
    continue;
  }
  // The step the default mode never reaches. Same call the app makes, same
  // order: cluster, then one node per prefix. provenNodeIds is empty because
  // the fixture records each observer's link DISTANCES but not the peer at the
  // other end, so the proven-link tier cannot fire here. That leaves every
  // collision to the nearest-the-centroid tier, which is the circular step #54
  // named and #78 exists to measure. Counted, not hidden.
  const trueIds = new Set([...testCase.observers, ...secondHop]
    .map((o) => o.id.toLowerCase()));
  let deduped = clustered;
  let centroidDecided = 0;
  let trueObserversDropped = 0;
  if (PREFIX_HEX) {
    const result = estimator.dedupeByPrefix(
      clustered, estimator.centroidOfNodes(clustered), new Set());
    deduped = result.kept;
    centroidDecided = result.centroidDecided;
    trueObserversDropped = result.removed
      .filter((n) => trueIds.has(String(n.key).toLowerCase())).length;
  }
  if (deduped.length < 2) {
    skipped.push(testCase.targetId);
    continue;
  }
  const best = estimate(deduped);
  const errorKm = estimator.haversineKm(best, testCase.target);
  // Geometry strata below stay a statement about the DIRECT evidence, so they
  // are measured over the 1st-hop members of the cluster. A 2nd-hop node 3 km
  // out does not pin the target the way a 1st-hop one does, and letting it into
  // "nearest observer < 5 km" would move cases between strata for no reason
  // connected to accuracy.
  const direct = deduped.filter((o) => o.hop !== 2);
  const clusterDistances = (direct.length ? direct : deduped)
    .map((o) => estimator.haversineKm(o, testCase.target));
  // Naive baselines. If the estimator cannot beat the centroid of the observers
  // it is not earning its complexity, and any future change should be judged
  // against the same yardstick rather than only against its predecessor.
  const centroid = {
    lat: deduped.reduce((sum, o) => sum + o.lat, 0) / deduped.length,
    lon: deduped.reduce((sum, o) => sum + o.lon, 0) / deduped.length
  };
  const tightest = deduped.reduce((a, b) =>
    (estimator.anchorRangeKm(a) <= estimator.anchorRangeKm(b) ? a : b));
  results.push({
    targetId: testCase.targetId,
    errorKm,
    scoreSpanNats: best.scoreSpanNats,
    tiedShare: best.tiedShare,
    tiedRadiusKm: best.tiedRadiusKm,
    centroidErrorKm: estimator.haversineKm(centroid, testCase.target),
    // Sitting on whichever observer claims the smallest coverage: the crudest
    // possible reading of "heard by the shortest-range node".
    tightestObserverErrorKm: estimator.haversineKm(tightest, testCase.target),
    observerCount: deduped.length,
    // In the fixture vs in the cluster actually scored. A case can carry 2nd-hop
    // observers and still score none of them, which is not the same thing as
    // having none: only the second number means the 2nd-hop paths ran.
    secondHopOffered: secondHop.length,
    secondHopScored: deduped.filter((o) => o.hop === 2).length,
    droppedByClustering: hop1Pool.observers.length + hop2Pool.observers.length - clustered.length,
    // Prefix-mode bookkeeping; 0 in the default mode.
    decoysPulledIn: hop1Pool.decoys + hop2Pool.decoys,
    droppedByDedupe: clustered.length - deduped.length,
    centroidDecided,
    trueObserversDropped,
    maxObserverKm: Math.max(...clusterDistances),
    // Distance from the target to its nearest observer. A useful floor: no
    // estimator can do better than the geometry allows.
    nearestObserverKm: Math.min(...clusterDistances)
  });
}

const errors = results.map((r) => r.errorKm);
console.log(`source: ${sourcePath}`);
console.log(`fixture: ${fixture.cases.length} cases, generated ${fixture.generated}`);
const offeredCases = fixture.cases.filter((c) => (c.secondHopObservers || []).length).length;
const offeredNodes = fixture.cases.reduce((sum, c) => sum + (c.secondHopObservers || []).length, 0);
console.log(`2nd-hop in fixture: ${offeredNodes} observers across ${offeredCases} cases` +
  (hop1Only ? " (ignored, --hop1-only)" : ""));
console.log(`grid: ${latStep} x ${lonStep} deg, pad ${latPad} / ${lonPad}; ` +
  `cluster ${CLUSTER_KM} km, 2nd-hop ${HOP2_KM} km`);
console.log(`observers by ${PREFIX_HEX ? `${PREFIX_HEX}-hex prefix` : "full id"}; cluster pick ${PICK}`);
console.log(`scored ${results.length}, skipped ${skipped.length} whose ` +
  `${PICK === "oracle" ? "target-nearest" : "rank-1"} cluster came out a single node`);
const dropped = results.reduce((sum, r) => sum + r.droppedByClustering, 0);
console.log(`observers dropped by clustering: ${dropped}`);
if (PREFIX_HEX) {
  const sum = (key) => results.reduce((total, r) => total + r[key], 0);
  const collided = results.filter((r) => r.droppedByDedupe > 0).length;
  console.log(
    `resolved out of ${universe[1].size} 1st-hop and ${universe[2].size} 2nd-hop known nodes: ` +
    `${sum("decoysPulledIn")} decoys pulled in, ${sum("droppedByDedupe")} dropped by dedupe ` +
    `across ${collided} of ${results.length} cases`
  );
  console.log(
    `  of those, ${sum("centroidDecided")} prefixes decided by nearest-the-centroid, ` +
    `${sum("trueObserversDropped")} nodes that really did hear the target discarded`
  );
}
console.log();
report("error km, all cases", errors);
report("  baseline: observer centroid", results.map((r) => r.centroidErrorKm));
report("  baseline: tightest observer", results.map((r) => r.tightestObserverErrorKm));
const beatsCentroid = results.filter((r) => r.errorKm < r.centroidErrorKm - 0.05).length;
const losesToCentroid = results.filter((r) => r.errorKm > r.centroidErrorKm + 0.05).length;
console.log(`  estimator beats centroid on ${beatsCentroid} of ${results.length}, loses on ${losesToCentroid}`);
console.log();
report("  3-4 observers", results.filter((r) => r.observerCount <= 4).map((r) => r.errorKm));
report("  5-9 observers", results.filter((r) => r.observerCount >= 5 && r.observerCount <= 9).map((r) => r.errorKm));
report("  10+ observers", results.filter((r) => r.observerCount >= 10).map((r) => r.errorKm));
console.log();
report("  no observer beyond 60 km", results.filter((r) => r.maxObserverKm <= 60).map((r) => r.errorKm));
report("  has observer beyond 60 km", results.filter((r) => r.maxObserverKm > 60).map((r) => r.errorKm));
console.log();
report("  nearest observer < 5 km", results.filter((r) => r.nearestObserverKm < 5).map((r) => r.errorKm));
report("  nearest observer >= 5 km", results.filter((r) => r.nearestObserverKm >= 5).map((r) => r.errorKm));
console.log();
// The stratum that says whether a 2nd-hop change was measured at all. A change
// to #46/#65/#66/#67 that moves nothing here moved nothing anywhere, and a
// --compare over the whole set would have reported that as "no regression".
const withSecond = results.filter((r) => r.secondHopScored > 0);
report("  scored 1st-hop only", results.filter((r) => r.secondHopScored === 0).map((r) => r.errorKm));
report("  scored some 2nd-hop", withSecond.map((r) => r.errorKm));
const secondScored = results.reduce((sum, r) => sum + r.secondHopScored, 0);
const secondOffered = results.reduce((sum, r) => sum + r.secondHopOffered, 0);
console.log(`  2nd-hop observers scored: ${secondScored} of ${secondOffered} offered ` +
  `(rest fell outside the rank-1 cluster)`);

// Is the argmax pinning anything down? Reported alongside the error, because
// an error distribution alone cannot tell a model that is right from one that
// had nothing to say and guessed the middle (#69).
console.log();
function reportFlatness(label, rows) {
  if (!rows.length) return console.log(`${label.padEnd(30)} no cases`);
  const span = percentile(rows.map((r) => r.scoreSpanNats), 0.5);
  const share = percentile(rows.map((r) => r.tiedShare), 0.5);
  const radius = percentile(rows.map((r) => r.tiedRadiusKm), 0.5);
  console.log(
    `${label.padEnd(30)} n=${String(rows.length).padEnd(4)} ` +
    `span=${span.toFixed(2)} nats  tied=${(share * 100).toFixed(0)}% of grid  ` +
    `tied within ${radius.toFixed(1)} km`
  );
}
console.log("likelihood surface, medians (span = best cell minus worst cell)");
reportFlatness("  all cases", results);
reportFlatness("  nearest observer < 5 km", results.filter((r) => r.nearestObserverKm < 5));
reportFlatness("  nearest observer >= 5 km", results.filter((r) => r.nearestObserverKm >= 5));

const args = argv;
const baselineIndex = args.indexOf("--baseline");
if (baselineIndex !== -1 && args[baselineIndex + 1]) {
  writeFileSync(args[baselineIndex + 1], JSON.stringify(results, null, 1));
  console.log(`\nwrote ${args[baselineIndex + 1]}`);
}
const compareIndex = args.indexOf("--compare");
if (compareIndex !== -1 && args[compareIndex + 1]) {
  const previous = new Map(
    JSON.parse(readFileSync(args[compareIndex + 1], "utf8")).map((r) => [r.targetId, r.errorKm])
  );
  const deltas = results
    .filter((r) => previous.has(r.targetId))
    .map((r) => ({ id: r.targetId, delta: r.errorKm - previous.get(r.targetId) }));
  const better = deltas.filter((d) => d.delta < -0.05);
  const worse = deltas.filter((d) => d.delta > 0.05);
  console.log(`\nvs ${args[compareIndex + 1]}: ${better.length} better, ${worse.length} worse, ` +
    `${deltas.length - better.length - worse.length} unchanged`);
  console.log(`median error change: ${(percentile(results.map((r) => r.errorKm), 0.5) -
    percentile([...previous.values()], 0.5)).toFixed(2)} km`);
  // Regressions are what a summary statistic hides, so name the worst.
  worse.sort((a, b) => b.delta - a.delta).slice(0, 5)
    .forEach((d) => console.log(`  worse: ${d.id} +${d.delta.toFixed(1)} km`));
}
