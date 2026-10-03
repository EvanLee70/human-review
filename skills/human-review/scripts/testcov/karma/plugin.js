// Node half of /human-review's per-spec Karma coverage.
//
// The browser (client.js) reports, per spec, which Istanbul statements and functions it
// executed — functions because a callback's head (`error => {`) is no statement's line. At the
// end of the run the full coverage object arrives with the browser's completion, carrying
// every file's statement map and the source map of the transpiled JS it was taken from;
// this maps statements -> JS lines -> TypeScript lines and writes one JSON file:
//   {"executable": {<abs ts path>: [lines]}, "tests": [{id, description, status, hits}]}
'use strict';
const fs = require('fs');
const path = require('path');

function loadSourceMap() {
  const from = [path.dirname(path.resolve(process.env.HR_TESTCOV_KARMA_BASE || '.')), process.cwd()];
  return require(require.resolve('source-map', {paths: from}));
}

function initFramework(files) {
  files.push({pattern: path.join(__dirname, 'client.js'), included: true, served: true,
              watched: false, nocache: false});
}
initFramework.$inject = ['config.files'];

function Reporter(logger) {
  const log = logger.create('hr-testcov');
  const specs = [];
  let coverage = null;
  this.adapters = [];

  this.onBrowserInfo = function (browser, info) {
    if (info && info.hrTestcov) specs.push(info.hrTestcov);
  };
  this.onBrowserComplete = function (browser, result) {
    if (result && result.coverage) coverage = result.coverage;
  };
  this.onExit = function (done) {
    write().then(done, err => { log.error(String(err && err.stack || err)); done(); });
  };

  async function write() {
    const out = process.env.HR_TESTCOV_OUT;
    if (!out) return;
    if (!coverage) {
      log.warn('no coverage object came back from the browser — was --code-coverage on?');
      return;
    }
    const {SourceMapConsumer} = loadSourceMap();
    const lines = {};          // file -> statement id -> [ts lines]
    const fnLines = {};        // file -> function id -> [ts line of its declaration]
    const executable = {};
    for (const [file, fc] of Object.entries(coverage)) {
      const key = fc.path || file;
      let consumer = null;
      if (fc.inputSourceMap) {
        try { consumer = await new SourceMapConsumer(fc.inputSourceMap); } catch (e) { consumer = null; }
      }
      const byId = {};
      const all = new Set();
      // A statement owns the lines of its span that nothing nested inside it owns. Without
      // this, `this.request = svc.get().subscribe(page => {…}, error => {…})` — one
      // statement over thirteen lines — charged the success callback's body to the spec
      // that only made the request fail: every line of the span went to whoever ran it.
      // The callbacks' heads are their functions' (`byFn` below), their bodies their own
      // statements'.
      const spans = [...Object.values(fc.statementMap || {}),
                     ...Object.values(fc.fnMap || {}).map(f => f.loc).filter(Boolean)]
        .map(l => [l.start.line, l.end.line]);
      const nested = (a, b) => spans.filter(([x, y]) => x >= a && y <= b && !(x === a && y === b));
      for (const [id, loc] of Object.entries(fc.statementMap || {})) {
        const got = new Set();
        const inner = loc.end.line > loc.start.line ? nested(loc.start.line, loc.end.line) : [];
        for (let l = loc.start.line; l <= loc.end.line; l++) {
          if (l !== loc.start.line && inner.some(([x, y]) => l >= x && l <= y)) continue;
          if (!consumer) { got.add(l); continue; }
          const p = consumer.originalPositionFor({line: l, column: l === loc.start.line ? loc.start.column : 0,
                                                  bias: SourceMapConsumer.LEAST_UPPER_BOUND});
          if (p && p.line) got.add(p.line);
        }
        byId[id] = [...got];
        got.forEach(x => all.add(x));
      }
      // A function's own head: the line its declaration starts on, charged to a spec
      // whenever the spec called it.
      const byFn = {};
      for (const [id, fn] of Object.entries(fc.fnMap || {})) {
        const at = (fn.decl || fn.loc || {}).start;
        if (!at || !at.line) continue;
        let l = at.line;
        if (consumer) {
          const p = consumer.originalPositionFor({line: at.line, column: at.column || 0,
                                                  bias: SourceMapConsumer.LEAST_UPPER_BOUND});
          l = p && p.line;
        }
        if (l) { byFn[id] = [l]; all.add(l); }
      }
      if (consumer && consumer.destroy) consumer.destroy();
      lines[key] = byId;
      fnLines[key] = byFn;
      executable[key] = [...all].sort((a, b) => a - b);
    }
    const tests = specs.map(s => {
      const hits = {};
      for (const [file, ids] of Object.entries(s.hits || {})) {
        const key = (coverage[file] && coverage[file].path) || file;
        const got = new Set();
        for (const id of ids) (lines[key] && lines[key][id] || []).forEach(x => got.add(x));
        if (got.size) hits[key] = [...got].sort((a, b) => a - b);
      }
      for (const [file, ids] of Object.entries(s.fhits || {})) {
        const key = (coverage[file] && coverage[file].path) || file;
        const got = new Set(hits[key] || []);
        for (const id of ids) (fnLines[key] && fnLines[key][id] || []).forEach(x => got.add(x));
        if (got.size) hits[key] = [...got].sort((a, b) => a - b);
      }
      return {id: s.id, description: s.description, status: s.status, hits};
    });
    fs.mkdirSync(path.dirname(out), {recursive: true});
    fs.writeFileSync(out, JSON.stringify({executable, tests}));
    log.info(`per-spec coverage of ${tests.length} spec(s) -> ${out}`);
  }
}
Reporter.$inject = ['logger'];

module.exports = {
  'framework:hr-testcov': ['factory', initFramework],
  'reporter:hr-testcov': ['type', Reporter],
};
