document.querySelectorAll('.vidwrap').forEach(function (wrap) {
  var video = wrap.querySelector('video');
  var items = Array.prototype.slice.call(wrap.querySelectorAll('.transcript li[data-t]'));
  if (!items.length) return;
  // A run that failed to record still ships the transcript, with a notice where the player
  // would be. There is nothing to seek, so the captions stay plain text — and nothing here
  // may throw, or the scripts after it never run.
  if (!video) return;

  // The voice switch: the same take in another voice, one radio button per film under the
  // player. All the films share one cue clock, so the swap keeps the second the reader was
  // at, and playing stays playing. The choice is remembered per browser — whoever picked a
  // voice once wants it on the next review too.
  var radios = Array.prototype.slice.call(wrap.querySelectorAll('.voice-switch input'));
  if (radios.length) {
    var swap = function (radio) {
      var t = video.currentTime, playing = !video.paused;
      video.src = radio.dataset.src;
      video.addEventListener('loadedmetadata', function once() {
        video.removeEventListener('loadedmetadata', once);
        video.currentTime = t;
        if (playing) video.play();
      });
      try { localStorage.setItem('hr-voice', radio.value); } catch (e) {}
    };
    radios.forEach(function (r) {
      r.addEventListener('change', function () { if (r.checked) swap(r); });
    });
    try {
      // 'hr-cloned-voice' is what the old 🐘 checkbox remembered.
      var kept = localStorage.getItem('hr-voice');
      if (kept === null && localStorage.getItem('hr-cloned-voice')) kept = 'trump';
      var pick = kept && radios.filter(function (r) { return r.value === kept; })[0];
      if (pick) { pick.checked = true; swap(pick); }
    } catch (e) {}
  }
  items.forEach(function (li) {
    li.addEventListener('click', function (ev) {
      // Captions carry links to the pages they describe. A click on one opens that page
      // and nothing else — seeking as well would yank the video out from under a reader
      // who was only following the link.
      if (ev.target.closest && ev.target.closest('a, .cue-drive')) return;
      // Seek, and stop there. Clicking a caption is how a reader *finds* a moment — often
      // one they want to look at, or read around, before watching. Starting playback on
      // that click takes the decision away from them and starts talking; the same rule
      // that keeps the film paused when the tab opens applies to every click after it.
      // The play button is right there, and the frame they asked for is now under it.
      // Seeking alone keeps whatever state the video was in: paused stays paused, and a
      // film already running keeps running from the new point.
      video.currentTime = parseFloat(li.dataset.t);
    });
  });

  // It does NOT play on arrival. A film that starts talking the moment a tab opens
  // interrupts the reader instead of serving them — they may be here for the transcript,
  // or reading with someone next to them. The play button is right there.
  // Leaving the tab still pauses it: sound following you to another tab is worse.
  var panel = wrap.closest && wrap.closest('.panel');
  if (panel) {
    panel.addEventListener('panelhide', function () { video.pause(); });
  }
  video.addEventListener('timeupdate', function () {
    var active = null;
    items.forEach(function (li) {
      if (parseFloat(li.dataset.t) <= video.currentTime) active = li;
    });
    items.forEach(function (li) { li.classList.toggle('on', li === active); });
    if (!active) return;
    // Measured against the panel's own box: offsetTop is relative to the nearest
    // positioned ancestor, which is not necessarily the scroller.
    var panel = active.parentNode;
    var a = active.getBoundingClientRect();
    var p = panel.getBoundingClientRect();
    if (a.top < p.top) panel.scrollTop += a.top - p.top - 8;
    else if (a.bottom > p.bottom) panel.scrollTop += a.bottom - p.bottom + 8;
  });
});
