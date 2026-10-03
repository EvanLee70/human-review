// Browser half of /human-review's per-spec Karma coverage: which Istanbul statement
// counters each spec moved. Istanbul keeps one global set of counters for the whole run,
// so a spec's own coverage is the difference between the counters after it and before it.
// Function counters too, not only statements: `error => {` heads no statement of its own,
// so a spec that ran the error callback left its first line looking unrun (eval run 8,
// owner-list.component.ts:67, listed under "changed lines no test runs").
(function () {
  var karma = window.__karma__;
  var env = window.jasmine && window.jasmine.getEnv && window.jasmine.getEnv();
  if (!karma || !env) return;
  var before = null;
  function snapshot() {
    var cov = window.__coverage__ || {}, out = {};
    for (var k in cov) {
      var s = cov[k].s, f = cov[k].f || {}, copy = {s: {}, f: {}};
      for (var id in s) copy.s[id] = s[id];
      for (var fid in f) copy.f[fid] = f[fid];
      out[k] = copy;
    }
    return out;
  }
  env.addReporter({
    specStarted: function () { before = snapshot(); },
    specDone: function (r) {
      var cov = window.__coverage__ || {}, hits = {}, fhits = {};
      for (var k in cov) {
        var s = cov[k].s, f = cov[k].f || {}, b = (before && before[k]) || {s: {}, f: {}};
        var ids = [], fids = [];
        for (var id in s) if (s[id] > (b.s[id] || 0)) ids.push(id);
        for (var fid in f) if (f[fid] > (b.f[fid] || 0)) fids.push(fid);
        if (ids.length) hits[k] = ids;
        if (fids.length) fhits[k] = fids;
      }
      karma.info({hrTestcov: {id: r.fullName, description: r.description,
                              status: r.status, hits: hits, fhits: fhits}});
    }
  });
})();
