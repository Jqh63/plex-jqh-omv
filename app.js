var config=null,isOnline=false,wolSent=false,checking=false,checkInterval=null;
var relayReachable=true;
// v8.2 — N-consecutive-miss debounce on the relay-DOWN cosmetic only. A relay
// /status transport failure is most often a slow-but-alive e2-micro (cold
// burstable CPU spanning more than one 15 s tick) or a last-mile blip, NOT a
// dead relay. So a miss keeps relayReachable optimistic (button stays enabled,
// no "Relais injoignable" alarm) and only bumps `relayMissStreak`; the alarm
// hardens only once RELAY_DOWN_MISSES misses land in a row. Any answered/up
// probe resets the streak. This debounces the passive cosmetic ONLY — the
// up/down verdict stays single-probe (v8 core), and a genuine relay-down the
// user actually hits via WoL still surfaces instantly (postWol catch). v8.1
// used a 1-tick debounce (2 misses) — too tight against a cold relay that
// misses across two ticks, which painted a false "relais off" on cold open.
var relayMissStreak=0;
var wolStartTime=0,wolPollTimer=null;
// v8.25 — true while a wake fired from ANOTHER device (or an earlier session of
// ours) is in progress, surfaced by the relay's /status `waking` flag. Distinct
// from wolSent (this session initiated the wake): remoteWaking shows the same
// boot countdown WITHOUT firing our own retry POSTs. Cleared on the next settle
// (setOnline / setOffline) and on startApp.
var remoteWaking=false;
// v8.10 — epoch ms of the last confident-verdict paint (setOnline / setOffline,
// live probe settle or cache pre-paint). Read by the checkStatus() staleness
// guard: a confirmed on-screen verdict older than STATUS_LOCAL_TTL_MS no longer
// suppresses the orange "Vérification…" (see the guard comment in checkStatus).
var lastVerdictAtMs=0;
// Declared here (was an implicit global until v8.16): true once setOnline /
// setOffline has fired this session — full semantics on the comment block
// above startApp()'s cache pre-paint.
var hasConfirmedState=false;

// v8.11 — surface that freshness to the user: a small "vérifié à l'instant /
// il y a Xs" line under the status card, refreshed by the 1 s poll. Makes the
// trust level of the on-screen verdict visible (the stale-green saga taught us
// the verdict's AGE is information the user needs, not just its color).
// v8.29 — coarse buckets on purpose. In nominal green lastVerdictAtMs is rewritten
// every 8 s poll, so a per-second label just oscillated 0→8 s forever (the churn
// the user saw). Buckets keep the line stable at "à l'instant" while the verdict
// is fresh and only speak up once it genuinely ages (device slept through polls).
function fmtAge(ms){
  var s=Math.round(ms/1000);
  if(s<30)return "à l'instant";
  if(s<90)return "il y a moins d'une minute";
  var m=Math.round(s/60);
  if(m<60)return 'il y a '+m+' min';
  return 'il y a +1 h';
}
function updateVerdictAge(){
  var el=document.getElementById('statusAge');
  if(!el)return;
  el.textContent=(hasConfirmedState&&lastVerdictAtMs)?'vérifié '+fmtAge(Date.now()-lastVerdictAtMs):'';
}
// True once setOnline / setOffline has fired this session (a cache pre-paint or
// a live probe settle). Two jobs:
//   1. Gate the orange "Vérification…" card so we don't strobe orange on every
//      self-healing tick when a state is already on screen.
//   2. Drive the open/resume model: a recent (<60 s) cached "up" is REUSED on
//      open/resume — painted as the confident green with the refresh spinner
//      running (= "re-checking"). v8.7: a cached "down" is NOT reused as a
//      confident red (a stale cache must never flash red — see DOWN_CONFIRM
//      below); it shows the orange "Vérification…" until the live probe settles.
//      When nothing recent is cached, no verdict is shown → orange too. A fresh
//      probe then confirms or corrects within ~1 probe.
// The brief cache-vs-reality window the "up" reuse allows is the accepted
// trade-off (the probe + the 8 s self-healing poll correct it fast). v8.6 dropped
// the v8.4/v8.5 `verdictFresh` honesty gate (reuse-the-recent-verdict over
// honest-orange-on-cache); v8.7 keeps the green reuse but makes "down" asymmetric.
// v8.0 — single-probe status model. The whole v4→v7 pile of cold-radio
// defences (retry chains, 2 fail-streaks, all-timeout HOLD, adaptive tick)
// existed for ONE reason: a 5 s status timeout was too tight against a cold
// mobile radio (~3 s to warm) + TLS handshake, so the fetch timed out and the
// code cascaded — up to ~33 s of orange/"reconnexion…" on reopen (the IRL bug:
// "PWA en background, réouverture → check orange 30 s ou plus"). v8 replaces
// all of it with ONE generous-timeout probe and a generation guard:
//   checkStatus() → probe() resolves ONCE to {up, relayReachable}, never rejects.
//   A probeGen counter ignores a stale in-flight probe that resolves AFTER a
//   resume (the Android suspend-mid-fetch race) instead of letting it repaint.
// The generous timeout lets the radio warm INSIDE the first attempt, so there's
// nothing left to retry/hold/streak. Worst case = PROBE + HOME fallback (~13 s)
// and only on a genuine relay+home outage; the common reopen settles in <3 s.
// v8.7: a "down" verdict is no longer painted red on a single probe — see the
// DOWN_CONFIRM block below. A cold-radio first-cycle timeout (relay + home both
// time out, then warm on the re-probe) now shows orange and self-corrects to
// green instead of flashing the transient red v8.0–v8.6 accepted. See the ADR
// (knowledge-base) superseding the 2026-05-27 relay-as-oracle addendum.
var probeGen=0;
// Relay /status fetch budget. Generous on purpose: it must outlast a cold
// mobile-radio TCP+TLS handshake (~3 s observed on Android 4G) so the first
// attempt succeeds rather than timing out into the fallback. The relay's own
// /status is server-side SWR-cached, so the relay never makes us wait on the
// relay→home leg — this budget covers only the PWA→relay last mile.
var PROBE_TIMEOUT_MS=8000;
// Direct-home fallback budget. Only used when the relay /status fetch fails
// (transport failure or answered-but-degraded). One shot, no retry — by the
// time we reach it the radio is warm, so 5 s is ample.
var HOME_FALLBACK_TIMEOUT_MS=5000;
// Consecutive relay /status misses before the (advisory) "Relais injoignable"
// cosmetic hardens — see relayMissStreak above. 3 misses ≈ 2 self-healing
// ticks of patience, enough to ride out a cold e2-micro without crying wolf.
var RELAY_DOWN_MISSES=3;
// v8.7 — asymmetric verdict commit (confirm before red). The up/down verdict is
// no longer committed symmetrically: an "up" paints green instantly (optimistic —
// the relay only says up after a real HEAD < 500, rarely wrong), but a "down" is
// NEVER trusted on a single live verdict. The first "down" paints the orange
// "Vérification…" card and fires ONE fast re-probe (DOWN_RECHECK_MS); red is
// committed only once DOWN_CONFIRM consecutive downs agree. Any "up" in between
// cancels back to green. This kills the transient false red the v8.6 raw verdict
// produced (the user's report: a red that was green a moment later, with no
// orange in between). Two real sources of a transient {up:false}: the relay's
// server-side SWR cache catching a momentary home blip, or a cold mobile radio
// whose relay /status AND direct-home fallback both time out on the first cycle
// then warm on the re-probe. A genuine down still reaches red, ~DOWN_RECHECK_MS
// later — the accepted cost (validated in tests/state-machine-sim.py).
var DOWN_CONFIRM=2,DOWN_RECHECK_MS=2500,downStreak=0,downRecheckTimer=null;
// v8.2 — `checking` watchdog. A check still in flight past this is presumed
// WEDGED: the Android suspend-mid-fetch race can tear down the socket and
// freeze the abort timer with it, so a probe never resolves and never resets
// `checking` — and checkStatus()'s `if(checking)return` then blocks EVERY
// subsequent re-probe forever (the "total KO, statut figé, must kill the app"
// bug). Past this budget, any re-probe trigger (the self-healing tick is the
// guaranteed-eventually one) reclaims the stuck flag and starts fresh; the
// probeGen bump drops the wedged probe if it ever resolves late. Sized at
// PROBE+HOME+slack so a legitimately slow probe (≤13 s) is never preempted.
// Since v8.5 it exceeds STATUS_POLL_INTERVAL_MS (8 s), so a wedge is reclaimed
// on the first self-healing tick whose age clears the watchdog (~2 ticks ≈ 16 s
// worst case) rather than the next single tick — still guaranteed-eventually.
var CHECK_WATCHDOG_MS=PROBE_TIMEOUT_MS+HOME_FALLBACK_TIMEOUT_MS+1000;
var checkStartedAt=0;
// Mini-cache for back-to-back reopens (closing then reopening the PWA
// within a minute). Kept short on purpose — beyond a minute the user
// expects a fresh check, and we already learned (v6.0 drop-cache fix)
// that a longer cache lies confidently when the server has flipped
// state in the meantime.
var STATUS_LOCAL_TTL_MS=60000,STATUS_LOCAL_KEY='plex-jqh-omv-status';
// Self-healing status poll cadence. v8.5: 15 s → 8 s. When the home goes down,
// the relay only learns it on a background SWR refresh (~4.5 s after the first
// /status poll lands on a stale "up"); at the old 15 s cadence the corrected
// verdict was picked up only on the NEXT tick, so a "just stopped the server"
// reopen could stay green ~15 s. 8 s is comfortably past the relay's refresh
// yet roughly halves the worst-case correction window (~7-8 s). It stays above
// the relay's 5 s fresh window, so a healthy poll is still served from the
// relay's server-side cache (cheap). Relay-outage probes are bounded by
// PROBE_TIMEOUT_MS (8 s), not this interval — their cadence is unchanged.
var STATUS_POLL_INTERVAL_MS=8000;
// v5.3: 15 s → 5 s. The "Démarrage…" state hung up to 15 s past the
// actual server-up moment because the next poll hadn't fired yet —
// a manual refresh would flip to green immediately. 5 s caps the
// post-up delay.
var WOL_POLL_MS=5000, WOL_TIMEOUT_MS=300000;
// v8.47 — the wake retry campaign (+15/30/60/90 s bursts, ARP-cache-TTL
// rationale) moved SERVER-SIDE to the relay: local setTimeout retries were
// frozen by Android the moment the phone was pocketed — exactly the nominal
// family gesture — so the retry that matters most (+15 s) rarely fired. The
// relay arms the campaign on our single POST and stops it when the home
// answers; waking no longer depends on the phone's sleep state.

// Fallback ETA before any boot history is recorded. Calibrated to the actual
// observed boot time on the author's J5005 OMV (~80 s wall-clock from magic
// packet to first HTTPS response), which the median will converge on after a
// few wakes anyway.
var ETA_FALLBACK_MS=80000;
// v8.27 — app-warm-up grace after a wake. The status flips green as soon as the
// HOST answers HTTP, but the Docker apps (Seerr, Plex…) can still be starting for
// a minute or two post-boot (the home's documented ~1-3 min post-boot service
// spin-up). So for this long after a wake-driven green, tapping an app shows a
// non-blocking "le serveur vient de démarrer" hint — optimistic (the link still
// opens; it might be ready) rather than blocking. ~90 s covers the common case.
var APP_WARMUP_MS=90000;
var serverReadyHintUntil=0;
// v8.28 — canonical boot ETA served by the relay (`eta_s` in /status), in ms.
// The relay measures the wall-clock from /wol to the next "up" flip and serves
// the median, so EVERY open PWA seeds its wake countdown from the same value —
// the timer is identical across devices instead of each running its own local
// boot-history median (the desync the user saw: one device 80 s fallback, another
// 70 s). Preferred by getEta() when present (and sane); the local boot history
// below stays the offline / no-relay fallback. Adopted on each /status poll and
// persisted (config.eta) so an offline open still seeds a sane countdown.
var relayEtaMs=0;
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
  // `gated`: an external app.url link that should STILL be blocked while the
  // home server is offline. app.plex.tv loads fine on its own, but with the
  // server down it just lands the user on Plex's own "server unavailable"
  // screen — bypassing the PWA's friendly "wake it first" toast. Gating it
  // makes the offline behaviour consistent with the server-hosted links.
  plexweb:    {url:'https://app.plex.tv', label:'Regarder sur Plex',  subText:'app.plex.tv', icon:'▶', cls:'plex', gated:true}
};

// v8.25 — stable opaque per-device id, generated once and persisted. Sent as
// X-Client-Id on /status and /wol so the relay's audit log can distinguish
// devices (which one woke the server, when the PWA is open) WITHOUT any account
// or PII — it's a random UUID, not a secret. crypto.randomUUID needs a secure
// context (GitHub Pages is HTTPS); the fallback covers file:// / old engines.
var CLIENT_ID_KEY='plex-jqh-omv-cid';
function getClientId(){
  try{
    var c=localStorage.getItem(CLIENT_ID_KEY);
    if(c)return c;
    c=(window.crypto&&crypto.randomUUID)?crypto.randomUUID():('cid-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10));
    localStorage.setItem(CLIENT_ID_KEY,c);
    return c;
  }catch(e){return '';}
}
var CLIENT_ID=getClientId();
function loadConfig(){try{var r=localStorage.getItem('plex-jqh-omv-cfg');if(r)return JSON.parse(r)}catch(e){}return null}
function storeConfig(c){try{localStorage.setItem('plex-jqh-omv-cfg',JSON.stringify(c))}catch(e){}}
function cleanMac(m){return m.replace(/[:\-\s]/g,'').toLowerCase()}
function validMac(m){return /^[0-9a-f]{12}$/.test(m)}
function macToColon(m){return m.replace(/(.{2})/g,'$1:').slice(0,-1)}
function validHost(h){return h.length>0&&h.length<255&&/\./.test(h)&&!h.includes('..')&&/^[a-zA-Z0-9][a-zA-Z0-9\-\.]*[a-zA-Z0-9]$/.test(h)}
function validIp(s){return /^(\d{1,3}\.){3}\d{1,3}$/.test(s)}
function cleanRelay(u){return u.replace(/\/+$/,'')}
function validRelay(u){return /^https:\/\/[a-zA-Z0-9.\-]+(:\d+)?(\/.*)?$/.test(u)&&u.length<255}
// v8.11 — scheduled-uptime window. Format "HH:MM-HH:MM" or "HHhMM-HHhMM"
// ("13:50-00:10" / "13h50-00h10"), may wrap past midnight. Purely informative:
// it only rephrases the red card ("Éteint (prévu)" + auto-wake hint vs "Hors ligne")
// so a deliberate nightly shutdown doesn't read like an outage. It never gates
// anything — WoL stays available either way (RTC auto-wake ≠ no manual wake).
function parseWindow(s){
  var m=/^([01]?\d|2[0-3])[h:]([0-5]\d)\s*-\s*([01]?\d|2[0-3])[h:]([0-5]\d)$/.exec((s||'').trim());
  if(!m)return null;
  return {start:(+m[1])*60+(+m[2]),end:(+m[3])*60+(+m[4])};
}
// true/false = now inside/outside the configured window; null = no window set.
function inUptimeWindow(){
  var w=parseWindow(config&&config.window);
  if(!w)return null;
  var d=new Date(),n=d.getHours()*60+d.getMinutes();
  return w.start<=w.end?(n>=w.start&&n<w.end):(n>=w.start||n<w.end);
}
function windowStartLabel(){
  var w=parseWindow(config&&config.window);
  if(!w)return '';
  return ('0'+Math.floor(w.start/60)).slice(-2)+'h'+('0'+(w.start%60)).slice(-2);
}
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
  // Prefer the relay-served canonical ETA (shared across devices) when present
  // and within sane bounds; fall back to the local boot-history median, then the
  // hardcoded fallback. This is what syncs the wake countdown between devices.
  if(relayEtaMs>=BOOT_MIN_MS&&relayEtaMs<=BOOT_MAX_MS)return relayEtaMs;
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
  var win=p.get('window');if(win&&parseWindow(win))config.window=win;
  storeConfig(config);
  // Strip the provisioning params from the address bar once adopted: the URL
  // carries the relay token in clear, and it would otherwise persist in the
  // browser history / share sheet / screenshots. The config now lives in
  // localStorage; preconnect.js already ran at parse time so it saw the param.
  try{history.replaceState(null,'',location.pathname);}catch(e){}
  return true;
}

function showSettings(){
  document.getElementById('mainScreen').style.display='none';
  document.getElementById('settingsScreen').style.display='flex';
  document.getElementById('cancelBtn').style.display=config?'block':'none';
  document.getElementById('backBtn').style.display=config?'flex':'none';
  if(config){
    document.getElementById('cfgTitle').value=config.title||'';
    document.getElementById('cfgMac').value=config.mac||'';
    document.getElementById('cfgHost').value=config.host||'';
    document.getElementById('cfgPort').value=config.port||'9';
    document.getElementById('cfgIp').value=config.ip||'';
    document.getElementById('cfgRelay').value=config.relay||'';
    document.getElementById('cfgToken').value=config.token||'';
    document.getElementById('cfgApps').value=config.apps||'';
    document.getElementById('cfgWindow').value=config.window||'';
    // Relay-owned window: field is display-only (a manual edit would be
    // silently overwritten by the next /status poll — the relay wins).
    var winRelay=!!(config.relay&&config.winSrc==='relay');
    document.getElementById('cfgWindow').disabled=winRelay;
    document.getElementById('cfgWindowHint').textContent=winRelay
      ?'Synchronisée automatiquement depuis le relais (plage d\'extinction du serveur) — non modifiable ici'
      :'Si le serveur s\'éteint volontairement la nuit : hors plage, l\'arrêt s\'affiche « Éteint (prévu) » en bleu avec l\'heure de réveil auto';
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
  var win=document.getElementById('cfgWindow').value.trim();
  // `status` (explicit status-host override) is provisioned via ?status= only —
  // there's no settings field for it. Carry the existing value across a save so
  // editing other fields doesn't silently drop it.
  var prevStatus=(config&&config.status)||'';
  var prevWinSrc=(config&&config.winSrc)||'';
  var prevWindow=(config&&config.window)||'';
  if(!host){showToast('⚠ Domaine requis',true);return}
  if(!validHost(host)){showToast('⚠ Domaine invalide',true);return}
  var cleaned='';
  if(mac){cleaned=cleanMac(mac);if(!validMac(cleaned)){showToast('⚠ MAC invalide (12 caractères hex)',true);return}}
  var portNum=parseInt(port,10);
  if(isNaN(portNum)||portNum<1||portNum>65535){showToast('⚠ Port invalide (1-65535)',true);return}
  var cleanedRelay='';
  if(relay){cleanedRelay=cleanRelay(relay);if(!validRelay(cleanedRelay)){showToast('⚠ Relais invalide (URL HTTPS)',true);return}}
  if(ip&&!validIp(ip)){showToast('⚠ IP invalide (format A.B.C.D)',true);return}
  if(win&&!parseWindow(win)){showToast('⚠ Plage invalide (format 13h50-00h10)',true);return}
  config={host:host,port:String(portNum)};
  if(cleaned)config.mac=cleaned;
  if(ip)config.ip=ip;
  if(cleanedRelay)config.relay=cleanedRelay;
  if(token)config.token=token;
  if(title)config.title=title;
  if(apps)config.apps=apps;
  if(win)config.window=win;
  // Relay-owned window survives a save untouched (its field was disabled);
  // dropping the relay hands the window back to manual editing.
  if(prevWinSrc==='relay'&&cleanedRelay){config.window=prevWindow;config.winSrc='relay';}
  if(prevStatus)config.status=prevStatus;
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
        // fetchOnce (not bare fetch) so this inherits the AbortController +
        // timeout: a half-open relay socket would otherwise never resolve/reject
        // and leave the "Tester le relais" button stuck disabled (v7.8 fix).
        fetchOnce(cleaned+'/health').then(function(r2){
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
    // App links open as a top-level navigation, NOT target="_blank" —
    // server-hosted (Seerr) AND external (app.plex.tv) alike. From an
    // installed PWA, _blank lands in an ephemeral in-app browser context
    // with its own cookie jar, so the target app's login session never
    // persists → relogin on every visit (reported on both Seerr and
    // app.plex.tv, iOS standalone Safari and Android S24). A top-level nav
    // breaks out to the real browser, whose persistent cookie jar keeps the
    // session. Store/help links (fallback.html, Play/App Store) keep _blank
    // in the static HTML — they carry no login session.
    a.href=app.url||('https://'+(app.sub?app.sub+'.'+config.host:config.host));
    // Sub-based links live on the user's server; external app.url links don't.
    // Grey out + block clicks on the former when the server is offline — plus
    // any `gated` external link (e.g. app.plex.tv) whose target is useless
    // until the home server is up. The href stays app.url, so once online the
    // click handler returns early and the link opens normally.
    if(!app.url||app.gated){
      a.classList.add('server-dependent');
      if(!isOnline)a.classList.add('offline');
      a.addEventListener('click',function(e){
        if(isOnline){
          // v8.27 — server up but maybe just woken: the host answers while the
          // apps still spin up. Non-blocking heads-up (the link opens anyway)
          // so a "j'ai cliqué et ça charge dans le vide" right after a wake is
          // explained rather than confusing. Only within the warm-up window.
          if(Date.now()<serverReadyHintUntil)showToast('⏳ Serveur tout juste démarré — l\'app peut mettre quelques secondes',false,4000);
          return;
        }
        e.preventDefault();
        // During an active WoL boot the server is in transition, not "off" —
        // the generic "allume-le" message is misleading and frustrating
        // ("but I just did!"). Differentiate the two cases.
        if(wolSent||remoteWaking)showToast('⏳ Réveil en cours — patiente',true);
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

// v8.18 — Screen Wake Lock during a WoL boot (ADR 2026-06-11, knowledge-base).
// The screen used to auto-lock ~30 s into the ~80 s boot, killing the countdown
// mid-wake. Held only while a wake is in progress; the OS releases it on
// background, onForeground() re-acquires it if the wake is still running.
// Graceful no-op where the API is missing (pre-18.4 Safari).
var wakeLock=null;
function acquireWakeLock(){
  if(!('wakeLock' in navigator)||!wolSent)return;
  navigator.wakeLock.request('screen').then(function(l){
    if(!wolSent){l.release().catch(function(){});return;}
    wakeLock=l;
  }).catch(function(){});
}
function releaseWakeLock(){
  if(wakeLock){wakeLock.release().catch(function(){});wakeLock=null;}
}

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
  releaseWakeLock();
  isOnline=false;wolSent=false;remoteWaking=false;checking=false;checkStartedAt=0;relayReachable=true;relayMissStreak=0;hasConfirmedState=false;
  // v8.28 — restore the persisted relay-served ETA so a wake fired right after an
  // offline open still seeds a shared-value countdown before the first poll lands.
  relayEtaMs=(config&&typeof config.eta==='number'&&config.eta*1000>=BOOT_MIN_MS&&config.eta*1000<=BOOT_MAX_MS)?config.eta*1000:0;
  downStreak=0;if(downRecheckTimer){clearTimeout(downRecheckTimer);downRecheckTimer=null;}
  // Reuse the localStorage cache (<60 s) for an instant paint so back-to-back
  // reopens don't strobe orange. v8.7: only an "up" cache is pre-painted (the
  // confident green) — a cached "down" is NOT pre-painted red (a stale cache must
  // never show a confident red); we leave hasConfirmedState=false so checkStatus()
  // shows the orange "Vérification…" until the live probe settles green or red.
  var cached=readLocalStatus();
  if(cached&&cached.up){
    relayReachable=cached.relayOk!==false;
    setOnline();
  }
  checkStatus();
  if(checkInterval)clearInterval(checkInterval);
  // Self-healing poll (v7.7): the interval is NEVER cleared on background.
  // Its body no-ops while hidden and fires a fresh check on the first tick
  // after the app returns to foreground — so the state corrects within one
  // STATUS_POLL_INTERVAL_MS even if NO focus/visibilitychange event fires on
  // return (v8.5: 8 s, see the constant). This kills the
  // IRL bug where a backgrounded PWA reopened to a frozen green: the old
  // code cleared the interval on hidden and only restarted it from the
  // visibilitychange handler, so when that event didn't fire (Android PWA
  // standalone quirk) nothing ever re-probed. onForeground() below is the
  // fast path; this interval is the guaranteed-eventually safety net.
  checkInterval=setInterval(function(){if(!document.hidden)checkStatus();},STATUS_POLL_INTERVAL_MS);
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

// timeoutMs defaults to PROBE_TIMEOUT_MS (the relay /status budget). The
// direct-home fallback passes HOME_FALLBACK_TIMEOUT_MS explicitly.
function fetchOnce(url,opts,timeoutMs){
  var ctrl=new AbortController(),timer=setTimeout(function(){ctrl.abort();},timeoutMs||PROBE_TIMEOUT_MS);
  var init=Object.assign({cache:'no-store',signal:ctrl.signal},opts||{});
  return fetch(url,init).finally(function(){clearTimeout(timer);});
}

function fetchStatusFromRelay(){
  // Single shape we trust: HTTP 200 with a JSON body that has an "up"
  // boolean. Anything else triggers the fallback path — but we tag *how*
  // it failed so checkStatus() can tell two very different cases apart:
  //   - rejection with .answered=true → the relay returned an HTTP response
  //     (503 "status target not configured", 404 legacy, 5xx, 200-bad-shape).
  //     The relay process is alive and /wol still works; only the status
  //     oracle is degraded. Keep the wake button enabled.
  //   - rejection WITHOUT .answered → transport failure (timeout / network /
  //     DNS): the relay is genuinely unreachable, /wol would fail too.
  // See ADR 2026-05-27 (relay-as-oracle) addendum.
  var answered=function(msg){var e=new Error(msg);e.answered=true;return Promise.reject(e);};
  // v8.17 — /status is token-protected on the relay (same shared token as
  // /wol). Send it when configured; without a token the relay answers 401,
  // which lands on the answered-rejection path → direct-home fallback.
  // v8.25 — always send X-Client-Id (device telemetry); add X-Token when set.
  var headers={'X-Client-Id':CLIENT_ID};
  if(config.token)headers['X-Token']=config.token;
  var opts={headers:headers};
  return fetchOnce(config.relay+'/status',opts).then(function(r){
    if(!r.ok)return answered('HTTP '+r.status);
    return r.json().catch(function(){return answered('bad json');});
  }).then(function(j){
    if(!j||typeof j.up!=='boolean')return answered('bad shape');
    return j;
  });
}

function fetchHomeDirectly(){
  // no-cors: response is opaque but a fulfilled promise still tells us
  // the home accepted the TCP/TLS handshake and returned *something*.
  // That's enough to flip the up/down state when the relay is dead.
  return fetchOnce('https://'+statusHost(),{mode:'no-cors'},HOME_FALLBACK_TIMEOUT_MS);
}

// v8.0 — single-probe status check. One probe, one generous timeout, no
// cascade. The generation guard makes a stale in-flight probe (one that was
// suspended mid-fetch while the PWA was backgrounded and resolves only after
// resume) a no-op, so a fresh resume probe always wins without the old
// retry/hold/streak machinery.
function checkStatus(){
  if(!config)return;
  // v8.2 watchdog (see CHECK_WATCHDOG_MS): don't let a wedged in-flight check —
  // a probe suspended mid-fetch that never resolved, or a check whose resume
  // event never fired — block re-probing forever. If the prior check is older
  // than the watchdog budget, fall through and start a fresh one; the ++probeGen
  // below drops the stale probe if it ever resolves late.
  if(checking&&Date.now()-checkStartedAt<CHECK_WATCHDOG_MS)return;
  checking=true;checkStartedAt=Date.now();
  var gen=++probeGen;
  var label=document.getElementById('statusLabel'),sub=document.getElementById('statusSub'),btn=document.getElementById('refreshBtn');
  btn.classList.add('spinning');
  // v8.10 staleness guard — a confirmed state only earns the "keep the prior
  // visual" treatment while the last SETTLED verdict is fresh (in-memory
  // lastVerdictAtMs, same freshness window as the localStorage cache). A stale
  // verdict means the device likely slept through the poll (IRL bug 2026-06-10:
  // prolonged sleep with no visibilitychange flip → first 8 s tick re-probed
  // under yesterday's confident green while the home was off). Demote to the
  // orange "Vérification…" instead of vouching for a verdict we can no longer
  // trust. In-memory on purpose (not readLocalStatus()): localStorage can be
  // unavailable (private mode) and a settled verdict is written every poll, so
  // the variable is strictly fresher and storage-independent.
  if(hasConfirmedState&&Date.now()-lastVerdictAtMs>STATUS_LOCAL_TTL_MS)hasConfirmedState=false;
  // Keep the prior visual when we already have a confirmed (or cached) state:
  // the card text is left UNTOUCHED and the spinning refresh button is the only
  // in-flight signal. v8.29 — we used to flip the sub to "vérification…" on every
  // 8 s poll, which strobed the subtitle back and forth under a steady green.
  // Orange "Vérification…" only appears when nothing is known yet (cold open).
  // v8.30 — never clobber during an active wake: setStarting() painted the
  // "Démarrage…" card but doesn't set hasConfirmedState, so on a cold-open wake
  // each 5 s WoL poll fell into this branch and strobed "Démarrage…" ⇄
  // "Vérification…". The countdown UI owns the card while wolSent/remoteWaking.
  // v8.31 — outside the uptime window, presume the scheduled shutdown instead of
  // painting orange while the probe runs. Proving a machine is OFF costs a full
  // timeout: the home drops the packets, so the relay pays FIRST+RETRY (~7 s) and
  // only THEN answers "down". During the nightly window that wait was the common
  // case — the user opened the app precisely because the server is off, and stared
  // at "Vérification…" for 7 s before getting the button they came for.
  // Outside the window, "off" is what the schedule says, so we render it at once:
  // the blue "Éteint (prévu)" card + an armed wake button. The probe keeps running
  // underneath and setOnline() corrects to green if the home answers (woken by
  // home-watch's auto-WoL, or by another family member). The wrong-way error is
  // harmless: a magic packet sent to an already-running host is ignored by the NIC
  // — WoL cannot reboot a live machine.
  // This is a PRESUMPTION, not a verdict: hasConfirmedState stays false (no
  // "vérifié il y a…" age is claimed, and the next poll re-enters this branch
  // rather than strobing back to orange). downStreak is left pinned by setOffline()
  // so the first agreeing live "down" commits red without a detour through the
  // orange re-check — it agrees with what is already on screen.
  if(!hasConfirmedState&&!wolSent&&!remoteWaking&&navigator.onLine&&inUptimeWindow()===false){
    setOffline();
    hasConfirmedState=false;lastVerdictAtMs=0;updateVerdictAge();
  }else if(!hasConfirmedState&&!wolSent&&!remoteWaking){
    document.getElementById('statusDot').className='status-dot checking';
    document.getElementById('statusCard').className='status-card';
    label.textContent='Vérification...';sub.textContent='ping en cours';
    setButtonChecking();
  }
  probe().then(function(res){
    // A newer probe (e.g. a resume re-probe) superseded this one — drop the
    // stale verdict without touching `checking`, which the newer probe owns.
    if(gen!==probeGen)return;
    checking=false;btn.classList.remove('spinning');
    // v8.12 — adopt the relay-served uptime window (UPTIME_WINDOW env on the
    // relay). The relay value wins over a locally-set one: it's the
    // admin-controlled source of truth, so changing it on the relay updates
    // every installed client on its next poll — no re-provisioning URL to
    // resend. Persisted so it survives offline opens and relay outages.
    // winSrc='relay' marks the value as relay-owned: the settings field then
    // renders read-only (editing it would be a lie — the next poll overwrites).
    // Cleared implicitly when the user removes the relay (manual editing back).
    if(res.window&&parseWindow(res.window)&&(config.window!==res.window||config.winSrc!=='relay')){
      config.window=res.window;config.winSrc='relay';storeConfig(config);
    }
    // v8.28 — adopt the relay's canonical boot ETA (see relayEtaMs). Bounded like
    // the local history; persisted so an offline open seeds the same countdown.
    if(res.etaS>0&&res.etaS*1000>=BOOT_MIN_MS&&res.etaS*1000<=BOOT_MAX_MS){
      relayEtaMs=res.etaS*1000;
      if(config.eta!==res.etaS){config.eta=res.etaS;storeConfig(config);}
    }
    // N-consecutive-miss debounce on relay reachability (see relayMissStreak
    // comment): a miss stays optimistic until RELAY_DOWN_MISSES in a row; any
    // answered/up probe resets the streak. The home up/down verdict (res.up) is
    // used raw — never debounced.
    if(res.relayReachable){
      relayReachable=true;relayMissStreak=0;
    }else{
      relayMissStreak++;
      relayReachable=!(relayMissStreak>=RELAY_DOWN_MISSES||!relayReachable);
    }
    // v8.7 asymmetric verdict commit. UP commits green instantly and resets the
    // down streak. DOWN is held: the first live "down" paints orange and fires
    // ONE fast re-probe; red is committed only once DOWN_CONFIRM consecutive
    // downs agree. An already-confirmed red (streak ≥ DOWN_CONFIRM) re-commits
    // red without flickering back to orange. The cache is written only on a
    // settled verdict so an unconfirmed down never persists a premature "down".
    if(res.up){
      // degraded = host awake, reverse proxy serving, but the probed app
      // (Seerr) returned 5xx. Stay green — no pointless WoL on an awake box —
      // and arm the same warm-up hint the post-wake grace uses, so tapping an
      // app link warns "still starting" instead of silently landing on a 502.
      if(res.degraded)serverReadyHintUntil=Date.now()+APP_WARMUP_MS;
      writeLocalStatus(true,relayReachable);
      setOnline(res.degraded);
    }else if(res.waking&&!wolSent){
      // v8.25 — a wake fired elsewhere (another device, or an earlier session of
      // ours) is in progress per the relay. Show the boot countdown without
      // firing our own POSTs; the normal poll flips to green when the home
      // answers, or to the down path once the relay's waking signal expires.
      // Takes priority over the down-confirmation: waking is a confident
      // "it's coming up" signal, so don't paint red underneath it.
      enterRemoteWaking(res.wakeAgeS);
    }else if(res.declared||++downStreak>=DOWN_CONFIRM){
      // v8.48 — a heartbeat-sourced "down" is the home's own last words (clean
      // shutdown last-gasp), not a flaky probe: commit red at once instead of
      // the orange re-confirmation detour. Covers "extinction avec app ouverte"
      // — the card flips to Éteint on the next poll, no Vérification… dance.
      downStreak=DOWN_CONFIRM;
      writeLocalStatus(false,relayReachable);
      setOffline();
    }else{
      setRechecking();
      if(downRecheckTimer)clearTimeout(downRecheckTimer);
      downRecheckTimer=setTimeout(function(){downRecheckTimer=null;checkStatus();},DOWN_RECHECK_MS);
    }
  });
}

// v8.7 — orange "Vérification…" shown while a "down" verdict is being
// re-confirmed (DOWN_CONFIRM). Distinct from the cold-open orange in
// checkStatus(): here we already had a verdict (often a confident green) but a
// single "down" is not trusted yet.
function setRechecking(){
  document.getElementById('refreshBtn').classList.add('spinning');
  // During an active WoL wake, keep the "Démarrage…" state — a re-check card
  // would contradict the wake-in-progress UI (mirrors setOffline's wolSent guard).
  if(wolSent){setStarting();return;}
  document.getElementById('statusDot').className='status-dot checking';
  document.getElementById('statusCard').className='status-card';
  document.getElementById('statusLabel').textContent='Vérification...';
  document.getElementById('statusSub').textContent='nouvelle tentative…';
  setButtonChecking();
}

// v8.7 follow-up (user feedback 2026-06-07) — the power button must not keep a
// stale confident green while the card is showing an orange check. Paint a
// neutral "Vérification…" button whenever the card is orange (cold-open check or
// a down being re-confirmed). NOT during a WoL wake — the button owns the
// "Démarrage…" / progress UI then — nor without a configured MAC (no wake to offer).
function setButtonChecking(){
  if(!config||!config.mac||wolSent)return;
  var pBtn=document.getElementById('powerBtn'),pLbl=document.getElementById('powerLabel');
  pBtn.className='power-btn checking';
  pLbl.textContent='Vérification…';pLbl.className='power-label checking';
}

// Resolves EXACTLY ONCE to {up, relayReachable}; never rejects. One relay
// /status fetch, and on its failure exactly one direct-home fallback:
//   - relay answers 200 {up}            → trust it (relay reachable).
//   - relay *answers* but degraded      → relay alive, oracle off: fall back to
//     (503 STATUS_TARGET_URL unset, 404)  direct-home for up/down, keep WoL on.
//   - relay *transport*-fails (timeout) → relay unreachable: fall back, mark it
//                                         down (→ "Réveil indisponible").
// No retry, no hold, no streak — the generous PROBE_TIMEOUT_MS absorbs the
// cold-radio handshake that the old cascade was built to paper over.
function probe(){
  if(!config.relay){
    // No relay configured → direct-home only; no relay-down state to show.
    return fetchHomeDirectly().then(
      function(){return {up:true,relayReachable:true};},
      function(){return {up:false,relayReachable:true};}
    );
  }
  return fetchStatusFromRelay().then(
    // v8.12 — pass the relay-served uptime window through (see the adoption
    // logic in checkStatus): the relay is the admin-controlled config channel.
    // v8.25 — thread the relay's wake-in-progress signal through (see the
    // remoteWaking branch in checkStatus): `waking` true while a /wol fired
    // recently and the home is still down, `wake_age_s` its age for the ETA.
    function(j){return {up:j.up,relayReachable:true,window:(typeof j.window==='string'?j.window:null),waking:j.waking===true,wakeAgeS:(typeof j.wake_age_s==='number'?j.wake_age_s:0),etaS:(typeof j.eta_s==='number'?j.eta_s:0),degraded:j.degraded===true,declared:j.source==='heartbeat'};},
    function(err){
      var relayUp=!!(err&&err.answered);
      return fetchHomeDirectly().then(
        function(){return {up:true,relayReachable:relayUp};},
        function(){return {up:false,relayReachable:relayUp};}
      );
    }
  );
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
      a.textContent='⚠ Réveil manuel';
    }else{
      link.classList.add('promoted');
      a.textContent='Réveil manuel';
    }
  }else{
    a.textContent='Réveil manuel';
  }
}

function setOnline(degraded){
  // v8.27 — capture whether we got here off the back of a wake (local or remote)
  // BEFORE the flags are cleared below: if so, arm the app-warm-up grace so a tap
  // in the next APP_WARMUP_MS gets the "apps still starting" heads-up.
  if(wolSent||remoteWaking)serverReadyHintUntil=Date.now()+APP_WARMUP_MS;
  isOnline=true;
  remoteWaking=false;
  hasConfirmedState=true;
  lastVerdictAtMs=Date.now();
  // v8.7 — green cancels any in-progress down-confirmation (streak + pending
  // re-probe), whether this fires from a live probe or a cache pre-paint.
  downStreak=0;if(downRecheckTimer){clearTimeout(downRecheckTimer);downRecheckTimer=null;}
  stopCountdown();
  clearWolPoll();
  // v8.20 — wake-lock release is deferred to the end of this function: after a
  // successful wake we keep the screen on a few more seconds so the green card
  // + success toast are actually seen before the screen may re-lock.
  applyLinksState();
  // Confident green. setOnline fires either from a cache pre-paint (open/resume
  // with a <60 s verdict — reused, with the refresh spinner already running from
  // checkStatus to signal the in-flight re-check) or from a live probe settle.
  // Both are treated as "up"; a contradicting probe corrects to red within ~1
  // probe (see hasConfirmedState note).
  document.getElementById('statusDot').className='status-dot online';
  document.getElementById('statusCard').className='status-card online';
  document.getElementById('statusLabel').textContent='En ligne';
  // v8.48 — surface the relay's `degraded` on the card itself: host up but the
  // apps (Seerr…) still starting. Green stays (no pointless wake) but the sub
  // says WHY a tapped app may spin — the toast hint alone was invisible until
  // the user actually tapped a link. Self-corrects: the next non-degraded poll
  // repaints the normal sub.
  document.getElementById('statusSub').textContent=degraded?'services en cours de démarrage…':'serveur accessible';
  updateVerdictAge();
  if(config.mac){
    var pBtn=document.getElementById('powerBtn'),pLbl=document.getElementById('powerLabel');
    pBtn.className='power-btn online';
    pLbl.textContent='Serveur allumé';pLbl.className='power-label sent';
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
    setTimeout(releaseWakeLock,10000);
  }else{
    releaseWakeLock();
  }
}

function setStarting(){
  document.getElementById('statusDot').className='status-dot checking';
  document.getElementById('statusCard').className='status-card';
  document.getElementById('statusLabel').textContent='Démarrage…';
  document.getElementById('statusSub').textContent='réveil en cours';
}

// v8.25 — render a wake THIS device didn't initiate (relay `waking`). Mirror the
// local-wake "Démarrage…" view + boot countdown, but never touch the retry-POST
// machinery (we didn't fire). Idempotent across polls: while waking persists each
// poll re-enters here, but the countdown is only (re)armed when none is running.
// The user can still tap the power button (sendWol re-fires harmlessly — extra
// magic packets, idempotent). Cleared on the green/red settle.
function enterRemoteWaking(wakeAgeS){
  hasConfirmedState=true;lastVerdictAtMs=Date.now();
  downStreak=0;if(downRecheckTimer){clearTimeout(downRecheckTimer);downRecheckTimer=null;}
  remoteWaking=true;
  setStarting();
  if(config.mac){
    var pBtn=document.getElementById('powerBtn'),pLbl=document.getElementById('powerLabel');
    pBtn.className='power-btn sent';pLbl.className='power-label sent';
  }
  if(!countdownTimer){
    var elapsedMs=(wakeAgeS>0?wakeAgeS:0)*1000;
    wolStartTime=Date.now()-elapsedMs;
    startCountdown(elapsedMs);
  }
}

var countdownTimer=null,countdownEndsAt=0,wolEtaMs=0;
// elapsedMs (default 0) = how far into the boot we already are. 0 for a fresh
// local wake; >0 when adopting an in-progress remote wake (relay `wake_age_s`),
// so the countdown + progress bar start from the right position instead of 0.
function startCountdown(elapsedMs){
  stopCountdown();
  var etaMs=getEta();
  wolEtaMs=etaMs;
  elapsedMs=Math.min(Math.max(elapsedMs||0,0),etaMs);
  countdownEndsAt=Date.now()+(etaMs-elapsedMs);
  var pl=document.getElementById('powerLabel');
  var bar=document.getElementById('powerProgressBar');
  var box=document.getElementById('powerProgress');
  box.classList.add('active');
  // Snap to the already-elapsed ratio then animate the remaining time. Force a
  // reflow between the two width assignments so the browser registers the start
  // state before the transition begins — otherwise the second assignment
  // collapses with the first and the bar jumps with no animation.
  bar.style.transition='none';
  bar.style.width=(etaMs?elapsedMs/etaMs*100:0)+'%';
  void bar.offsetWidth;
  bar.style.transition='width '+((etaMs-elapsedMs)/1000)+'s linear';
  bar.style.width='100%';
  // Three labels by elapsed time. Past T=0 we used to leave "presque prêt"
  // displayed for up to 5 min (the WoL_TIMEOUT_MS) which made the family
  // wonder whether the relay was actually doing anything. 30 s past ETA is
  // ~38% above the median boot, which is a fair signal that something is
  // slower than usual — gives the user information without crying wolf.
  var tick=function(){
    var diff=Math.round((countdownEndsAt-Date.now())/1000);
    if(isOnline||(!wolSent&&!remoteWaking)){stopCountdown();return;}
    var txt;
    if(diff<-30)txt='Démarrage long…';
    else if(diff<=0)txt='Réveil… presque prêt';
    else txt='Réveil… environ '+diff+'s';
    pl.textContent=txt;
    // Status-only devices (no mac/relay/token) have the whole power section
    // hidden, so the countdown above is invisible to them — a remote wake read
    // as a bare "réveil en cours" with no ETA (seen 2026-07-13). Mirror the
    // ticking label into the status-card subtitle for those devices.
    if(document.getElementById('powerSection').style.display==='none')
      document.getElementById('statusSub').textContent='réveil en cours · '+txt.replace('Réveil… ','').toLowerCase();
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
// One POST to the relay — the relay runs the retry campaign server-side
// (v8.47). Strict 401/403/network handling so a misconfigured token
// surfaces immediately instead of waiting for the 5-min timeout.
function postWol(){
  fetch(config.relay+'/wol',{
    method:'POST',
    cache:'no-store',
    headers:{'Content-Type':'application/json','X-Token':config.token,'X-Client-Id':CLIENT_ID},
    body:JSON.stringify({mac:macToColon(config.mac)})
  }).then(function(r){
    if(r.ok)return;
    wolSent=false;wolStartTime=0;stopCountdown();clearWolPoll();releaseWakeLock();
    var msg=(r.status===401||r.status===403)?'Relais : accès refusé':'Erreur relais HTTP '+r.status;
    if(navigator.vibrate)navigator.vibrate(300);
    showToast('⚠ '+msg+' — réveil manuel ↓',true,5000);
    setOffline();
  }).catch(function(){
    wolSent=false;wolStartTime=0;stopCountdown();clearWolPoll();releaseWakeLock();
    // Flip relayReachable manually — a checkStatus() right now would race
    // the WoL POST, and we already know the relay just failed. This is a
    // CONFIRMED failure (the user actually tried to wake), so bypass the
    // miss-streak debounce and surface it immediately; pin the streak at the
    // confirmed-down ceiling so a following miss keeps it down.
    relayReachable=false;relayMissStreak=RELAY_DOWN_MISSES;
    if(navigator.vibrate)navigator.vibrate(300);
    showToast('⚠ Relais injoignable — réveil manuel ↓',true,5000);
    setOffline();
  });
}

function setOffline(){
  isOnline=false;
  remoteWaking=false;
  hasConfirmedState=true;
  lastVerdictAtMs=Date.now();
  // v8.7 — reaching setOffline means red is committed (either DOWN_CONFIRM live
  // downs agreed, or a confirmed user-triggered WoL failure). Pin the streak at
  // the ceiling so a following status "down" keeps red sticky instead of
  // flickering back through the orange re-check; setOnline() resets it to 0.
  downStreak=DOWN_CONFIRM;if(downRecheckTimer){clearTimeout(downRecheckTimer);downRecheckTimer=null;}
  applyLinksState();
  // While a WoL request is being processed, keep the "starting" state — a red
  // "offline" card next to the spinning power button is contradictory.
  if(wolSent){setStarting();return;}
  // v8.25 — past the wolSent guard a real red is being committed (wolSent is
  // false here). Stop any countdown left running by an expired remote wake so
  // the progress bar clears with the red paint rather than on the next tick.
  // Safe: it cannot kill a local-wake countdown (that path returns above).
  stopCountdown();
  // v8.11 — window-aware red. Outside the configured uptime window a red is
  // the EXPECTED nightly shutdown: say so ("Éteint (prévu)" + the auto-wake time)
  // instead of the alarming "Hors ligne", so the family doesn't read a
  // deliberate sleep as an outage. Inside the window (or no window set) the
  // plain "Hors ligne" stands — there, red IS the anomaly signal.
  // v8.12 — the expected sleep also gets its own calm blue card/dot style
  // instead of the alarming outage red.
  var inWin=inUptimeWindow();
  var sleeping=navigator.onLine&&inWin===false;
  document.getElementById('statusDot').className='status-dot '+(sleeping?'sleep':'offline');
  document.getElementById('statusCard').className='status-card '+(sleeping?'sleep':'offline');
  if(!navigator.onLine){
    document.getElementById('statusLabel').textContent='Hors ligne';
    document.getElementById('statusSub').textContent='pas de réseau';
  }else if(inWin===false){
    // v8.15 — "En veille" implied a suspend; the box actually powers OFF
    // (autoshutdown + RTC wake). "Éteint (prévu)" matches reality while the
    // blue card + auto-wake time keep the calm "this is expected" framing.
    document.getElementById('statusLabel').textContent='Éteint (prévu)';
    // v8.13 — short copy: the power button sits right below, the "ou
    // allume-le ↓" hint wrapped on narrow phones (S24) for no added info.
    document.getElementById('statusSub').textContent='réveil auto à '+windowStartLabel();
  }else{
    document.getElementById('statusLabel').textContent='Hors ligne';
    // v8.14 — single short copy for both branches: the red card already
    // signals the anomaly; any longer string collides with the refresh
    // button on narrow phones once Android font scaling kicks in (S24).
    document.getElementById('statusSub').textContent='serveur éteint';
  }
  updateVerdictAge();
  if(wolReady()){
    var btn=document.getElementById('powerBtn'),lbl=document.getElementById('powerLabel');
    if(relayReachable){btn.className='power-btn';lbl.textContent='Allumer le serveur';lbl.className='power-label';}
    else{btn.className='power-btn unavailable';lbl.textContent='Réveil indisponible';lbl.className='power-label unavailable';}
    setFallbackState();
  }
}

function sendWol(){
  if(isOnline||wolSent||!wolReady())return;
  if(!relayReachable){showToast('⚠ Relais injoignable — réveil manuel ↓',true,5000);return;}
  if(navigator.vibrate)navigator.vibrate(50);
  wolSent=true;
  wolStartTime=Date.now();
  acquireWakeLock();
  document.getElementById('powerBtn').className='power-btn sent';
  document.getElementById('powerLabel').className='power-label sent';
  setStarting();
  startCountdown();
  showToast('⚡ Demande de réveil envoyée');
  postWol();
  // No local retry POSTs (v8.47): the relay's server-side campaign re-sends
  // the packets at +15/30/60/90 s and stops when the home answers — immune
  // to Android freezing this page.
  // Single polling interval instead of 60/120/180 s setTimeouts. Two reasons:
  //  1. Tighter detection window — boots faster than 60 s previously missed
  //     the first check and waited for the 120 s one (visible gap of ~55 s).
  //  2. setInterval survives background freeze on mobile better than 3 staggered
  //     setTimeouts — at resume, the next tick lands quickly without juggling
  //     which of the three timers did or didn't fire.
  wolPollTimer=setInterval(function(){
    if(!wolSent||isOnline){clearWolPoll();return;}
    if(Date.now()-wolStartTime>WOL_TIMEOUT_MS){
      wolSent=false;wolStartTime=0;clearWolPoll();stopCountdown();releaseWakeLock();checkStatus();
      if(navigator.vibrate)navigator.vibrate(300);
      // Surface the timeout — silent failure (vibration + flip to red) used to
      // leave family members wondering whether the app was broken. Toast tells
      // them what happened and points to the manual fallback.
      showToast('⚠ Pas démarré — réessaie ou réveil manuel ↓',true,5000);
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
  // Navigate top-level instead of window.open: from a setTimeout the popup
  // has lost user activation, so iOS Safari blocks window.open (worked on
  // Android, not Apple). location.href is a same-origin navigation — no
  // popup, no activation requirement, fires reliably from the timer.
  var start=function(e){if(e.cancelable)e.preventDefault();lpTimer=setTimeout(function(){window.location.href='debug.html';},2000);};
  var cancel=function(){if(lpTimer){clearTimeout(lpTimer);lpTimer=null;}};
  document.querySelectorAll('.header h1').forEach(function(el){
    el.addEventListener('pointerdown',start);
    el.addEventListener('pointerup',cancel);
    el.addEventListener('pointerleave',cancel);
    el.addEventListener('pointercancel',cancel);
  });
})();

// Foreground re-probe (v7.7) — bound to BOTH `focus` and `visibilitychange`.
// On Android PWA standalone neither event alone is reliable on return from
// the app switcher: focus often doesn't fire, and visibilitychange usually
// does — but NOT always (the IRL bug this covers: a backgrounded PWA brought
// back to foreground stayed on a frozen green because visibilitychange never
// fired, and only a second app-switch finally triggered the re-probe). Same
// layered-defence reasoning as the service-worker update triggers above.
// The self-healing 15 s interval (see startApp) is the guaranteed-eventually
// safety net; this handler is the fast path that re-probes immediately.
var lastForegroundMs=0;
function onForeground(){
  if(!config||document.hidden)return;
  // Dedupe focus + visibilitychange both firing on a single foreground
  // (common on desktop) so we don't double-probe / double-resync.
  if(Date.now()-lastForegroundMs<1000)return;
  lastForegroundMs=Date.now();
  // v8.45 — reap a wake that went stale while the page was frozen.
  //
  // Android does not KILL a backgrounded PWA, it FREEZES it: reopening RESUMES the
  // page, it does not reload it, so startApp() never re-runs and the wake state
  // survives. The user's actual sequence (2026-07-14): the AM5's logon task wakes the
  // home and POSTs /wol to the relay on purpose, so every PWA shows the wake (runbook
  // wol-am5-windows-task). He keeps the PWA open to watch the countdown, pockets the
  // phone MID-BOOT, and the page freezes with remoteWaking=true and the bar running.
  // Reopened the NEXT MORNING, that countdown is still ticking ("Réveil… environ 62s")
  // on a home that is off, until two probes finally settle it — ~10 s on a cold radio.
  //
  // It must cover remoteWaking, NOT just wolSent: the phone never tapped anything —
  // the wake was ADOPTED from the relay. wolStartTime is the right anchor for both
  // (enterRemoteWaking sets it too). A wake younger than WOL_TIMEOUT_MS is left alone:
  // it may still be genuinely in flight (the user is just peeking mid-boot).
  if((wolSent||remoteWaking)&&Date.now()-wolStartTime>WOL_TIMEOUT_MS){
    wolSent=false;remoteWaking=false;wolStartTime=0;
    stopCountdown();clearWolPoll();releaseWakeLock();
  }
  // A fetch in flight when the screen locked may never resolve (Android
  // suspends network) — its `checking=true` flag would then permanently
  // block subsequent checks. Reset it on resume so the next checkStatus()
  // runs unhindered. Bumping probeGen here (belt-and-braces with the bump
  // inside checkStatus) guarantees that if that suspended probe DOES resolve
  // late, its verdict is dropped instead of repainting a stale state over the
  // fresh resume probe.
  checking=false;probeGen++;
  // v8.7 — a stale down episode from before the suspend must not count toward the
  // confirmation streak on resume; reset it (and any pending re-probe).
  downStreak=0;if(downRecheckTimer){clearTimeout(downRecheckTimer);downRecheckTimer=null;}
  // Reuse the local cache (<60 s) for an instant paint on rapid reopens. v8.7:
  // only an "up" cache is pre-painted (the confident green, refresh spinner
  // signalling the re-check); a cached "down" is NOT pre-painted red — it falls
  // through to the orange "Vérification…" like a stale/empty cache. The
  // background checkStatus() below confirms or corrects within ~1 probe.
  var cached=readLocalStatus();
  if(cached&&cached.up){
    relayReachable=cached.relayOk!==false;
    setOnline();
  } else {
    // No cache, stale cache (> STATUS_LOCAL_TTL_MS in background), OR a cached
    // "down" — the on-screen state may no longer reflect reality. Reset
    // hasConfirmedState so the upcoming checkStatus() repaints the orange
    // "Vérification…" card instead of keeping a stale verdict (or flashing a
    // cached red) visible during the re-probe.
    hasConfirmedState=false;
  }
  checkStatus();
  // v8.18 — the OS released the wake lock on background; re-hold it if the
  // wake is still in progress.
  if(wolSent)acquireWakeLock();
  // Countdown text self-corrects from Date.now() on the next tick, but the
  // CSS progress bar transition does NOT — it was started once with a
  // duration of etaMs and is frozen-then-resumed by the suspend, so on
  // unlock the bar fills at the original pace from its frozen position,
  // ending etaMs+(suspend_duration) later than the text countdown. Resync
  // it explicitly: snap to the elapsed-ratio position, then re-arm a fresh
  // transition for the remaining ms.
  if((wolSent||remoteWaking)&&countdownTimer&&wolStartTime&&wolEtaMs){
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
window.addEventListener('focus',onForeground);
document.addEventListener('visibilitychange',function(){if(!document.hidden)onForeground();});
// v7.9 — fast-path visibility-transition poll (1 s). Reads document.hidden
// directly and triggers onForeground on a hidden→visible flip. Absolute safety
// net for the Android PWA standalone case where neither focus nor
// visibilitychange fires reliably on app-switcher resume — the IRL bug behind
// "il faut attendre au moins 15 s pour voir le statut passer à rouge". The
// 15 s self-healing interval is still the eventual catch-up; this poll cuts
// the worst case from CHECK_INTERVAL_MS (15 s) down to ~1 s without depending
// on any DOM event firing.
// v8.10 — clock-jump detector folded into the same 1 s poll. A prolonged device
// sleep can end WITHOUT any hidden→visible flip (screen lock that never fired
// visibilitychange, so document.hidden stayed false throughout — the IRL bug
// 2026-06-10: reopen after a long sleep kept yesterday's confident green ~10 s,
// home off, until the 8 s self-healing tick + DOWN_CONFIRM finally corrected
// it). JS timers are frozen during the sleep, so a tick-to-tick Date.now() gap
// well beyond 1 s is a reliable "we just woke up" signal: route it through
// onForeground(), which already does the right thing (probeGen bump, stale-cache
// → orange, immediate re-probe). Threshold is generous (5 s) so background-tab
// timer throttling (~1 min ticks, hidden) can't false-positive while visible.
var lastHiddenAtPoll=document.hidden,lastPollTickMs=Date.now(),SLEEP_JUMP_MS=5000;
setInterval(function(){
  var nowHidden=document.hidden;
  var now=Date.now(),jumped=now-lastPollTickMs>SLEEP_JUMP_MS;
  lastPollTickMs=now;
  if(!nowHidden&&(lastHiddenAtPoll||jumped))onForeground();
  lastHiddenAtPoll=nowHidden;
  // v8.11 — keep the "vérifié il y a Xs" line ticking while visible.
  if(!nowHidden)updateVerdictAge();
},1000);

// Wire up the 5 button handlers (migrated from inline onclick="..." attributes
// so the CSP can drop 'unsafe-inline' from script-src — see <meta http-equiv
// "Content-Security-Policy"> in index.html).
document.getElementById('testRelayBtn').addEventListener('click',function(){testRelay(this);});
document.getElementById('cancelBtn').addEventListener('click',cancelSettings);
document.getElementById('backBtn').addEventListener('click',cancelSettings);
document.getElementById('saveBtn').addEventListener('click',saveConfig);
document.getElementById('refreshBtn').addEventListener('click',checkStatus);
document.getElementById('powerBtn').addEventListener('click',sendWol);

// Derive footer version from the active SW cache name (mirrors debug.js
// pattern — single source of truth in sw.js, no hardcoded version to drift).
if(window.caches){
  caches.keys().then(function(names){
    var ours=names.filter(function(n){return n.indexOf('plex-jqh-omv')===0;});
    // v8.12 — during an SW update both the old and new caches coexist for a
    // beat; ours[0] could surface the stale one. Pick the highest version.
    var best=null;
    ours.forEach(function(n){
      var m=n.match(/-v(\d+)\.(\d+)$/);
      if(!m)return;
      var v=[+m[1],+m[2]];
      if(!best||v[0]>best.v[0]||(v[0]===best.v[0]&&v[1]>best.v[1]))best={v:v,label:'v'+m[1]+'.'+m[2]};
    });
    var el=document.getElementById('footerVersion');
    if(el&&best)el.textContent=best.label;
  }).catch(function(){});
}

// Init: URL params > localStorage > settings screen
if(!readUrlParams())config=loadConfig();
if(config&&config.host)startApp();
else showSettings();
