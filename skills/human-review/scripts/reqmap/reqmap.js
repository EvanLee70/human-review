// The Tests tab's matrix renderer. Lifted verbatim from the model-written fragment the
// demo PR shipped (petclinic test-pr, ticket #37) when the matrix stopped being written by a
// model: `semcov.py` now draws the HTML and fills `.rm-data` itself, and this script is
// what turns that data into the card's rows, the selection, the outline and the wires.
// The only edits are marked `semcov:` - who paired a sentence (the script or a model)
// travels with each link and is said on the hover and on the blind-spot box.
(function(){
  var root=document.currentScript.closest('.reqmap');
  var D=JSON.parse(root.querySelector('.rm-data').textContent);
  var list=root.querySelector('.rm-list'),gap=root.querySelector('.rm-gap'),cur=null;
  function esc(s){return String(s).replace(/[&<>]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
  function where(id){return id.split('/').pop();}
  // `add-visit.component.spec.ts:52` -> the name, then the run of extensions that names
  // the kind of test (`.component.spec.ts`), then the `:line`. The run starts at the first
  // dot after the name: a dotted name (`visits.page.spec.ts`) would colour from `.page`
  // on, which is still where the eye should land.
  function kind(face){
    var m=/^([^.]+)((?:\.[^.:]+)+)(:\d+)?$/.exec(face);
    return m?m[1]+'<span class="rm-tk">'+m[2]+'</span>'+(m[3]||''):face;
  }

  // Test names arrive as identifiers - `getById_exposesTheAttendingVet` - because that is
  // what the source calls them, and a column of those is read one hump at a time. A name
  // that already has spaces in it (a Gherkin scenario, a Playwright title) is a sentence
  // someone wrote and is left exactly as written; only identifiers are unpacked.
  function human(name){
    if(/\s/.test(name))return name;
    var w=name.replace(/[_\-]+/g,' ')
              .replace(/([a-z0-9])([A-Z])/g,'$1 $2')      // getById  -> get By Id
              .replace(/([A-Z]+)([A-Z][a-z])/g,'$1 $2')   // HTTPServer -> HTTP Server
              .split(/\s+/).filter(Boolean)
              // An all-caps run is an acronym and keeps its case; everything else was
              // only capitalised to mark a word boundary that is now a space.
              .map(function(x){return /^[A-Z0-9]{2,}$/.test(x)?x:x.toLowerCase();});
    return w.length?w[0].charAt(0).toUpperCase()+w[0].slice(1)+(w.length>1?' '+w.slice(1).join(' '):''):name;
  }

  // Every acceptance test the branch offers, UI first, then API - the same order the
  // groups used to impose, now applied once to a list that is always on screen.
  var ORDER={e2e:0,api:1,unit:2};
  // semcov: why a test is on the card comes first - paired with a sentence, written by the
  // branch, aimed at the change, only passing through it (`rank`, from semcov.py). A test
  // about vets that runs the exception advice this PR touched belongs at the bottom, not
  // between the two that prove the ticket.
  function rank(id){var r=D.tests[id].rank;return r===undefined?0:r;}
  var ids=Object.keys(D.tests).sort(function(a,b){
    var d=rank(a)-rank(b)||ORDER[D.tests[a].cat]-ORDER[D.tests[b].cat];
    return d||human(D.tests[a].title).localeCompare(human(D.tests[b].title));});
  var groupsShown=ids.length&&D.ranks&&rank(ids[0])!==rank(ids[ids.length-1]);

  // The first part of a test is the test's own body, so its diff is the test's own story:
  // new, edited, or (no stamp) one this branch left exactly as it found it.
  // The file glyph is VS Code's own `new-file` body, corner already cut for a badge; the
  // plus is its plus. The pencil is drawn rather than borrowed, because the codicon pencil
  // at the size that corner leaves is a few hairlines and disappears - it is a filled bar
  // along the diagonal, with a point at one end and a ferrule near the other.
  // The two places a handle can take you, drawn rather than abbreviated. `\u21c6 GH` and
  // `\u21c6 VSC` were initials in a box, the width of a short file name and reading as
  // labels to be decoded; a logo is recognised without being read, so it can sit right
  // against the name it opens. The tooltips are unchanged and still carry the sentence -
  // they were already doing that, because "GH" did not say github.com either. Octicon
  // `mark-github` (the octocat, already in its circle) and the VS Code ribbon, which keeps
  // its own blue because it is only recognisable as VS Code while it is that blue.
  var GHMARK='<svg class="ico ico-gh" viewBox="0 0 16 16" aria-hidden="true">'
    +'<path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38'
    +' 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95'
    +' 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27'
    +'s-1.36.09-2 .27c-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.27-.82 2.15'
    +' 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82'
    +'-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49'
    +' 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>',
      VSCMARK='<svg class="ico ico-vsc" viewBox="0 0 24 24" aria-hidden="true">'
    +'<path d="M23.15 2.587 18.21.21a1.494 1.494 0 0 0-1.705.29l-9.46 8.63-4.12-3.128'
    +'a.999.999 0 0 0-1.276.057L.327 7.261A1 1 0 0 0 .326 8.74L3.899 12 .326 15.26'
    +'a1 1 0 0 0 .001 1.479L1.65 17.94a.999.999 0 0 0 1.276.057l4.12-3.128 9.46 8.63'
    +'a1.492 1.492 0 0 0 1.704.29l4.942-2.377A1.5 1.5 0 0 0 24 20.06V3.939a1.5 1.5 0 0 0-.85-1.352z'
    +'m-5.146 14.861L10.826 12l7.178-5.448v10.896z"/></svg>';
  var PAGE='<path class="rm-page" d="M9.5 1.1l3.4 3.5.1.4v2h-1V6H8V2H3v11h4v1H2.5l-.5-.5'
          +'v-12l.5-.5h6.7l.3.1zM9 2v3h2.9L9 2z"/>',
      PLUS='<path class="rm-badge" d="M13 16h-1v-3H9v-1h3V9h1v3h3v1h-3v3z"/>',
      PENCIL='<path class="rm-badge" d="M8.65 13.65 13.65 8.65 15.55 10.55 10.55 15.55Z'
            +'M8.65 13.65 10.55 15.55 7.9 16.3Z"/>'
            +'<path class="rm-badge" d="M12.5 9.8 14.4 11.7 13.75 12.35 11.85 10.45Z"/>',
      // The third of the set: the plus turned 45 degrees, because it is the same fact with
      // the sign flipped - a file this branch wrote, changed, or took away.
      CROSS='<path class="rm-badge" d="M16 9.7 15.3 9 12.5 11.8 9.7 9 9 9.7 11.8 12.5'
           +' 9 15.3 9.7 16 12.5 13.2 15.3 16 16 15.3 13.2 12.5Z"/>',
      ICON='<svg viewBox="0 0 16 16" aria-hidden="true">'+PAGE,
      // A test is not a file. The row's stamp used to be the same page glyph with the same
      // badge in its cut corner - right one level down, where the subject really is a file,
      // and wrong here, where the subject is the one test named beside it. Read literally
      // it claimed the branch had added a *file*, which for one test among nine in a file
      // that already existed is simply false, and the `aria-label` said so out loud. So the
      // mark alone, and big: the same three badges blown up to fill the box the page used
      // to occupy (`2x - 16` maps the cut corner onto the whole viewBox), so a reader who
      // has learned plus/pencil/cross downstairs reads them unchanged up here. The page
      // shape stays where it belongs - on the excerpt's badge, which is about a file.
      // `2x - 17` rather than `2x - 16`: the badges are drawn in the 9..16 corner, so
      // doubling them lands 2..16 - hard against two edges and a unit off centre. The odd
      // offset puts them at 1..15, which is the middle of the box they now have to
      // themselves.
      BIG='<svg viewBox="0 0 16 16" aria-hidden="true">'
         +'<g transform="matrix(2 0 0 2 -17 -17)">',
      // [glyph, hover, name — and the name is also the `data-st` the colour hangs off,
      // so a status added here cannot end up wearing another one's badge colour.]
      STAMP={new:[BIG+PLUS+'</g></svg>','a test this branch wrote','new test','new'],
             changed:[BIG+PENCIL+'</g></svg>',
                      'it existed before - this branch changed it','edited test','edited'],
             // semcov: its own lines are as they were, but it calls a helper in the same
             // file that this branch rewrote - so the run exercises different code. The
             // pencil and the edited colour (`data-st` is `edited`: it is counted with
             // them); the hover says which helper, from `t.via`.
             helper:[BIG+PENCIL+'</g></svg>',
                     'edited via helper - its own lines are unchanged',
                     'edited via helper','edited'],
             deleted:[BIG+CROSS+'</g></svg>',
                      'this branch removed it - nothing asserts what it asserted',
                      'deleted test','deleted'],
             // The one state where the subject IS a file, untouched: the page glyph with no
             // badge on it, same as the excerpt badge one level down says "unchanged".
             // Not blown up like the others - there is nothing in its corner to read.
             unchanged:[ICON+'</svg>',
                        'unchanged - this branch did not touch this test; it is listed '
                        +'because it runs through code the change set edited',
                        'unchanged test','unchanged']};
  function stamp(t){
    // The manifest is the authority on what the branch did to a TEST; a part's own diff
    // only knows what happened inside the lines quoted, which is not the same question -
    // a test edited three lines above the excerpt reads as untouched otherwise.
    var st=STAMP[t.status]||STAMP.unchanged;
    // semcov: and why it is on this card at all - the changed lines its coverage ran.
    var tip=st[1]+(t.via?' \u2014 '+esc(t.via).replace(/"/g,'&quot;'):'')
      +(t.why?' \u2014 '+esc(t.why).replace(/"/g,'&quot;'):'');
    // The word is gone from the page but not from the accessibility tree: a screen reader
    // reading this row still gets "new test", which is what the glyph is for.
    return '<span class="rm-st" data-st="'+st[3]
      +'" role="img" aria-label="'+st[2]+'" data-tip="'+tip+'">'+st[0]+'</span>';
  }
  // Which of the three the excerpt's own badge is. Keyed off the badge's words, not off
  // `p.diff`: `new file` and `new code` are both `diff:"new"` and are not the same fact -
  // one is a file that did not exist, the other is fresh lines inside one that did.
  function fbadge(p){
    if(!p.badge)return '';
    var kind=/^new file/.test(p.badge)?'new'
            :/^unchanged/.test(p.badge)?'unchanged':'edited',
        mark=kind==='new'?PLUS:kind==='edited'?PENCIL:'';
    // The words the badge used to print stay reachable, in front of the sentence that
    // was already on it: the glyph narrows what is said, it does not delete it.
    return '<span class="rm-fbadge" data-kind="'+kind+'" role="img" aria-label="'
      +esc(p.badge)+'" data-tip="'+esc(p.badge[0].toUpperCase()+p.badge.slice(1))
      +' \u2014 '+esc(p.tip).replace(/"/g,'&quot;')+'">'+ICON+mark+'</svg></span>';
  }

  function partsHtml(tid){
    var t=D.tests[tid],out='';
    t.parts.forEach(function(p){
      var badge=fbadge(p);
      // The handle that opens this file as a before/after rather than as a file. Same
      // attributes the guide's own diff links carry, so the page's existing click handler
      // picks it up: the served page asks its origin, a page read off disk hands the URI
      // to victor-vsc, and with neither the href underneath still opens the file.
      // Two destinations for the same comparison, each wearing the mark of the place it
      // opens: VS Code, and the pull request on github.com. Each is emitted only where
      // that side can really show it - no diff for a file with no before-state, and no
      // github.com link for work github.com has not seen.
      var d=p.diff_link?'<a class="srcref rm-diff" href="'+p.diff_link.href+'"'
        +' data-diff-uri="'+p.diff_link.uri+'" data-diff-path="'+esc(p.diff_link.path)+'"'
        +' data-diff-base="'+esc(p.diff_link.base)+'" data-tip="Open Diff in VSC"'
        +' aria-label="open as a diff in VS Code" target="_blank" rel="noopener">'+VSCMARK+'</a>':'';
      if(p.gh_link)d+='<a class="srcref rm-diff" href="'+p.gh_link.href+'" target="_blank"'
        +' rel="noopener" data-tip="GitHub"'
        +' aria-label="open this diff on github.com">'+GHMARK+'</a>';
      out+='<div class="rm-part"><div class="rm-srcbar">'+d
         // The name, and the path on hover - the same trade every other quoted block on
         // the page makes. A repo-relative Java path spends five segments on module,
         // `src/main/java` and the org package before it reaches the one word that
         // answers "which file is this?", which is the only question this row exists to
         // answer, and it was wrapping over three lines to say the other five.
         +(function(){
            var cut=p.label.lastIndexOf(':'),
                rel=cut<0?p.label:p.label.slice(0,cut),
                lines=cut<0?'':p.label.slice(cut),
                face=esc(rel.split('/').pop()+lines);
            return '<a class="srcref" href="'+p.href+'" data-tip="Open in VS Code: '
              +esc(rel)+'" target="_blank" rel="noopener">'+face+'</a>';})()
         // ...and the badge last, because `new file` is a fact ABOUT a file: leading with
         // it made the reader hold it in mind across the whole bar before the bar said
         // which file was new.
         +badge
         +'</div><div class="rm-scroll"><pre class="code'
         +(p.diff==='changed'?' diff-changed':'')+'"><code>';
      // p.html is already-escaped, Pygments-tokenised HTML - one entry per source line.
      // .ln-row / .dm and their colours come from the shared snippet stylesheet.
      p.html.forEach(function(l,i){
        var a=p.add&&p.add[i];
        out+='<span class="ln-row'+(a?' added':'')+'">'
           +(p.marks?'<span class="dm">'+(a?'+':' ')+'</span>':'')
           +'<span class="rm-ln">'+(p.from+i)+'</span>'+l+'</span>\n';});
      out+='</code></pre></div></div>';});
    return out;
  }
  var COVERS={};                       // test id -> [sentence id], inverted from D
  Object.keys(D.sentences).forEach(function(sid){
    (D.sentences[sid].groups||[]).forEach(function(g){g.tests.forEach(function(ev){
      (COVERS[ev.id]=COVERS[ev.id]||[]).push(sid);});});});

  // semcov: the "untouched and unpaired" groups (rank >= D.fold.from) start folded behind
  // one button that counts them - tests about something else that only happen to run a
  // changed line. Folded only when something stays open above them: a card of nothing but
  // pass-through tests shows them, rather than an empty card and a button.
  var F=D.fold,foldN=0,foldBtn=null;
  if(F&&ids.some(function(id){return rank(id)<F.from;}))
    ids.forEach(function(id){if(rank(id)>=F.from)foldN++;});
  function folded(id){return foldN>0&&rank(id)>=F.from;}
  function setFold(open){
    list.dataset.unfold=open?'yes':'no';
    foldBtn.setAttribute('aria-expanded',open?'true':'false');
    foldBtn.textContent=foldN+' '+(foldN===1?F.label.replace(/^more tests/,'more test'):F.label)
      +' — '+(open?'hide':'show');
  }
  var lastRank=null;
  ids.forEach(function(id){
    if(folded(id)&&!foldBtn){
      foldBtn=document.createElement('button');
      foldBtn.type='button';foldBtn.className='rm-fold';
      list.appendChild(foldBtn);setFold(false);
    }
    if(groupsShown&&rank(id)!==lastRank){
      lastRank=rank(id);
      var g=document.createElement('div');
      g.className='rm-tgroup';g.dataset.rank=lastRank;
      if(folded(id))g.dataset.fold='yes';
      g.textContent=D.ranks[String(lastRank)]||'';
      list.appendChild(g);
    }
    var t=D.tests[id],row=document.createElement('div');
    row.className='rm-t';row.dataset.open='no';row.dataset.id=id;
    if(folded(id))row.dataset.fold='yes';
    // A test the branch deleted is listed and not openable: this checkout has no source
    // for it, and the run has no assertion from it.
    if(t.status==='deleted')row.dataset.gone='yes';
    // The location is a link in its own right, not decoration on a button: the row opens
    // the source here, the link opens the file in the editor, and both are wanted.
    var pins=(COVERS[id]||[]).length;
    row.innerHTML='<div class="rm-thead" role="button" tabindex="0" aria-expanded="false">'
      // The other direction, asked for rather than thrown in: this draws the sentences the
      // test pins, and nothing else on the row does.
      +'<button class="rm-link" type="button" aria-pressed="false"'
      +(pins?'':' disabled')
      +' data-tip="Outline the '+pins+' sentence'+(pins===1?'':'s')
      +' this test pins \u2014 click again to release"'
      +' aria-label="outline the sentences this test pins">'
      +'<svg class="rm-fan" viewBox="0 0 12 14" aria-hidden="true">'
      +'<path d="M7 7H11.2"/><path d="M7 7H1.2"/>'
      +'<path d="M7 7C4.6 7 4.6 1.7 1.2 1.7"/>'
      +'<path d="M7 7C4.6 7 4.6 12.3 1.2 12.3"/></svg></button>'
      +'<span class="rm-cat" data-cat="'+t.cat+'">'+esc(D.cats[t.cat])+'</span>'
      // The arrow leads the title rather than closing the row: it is the control, and a
      // control at the far right of a variable-width row is a control nobody finds.
      +'<span class="rm-chev">&#9654;</span>'
      +'<span class="rm-tt">'+esc(human(t.title))+'</span>'
      // The link lands on the test's own declaration, not on the first line of the
      // window quoted around it - a window often opens on the context above the test.
      +(function(){
         // Where it lived. A link while there is something to open; a deleted test has no
         // file left in this checkout, and a dead vscode:// URL is the one thing this page
         // never emits, so it keeps the location and loses the link.
         var href=t.href||(t.parts&&t.parts[0]&&t.parts[0].href),
             face=kind(esc((t.where||where(id)).replace(/:\d+(?:[-\u2013]\d+)?$/,'')));
         // semcov: a deleted test's href is its blob at the base commit, and says so.
         return href?'<a class="rm-tw srcref" href="'+href+'" data-tip="'+esc(t.hrefTip||'Open in VS Code')+'" target="_blank" rel="noopener">'
                     +face+'</a>'
                   :'<span class="rm-tw srcref tgone" data-tip="the file is gone">'
                     +face+'</span>';})()
      // The stamp closes the row, on the right of the file it is about - the same order
      // the source bar inside the row reads in, and the same reason: "new" is a fact ABOUT
      // a file, so it comes after the file has been named. Between the title and the
      // location it sat between two things that belong together and was read as part of
      // the sentence naming the test.
      +stamp(t)+'</div>'
      +'<div class="rm-tbody"><div class="rm-tinner"></div></div>';
    // Not a button if there is nothing to press: a row that cannot open should not tell a
    // screen reader that it expands, nor take a tab stop to do nothing with.
    if(t.status==='deleted'){
      var h=row.querySelector('.rm-thead');
      h.removeAttribute('role');h.removeAttribute('tabindex');
      h.removeAttribute('aria-expanded');
    }
    list.appendChild(row);
  });

  function outline(id){
    Array.prototype.forEach.call(root.querySelectorAll('.rm-f[data-by]'),function(f){
      f.removeAttribute('data-by');});
    (COVERS[id]||[]).forEach(function(sid){
      var f=root.querySelector('.rm-f[data-s="'+sid+'"]');
      if(f)f.setAttribute('data-by','yes');});
    wired=id;redraw();
  }

  // ---- the wires -------------------------------------------------------------------
  // An outline says WHICH sentences a test pins; on a ticket four screens long it never
  // says which of them belong together, and with the card sticky the row that pinned them
  // is usually nowhere near any of them. So the relation is drawn: one wire per sentence,
  // from a dot on the ticket's own border across the gutter to the row it came from.
  // Geometry only - nothing here decides anything, it redraws what `outline` already
  // decided, from wherever the two columns currently happen to be.
  var wrap=root.querySelector('.rm-body'),
      ticket=root.querySelector('.rm-ticket'),
      sideCol=root.querySelector('.rm-side'),
      card=root.querySelector('.rm-code'),
      svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),
      wired=null,pending=false;
  svg.setAttribute('class','rm-wires');svg.setAttribute('aria-hidden','true');
  wrap.appendChild(svg);

  function draw(){
    var row=wired&&list.querySelector('.rm-t[data-link=yes]');
    if(!row){svg.textContent='';wrap.dataset.wired='no';return;}
    var base=wrap.getBoundingClientRect(),t=ticket.getBoundingClientRect(),
        sd=sideCol.getBoundingClientRect(),c=card.getBoundingClientRect();
    // Under 900px the two columns stack, one above the other. There is no gutter to draw
    // in and no left-to-right relation to draw; the outline carries it alone.
    if(sd.left<t.right+8){svg.textContent='';wrap.dataset.wired='no';return;}
    var x0=t.right-base.left,ax=sd.left-base.left,
        // The row's TITLE, not the row: an open row is its header plus however many
        // screens of source it quotes, and the middle of that is somewhere down in the
        // code. The wire has to hang off the name of the test, which is the part that
        // stays put and the part the reader is looking at.
        r=row.querySelector('.rm-thead').getBoundingClientRect(),
        // The card scrolls inside itself, so the row can be above or below what it shows.
        // The wire then lands on the card's edge at the point the row left it, which is
        // the direction the reader has to scroll.
        ay=Math.min(Math.max((r.top+r.bottom)/2,c.top+8),c.bottom-8)-base.top,d='';
    (COVERS[wired]||[]).forEach(function(sid){
      var f=root.querySelector('.rm-f[data-s="'+sid+'"]');if(!f)return;
      // A sentence that wraps is several rectangles, and the wire leaves from the middle
      // of all of them together - the middle of the block of text, not of its last line.
      var rects=f.getClientRects(),top=Infinity,bot=-Infinity;
      if(!rects.length)return;
      Array.prototype.forEach.call(rects,function(k){
        top=Math.min(top,k.top);bot=Math.max(bot,k.bottom);});
      // The wire lands on the ticket's own border, on the line the sentence sits on, and
      // the dot is what makes that a landing rather than a line running out of page.
      var mid=(top+bot)/2-base.top,dx=Math.max(12,Math.abs(ay-mid)*0.16);
      d+='<path class="rm-wire" d="M'+x0+' '+mid+'C'+(x0+dx)+' '+mid+' '
        +(ax-dx)+' '+ay+' '+ax+' '+ay+'"/>'
        +'<circle class="rm-dot" cx="'+x0+'" cy="'+mid+'" r="3"/>';
    });
    svg.innerHTML=d?d+'<circle class="rm-hub" cx="'+ax+'" cy="'+ay+'" r="3"/>':'';
    wrap.dataset.wired=d?'yes':'no';
  }

  // Every redraw is a full remeasure, and the things that move the endpoints are the page
  // scrolling, the card scrolling inside itself, the window resizing and a row opening.
  // One frame's worth of coalescing keeps that off the scroll path.
  function redraw(){
    if(pending)return;pending=true;
    requestAnimationFrame(function(){pending=false;draw();});
  }
  addEventListener('scroll',redraw,true);   // capture: the card's own scroll does not bubble
  addEventListener('resize',redraw);
  if(window.ResizeObserver)new ResizeObserver(redraw).observe(wrap);
  // The accordion animates its height, so the rows under it are still moving after the
  // click: follow them to where they land.
  list.addEventListener('transitionend',redraw);
  function toggle(row){
    if(row.dataset.gone==='yes')return;   // there is no source left to show
    var open=row.dataset.open==='yes';
    // An accordion, not a stack: opening one closes the rest, so the list never turns
    // into three screens of source with the row you wanted somewhere in the middle.
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t'),function(o){
      o.dataset.open='no';
      o.querySelector('.rm-thead').setAttribute('aria-expanded','false');});
    if(open){redraw();return;}
    var body=row.querySelector('.rm-tinner');
    if(!body.innerHTML)body.innerHTML=partsHtml(row.dataset.id);
    row.dataset.open='yes';
    row.querySelector('.rm-thead').setAttribute('aria-expanded','true');
    redraw();
  }

  // The other direction, drawn on demand: which sentences does THIS test pin. One row at a
  // time - two rows outlining into the same paragraph and the outline stops meaning
  // anything - and it releases the sentence selection first, because "these tests cover
  // that sentence" and "that test pins these sentences" are two different relations and
  // showing both in the same green leaves the reader working out which is which.
  function link(row){
    var on=row.dataset.link==='yes';
    clearSel();
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t'),function(o){
      o.dataset.link='no';
      o.querySelector('.rm-link').setAttribute('aria-pressed','false');});
    if(on){outline(null);return;}
    row.dataset.link='yes';
    row.querySelector('.rm-link').setAttribute('aria-pressed','true');
    outline(row.dataset.id);
  }

  // Everything the sentence -> tests direction put on the page, taken back off it.
  function clearSel(){
    if(cur){cur.removeAttribute('aria-current');cur=null;}
    delete list.dataset.sel;
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t'),function(row){
      delete row.dataset.hit;
      var b=row.querySelector('.rm-cat');if(b)delete b.dataset.strength;
      row.querySelector('.rm-thead').removeAttribute('data-tip');});
    gap.innerHTML='';gap.hidden=true;
  }
  list.addEventListener('click',function(e){
    if(e.target.closest('.rm-fold')){                   // semcov: show/hide the folded rest
      setFold(list.dataset.unfold!=='yes');redraw();return;}
    var l=e.target.closest('.rm-link');                 // draws, never opens
    if(l){link(l.closest('.rm-t'));return;}
    if(e.target.closest('.rm-tw'))return;              // the editor link is not a toggle
    var h=e.target.closest('.rm-thead');if(h)toggle(h.parentNode);});
  list.addEventListener('keydown',function(e){
    // A <button> already fires its own click on Enter and Space; letting the row's handler
    // see the key as well would open the source at the same time.
    if(e.target.closest&&e.target.closest('.rm-link'))return;
    var h=e.target.closest&&e.target.closest('.rm-thead');
    if(h&&(e.key==='Enter'||e.key===' ')){e.preventDefault();toggle(h.parentNode);}});

  // semcov: the decision a narrowed sentence rests on, named and linked where it was
  // recorded (`proposal.md:86`) - "Recorded: <the whole line>" left the reader to find it.
  function recorded(s){
    if(!s.decisionWhere)return 'Recorded: '+esc(s.decision);
    var w=s.decisionHref?'<a class="srcref" href="'+esc(s.decisionHref).replace(/"/g,'&quot;')
      +'" data-tip="Open in VS Code" target="_blank" rel="noopener">'+esc(s.decisionWhere)+'</a>'
      :esc(s.decisionWhere);
    return 'Recorded in '+w+': '+esc(s.decisionQuote||s.decision);
  }
  function open(sid){
    var s=D.sentences[sid];if(!s)return;
    // Clicking the sentence you already picked puts the ticket back the way it was. There
    // was no way out of a selection except by making another one, so the column stayed
    // sorted into "these prove it" and a dimmed rest long after the reader had finished
    // asking - and the dimming is the page's answer to a question, not its resting state.
    if(cur&&cur.dataset.s===sid){clearSel();return;}
    if(cur)cur.removeAttribute('aria-current');
    cur=root.querySelector('.rm-f[data-s="'+sid+'"]');cur.setAttribute('aria-current','true');
    // Which of the nine cover this sentence, and how hard each one pins it.
    var hit={};
    (s.groups||[]).forEach(function(g){g.tests.forEach(function(ev){
      hit[ev.id]={cat:g.cat,strength:ev.strength,why:ev.why,by:ev.by};});});
    list.dataset.sel='yes';outline(null);
    Array.prototype.forEach.call(list.querySelectorAll('.rm-t'),function(row){
      var h=hit[row.dataset.id],b=row.querySelector('.rm-cat');
      row.dataset.hit=h?'yes':'no';
      row.dataset.link='no';
      row.querySelector('.rm-link').setAttribute('aria-pressed','false');
      row.dataset.open='no';
      row.querySelector('.rm-thead').setAttribute('aria-expanded','false');
      if(h){b.dataset.strength=h.strength;row.querySelector('.rm-thead')
        .setAttribute('data-tip',(h.by==='model'?'\uD83E\uDD16 ':'')+h.strength+' — '+(h.why||''));}
      else{delete b.dataset.strength;row.querySelector('.rm-thead').removeAttribute('data-tip');}
    });
    // A gap is a criticism of the REQUIREMENT, so it is shown beside the requirement.
    // "Blind spot" alone left the reader asking whose fault it is - the tests', for not
    // reaching a claim the ticket makes, or the ticket's, for never making the claim.
    // The two are different findings and go to different people.
    var kind=s.gapKind==='requirement'?'in the requirement':'in the tests';
    gap.dataset.kind=s.gapKind||'tests';
    // semcov: the links a model read and turned down, under the blind spot - the keyword
    // match that would have painted this sentence, and the one line on why it does not count.
    var rej=(s.rejected||[]).map(function(r){
      return '<li><span class="rm-rid">'+esc(where(r.id))+'</span> \u2014 '+esc(r.why)+'</li>';}).join('');
    var head=s.cov==='narrowed'?'Narrowed on purpose':'Blind spot';
    gap.innerHTML=(s.gap?'<h4>'+head+' <span class="rm-gapkind">'+kind+'</span>'
      +(s.by==='model'?'<sup class="rm-ai" data-tip="as inferred by AI">🤖</sup>':'')+'</h4><p>'+esc(s.gap)+'</p>'
      +(s.decision?'<p class="rm-dec">'+recorded(s)+'</p>':''):'')
      +(rej?'<h4>Matched on words, rejected by AI <sup class="rm-ai">🤖</sup></h4><ul class="rm-rej">'+rej+'</ul>':'');
    gap.hidden=!(s.gap||rej);
  }
  root.addEventListener('click',function(e){
    var f=e.target.closest('.rm-f');if(f&&root.contains(f))open(f.dataset.s);});
  // semcov: the tally over the ticket - "1 missing", "4 partially" - jumps to the sentences
  // it counts. Each press moves on to the next of that state, wrapping, and selects it the
  // way a click on the sentence would, so its tests and its blind spot come up at once.
  // The href is the first one, for a reader with no script; with one, the page's own
  // hash is left alone (a `#rm-s-…` in the URL would fight the tab router).
  root.addEventListener('click',function(e){
    var j=e.target.closest&&e.target.closest('.rm-jump');if(!j||!root.contains(j))return;
    var all=root.querySelectorAll('.rm-issue .rm-f[data-cov="'+j.dataset.cov+'"]');
    if(!all.length)return;
    e.preventDefault();
    var i=(+(j.dataset.i||-1)+1)%all.length;j.dataset.i=i;
    all[i].scrollIntoView({block:'center',behavior:'smooth'});
    // `open` on the sentence already picked would put it down again; a jump never does.
    if(!(cur&&cur.dataset.s===all[i].dataset.s))open(all[i].dataset.s);
    try{all[i].focus({preventScroll:true});}catch(_){}
  });
  root.addEventListener('keydown',function(e){
    var f=e.target.closest&&e.target.closest('.rm-f');
    if(f&&(e.key==='Enter'||e.key===' ')){e.preventDefault();open(f.dataset.s);}});

  // The hover on a sentence shows the badges themselves, not their names in prose, so
  // the tip and the list on the right are visibly the same two concepts - then says what
  // clicking will do, because nothing else on the page announces that.
  Array.prototype.forEach.call(root.querySelectorAll('.rm-f[data-s]'),function(f){
    var s=D.sentences[f.dataset.s];if(!s)return;
    var tip=(s.groups||[]).map(function(g){
      return '<span class="rm-cat" data-cat="'+g.cat+'">'+esc(D.cats[g.cat])
        +'</span><span class="rm-n">\u00D7'+g.tests.length+'</span>';}).join('');
    tip=tip?tip
           :'<span class="rm-do">no test \u00B7 click for what is missing</span>';
    // semcov: who paired it, after the badges - the script's evidence or a model's reading.
    tip+=s.cov==='unmapped'?'<span class="rm-do"> not paired yet \u00B7 run the 🤖</span>'
        :s.cov==='unconfirmed'?'<span class="rm-do"> \u00B7 shared words only, not confirmed \u00B7 run the 🤖</span>'
        :s.cov==='narrowed'?'<span class="rm-do"> \u00B7 narrowed by '
          +(s.decisionName?esc(s.decisionName):'a recorded decision')+'</span>'
        :'<span class="rm-do"> \u00B7 '+(s.by==='model'?'🤖 checked by AI':'paired by script')+'</span>';
    f.setAttribute('data-tip-html',tip);
    f.removeAttribute('data-tip');
  });
})();
