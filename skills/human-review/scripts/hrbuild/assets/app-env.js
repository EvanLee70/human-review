// The deployed-app row: what is up, and the one or two things you can do about it.
//
// Two copies of this report exist and the row has to be honest in both, and it is now the
// *same row* in both — one line of verbs, and only the click differs. Served, a verb runs
// its command through the review server and the row keeps its own state: nothing
// answering, so Start; something answering, so the address as a link, then Stop. Off disk
// nothing here can run, so every verb is a clipboard for its command and both are on
// screen, because which line the reader wants to paste is their business.
//
// There is no Where any more. Served, the row asks the host itself, once, when the address
// it remembers does not answer — a reader who started the stack in another tab gets the
// link without having to know there was a question to ask.
//
// What this file decides is *which verbs apply*. Which face each one wears — clipboard or
// play — is SERVER_JS's answer, per action, and it is why the wrapper span is what gets
// hidden here rather than the buttons: two owners of `hidden` on one element is how a
// control ends up flickering between two truths.
//
// A control is hidden when it cannot be used, not greyed. Greying is for a thing you
// could have had under a condition worth teaching; Stop before anything has started is
// not that, it is noise in the four-item row a reader scans in one glance.
//
// Everything is written to start in the degraded state and *rise*. A button drawn as live
// that falls back 30ms later has already been clicked by then, and has already lied.
(function () {
  var bar = document.querySelector('.appenv');
  if (!bar) return;
  var state = bar.querySelector('.appenv-state');
  var addr = bar.querySelector('.appenv-url');
  // The wrapper per verb, not the button: each wrapper holds the clipboard/play pair that
  // `command_html` emitted, and SERVER_JS owns which of the two is up.
  var acts = {start: bar.querySelector('.appenv-start'),
              stop: bar.querySelector('.appenv-stop')};
  // The seed's button, which the build draws, and after it one per fixture the running
  // environment reports — those are drawn here, because only the environment knows them.
  var resets = bar.querySelector('.appenv-resets');
  var reset = bar.querySelector('.appenv-reset');
  var resetsTo = bar.querySelector('.appenv-resets-to');
  // One command at a time. `docker compose up` is minutes, the row stays readable
  // throughout, and a second press in the middle of it is a reader who could not tell the
  // first one had started — a Stop sent into a half-built stack is the worst of them.
  var busy = false;
  // Raised by the probe in SERVER_JS, never assumed: a page on GitHub Pages is https and
  // is not served by us, and the buttons here must not believe otherwise.
  var served = false;
  // Whether the host has been asked where the app is. Once per page: the answer is either
  // an address, which is then remembered, or nothing, and asking again would say nothing.
  var looked = false;
  var links = Array.prototype.slice.call(document.querySelectorAll('a[data-app]'));
  // Per page, not per machine: two review pages describe two branches, and each branch
  // gets its own instance on its own port.
  var KEY = 'human-review:appbase:' + location.pathname;

  function stored() {
    // A browser with site data blocked throws on read; the page must still work.
    try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
  }
  function remember(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  // The address the row is about. There is no box to type one into any more — served,
  // Start prints it and we scrape it; off disk, the build's own `base` is the only guess
  // anyone had — so it lives here and in localStorage, which is what survives a reload.
  var current = stored();
  function base() {
    return (current || bar.dataset.fallback || '').replace(/\/+$/, '');
  }

  function apply() {
    var b = base();
    links.forEach(function (a) {
      var path = a.dataset.app;
      if (b) { a.href = b + path; a.classList.remove('dead'); }
      // No base and no fallback: the link has nowhere to point, and saying so by going
      // grey is honest where a live-looking link that 404s is not.
      else { a.removeAttribute('href'); a.classList.add('dead'); }
    });
  }

  var blocked = function (el) { return el.getAttribute('aria-disabled') === 'true'; };
  function arm(el, on, tip) {
    if (!el) return;
    el.setAttribute('aria-disabled', on ? 'false' : 'true');
    el.dataset.tip = tip;
  }
  // Disarmed *and* gone. Both, and in that order, because `hidden` is a style and a
  // stylesheet that failed to load would otherwise leave a live button behind.
  function gate(el, on, tip) { if (!el) return; arm(el, on, tip); el.hidden = !on; }

  // A whole verb, in or out of the row. The tip is not touched: it was written by the
  // build and it says what a click here does, which does not change with the state.
  function show(wrap, on) { if (wrap) wrap.hidden = !on; }

  // The row's whole truth, in one call. Each verb is gated on the thing it actually needs
  // — because a control that can be pressed while its precondition is missing is a
  // control that lies: Reset would fail, and \u25b8 would drive an app that is not there.
  //
  // Off disk the gate is open on both. Neither can *run* there — they are
  // clipboards — and a clipboard for `stop` is exactly as useful with the app down as up:
  // the reader is pasting it into a terminal, where the state of things is their business
  // and not this page's. Hiding two thirds of the commands behind a health check was the
  // old second row's worst habit and there is no reason to inherit it.
  function setLive(live, why) {
    show(acts.start, !served || !live);
    show(acts.stop, !served || live);
    resetButtons().forEach(function (el) { gate(el, live, resetTip(el.dataset.fixture)); });
    if (resetsTo && !live) resetsTo.hidden = true;
    [].forEach.call(document.querySelectorAll('.cue-drive'), function (el) {
      arm(el, live, live ? 'Drive the app to this point' : why);
    });
    // The address is shown only while something answers at it. A URL on a page next to a
    // dead port is the one thing here that can waste a reader's afternoon.
    if (addr) {
      addr.hidden = !live;
      if (live) { addr.href = base(); addr.textContent = base(); }
      else { addr.removeAttribute('href'); addr.textContent = ''; }
    }
  }

  // Live has nothing to say that the address does not say better, so the pill empties and
  // disappears; every other state is a word in its place. `Offline` and not `offline at
  // http://…`: the URL of a thing that is not answering is an invitation to click it.
  function say(kind, text) {
    state.dataset.state = kind;
    state.textContent = text || '';
    state.hidden = !text;
  }

  // `failed` is a command that just died: what to say, and the line it died on, in place
  // of a bare `Offline` if nothing answers. It has to ride *through* the probe rather than
  // be written after it — the health check answers asynchronously, and writing the failure
  // first meant `Offline` landed on top of it a moment later. A start that dies in two
  // seconds then looked like a button that did nothing at all.
  function probe(failed) {
    var b = base();
    function down(tip) {
      say('down', failed ? failed.word : 'Offline');
      if (failed) state.dataset.tip = failed.tip;
      else state.removeAttribute('data-tip');
      setLive(false, failed ? failed.tip : tip);
    }
    if (!b) { if (!look(failed)) down('Nothing is running yet'); return; }
    say('unknown', 'checking\u2026');
    setLive(false, 'Checking whether anything is listening\u2026');
    // /healthz answers with CORS open, so this works from a file:// page too. A failure
    // here means "nothing is listening", which is the normal case for an old report.
    fetch(b + '/healthz', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw 0;
      say('live', '');
      setLive(true);
      listFixtures(b);
    }).catch(function () {
      if (!look(failed)) down('Nothing is answering at ' + b + ' \u2014 start it first');
    });
  }

  // Nothing answers at the address this browser remembers — or it remembers none — so ask
  // the host whether an instance is up somewhere else, and take its address if one is.
  // Served only, since only a served page can run the host's `url` command, and once: the
  // reader who started the stack from a terminal, in another tab, or before clearing their
  // site data lands on the row with the link already in it. It is a read — the host is
  // asked where, nothing is started — which is why it may run merely because the page
  // opened. It replaced the Where button, which asked the same question on a press, and
  // was a button nobody could guess the meaning of.
  //
  // Not after a failure: a start that just died has something to say, and the question has
  // been asked already by then anyway. And not over a Start or Stop pressed meanwhile —
  // those own the row until they finish.
  function look(failed) {
    if (failed || looked || !served || busy || !window.HR.can('demo-env-url')) return false;
    looked = true;
    say('unknown', 'looking\u2026');
    setLive(false, 'Asking the host whether the app is already up\u2026');
    window.HR.run('demo-env-url', {}).then(function (done) {
      if (busy) return;
      if (done.state === 'done' && adopt(done.result && done.result.base)) return;
      probe();
    }).catch(function () { if (!busy) probe(); });
    return true;
  }

  function resetButtons() {
    return resets ? [].slice.call(resets.querySelectorAll('.appenv-reset')) : [];
  }
  function resetTip(name) {
    return name ? 'Put the demo data back to its seed, then load the \u201c' + name
                  + '\u201d fixture on top of it'
                : 'Put the demo data back to its seed';
  }
  // With fixtures the group is one verb and its arguments — "Reset DB to: [default]
  // [green]" — and without, the seed's button says the whole thing on its own.
  function nameResets(any) {
    if (resetsTo) resetsTo.hidden = !any;
    if (reset) reset.textContent = any ? 'default' : 'Reset DB';
  }

  // The fixtures are the environment's to name, and asked for every time it is found up:
  // a file added to the project is a button on the next probe, and an instance built from
  // a commit that had none answers with none — or, older still, with no list at all,
  // which is the same thing. Any failure leaves the lone Reset DB, which is what it was.
  function listFixtures(b) {
    if (!bar.dataset.reset) return;
    fetch(b + bar.dataset.reset, {cache: 'no-store'}).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (info) {
      // The row may have moved on while this was in flight — stopped, or pointed at
      // another instance — and buttons for that one must not land in this one's row.
      if (!info || base() !== b || state.dataset.state !== 'live') return;
      var names = Array.isArray(info.fixtures) ? info.fixtures : [];
      resetButtons().slice(1).forEach(function (el) { el.remove(); });
      names.forEach(function (name) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'appenv-reset';
        btn.dataset.fixture = name;
        btn.textContent = name;
        gate(btn, true, resetTip(name));
        resets.appendChild(btn);
      });
      nameResets(names.length > 0);
    }).catch(function () {});
  }

  // What a verb that exited non-zero leaves in the row: its own word, and the last line it
  // printed as the tip, which is where the reason is.
  function failure(word, done, otherwise) {
    return {word: word, tip: window.HR.tail(done) || otherwise};
  }

  apply(); probe();

  // Learned once the environment answers on its own, and only then. `adopt` is the whole
  // reason the Start button is worth more than the clipboard: `start-docker.sh` ends by
  // printing the port the host gave it, the server scrapes that line, and the address the
  // reader would otherwise have had to hunt for appears in the row. Two round trips to
  // the terminal become none — the second being the one nobody counts, where you go
  // back to find the URL again because the clipboard has moved on.
  function adopt(url) {
    if (!url) return false;
    current = url.replace(/\/+$/, '');
    remember(current); apply(); probe();
    return true;
  }

  // The copy button used to be this block's own, with its own clipboard call and its own
  // "Copied" label. It is `command_html`'s now, like every other command on the page, and
  // handled by the one clipboard-and-toast handler in EDITOR_JS — a second implementation
  // for one button is how two of them end up behaving differently.

  // While a command is in flight nothing else in the row may be pressed — a Stop sent
  // into the middle of a docker build is a half-torn-down instance nobody asked for —
  // and the pill carries the last line the command printed, which is where a reader who
  // wants to know it is still moving can look. A docker build prints thousands.
  function drive(id, face, then) {
    busy = true;
    setLive(false, 'Waiting for the app \u2014 ' + face.toLowerCase() + '\u2026');
    say('unknown', face + '\u2026');
    return window.HR.run(id, {}, function (snap) {
      var line = window.HR.tail(snap);
      if (line) state.dataset.tip = line;
    }).then(function (done) {
      busy = false;
      state.removeAttribute('data-tip');
      then(done);
    }).catch(function (e) {
      busy = false;
      say('down', face + ' failed');
      state.dataset.tip = e.message || 'the review server is no longer running';
      setLive(false, state.dataset.tip);
    });
  }

  // The run half of one verb, and the click that belongs to it.
  //
  // `stopPropagation` is the whole reason these are bound here rather than left to
  // EDITOR_JS, which already runs any `.runhere[data-action]` on the page: that handler
  // reloads the page when the command finishes, and this row's entire job is to *keep* the
  // page while the app comes up underneath it — the address it scraped, the links it
  // aimed, the place the reader had got to. A listener on the button itself runs in the
  // target phase, before the document-level one in the bubble phase, so stopping there is
  // enough. The clipboard half is deliberately left alone: off disk the generic
  // copy-and-toast handler is exactly right, and a second implementation of it for this
  // row is how two of them end up behaving differently.
  function onrun(wrap, fn) {
    var btn = wrap && wrap.querySelector('.cmd-run');
    if (!btn) return;
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (busy) return;
      fn();
    });
  }

  onrun(acts.start, function () {
    drive('demo-env', 'Starting', function (done) {
      if (done.state === 'done' && adopt(done.result && done.result.base)) return;
      // It ran and printed no URL we recognised, or it failed. Either way the reader is
      // back where they started rather than stuck: `probe` re-reads whatever base we
      // have, and the command below is still there to be run by hand.
      probe(done.state === 'done' ? null
            : failure('start failed', done, 'the command exited ' + done.exit));
    });
  });

  // Stop forgets the address as well as freeing the port. Remembering it would leave
  // every link in the transcript pointing confidently at nothing, and the next Start will
  // hand us a different port anyway.
  onrun(acts.stop, function () {
    drive('demo-env-stop', 'Stopping', function (done) {
      if (done.state === 'done') { current = ''; remember(''); apply(); }
      probe(done.state === 'done' ? null
            : failure('stop failed', done, 'the command exited ' + done.exit));
    });
  });

  // One command per caption. Off disk it is copied, because a file cannot drive a browser
  // on your machine; served, it is run, and what the reader gets back is the app already
  // sitting on the screen the caption describes. Same template either way — the one in
  // `data-drive` for the clipboard, the one the build declared for the server — so the
  // two paths cannot drift into doing different things.
  var tmpl = bar.dataset.drive;
  if (tmpl) document.querySelectorAll('.cue-drive').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (blocked(btn)) return;
      var was = btn.innerHTML;
      function tick(mark, hold) {
        btn.classList.add('copied'); btn.innerHTML = mark;
        setTimeout(function () { btn.classList.remove('copied'); btn.innerHTML = was; }, hold);
      }
      if (window.HR.can('cue-drive')) {
        btn.innerHTML = '&#8943;';
        window.HR.run('cue-drive', {n: btn.dataset.n, base: base()})
          .then(function (done) {
            if (done.state === 'done') { tick('&#10003;', 1400); return; }
            btn.dataset.tip = window.HR.tail(done) || 'the driver exited ' + done.exit;
            tick('&#10007;', 2600);
          })
          .catch(function (e) { btn.dataset.tip = e.message; tick('&#10007;', 2600); });
        return;
      }
      var cmd = tmpl.replace(/\{n\}/g, btn.dataset.n).replace(/\{base\}/g, base());
      window.HR.copy(cmd).then(function () {
        tick('&#10003;', 1400);
      }).catch(function () { btn.dataset.tip = 'could not copy'; });
    });
  });

  window.HR.onready(function () {
    // `demo-env` and not merely "is there a server": a build that stopped declaring the
    // start command has to be able to take the verb away from a page that is still open.
    served = window.HR.can('demo-env');
    if (served) {
      // Which swaps the whole row: the verbs come back and the terminal command steps
      // out, since pressing Start here does the same thing without leaving the page.
      bar.classList.add('appenv-served');
      // Re-ask rather than re-deriving from whatever the pill happens to say: the first
      // probe may still be in flight, and `served` has just changed the answer for two
      // of the three verbs.
      probe();
    }
    // The `probe` above is also what asks the host where the app is, when the remembered
    // address does not answer — see `look`. It waits for this point because `served` is
    // what says the host can be asked at all.
  });

  // Explicit, never automatic. Resetting on every link click would throw away work the
  // reviewer was in the middle of; the duplicate rows and unique-constraint collisions it
  // exists to prevent are the reviewer's own repeated form submissions, and they know
  // when they have made a mess.
  //
  // One listener for the whole group, since the fixture buttons come and go with the
  // probe.
  if (resets) resets.addEventListener('click', function (ev) {
    var btn = ev.target.closest('.appenv-reset');
    if (!btn || blocked(btn)) return;
    var b = base();
    if (!b) return;
    var name = btn.dataset.fixture, face = btn.textContent, all = resetButtons();
    all.forEach(function (el) { el.disabled = true; });
    btn.textContent = 'Resetting\u2026';
    fetch(b + bar.dataset.reset + (name ? '/' + encodeURIComponent(name) : ''),
          {method: 'POST', cache: 'no-store'}).then(function (r) {
      btn.textContent = r.ok ? 'Reset' : 'Reset failed';
    }).catch(function () { btn.textContent = 'Reset failed'; }).then(function () {
      setTimeout(function () {
        btn.textContent = face;
        all.forEach(function (el) { el.disabled = false; });
      }, 1400);
    });
  });
})();
