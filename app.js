var config=null,isOnline=false,wolSent=false,checking=false,checkInterval=null;
var relayReachable=true;
var wolStartTime=0,wolPollTimer=null,wolRetryTimers=[];
// True once setOnline / setOffline has fired this session. Gates the
// orange "Vérification…" card so we don't strobe orange on every 15 s
// tick when the prior state is already on screen.
var hasConfirmedState=false;
// v7.0 — single-fetch model. The PWA now asks the relay one question
// (GET /status → {up, stale, age_s}) instead of running a probe + a
// direct home check in parallel. One cold-radio window instead of two,
// no races, no defensive layers. ADR `2026-05-27-pwa-plex-jqh-omv-
// relay-as-oracle` in the operator's private knowledge-base has the
// full design (alternatives + critères d'acceptance + plan).
// v7.1 (2026-05-27) bumped 3000 → 5000: family test reported a ~3 s
// cold open on Android over 4G, right at the timeout boundary. 5 s
// gives 2 s of headroom for the TCP+TLS handshake on a cold mobile
// radio without changing UX on the warm path (timeout only fires when
// the request really hangs).
var STATUS_FETCH_TIMEOUT_MS=5000;
// Mini-cache for back-to-back reopens (closing then reopening the PWA
// within a minute). Kept short on purpose — beyond a minute the user
// expects a fresh check, and we already learned (v6.0 drop-cache fix)
// that a longer cache lies confidently when the server has flipped
// state in the meantime.
var STATUS_LOCAL_TTL_MS=60000,STATUS_LOCAL_KEY='plex-jqh-omv-status';
// v5.3: 15 s → 5 s. The "Démarrage…" state hung up to 15 s past the
// actual server-up moment because the next poll hadn't fired yet —
// a manual refresh would flip to green immediately. 5 s caps the
// post-up delay.
var WOL_POLL_MS=5000, WOL_TIMEOUT_MS=300000;
// Resend the POST at these offsets after the initial fire (server-side
// already sends 3 packets per POST). 4 POSTs × 3 packets = 15 magic
// packets over 90 s. The 15 s first retry is tuned for the ARP-cache
// timing on the home router: the initial wake often misses because the
// router still has a fresh ARP entry pointing at the now-sleeping NIC
// and unicasts the packet instead of broadcasting. By T+15 s that
// cache has usually started to expire on the affected hop, and a
// retry stands a much better chance of being broadcast through.
var WOL_RETRY_DELAYS_MS=[15000, 30000, 60000, 90000];

// Fallback ETA before any boot history is recorded. Calibrated to the actual
// observed boot time on the author's J5005 OMV (~80 s wall-clock from magic
// packet to first HTTPS response), which the median will converge on after a
// few wakes anyway.
var ETA_FALLBACK_MS=80000;
var BOOT_HISTORY_KEY='plex-jqh-omv-boot-history';
var BOOT_HISTORY_MAX=10;
// Exclude outliers: <10s = false positive (server was already up when we
// fired), >5min = anomaly (network glitch, manual interference). Either
// would skew the median for the rest of the user's sessions.
var BOOT_MIN_MS=10000, BOOT_MAX_MS=300000;

var APP_CATALOG={
  seerr:      {sub:'seerr',      label:'Demander un film / une série', icon:'🎬', cls:'seerr'},
  overseerr:  {sub:'overseerr',  label:'Demander un film / une série', icon:'🎬', cls:'seerr'},
  jellyseerr: {sub:'jellyseerr', label:'Demander un film / une série', icon:'🎬', cls:'seerr'},
  jellyfin:   {sub:'jellyfin',   label:'Regarder sur Jellyfin',        icon:'▶',  cls:'plex'},
  plexweb:    {url:'https://app.plex.tv', label:'Regarder sur Plex',  subText:'app.plex.tv', icon:'▶', cls:'plex'}
};

function loadConfig(){try{var r=localStorage.getItem('plex-jqh-omv-cfg');if(r)return JSON.parse(r)}catch(e){}return null}
function storeConfig(c){try{localStorage.setItem('plex-jqh-omv-cfg',JSON.stringify(c))}catch(e){}}
function cleanMac(m){return m.replace(/[:\-\s]/g,'').toLowerCase()}
function validMac(m){return /^[0-9a-f]{12}$/.test(m)}
function macToColon(m){return m.replace(/(.{2})/g,'$1:').slice(0,-1)}
function validHost(h){return h.length>0&&h.length<255&&/\./.test(h)&&!h.includes('..')&&/^[a-zA-Z0-9][a-zA-Z0-9\-\.]*[a-zA-Z0-9]$/.test(h)}
function validIp(s){return /^(\d{1,3}\.){3}\d{1,3}$/.test(s)}
function cleanRelay(u){return u.replace(/\/+$/,'')}
function validRelay(u){return /^https:\/\/[a-zA-Z0-9.\-]+(:\d+)?(\/.*)?$/.test(u)&&u.length<255}
// ms defaults to 3000 — short ack/validation toasts. Pass 5000 for messages
// that the user needs time to read (success confirmation after a long wait,
// explanatory failures with a "use the manual fallback" call to action).
function showToast(msg,warn,ms){var t=document.getElementById('toast');t.textContent=msg;t.className=warn?'toast warn show':'toast show';setTimeout(function(){t.className='toast'},ms||3000)}

function getBootHistory(){try{var r=localStorage.getItem(BOOT_HISTORY_KEY);if(r){var a=JSON.parse(r);if(Array.isArray(a))return a;}}catch(e){}return [];}
function recordBootTime(ms){
  if(ms<BOOT_MIN_MS||ms>BOOT_MAX_MS)return;
  var h=getBootHistory();
  h.push(ms);
  if(h.length>BOOT_HISTORY_MAX)h=h.slice(-BOOT_HISTORY_MAX);
  try{localStorage.setItem(BOOT_HISTORY_KEY,JSON.stringify(h))}catch(e){}
}
function getEta(){
  var h=getBootHistory();
  if(h.length===0)return ETA_FALLBACK_MS;
  var sorted=h.slice().sort(function(a,b){return a-b;});
  var mid=Math.floor(sorted.length/2);
  return sorted.length%2===0?Math.round((sorted[mid-1]+sorted[mid])/2):sorted[mid];
}

function parseApps(str){
  var keys=(str||'').split(',').map(function(s){return s.trim()}).filter(Boolean);
  return keys.map(function(k){return APP_CATALOG[k]||{sub:k,label:k,icon:'🔗',cls:'cfg'}});
}

function firstSubOf(apps){
  for(var i=0;i<apps.length;i++){if(apps[i].sub)return apps[i].sub;}
  return null;
}

// status target: explicit override > first app subdomain > base host
function statusHost(){
  if(config.status)return config.status;
  var apps=parseApps(config.apps||'seerr,plexweb');
  var sub=firstSubOf(apps);
  return sub?sub+'.'+config.host:config.host;
}

function readUrlParams(){
  var p=new URLSearchParams(window.location.search);
  var host=p.get('host');
  if(!host)return false;
  if(!validHost(host))return false;
  var mac=p.get('mac'),cleaned='';
  if(mac){cleaned=cleanMac(mac);if(!validMac(cleaned))return false;}
  var portNum=parseInt(p.get('port')||'9',10);
  if(isNaN(portNum)||portNum<1||portNum>65535)portNum=9;
  config={host:host,port:String(portNum)};
  if(cleaned)config.mac=cleaned;
  var relay=p.get('relay');if(relay){var cr=cleanRelay(relay);if(validRelay(cr))config.relay=cr;}
  var token=p.get('token');if(token)config.token=token;
  var title=p.get('title');if(title)config.title=title;
  var apps=p.get('apps');if(apps)config.apps=apps;
  var status=p.get('status');if(status&&validHost(status))config.status=status;
  var ip=p.get('ip');if(ip&&validIp(ip))config.ip=ip;
  storeConfig(config);
  return true;
}

function showSettings(){
  document.getElementById('mainScreen').style.display='none';
  document.getElementById('settingsScreen').style.display='flex';
  document.getElementById('cancelBtn').style.display=config?'block':'none';
  if(config){
    document.getElementById('cfgTitle').value=config.title||'';
    document.getElementById('cfgMac').value=config.mac||'';
    document.getElementById('cfgHost').value=config.host||'';
    document.getElementById('cfgPort').value=config.port||'9';
    document.getElementById('cfgIp').value=config.ip||'';
    document.getElementById('cfgRelay').value=config.relay||'';
    document.getElementById('cfgToken').value=config.token||'';
    document.getElementById('cfgApps').value=config.apps||'';
  }
  if(checkInterval)clearInterval(checkInterval);
  setTimeout(function(){document.getElementById('cfgHost').focus();},50);
}

function cancelSettings(){
  if(!config)return;
  startApp();
}

function saveConfig(){
  var title=document.getElementById('cfgTitle').value.trim();
  var mac=document.getElementById('cfgMac').value.trim();
  var host=document.getElementById('cfgHost').value.trim();
  var port=document.getElementById('cfgPort').value.trim()||'9';
  var ip=document.getElementById('cfgIp').value.trim();
  var relay=document.getElementById('cfgRelay').value.trim();
  var token=document.getElementById('cfgToken').value.trim();
  var apps=document.getElementById('cfgApps').value.trim();
  if(!host){showToast('⚠ Domaine requis',true);return}
  if(!validHost(host)){showToast('⚠ Domaine invalide',true);return}
  var cleaned='';
  if(mac){cleaned=cleanMac(mac);if(!validMac(cleaned)){showToast('⚠ MAC invalide (12 caractères hex)',true);return}}
  var portNum=parseInt(port,10);
  if(isNaN(portNum)||portNum<1||portNum>65535){showToast('⚠ Port invalide (1-65535)',true);return}
  var cleanedRelay='';
  if(relay){cleanedRelay=cleanRelay(relay);if(!validRelay(cleanedRelay)){showToast('⚠ Relais invalide (URL HTTPS)',true);return}}
  if(ip&&!validIp(ip)){showToast('⚠ IP invalide (format A.B.C.D)',true);return}
  config={host:host,port:String(portNum)};
  if(cleaned)config.mac=cleaned;
  if(ip)config.ip=ip;
  if(cleanedRelay)config.relay=cleanedRelay;
  if(token)config.token=token;
  if(title)config.title=title;
  if(apps)config.apps=apps;
  storeConfig(config);
  startApp();
}

// Pings the configured relay's /health/deep (falls back to /health on older
// relays that don't expose the deep endpoint). Designed for the settings
// "Tester le relais" button: surfaces reachability + DNS/UDP readiness inline,
// without sending a /wol POST (would wake the server) and without touching
// the configured token (testing it would require POST /wol — same problem).
function testRelay(btn){
  var status=document.getElementById('relayTestStatus');
  var relay=document.getElementById('cfgRelay').value.trim();
  if(!relay){status.className='test-status fail';status.textContent='✕ URL relais vide';return;}
  var cleaned=cleanRelay(relay);
  if(!validRelay(cleaned)){status.className='test-status fail';status.textContent='✕ URL invalide (https://…)';return;}
  status.className='test-status';status.textContent='Test en cours…';
  btn.disabled=true;
  var done=function(cls,txt){btn.disabled=false;status.className='test-status '+cls;status.textContent=txt;};
  var ctrl=new AbortController(),timer=setTimeout(function(){ctrl.abort()},5000);
  fetch(cleaned+'/health/deep',{cache:'no-store',signal:ctrl.signal})
    .then(function(r){
      clearTimeout(timer);
      if(r.ok){
        r.json().then(function(j){
          var c=j.checks||{};
          var ok=Object.keys(c).filter(function(k){return c[k]==='ok'});
          done('ok','✓ Relais OK ('+ok.join(', ')+')');
        }).catch(function(){done('ok','✓ Relais OK');});
      }else if(r.status===503){
        r.json().then(function(j){
          var c=j.checks||{};
          var failed=Object.keys(c).filter(function(k){return c[k]!=='ok'});
          done('warn','⚠ Dégradé : '+(failed.join(', ')||'inconnu'));
        }).catch(function(){done('warn','⚠ Relais dégradé');});
      }else if(r.status===404){
        // Older relay without /health/deep — fall back to /health for compat.
        fetch(cleaned+'/health',{cache:'no-store'}).then(function(r2){
          if(r2.ok)done('ok','✓ Relais OK (legacy /health)');
          else done('fail','✕ Relais répond mais /health KO ('+r2.status+')');
        }).catch(function(){done('fail','✕ Relais injoignable');});
      }else{
        done('fail','✕ HTTP '+r.status);
      }
    })
    .catch(function(){clearTimeout(timer);done('fail','✕ Relais injoignable');});
}

function buildLinks(){
  var container=document.getElementById('linksContainer');
  while(container.firstChild)container.removeChild(container.firstChild);
  parseApps(config.apps||'seerr,plexweb').forEach(function(app){
    var a=document.createElement('a');
    a.className='link-btn';
    a.target='_blank';
    a.rel='noopener';
    a.href=app.url||('https://'+(app.sub?app.sub+'.'+config.host:config.host));
    // Sub-based links live on the user's server; external app.url links don't.
    // Grey out + block clicks on the former when the server is offline.
    if(!app.url){
      a.classList.add('server-dependent');
      if(!isOnline)a.classList.add('offline');
      a.addEventListener('click',function(e){
        if(isOnline)return;
        e.preventDefault();
        // During an active WoL boot the server is in transition, not "off" —
        // the generic "allume-le" message is misleading and frustrating
        // ("but I just did!"). Differentiate the two cases.
        if(wolSent)showToast('⏳ Serveur en cours de réveil — patiente quelques instants',true);
        else showToast('⚠ Serveur éteint — allume-le d\'abord',true);
      });
    }
    var icon=document.createElement('div');
    icon.className='link-icon '+app.cls;
    icon.textContent=app.icon;
    var text=document.createElement('div');
    text.className='link-text';
    text.textContent=app.label;
    var sub=document.createElement('div');
    sub.className='link-sub';
    sub.textContent=app.subText||(app.sub?app.sub+'.'+config.host:config.host);
    text.appendChild(sub);
    a.appendChild(icon);
    a.appendChild(text);
    container.appendChild(a);
  });
  var cfg=document.createElement('div');
  cfg.className='link-btn';
  cfg.addEventListener('click',showSettings);
  var cfgIcon=document.createElement('div');
  cfgIcon.className='link-icon cfg';
  cfgIcon.textContent='⚙';
  var cfgText=document.createElement('div');
  cfgText.className='link-text';
  cfgText.textContent='Paramètres';
  var cfgSub=document.createElement('div');
  cfgSub.className='link-sub';
  cfgSub.textContent='modifier la configuration';
  cfgText.appendChild(cfgSub);
  cfg.appendChild(cfgIcon);
  cfg.appendChild(cfgText);
  container.appendChild(cfg);
}

function wolReady(){return !!(config&&config.mac&&config.relay&&config.token);}

function startApp(){
  document.getElementById('settingsScreen').style.display='none';
  document.getElementById('mainScreen').style.display='flex';
  document.getElementById('appTitle').textContent=config.title||'Plex jqh omv';
  document.getElementById('headerSub').textContent=config.host;
  document.getElementById('powerSection').style.display=wolReady()?'flex':'none';
  document.getElementById('fallbackLink').style.display=config.mac?'block':'none';
  if(config.mac){
    var fbUrl='./fallback.html?mac='+encodeURIComponent(config.mac)+'&host='+encodeURIComponent(config.host)+'&port='+encodeURIComponent(config.port||'9');
    if(config.ip)fbUrl+='&ip='+encodeURIComponent(config.ip);
    document.getElementById('fallbackLinkA').href=fbUrl;
  }
  buildLinks();
  clearWolPoll();
  clearWolRetries();
  isOnline=false;wolSent=false;checking=false;relayReachable=true;hasConfirmedState=false;
  // Paint the localStorage cache (<60 s) immediately so back-to-back
  // reopens don't strobe orange. The background checkStatus() below
  // confirms or corrects.
  var cached=readLocalStatus();
  if(cached){
    relayReachable=cached.relayOk!==false;
    if(cached.up)setOnline();
    else setOffline();
  }
  checkStatus();
  if(checkInterval)clearInterval(checkInterval);
  checkInterval=setInterval(checkStatus,15000);
  // Install hint: Chrome on Android = "menu ⋮ → Ajouter à l'écran d'accueil";
  // Safari on iOS/iPadOS uses the share sheet. iPad on iPadOS 13+ reports
  // as "Macintosh" — detect it via touch points to avoid showing the wrong
  // hint to family members on iPad.
  if(!window.matchMedia('(display-mode:standalone)').matches)setTimeout(function(){
    var ua=navigator.userAgent;
    var isIOS=/iPad|iPhone|iPod/.test(ua)||(/Macintosh/.test(ua)&&navigator.maxTouchPoints>1);
    if(isIOS)document.getElementById('installHintText').textContent='Partage → « Sur l\'écran d\'accueil »';
    document.getElementById('installHint').style.display='block';
  },3000);
}

// v7.0 — relay-as-oracle. One fetch to the relay's /status answers both
// "is the relay reachable?" and "is the home server up?". On relay
// timeout we retry once; if both fail we fall back to a direct no-cors
// fetch against the home so up/down detection survives a GCP outage.
function readLocalStatus(){
  try{
    var raw=localStorage.getItem(STATUS_LOCAL_KEY);if(!raw)return null;
    var d=JSON.parse(raw);
    if(!d||typeof d!=='object'||typeof d.t!=='number')return null;
    if(Date.now()-d.t>STATUS_LOCAL_TTL_MS)return null;
    return d;
  }catch(e){return null;}
}
function writeLocalStatus(up,relayOk){
  try{localStorage.setItem(STATUS_LOCAL_KEY,JSON.stringify({up:!!up,relayOk:relayOk!==false,t:Date.now()}));}catch(e){}
}

function fetchOnce(url,opts){
  var ctrl=new AbortController(),timer=setTimeout(function(){ctrl.abort();},STATUS_FETCH_TIMEOUT_MS);
  var init=Object.assign({cache:'no-store',signal:ctrl.signal},opts||{});
  return fetch(url,init).finally(function(){clearTimeout(timer);});
}

function fetchStatusFromRelay(){
  // Single shape we trust: HTTP 200 with a JSON body that has an "up"
  // boolean. Anything else (5xx, 503 "status target not configured",
  // network error) is treated as a relay failure and triggers the
  // fallback path.
  return fetchOnce(config.relay+'/status').then(function(r){
    if(!r.ok)return Promise.reject(new Error('HTTP '+r.status));
    return r.json();
  }).then(function(j){
    if(!j||typeof j.up!=='boolean')return Promise.reject(new Error('bad shape'));
    return j;
  });
}

function fetchHomeDirectly(){
  // no-cors: response is opaque but a fulfilled promise still tells us
  // the home accepted the TCP/TLS handshake and returned *something*.
  // That's enough to flip the up/down state when the relay is dead.
  return fetchOnce('https://'+statusHost(),{mode:'no-cors'});
}

function checkStatus(){
  if(checking||!config)return;checking=true;
  var label=document.getElementById('statusLabel'),sub=document.getElementById('statusSub'),btn=document.getElementById('refreshBtn');
  btn.classList.add('spinning');
  // Keep the prior visual when we already have a confirmed (or cached)
  // state; orange "Vérification…" only appears on the very first paint.
  if(hasConfirmedState){
    sub.textContent='vérification…';
  }else{
    document.getElementById('statusDot').className='status-dot checking';
    document.getElementById('statusCard').className='status-card';
    label.textContent='Vérification...';sub.textContent='ping en cours';
  }

  var finish=function(){checking=false;btn.classList.remove('spinning');};

  if(!config.relay){
    // No relay configured → straight to direct-home detection. Relay
    // warnings are silenced (no relay = no relay-down state to show).
    relayReachable=true;
    fetchHomeDirectly()
      .then(function(){finish();writeLocalStatus(true,true);setOnline();})
      .catch(function(){finish();writeLocalStatus(false,true);setOffline();});
    return;
  }

  fetchStatusFromRelay()
    .catch(function(){return fetchStatusFromRelay();})   // 1 retry on transient failures
    .then(function(j){
      // Happy path: relay answered, trust its verdict.
      finish();
      relayReachable=true;
      writeLocalStatus(j.up,true);
      if(j.up)setOnline();else setOffline();
    })
    .catch(function(){
      // Relay failed twice → fall back to direct home. Mark the relay
      // as unreachable so the UI surfaces "Réveil indisponible" even
      // when the home itself is up.
      relayReachable=false;
      fetchHomeDirectly()
        .then(function(){finish();writeLocalStatus(true,false);setOnline();})
        .catch(function(){finish();writeLocalStatus(false,false);setOffline();});
    });
}

// Note: relay preconnect lives in preconnect.js (loaded BEFORE app.js).
// Running it from here was a no-op for the very first /status fetch —
// the <link> is added at the same tick as the fetch starts. Moved to
// a static pre-script in v7.1 so the TCP+TLS handshake begins ~100-
// 200 ms ahead of fetch() instead of racing it.

function applyLinksState(){
  var off=!isOnline;
  document.querySelectorAll('.link-btn.server-dependent').forEach(function(el){
    if(off)el.classList.add('offline');else el.classList.remove('offline');
  });
}

// Three-state fallback link reflecting both server and relay reachability.
// Style/wording chosen so the admin sees a relay outage even while the server
// is up — otherwise the issue only surfaces the next time WoL is needed.
function setFallbackState(){
  if(!config||!config.mac)return;
  var link=document.getElementById('fallbackLink');
  var a=document.getElementById('fallbackLinkA');
  link.classList.remove('promoted','warn');
  if(!relayReachable){
    if(isOnline){
      link.classList.add('warn');
      a.textContent='⚠ Relais WoL injoignable — Réveil manuel';
    }else{
      link.classList.add('promoted');
      a.textContent='Réveil ne marche pas ? Réveil manuel';
    }
  }else{
    a.textContent='Réveil ne marche pas ? Réveil manuel';
  }
}

function setOnline(){
  isOnline=true;
  hasConfirmedState=true;
  stopCountdown();
  clearWolPoll();
  clearWolRetries();
  applyLinksState();
  document.getElementById('statusDot').className='status-dot online';
  document.getElementById('statusCard').className='status-card online';
  document.getElementById('statusLabel').textContent='En ligne';
  document.getElementById('statusSub').textContent='serveur accessible';
  if(config.mac){
    document.getElementById('powerBtn').className='power-btn online';
    document.getElementById('powerLabel').textContent='Serveur allumé';
    document.getElementById('powerLabel').className='power-label sent';
    setFallbackState();
  }
  if(wolSent){
    if(wolStartTime){
      recordBootTime(Date.now()-wolStartTime);
      wolStartTime=0;
    }
    showToast('✓ Serveur démarré avec succès',false,5000);
    if(navigator.vibrate)navigator.vibrate([100,50,100]);
    wolSent=false;
  }
}

function setStarting(){
  document.getElementById('statusDot').className='status-dot checking';
  document.getElementById('statusCard').className='status-card';
  document.getElementById('statusLabel').textContent='Démarrage…';
  document.getElementById('statusSub').textContent='réveil en cours';
}

var countdownTimer=null,countdownEndsAt=0,wolEtaMs=0;
function startCountdown(){
  stopCountdown();
  var etaMs=getEta();
  wolEtaMs=etaMs;
  countdownEndsAt=Date.now()+etaMs;
  var pl=document.getElementById('powerLabel');
  var bar=document.getElementById('powerProgressBar');
  var box=document.getElementById('powerProgress');
  box.classList.add('active');
  // Reset to 0 then animate to 100% over etaMs. Force a reflow between the two
  // width assignments so the browser registers the start state before the
  // transition begins — otherwise the second assignment collapses with the
  // first and the bar jumps to 100% with no animation.
  bar.style.transition='none';
  bar.style.width='0%';
  void bar.offsetWidth;
  bar.style.transition='width '+(etaMs/1000)+'s linear';
  bar.style.width='100%';
  // Three labels by elapsed time. Past T=0 we used to leave "presque prêt"
  // displayed for up to 5 min (the WoL_TIMEOUT_MS) which made the family
  // wonder whether the relay was actually doing anything. 30 s past ETA is
  // ~38% above the median boot, which is a fair signal that something is
  // slower than usual — gives the user information without crying wolf.
  var tick=function(){
    var diff=Math.round((countdownEndsAt-Date.now())/1000);
    if(isOnline||!wolSent){stopCountdown();return;}
    if(diff<-30)pl.textContent='Démarrage un peu plus long — patiente encore';
    else if(diff<=0)pl.textContent='Réveil… presque prêt';
    else pl.textContent='Réveil… environ '+diff+'s';
  };
  tick();
  countdownTimer=setInterval(tick,1000);
}
function stopCountdown(){
  if(countdownTimer){clearInterval(countdownTimer);countdownTimer=null;}
  wolEtaMs=0;
  var box=document.getElementById('powerProgress');
  if(box)box.classList.remove('active');
}
function clearWolPoll(){
  if(wolPollTimer){clearInterval(wolPollTimer);wolPollTimer=null;}
}
function clearWolRetries(){
  wolRetryTimers.forEach(function(t){clearTimeout(t)});
  wolRetryTimers=[];
}

// One POST to the relay. `isRetry=true` means a follow-up shot to cover
// transient UDP loss: we don't want a single retry hitting a 503 or a
// network blip to reset the wake state — the original POST may already
// have woken the server, we're just being paranoid. Initial POST keeps
// the strict 401/403/network handling so a misconfigured token surfaces
// immediately instead of waiting for the 5-min timeout.
function postWol(isRetry){
  fetch(config.relay+'/wol',{
    method:'POST',
    cache:'no-store',
    headers:{'Content-Type':'application/json','X-Token':config.token},
    body:JSON.stringify({mac:macToColon(config.mac)})
  }).then(function(r){
    if(r.ok||isRetry)return;
    wolSent=false;wolStartTime=0;stopCountdown();clearWolPoll();clearWolRetries();
    var msg=(r.status===401||r.status===403)?'Authentification refusée par le relais':'Erreur relais HTTP '+r.status;
    if(navigator.vibrate)navigator.vibrate(300);
    showToast('⚠ '+msg+' — utilise le réveil manuel ↓',true,5000);
    setOffline();
  }).catch(function(){
    if(isRetry)return;
    wolSent=false;wolStartTime=0;stopCountdown();clearWolPoll();clearWolRetries();
    // Flip relayReachable manually — a checkStatus() right now would race
    // the WoL POST, and we already know the relay just failed.
    relayReachable=false;
    if(navigator.vibrate)navigator.vibrate(300);
    showToast('⚠ Relais injoignable — utilise le réveil manuel ↓',true,5000);
    setOffline();
  });
}

function setOffline(){
  isOnline=false;
  hasConfirmedState=true;
  applyLinksState();
  // While a WoL request is being processed, keep the "starting" state — a red
  // "offline" card next to the spinning power button is contradictory.
  if(wolSent){setStarting();return;}
  document.getElementById('statusDot').className='status-dot offline';
  document.getElementById('statusCard').className='status-card offline';
  document.getElementById('statusLabel').textContent='Hors ligne';
  document.getElementById('statusSub').textContent=navigator.onLine?'serveur éteint':'pas de réseau';
  if(wolReady()){
    var btn=document.getElementById('powerBtn'),lbl=document.getElementById('powerLabel');
    if(relayReachable){btn.className='power-btn';lbl.textContent='Allumer le serveur';lbl.className='power-label';}
    else{btn.className='power-btn unavailable';lbl.textContent='Réveil indisponible — utilise le réveil manuel ↓';lbl.className='power-label unavailable';}
    setFallbackState();
  }
}

function sendWol(){
  if(isOnline||wolSent||!wolReady())return;
  if(!relayReachable){showToast('⚠ Relais WoL injoignable — utilise le réveil manuel ↓',true,5000);return;}
  if(navigator.vibrate)navigator.vibrate(50);
  wolSent=true;
  wolStartTime=Date.now();
  document.getElementById('powerBtn').className='power-btn sent';
  document.getElementById('powerLabel').className='power-label sent';
  setStarting();
  startCountdown();
  showToast('⚡ Demande de réveil envoyée');
  postWol(false);
  // Retry POSTs to drown out UDP loss AND walk past the router's ARP
  // cache TTL (see WOL_RETRY_DELAYS_MS comment). Server-side also sends
  // 3 packets per POST, so 5 POSTs × 3 = 15 magic packets over 90 s.
  WOL_RETRY_DELAYS_MS.forEach(function(delay){
    wolRetryTimers.push(setTimeout(function(){
      if(!wolSent||isOnline)return;
      postWol(true);
    },delay));
  });
  // Single polling interval instead of 60/120/180 s setTimeouts. Two reasons:
  //  1. Tighter detection window — boots faster than 60 s previously missed
  //     the first check and waited for the 120 s one (visible gap of ~55 s).
  //  2. setInterval survives background freeze on mobile better than 3 staggered
  //     setTimeouts — at resume, the next tick lands quickly without juggling
  //     which of the three timers did or didn't fire.
  wolPollTimer=setInterval(function(){
    if(!wolSent||isOnline){clearWolPoll();return;}
    if(Date.now()-wolStartTime>WOL_TIMEOUT_MS){
      wolSent=false;wolStartTime=0;clearWolPoll();clearWolRetries();stopCountdown();checkStatus();
      if(navigator.vibrate)navigator.vibrate(300);
      // Surface the timeout — silent failure (vibration + flip to red) used to
      // leave family members wondering whether the app was broken. Toast tells
      // them what happened and points to the manual fallback.
      showToast('⚠ Le serveur n\'a pas démarré — réessaie ou utilise le réveil manuel ↓',true,5000);
      setOffline();
      return;
    }
    checkStatus();
  },WOL_POLL_MS);
}

if('serviceWorker' in navigator){
  // Robust update detection — stack multiple triggers because no single one
  // is reliable on Android PWA standalone (focus often doesn't fire on
  // foreground from app switcher, navigation events are rare).
  var forceSwCheck=function(){
    navigator.serviceWorker.getRegistration().then(function(reg){
      if(reg&&reg.update)reg.update();
    }).catch(function(){});
  };
  // updateViaCache:'none' bypasses the HTTP cache when fetching sw.js itself.
  // Without it, the browser may serve a stale sw.js for up to 24h.
  navigator.serviceWorker.register('sw.js',{updateViaCache:'none'}).then(function(reg){
    if(reg&&reg.update)reg.update();
  }).catch(function(){});
  // 1. window focus — Chrome desktop, sometimes Android PWA
  window.addEventListener('focus',forceSwCheck);
  // 2. document visibility — most reliable on Android PWA standalone
  document.addEventListener('visibilitychange',function(){
    if(!document.hidden)forceSwCheck();
  });
  // 3. Periodic safety net every 5 minutes while visible — catches the case
  // where the user keeps the PWA open for hours without any event firing.
  setInterval(function(){if(!document.hidden)forceSwCheck();},5*60*1000);
  // Auto-reload on SW update. Skip the very first install (no prior controller)
  // so a fresh visit isn't reloaded mid-startup.
  var hadController=!!navigator.serviceWorker.controller;
  var refreshing=false;
  navigator.serviceWorker.addEventListener('controllerchange',function(){
    if(refreshing||!hadController)return;
    refreshing=true;
    window.location.reload();
  });
}

// Long-press (2s) on the app title opens a debug snapshot page.
// Discoverable for the admin without adding visible UI for the family.
// Anchored on the always-visible header instead of the bottom footer
// (which may need scrolling on short viewports).
(function(){
  var lpTimer=null;
  var start=function(e){if(e.cancelable)e.preventDefault();lpTimer=setTimeout(function(){window.open('debug.html','_blank','noopener');},2000);};
  var cancel=function(){if(lpTimer){clearTimeout(lpTimer);lpTimer=null;}};
  document.querySelectorAll('.header h1').forEach(function(el){
    el.addEventListener('pointerdown',start);
    el.addEventListener('pointerup',cancel);
    el.addEventListener('pointerleave',cancel);
    el.addEventListener('pointercancel',cancel);
  });
})();

// Pause the 15s status polling while the PWA is hidden (background tab,
// app switcher, screen off). Resume immediately on return with a fresh
// check so the user sees a current state without waiting for the tick.
document.addEventListener('visibilitychange',function(){
  if(!config)return;
  if(document.hidden){
    if(checkInterval){clearInterval(checkInterval);checkInterval=null;}
  } else {
    // A fetch in flight when the screen locked may never resolve (Android
    // suspends network) — its `checking=true` flag would then permanently
    // block subsequent checks. Reset it on resume so the next checkStatus()
    // runs unhindered.
    checking=false;
    // The local cache (<60 s) gives an instant paint on rapid reopens; the
    // background checkStatus() below confirms or corrects within ~1 s.
    var cached=readLocalStatus();
    if(cached){
      relayReachable=cached.relayOk!==false;
      if(cached.up)setOnline();else setOffline();
    } else {
      // Stale cache (> STATUS_LOCAL_TTL_MS in background) — the on-screen
      // state may no longer reflect reality after a long absence. Reset
      // hasConfirmedState so the upcoming checkStatus() repaints the
      // orange "Vérification…" card instead of keeping the stale green/red
      // visible during the re-probe. Without this, the user returning after
      // hours sees the prior state until the fetch resolves (3-10 s),
      // sometimes the opposite of reality.
      hasConfirmedState=false;
    }
    checkStatus();
    if(!checkInterval)checkInterval=setInterval(checkStatus,15000);
    // Countdown text self-corrects from Date.now() on the next tick, but the
    // CSS progress bar transition does NOT — it was started once with a
    // duration of etaMs and is frozen-then-resumed by the suspend, so on
    // unlock the bar fills at the original pace from its frozen position,
    // ending etaMs+(suspend_duration) later than the text countdown. Resync
    // it explicitly: snap to the elapsed-ratio position, then re-arm a fresh
    // transition for the remaining ms.
    if(wolSent&&countdownTimer&&wolStartTime&&wolEtaMs){
      var elapsed=Date.now()-wolStartTime;
      var ratio=Math.min(1,Math.max(0,elapsed/wolEtaMs));
      var remainingMs=Math.max(0,countdownEndsAt-Date.now());
      var bar=document.getElementById('powerProgressBar');
      if(bar){
        bar.style.transition='none';
        bar.style.width=(ratio*100)+'%';
        void bar.offsetWidth;
        if(remainingMs>0){
          bar.style.transition='width '+(remainingMs/1000)+'s linear';
          bar.style.width='100%';
        }
      }
    }
  }
});

// Wire up the 5 button handlers (migrated from inline onclick="..." attributes
// so the CSP can drop 'unsafe-inline' from script-src — see <meta http-equiv
// "Content-Security-Policy"> in index.html).
document.getElementById('testRelayBtn').addEventListener('click',function(){testRelay(this);});
document.getElementById('cancelBtn').addEventListener('click',cancelSettings);
document.getElementById('saveBtn').addEventListener('click',saveConfig);
document.getElementById('refreshBtn').addEventListener('click',checkStatus);
document.getElementById('powerBtn').addEventListener('click',sendWol);

// Derive footer version from the active SW cache name (mirrors debug.js
// pattern — single source of truth in sw.js, no hardcoded version to drift).
if(window.caches){
  caches.keys().then(function(names){
    var ours=names.filter(function(n){return n.indexOf('plex-jqh-omv')===0;});
    var m=ours[0]&&ours[0].match(/-v(\d+\.\d+)$/);
    var el=document.getElementById('footerVersion');
    if(el&&m)el.textContent='v'+m[1];
  }).catch(function(){});
}

// Init: URL params > localStorage > settings screen
if(!readUrlParams())config=loadConfig();
if(config&&config.host)startApp();
else showSettings();
