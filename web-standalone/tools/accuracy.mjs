// Ground-truth accuracy harness for the estimator (#68).
//
// Runs the shipped scoring code against cases with a KNOWN target position and
// reports the error distribution. Without this, a change to the estimator can
// only be argued about, not measured.
//
//   node web-standalone/tools/accuracy.mjs
//   node web-standalone/tools/accuracy.mjs --baseline out.json   # write results
//   node web-standalone/tools/accuracy.mjs --compare out.json    # diff vs a run
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
const source = readFileSync(join(here, "..", "index.html"), "utf8");
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
  grab(/const COVERAGE_EDGE_SHARPNESS = \d+;/, "COVERAGE_EDGE_SHARPNESS"),
  grab(/function coverageLikelihood\(distanceKm, rangeKm\) \{[\s\S]*?\n    \}/, "coverageLikelihood"),
  grab(/const MIN_OBSERVATION_LIKELIHOOD = [\de.-]+;/, "MIN_OBSERVATION_LIKELIHOOD"),
  grab(/const SUPPORT_NODE_SCORE_WEIGHT = \d+;/, "SUPPORT_NODE_SCORE_WEIGHT"),
  grab(/function scorePoint\(point, matchedNodes, supportNodes\) \{[\s\S]*?\n    \}/, "scorePoint"),
  grab(/function connectedComponents\(nodesList, thresholdKm, wideThresholdKm = thresholdKm\) \{[\s\S]*?\n    \}/, "connectedComponents")
];

const estimator = new Function(`${extracted.join("\n")}
  return { scorePoint, anchorRangeKm, haversineKm, provenRadiusFromLinks, connectedComponents };`)();

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

// Rank-1 region as the app would show it: most observers, then tightest.
function largestCluster(observers) {
  const components = estimator.connectedComponents(observers, CLUSTER_KM, CLUSTER_KM);
  components.sort((a, b) => b.nodes.length - a.nodes.length || a.maxPairKm - b.maxPairKm);
  return components[0].nodes;
}

// Grid sweep constants, read from updateHeatmap() so they track the app.
const latStep = Number(grab(/lat \+= (0\.\d+)/, "lat step").split("+= ")[1]);
const lonStep = Number(grab(/lon \+= (0\.\d+)/, "lon step").split("+= ")[1]);
const latPad = Number(grab(/Math\.min\(\.\.\.lats\) - (0\.\d+)/, "lat pad").split("- ")[1]);
const lonPad = Number(grab(/Math\.min\(\.\.\.lons\) - (0\.\d+)/, "lon pad").split("- ")[1]);

function estimate(observers) {
  const lats = observers.map((o) => o.lat);
  const lons = observers.map((o) => o.lon);
  const minLat = Math.min(...lats) - latPad;
  const maxLat = Math.max(...lats) + latPad;
  const minLon = Math.min(...lons) - lonPad;
  const maxLon = Math.max(...lons) + lonPad;
  let best = null;
  for (let lat = minLat; lat <= maxLat; lat += latStep) {
    for (let lon = minLon; lon <= maxLon; lon += lonStep) {
      const point = { lat, lon };
      const score = estimator.scorePoint(point, observers, []);
      if (!best || score > best.score) best = { lat, lon, score };
    }
  }
  return best;
}

// Weight is times-heard. The fixture has one reception per observer, so 1.
// provenRadiusKm is derived with the shipped helper where the observer's own
// links were cached, else anchorRangeKm() falls through to the link_count tier,
// which is what the app does for observers outside its fetch budget.
function prepare(observers) {
  return observers.map((o) => {
    const node = { lat: o.lat, lon: o.lon, weight: 1, link_count: o.link_count };
    if (o.links && o.links.length) {
      const { radiusKm } = estimator.provenRadiusFromLinks(o.links);
      if (Number.isFinite(radiusKm)) node.provenRadiusKm = radiusKm;
    }
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

const results = [];
const skipped = [];
for (const testCase of fixture.cases) {
  const clustered = largestCluster(prepare(testCase.observers));
  // A case whose rank-1 cluster is a lone node has no geometry to solve. The
  // app would not offer an estimate either, so scoring it would flatter or
  // punish the estimator for something it never sees.
  if (clustered.length < 2) {
    skipped.push(testCase.targetId);
    continue;
  }
  const best = estimate(clustered);
  const errorKm = estimator.haversineKm(best, testCase.target);
  const clusterDistances = clustered.map((o) => estimator.haversineKm(o, testCase.target));
  // Naive baselines. If the estimator cannot beat the centroid of the observers
  // it is not earning its complexity, and any future change should be judged
  // against the same yardstick rather than only against its predecessor.
  const centroid = {
    lat: clustered.reduce((sum, o) => sum + o.lat, 0) / clustered.length,
    lon: clustered.reduce((sum, o) => sum + o.lon, 0) / clustered.length
  };
  const tightest = clustered.reduce((a, b) =>
    (estimator.anchorRangeKm(a) <= estimator.anchorRangeKm(b) ? a : b));
  results.push({
    targetId: testCase.targetId,
    errorKm,
    centroidErrorKm: estimator.haversineKm(centroid, testCase.target),
    // Sitting on whichever observer claims the smallest coverage: the crudest
    // possible reading of "heard by the shortest-range node".
    tightestObserverErrorKm: estimator.haversineKm(tightest, testCase.target),
    observerCount: clustered.length,
    droppedByClustering: testCase.observers.length - clustered.length,
    maxObserverKm: Math.max(...clusterDistances),
    // Distance from the target to its nearest observer. A useful floor: no
    // estimator can do better than the geometry allows.
    nearestObserverKm: Math.min(...clusterDistances)
  });
}

const errors = results.map((r) => r.errorKm);
console.log(`fixture: ${fixture.cases.length} cases, generated ${fixture.generated}`);
console.log(`grid: ${latStep} x ${lonStep} deg, pad ${latPad} / ${lonPad}; cluster ${CLUSTER_KM} km`);
console.log(`scored ${results.length}, skipped ${skipped.length} whose rank-1 cluster was a single node`);
const dropped = results.reduce((sum, r) => sum + r.droppedByClustering, 0);
console.log(`observers dropped by clustering: ${dropped}\n`);
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

const args = process.argv.slice(2);
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
