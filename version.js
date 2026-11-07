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

  // 2026-11-07 — discreet marks next to the version, same rule as pock and
  // dash-pat (Yann's ask: "am I on the latest, was it updated?"):
  //   ✓  latest published: sw.js re-read from the network (no-store) matches
  //      the running cache name. No verdict (offline, CDN behind) → no ✓.
  //   ⟳  a newer version is published and on its way (the SW reloads us).
  //   ✦  this device took a new version less than 24 h ago ("avant : …").
  // Tapping a mark shows its detail under the version (no hover on phones).
  var SEEN_KEY='plex-jqh-omv-version-seen', UPDATED_MS=24*3600*1000;

  // (stored raw, running label, now) → {store, updated}: store = what to write
  // back (null = nothing), updated = {prev, at} when ✦ must show. A first
  // visit has nothing to report.
  function versionState(raw,label,now){
    var s=null;
    if(raw){try{s=JSON.parse(raw);}catch(e){s=null;}}
    if(!s||typeof s!=='object'||!s.label)return {store:{label:label},updated:null};
    if(s.label!==label){
      return {store:{label:label,prev:s.label,at:now},updated:{prev:s.label,at:now}};
    }
    var fresh=s.prev&&typeof s.at==='number'&&now-s.at<UPDATED_MS;
    return {store:null,updated:fresh?{prev:s.prev,at:s.at}:null};
  }
  function ours(n){return typeof n==='string'&&n.indexOf('plex-jqh-omv')===0;}
  // (running cache name, server cache name) → 'latest' | 'newer' | null.
  function latestState(cur,srv){
    var c=ours(cur)&&parse(cur), s=ours(srv)&&parse(srv);
    if(!c||!s)return null;
    if(s.key===c.key)return 'latest';
    return s.key>c.key?'newer':null;
  }
  g.versionState=versionState;
  g.latestState=latestState;

  function currentName(){
    if(!g.caches)return Promise.resolve(null);
    return caches.keys().then(function(names){
      var best=null,bestKey=null;
      names.forEach(function(n){
        var c=ours(n)&&parse(n);
        if(c&&(bestKey===null||c.key>bestKey)){best=n;bestKey=c.key;}
      });
      return best;
    }).catch(function(){return null;});
  }
  // sw.js bypasses the SW cache (see the sw.js fetch handler) — otherwise this
  // would read the RUNNING version back and claim ✓ after a deploy.
  function serverName(){
    return fetch('sw.js',{cache:'no-store'}).then(function(r){return r.ok?r.text():'';})
      .then(function(t){var m=/var CACHE = '([^']+)'/.exec(t);return m?m[1]:null;})
      .catch(function(){return null;});
  }
  function fmt(t,withDay){
    var o={hour:'2-digit',minute:'2-digit'};
    if(withDay){o.day='2-digit';o.month='2-digit';}
    return new Date(t).toLocaleString('fr-FR',o);
  }

  // Paint "v8 · 2026-11-07a ✓ ✦" into `el` and keep the marks fresh.
  g.paintVersion=function(el){
    if(!el)return;
    el.textContent='';
    var text=document.createElement('span');
    text.className='v-label';text.textContent='—';
    var detail=document.createElement('span');
    detail.className='v-detail';detail.hidden=true;
    function mark(id,glyph,cls){
      var b=document.createElement('button');
      b.type='button';b.id=id;b.className='v-mark '+cls;b.textContent=glyph;b.hidden=true;
      b.addEventListener('click',function(){
        var same=!detail.hidden&&detail.getAttribute('data-from')===id;
        detail.hidden=same;detail.setAttribute('data-from',id);detail.textContent=b.title;
      });
      return b;
    }
    var latest=mark('vLatest','✓','v-ok'), newer=mark('vNewer','⟳','v-new'),
        updated=mark('vUpdated','✦','v-new');
    [text,latest,newer,updated,detail].forEach(function(n){el.appendChild(n);});
    function set(b,title){
      b.hidden=!title;b.title=title||'';
      if(title)b.setAttribute('aria-label',title);else b.removeAttribute('aria-label');
      if(detail.getAttribute('data-from')===b.id){
        if(!title)detail.hidden=true;else if(!detail.hidden)detail.textContent=title;
      }
    }
    g.withCacheLabel(function(label){
      text.textContent=label;
      var raw=null;try{raw=localStorage.getItem(SEEN_KEY);}catch(e){}
      var st=versionState(raw,label,Date.now());
      if(st.store){try{localStorage.setItem(SEEN_KEY,JSON.stringify(st.store));}catch(e){}}
      set(updated,st.updated&&('Mis à jour le '+fmt(st.updated.at,true)+' — avant : '+st.updated.prev));
    });
    function check(){
      Promise.all([currentName(),serverName()]).then(function(r){
        var v=latestState(r[0],r[1]);
        set(latest,v==='latest'&&('Dernière version publiée (vérifié à '+fmt(Date.now(),false)+')'));
        set(newer,v==='newer'&&('Version plus récente publiée : '+pick([r[1]])+' — chargement en cours'));
      });
    }
    check();
    if(g.navigator&&navigator.serviceWorker&&navigator.serviceWorker.ready)
      navigator.serviceWorker.ready.then(check).catch(function(){});
    document.addEventListener('visibilitychange',function(){if(!document.hidden)check();});
    setInterval(function(){if(!document.hidden)check();},5*60*1000);
  };
})(window);
