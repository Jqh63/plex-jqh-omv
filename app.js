var config=null,isOnline=false,wolSent=false,checking=false,checkInterval=null;
var relayReachable=true,relayProbing=false,probeFailStreak=0,statusFailStreak=0;
var wolStartTime=0,wolPollTimer=null,wolRetryTimers=[];
// True once setOnline or setOffline has been called this session. Cached
// state from localStorage counts too (paintCachedState → setOnline/setOffline).
// While false: checkStatus paints the orange "Vérification..." card. While
// true: checkStatus keeps the prior visual and only spins the refresh icon
// + updates sub to "vérification…" — avoids the disorienting flash on every
// 15 s tick (v5.1) when we already have a known state. See v5.0 design notes.
var hasConfirmedState=false;
// Adaptive polling timer (v5.0 B, retuned v5.1). Scheduled at +5 s when a
// post-window status fail is deferred by the streak. Bridges the gap to
// the next regular 15 s tick when the streak=1 fail lands just after a
// tick — without losing the cold-radio false-positive protection.
var adaptiveTickTimer=null;
function clearAdaptiveTick(){if(adaptiveTickTimer){clearTimeout(adaptiveTickTimer);adaptiveTickTimer=null;}}
// Last-known-state cache (v5.0 A). Persisted to localStorage on every
// confirmed setOnline / setOffline / probe flip; loaded on startApp() so
// the user sees the prior state immediately instead of an orange
// "Vérification..." card. TTL chosen short enough to avoid showing a stale
// state for too long: 15 min covers the common "open the PWA several
// times during a session" flow but expires before the user is likely
// to trust a cached green that's hours old.
// v5.3: 5 min → 15 min. The user's typical pattern is "open the PWA
// several times within a 30 min window" — extending TTL catches more
// of those re-opens with a fresh cached green/red, avoiding the orange
// "Vérification..." flash. Trade-off: a server that flips state
// between re-opens shows the stale paint for ~24 s (steady-state
// detection bound) before correcting — acceptable.
var STATE_CACHE_KEY='plex-jqh-omv-state', STATE_CACHE_TTL_MS=900000;
function saveState(){
  if(!hasConfirmedState)return;  // never persist unconfirmed state
  try{localStorage.setItem(STATE_CACHE_KEY,JSON.stringify({
    isOnline:isOnline,relayReachable:relayReachable,savedAt:Date.now()
  }))}catch(e){}
}
function loadCachedState(){
  try{
    var raw=localStorage.getItem(STATE_CACHE_KEY);if(!raw)return null;
    var d=JSON.parse(raw);
    if(typeof d!=='object'||d===null)return null;
    if(typeof d.savedAt!=='number'||Date.now()-d.savedAt>STATE_CACHE_TTL_MS)return null;
    return {isOnline:!!d.isOnline,relayReachable:!!d.relayReachable};
  }catch(e){return null;}
}
// Cold-radio resume grace window. On Android PWA resume (and on initial
// load), the first fetch from the foregrounded app often times out while
// the mobile radio is still warming up. Without a defer, the user sees a
// 5-7 s false-positive cycle: green → "Vérification…" → ⚠ "Relais
// injoignable" → red "Hors ligne" → back to green once the radio is up.
// While inResumeWindow(), the first failure of checkStatus is deferred
// (no KO paint, one quick retry scheduled at +5 s) and the first failure
// of probeRelay is deferred (relayReachable is not flipped to false). The
// 6 s deadline covers both handlers' first attempts and lets the natural
// 15 s tick resolve real outages. PR #19 covered checkStatus only; the
// probe was still flipping and painting "⚠ Relais injoignable" before
// the radio was warm.
var resumeUntil=0,resumeRetryTimer=null;
var RESUME_GRACE_MS=6000;
function inResumeWindow(){return resumeUntil>0&&Date.now()<resumeUntil;}
function openResumeWindow(){resumeUntil=Date.now()+RESUME_GRACE_MS;}
function clearResumeRetry(){if(resumeRetryTimer){clearTimeout(resumeRetryTimer);resumeRetryTimer=null;}}
// v5.3: 15 s → 5 s. The "Démarrage…" state hung up to 15 s past the
// actual server-up moment because the next poll hadn't fired yet —
// a manual refresh would flip to green immediately. 5 s caps the
// post-up delay; with STATUS_TIMEOUT=2 s, each poll is a tight ping
// (≤ 2 s response or timeout), so we get 12 polls/min during boot.
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
function cleanRelay(u){return u.replace(/\/+$/,'')}
function validRelay(u){return /^https:\/\/[a-zA-Z0-9.\-]+(:\d+)?(\/.*)?$/.test(u)&&u.length<255}
function showToast(msg,warn){var t=document.getElementById('toast');t.textContent=msg;t.className=warn?'toast warn show':'toast show';setTimeout(function(){t.className='toast'},3000)}

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
  config={host:host,port:String(portNum)};
  if(cleaned)config.mac=cleaned;
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
        if(!isOnline){e.preventDefault();showToast('⚠ Serveur éteint — allume-le d’abord',true);}
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
    document.getElementById('fallbackLinkA').href=fbUrl;
  }
  buildLinks();
  clearWolPoll();
  clearWolRetries();
  clearResumeRetry();
  clearAdaptiveTick();
  isOnline=false;wolSent=false;checking=false;relayReachable=true;probeFailStreak=0;statusFailStreak=0;hasConfirmedState=false;
  openResumeWindow();
  // Paint the last-known state from localStorage if recent — avoids the
  // disorienting orange "Vérification..." flash on cold launch when we
  // already have a known state from a previous session. setOnline /
  // setOffline both flip hasConfirmedState=true, so the subsequent
  // checkStatus() / probeRelay() will run in background-reverify mode
  // (keep visual, just spin the refresh icon + update sub).
  var cached=loadCachedState();
  if(cached){
    isOnline=cached.isOnline;
    relayReachable=cached.relayReachable;
    if(isOnline)setOnline();else setOffline();
  }
  checkStatus();
  probeRelay();
  if(checkInterval)clearInterval(checkInterval);
  checkInterval=setInterval(checkStatus,15000);
  if(!window.matchMedia('(display-mode:standalone)').matches)setTimeout(function(){document.getElementById('installHint').style.display='block'},3000);
}

function checkStatus(){
  if(checking||!config)return;checking=true;
  var dot=document.getElementById('statusDot'),label=document.getElementById('statusLabel'),sub=document.getElementById('statusSub'),card=document.getElementById('statusCard'),btn=document.getElementById('refreshBtn');
  btn.classList.add('spinning');
  // v5.0 A: when we already have a confirmed state (cached from previous
  // session OR confirmed by an earlier check), skip the orange
  // "Vérification..." card flash. Keep the prior dot/card/label visual
  // and just update sub + spin the refresh icon — the user sees "the app
  // is re-verifying" without losing the actual state they care about.
  if(hasConfirmedState){
    sub.textContent='vérification…';
  }else{
    dot.className='status-dot checking';card.className='status-card';label.textContent='Vérification...';sub.textContent='ping en cours';
  }
  // v5.3: 3 s → 2 s timeout. Home server typical RTT is <500 ms on
  // 4G/WG; a 2 s no-answer is essentially "down". Caps the orange
  // "Vérification..." card duration on cold launch without cache.
  // The 2-fail streak still absorbs cold-radio blips in the 0.5–2 s
  // slow-RTT range.
  var ctrl=new AbortController(),timer=setTimeout(function(){ctrl.abort()},2000);
  // no-cors keeps the response opaque (we only care that the server answered),
  // and per Fetch spec is incompatible with redirect:'manual' (returns a
  // network error). The Chrome PNA noise on redirects is cosmetic-only and
  // doesn't break detection — leaving redirect at its default ('follow').
  fetch('https://'+statusHost(),{mode:'no-cors',cache:'no-store',signal:ctrl.signal})
    .then(function(){clearTimeout(timer);btn.classList.remove('spinning');checking=false;statusFailStreak=0;clearResumeRetry();setOnline();})
    .catch(function(){
      clearTimeout(timer);btn.classList.remove('spinning');checking=false;
      if(inResumeWindow()){
        // Defer: keep the "Vérification…" pulsing already on screen, schedule
        // one retry at +5 s. The window stays open until its natural deadline
        // so probeRelay (running in parallel) can also defer its first
        // failure. Window-deferred fails stay out of the streak count.
        clearResumeRetry();
        resumeRetryTimer=setTimeout(checkStatus,5000);
        return;
      }
      statusFailStreak++;
      // 2-fail streak (v4.5): mirror of the probeRelay streak. Require 2
      // consecutive post-window status fails to paint setOffline RED. A
      // single transient failure (cold-radio Android, network blip past
      // the 6 s resume window) is absorbed; real outages are detected on
      // the second consecutive fail at the next adaptive tick.
      if(statusFailStreak<2){
        // Adaptive polling (v5.0 B, retuned v5.1, v5.3): instead of
        // waiting up to 15 s for the next regular tick, schedule a
        // faster follow-up at +5 s. Pinned to the steady-state
        // server-dies-mid-tick bound of ~24 s (15 s next tick + 2 s
        // timeout + 5 s adaptive + 2 s timeout). Cleared on success
        // or on background.
        clearAdaptiveTick();
        adaptiveTickTimer=setTimeout(checkStatus,5000);
        return;
      }
      setOffline();
    });
  probeRelay();
}

// Probe self-hosted relay reachability via GET /health (returns 200 + JSON).
// The relay sets CORS for this origin so a normal fetch can read the status
// code — unlike the previous depicus probe which had to use no-cors and was
// blind to Cloudflare 522s. Here a true non-OK response is detected directly.
// On every relayReachable flip, repaint UI even when the server is online —
// setOnline() reads relayReachable once at call time, so a later probe that
// flips it from false→true while isOnline=true would otherwise leave the
// "⚠ Relais injoignable" banner stuck until the next 30 s status tick.
function probeRelay(){
  if(relayProbing||!wolReady())return;
  relayProbing=true;
  var ctrl=new AbortController(),timer=setTimeout(function(){ctrl.abort()},2500);
  var applyFlip=function(ok){
    // Cold-radio defer (v4.3, see resumeUntil comment): during the resume
    // window the first probe failure is treated as radio-warmup noise.
    // Stays out of the streak count entirely.
    if(!ok&&relayReachable&&inResumeWindow())return;
    if(ok){probeFailStreak=0;}
    else{probeFailStreak++;}
    // 2-fail streak (v4.4): a single post-window probe failure is more
    // often transient (radio still cold past the 6 s window, single packet
    // loss to GCP, micro-burst on the relay) than a real outage. Require 2
    // consecutive fails to flip relayReachable → false. Without this, the
    // server-down + cold-radio scenario shows a false-positive "Réveil
    // indisponible" red paint at T+12 ish (window has closed by then, but
    // the retry's probe is still in cold-radio territory). Trade-off: real
    // relay-down detection delayed by ~30 s (caught at the next tick after
    // the first post-window fail) — acceptable because the WoL click path
    // surfaces relay errors immediately via the postWol() catch toast.
    if(!ok&&relayReachable&&probeFailStreak<2)return;
    var changed=(ok!==relayReachable);
    relayReachable=ok;
    if(!changed)return;
    if(!isOnline&&!wolSent)setOffline();
    else setFallbackState();
    saveState();
  };
  fetch(config.relay+'/health',{cache:'no-store',signal:ctrl.signal})
    .then(function(r){clearTimeout(timer);relayProbing=false;applyFlip(r.ok);})
    .catch(function(){clearTimeout(timer);relayProbing=false;applyFlip(false);});
}

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
  clearAdaptiveTick();
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
    showToast('✓ Serveur démarré avec succès');
    if(navigator.vibrate)navigator.vibrate([100,50,100]);
    wolSent=false;
  }
  saveState();
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
    if(diff<-30)pl.textContent='Réveil… plus long que d\'habitude';
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
    showToast('⚠ '+msg+' — utilise le réveil manuel ↓',true);
    setOffline();
  }).catch(function(){
    if(isRetry)return;
    wolSent=false;wolStartTime=0;stopCountdown();clearWolPoll();clearWolRetries();probeRelay();
    if(navigator.vibrate)navigator.vibrate(300);
    showToast('⚠ Relais injoignable — utilise le réveil manuel ↓',true);
    setOffline();
  });
}

function setOffline(){
  isOnline=false;
  hasConfirmedState=true;
  applyLinksState();
  // While a WoL request is being processed, keep the "starting" state — a red
  // "offline" card next to the spinning power button is contradictory. No
  // saveState() here either: "starting" is a transient state, not a
  // confirmed setOffline we want to persist.
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
  saveState();
}

function sendWol(){
  if(isOnline||wolSent||!wolReady())return;
  if(!relayReachable){showToast('⚠ Relais WoL injoignable — utilise le réveil manuel ↓',true);return;}
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
      wolSent=false;wolStartTime=0;clearWolPoll();clearWolRetries();stopCountdown();probeRelay();
      if(navigator.vibrate)navigator.vibrate(300);
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

// Pause the 30s status polling while the PWA is hidden (background tab,
// app switcher, screen off). Resume immediately on return with a fresh
// check so the user sees a current state without waiting up to 30s.
document.addEventListener('visibilitychange',function(){
  if(!config)return;
  if(document.hidden){
    if(checkInterval){clearInterval(checkInterval);checkInterval=null;}
    // No point keeping the adaptive tick alive while hidden — visibilitychange
    // resume will fire a fresh checkStatus + openResumeWindow anyway.
    clearAdaptiveTick();
  } else {
    // A fetch in flight when the screen locked may never resolve (Android
    // suspends network) — its `checking=true` flag would then permanently
    // block subsequent checks. Reset it on resume so the next checkStatus()
    // runs unhindered.
    checking=false;
    relayProbing=false;
    clearResumeRetry();
    clearAdaptiveTick();
    openResumeWindow();
    // Always force an immediate check on resume, even if checkInterval still
    // exists — wolPollTimer / checkInterval may have been frozen during the
    // background phase and the next scheduled tick could be seconds or
    // minutes away. The user just looked at the screen; give them fresh data.
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
