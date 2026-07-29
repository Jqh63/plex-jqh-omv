// Single source of truth for the version shown to the user: the name of the
// active service-worker cache (`sw.js`). Nothing here is hardcoded — the label
// is DERIVED, so there is no second marker that can drift from sw.js.
//
// 2026-07-29 — the cache name carries two things that used to be conflated in
// one inflating minor (v8.53 … v8.66, three bumps in a single day, none of them
// telling you what changed):
//
//   plex-jqh-omv-v8-2026-07-29a
//                 ^^  ^^^^^^^^^^^
//                 |   date the files were published (+ a letter for a second
//                 |   deploy the same day) — answers "did my phone take the
//                 |   new code?", which a minor number never could
//                 generation of the architecture: moves only on a deep rewrite
//                 backed by an ADR (v7 → v8 = the single-probe model). THIS is
//                 the "major change" signal, and it is visible in the footer on
//                 every family phone.
//
// Rendered as "v8 · 2026-07-29a" in the app footer, the debug page and the
// manual-wake page — all three used to carry their own copy of this parser.
(function(g){
  // Sort key, highest wins. The dated format always outranks the legacy one so
  // the label is right during the ONE update where both caches coexist (a
  // browser keeps the old cache alive for a beat while the new SW activates).
  function parse(n){
    var m=n.match(/-v(\d+)-(\d{4}-\d{2}-\d{2}[a-z]?)$/);
    if(m)return {key:'1'+m[2],label:'v'+m[1]+' · '+m[2]};
    // Legacy `plex-jqh-omv-vX.Y`, minted until 2026-07-29. Kept ONLY so the
    // footer still says something during the transition update; nothing new
    // will ever match it. Zero-padded so v8.9 does not outrank v8.66.
    m=n.match(/-v(\d+)\.(\d+)$/);
    if(m)return {key:'0'+('000'+m[1]).slice(-3)+('000'+m[2]).slice(-3),
                 label:'v'+m[1]+'.'+m[2]};
    return null;
  }
  // names → label string, or null when no cache of ours is present (the callers
  // then leave their placeholder alone rather than printing a wrong version).
  function pick(names){
    var best=null;
    (names||[]).forEach(function(n){
      if(typeof n!=='string'||n.indexOf('plex-jqh-omv')!==0)return;
      var c=parse(n);
      if(c&&(!best||c.key>best.key))best=c;
    });
    return best?best.label:null;
  }
  g.pickCacheLabel=pick;

  // Hand the label to a painter, now AND once the service worker is ready.
  // The second call is not belt-and-braces: on a FIRST visit the SW has not
  // finished installing when this runs, so no cache of ours exists yet and the
  // page would keep its "—" placeholder until the next launch. Found by the
  // render layer of tests/version-footer-e2e.py — the parser layer was green
  // throughout, which is precisely why the render pin exists.
  g.withCacheLabel=function(paint){
    if(!g.caches)return;
    var emit=function(){
      caches.keys().then(function(names){
        var label=pick(names);
        if(label)paint(label);
      }).catch(function(){});
    };
    emit();
    if(g.navigator&&navigator.serviceWorker&&navigator.serviceWorker.ready)
      navigator.serviceWorker.ready.then(emit).catch(function(){});
  };
})(window);
