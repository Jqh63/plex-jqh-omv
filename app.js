var config=null,isOnline=false,wolSent=false,checking=false,checkInterval=null;
var relayReachable=true;
// v8.53 — true once a wake attempt has FAILED in this session (relay refused,
// relay unreachable, or the 5-min boot timeout expired). Drives the promotion
// of the manual-wake page: the family can follow that page (it lists free WoL
// apps per OS with their parameters pre-filled), so a failed wake must lead
// them there instead of leaving them on a dead power button. Cleared by a
// successful wake / any green settle, and by a fresh tap.
// v8.68 — wakeFailed is now the SOLE input to the alarming red (see setOffline).
//
// It used to be `lastDownDeclared`: the relay's `source==='heartbeat'` told us a
// down was the home's own last-gasp (orderly) rather than silence (anomaly), and
// only silence earned the red. That split is unusable in practice — the relay's
// HEARTBEAT_TTL_S is 45 s, so a stop is "declared" for forty-five seconds and
// silent forever after. The nominal evening shutdown (gated on the AM5, so it
// lands INSIDE the uptime window most nights, ~22h30) was therefore painted the
// calm blue for 45 s and then the alarming "Hors ligne — contacte
// l'administrateur" for the rest of the night, on every open. The mechanism
// written to stop the nightly wolf-cry was 45 s wide.
//
// The replacement asks the only question the family can act on: did the WAKE
// fail? A server that is off is just off — the button below the card is the
// answer, whatever the reason. Escalating to "contacte l'administrateur" is
// warranted exactly when that button has been pressed and did not work.
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

// v8.72 — a presumption the relay has already refuted, in THIS session, must
// not be replayed. setUnknown() deliberately leaves hasConfirmedState false (it
// is not a verdict — nothing was measured), which used to send the very next
// poll tick back through the pre-paint guard below: re-presume green, get
// demoted to unknown ~1 s later, repeat. IRL 2026-07-31, once the cache aged
// past STATUS_LOCAL_TTL_MS: SIX green→grey round trips in 50 s, at the 8 s poll
// cadence. Each path was right on its own; only their composition oscillated.
//
// Same shape as the v8.52 guard that forbids re-presuming while a `down`
// confirmation is in flight — the app does not get to re-assert a guess the
// network has already contradicted.
//
// Scoped deliberately:
//   - IN MEMORY ONLY, never persisted. Reopening the app is a NEW episode and
//     must presume again — that is v8.71, confirmed on Yann's journal.
//   - cleared the moment the relay answers ANYTHING (below), so one blip does
//     not freeze the card grey for the rest of the session. The E2E's positive
//     control pins exactly that.
var presumptionRefuted=false;

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
// v8.58 — single door for the tile's text, crossed over inside the window the
// card border and the dot already glide in (.5s / .4s). Two guards:
//   - `tilePainted` makes an identical repaint a no-op. The 8 s poll re-enters
//     setOnline/setOffline every cycle: without this the tile would blink every
//     8 s, in the state seen most.
//   - the boot countdown writes the sub DIRECTLY, bypassing this — it ticks
//     every second and a fade per second is a strobe. (Only fires on
//     status-only devices, where the ticking label is mirrored into the sub.)
var tilePainted=null,tileSwapTimer=null;
function paintTile(label,sub){
  var key=label+'\n'+sub;
  if(tilePainted===key)return;
  var first=tilePainted===null;
  tilePainted=key;
  var box=document.getElementById('statusText');
  var write=function(){
    document.getElementById('statusLabel').textContent=label;
    document.getElementById('statusSub').textContent=sub;
  };
  // No fade on the very first paint (nothing to cross over from), and none when
  // the user asked for reduced motion — there the delay would buy nothing.
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(first||reduce||!box){write();return;}
  if(tileSwapTimer)clearTimeout(tileSwapTimer);
  box.classList.add('swapping');
  tileSwapTimer=setTimeout(function(){
    write();
    box.classList.remove('swapping');
    tileSwapTimer=null;
  },220);
}
// v8.54 — a verdict age is only worth a line when it is ABNORMALLY old. The
// status poll runs every 8 s, so in the overwhelming majority of paints this
// line said "vérifié il y a quelques secondes" — one more thing to read, every
// time, carrying nothing. Above the threshold it means something real (the
// oracle has gone quiet) and it appears. Well clear of the 8 s poll and of a
// screen-off gap, so a normal resume does not flash it.
var VERDICT_AGE_SHOW_MS=120000;
function updateVerdictAge(){
  var el=document.getElementById('statusAge');
  if(!el)return;
  var age=(hasConfirmedState&&lastVerdictAtMs)?Date.now()-lastVerdictAtMs:0;
  el.textContent=(age>=VERDICT_AGE_SHOW_MS)?'vérifié '+fmtAge(age):'';
}
// v8.67 — the single way to say "what is on screen is a PRIOR, not a verdict".
//
// FOUR paths pre-paint a state before any probe settles: the two SCHEDULE-based
// presumptions in checkStatus (off-window scheduled shutdown, in-window up), and
// the two CACHE pre-paints (startApp, resume). Only the first two are sealed
// here, and the split is deliberate — they are not the same claim:
//
//   - a schedule presumption is a PRIOR: nothing was ever measured, so no age
//     may be claimed and the next poll must be free to re-presume;
//   - a cache pre-paint replays a verdict that WAS measured, up to
//     STATUS_LOCAL_TTL_MS ago. setOnline() legitimately leaves
//     hasConfirmedState=true and a real lastVerdictAtMs behind, which is what
//     makes the "vérifié il y a…" line possible and what stops checkStatus from
//     strobing orange over a state already on screen.
//
// So sealing the cache paths would NOT be the harmless unification it looks
// like: it would suppress the age line and push both reopen paths back through
// the presumption branch. Checked before touching them — the comment in
// startApp claiming it "leaves hasConfirmedState=false" is simply wrong, since
// setOnline() sets it to true two lines later (fixed there too).
//
// setOnline/setOffline stamp lastVerdictAtMs and set cardKind='verdict'; for a
// prior this UNDOES that, which is the whole point — nothing here was verified.
function sealAsPresumption(){
  hasConfirmedState=false;
  lastVerdictAtMs=0;
  cardKind='presumed';
  updateVerdictAge();
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

// v8.65 — what the status card is CURRENTLY showing, as a claim about how much
// it is worth. The distinction the card itself has to make honest:
//   'verdict'  — an oracle answered (setOnline / setOffline). The only kind that
//                may be green or red, and the only one that claims a "vérifié…".
//   'presumed' — nobody answered yet; we render a PRIOR (the schedule's
//                "Éteint (prévu)", a fresh cached up) with its own framing.
//   'checking' — we are asking. 'wake' — a boot is owning the card.
//   'unknown'  — we asked and got no usable answer (see setUnknown).
// Read by the res.unknown branch of checkStatus, which must never overwrite a
// better-informed card with a shrug.
var cardKind='none';
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
// v8.49 — presumption ceiling for the cold-open pre-paint (see the prior-verdict
// branch in checkStatus). Beyond the 60 s confident reuse, a persisted "up" up
// to this old is still painted as a PRESUMPTION (spinner running, no age
// claimed) instead of the orange wait: within half an hour of last use the
// state almost never flipped, and the probe corrects within one cycle anyway.
var PRESUME_STALE_MAX_MS=30*60000;
// v8.70 — paint journal. IRL 2026-07-30 (foreign wifi, vacation, home off): the
// card went "Éteint (prévu)" → GREEN → éteint, and NOTHING could say where the
// green came from. The relay logs only its own state TRANSITIONS, the client kept
// no trace at all, so the report could not be replayed — every candidate had to
// be excluded by reading code (relay staleness: last-gasp DOWN 9 min earlier;
// the no-cors fallback: removed from the relayed path in v8.65; a cache
// pre-paint: both bounded to 60 s). That is a diagnosis by elimination, which is
// exactly what the "instrument first" rule exists to avoid.
//
// So every paint decision now records WHY it painted, in a localStorage ring
// read by debug.html. Two properties make it worth its ~2 KB:
//   - EXHAUSTIVE: a paint that reaches the screen without a logPaint call is a
//     blind spot, so the calls sit at the decision points, not in the painters
//     (the painters can't tell a presumption from a verdict — that's the whole
//     distinction we need).
//   - HONEST about repeats: the 8 s poll repaints the same verdict endlessly, so
//     an identical consecutive entry is COLLAPSED into a count + a first/last
//     timestamp. Without that, 40 slots hold ~5 min of ticks instead of history.
var PAINT_LOG_KEY='plex-jqh-omv-paints',PAINT_LOG_MAX=40;
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
// v8.66 — test-only override of the poll cadence, read from `?poll=<ms>` at
// startup. The E2E's cost is almost entirely DEAD WAIT on this interval: three
// scenarios exist to prove the relay-down warn only hardens on the 3rd
// consecutive miss (RELAY_DOWN_MISSES), which at 8 s meant sampling at T+18 and
// T+26 — 82 s of the 158 s per engine. Shrinking the cadence keeps the property
// (still 3 misses) and drops the wait by ~8×.
//
// Deliberately NOT persisted into config: it lives only as long as the tab, so
// a provisioning URL can never bake a fast poll into a family device (the one
// real risk of a knob like this — battery and relay load). Bounded at 200 ms
// so even a hand-typed value cannot turn a phone into a pinger.
(function(){
  try{
    var v=parseInt(new URLSearchParams(location.search).get('poll'),10);
    if(v>=200&&v<=60000)STATUS_POLL_INTERVAL_MS=v;
  }catch(e){}
})();
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
// boot-history median (the desync the user saw: one device 80 s fallback,
// another 70 s). Adopted on each /status poll and persisted (config.eta) so an
// offline open still seeds a sane countdown.
//
// v8.53 — the per-device boot history that used to sit behind this is GONE
// (getBootHistory / recordBootTime / a localStorage ring of the last 10 boots,
// ~30 lines). It could only ever be consulted when the relay served no eta_s,
// i.e. when the relay is unreachable or has never measured a wake — and in that
// state there is no wake to run a countdown for in the first place, since the
// PWA's only wake path is POST <relay>/wol. It was measuring, storing and
// medianing a value that could not be reached. The persisted config.eta covers
// the one real case (an offline open right after a relay outage).
var relayEtaMs=0;
// Sanity bounds on any ETA we adopt: <10 s = the server was already up when the
// wake fired, >5 min = an anomaly (network glitch, manual interference).
var BOOT_MIN_MS=10000, BOOT_MAX_MS=300000;

var APP_CATALOG={
  seerr:      {sub:'seerr',      label:'Demander un film / une série', icon:'🎬', cls:'seerr'},
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
// v8.71 — wall-clock instant at which the uptime window most recently CLOSED
// (ms epoch), or null when no window is configured. It is what dates a
// persisted verdict against the schedule: a verdict measured AFTER that
// boundary proves the home was woken outside the plan (manual WoL, home-watch
// auto-WoL, another family member) — something the schedule cannot know.
// Measured before it, the same verdict proves nothing about now: the scheduled
// shutdown has happened since.
function windowEndedAtMs(){
  var w=parseWindow(config&&config.window);
  if(!w)return null;
  var now=new Date();
  var end=new Date(now.getFullYear(),now.getMonth(),now.getDate(),
                   Math.floor(w.end/60),w.end%60,0,0);
  if(end.getTime()>now.getTime())end.setDate(end.getDate()-1);
  return end.getTime();
}
function windowStartLabel(){
  var w=parseWindow(config&&config.window);
  if(!w)return '';
  return ('0'+Math.floor(w.start/60)).slice(-2)+'h'+('0'+(w.start%60)).slice(-2);
}
// v8.59 — a toast ACKNOWLEDGES A GESTURE; what describes a STATE belongs on the
// tile, where it persists. Hence no "— réveil manuel ↓" tails (setFallbackState
// already promotes that link permanently) and a warm-up hint that stands down
// when the sub carries it. Durations were calibrated for an ack and read too
// fast for anything explanatory.
var TOAST_MS=4500,TOAST_LONG_MS=7000;
// Shared with setOnline so the warm-up toast can tell whether the tile already
// says this — comparing loose substrings of display copy is how that rots.
// Kept SHORT on purpose: the full 'services en cours de démarrage…' was CUT
// by 13 px at 320 px CSS (an Android phone on the 'large' display-size
// setting), and this sub is shown right after a wake — the moment the family
// is actually looking. Fourth truncation on this tile (v8.13, v8.14, v8.54).
var SUB_DEGRADED='services en démarrage…';
function showToast(msg,warn,ms){var t=document.getElementById('toast');t.textContent=msg;t.className=warn?'toast warn show':'toast show';setTimeout(function(){t.className='toast'},ms||TOAST_MS)}

function getEta(){
  // The relay-served canonical ETA (shared across devices, persisted as
  // config.eta and restored in startApp) when it is present and sane, else the
  // hardcoded fallback. This is what syncs the wake countdown between devices.
  if(relayEtaMs>=BOOT_MIN_MS&&relayEtaMs<=BOOT_MAX_MS)return relayEtaMs;
  return ETA_FALLBACK_MS;
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
  // v8.50 — admin-only rescue-page link, provisioned via ?rescue= (no settings
  // field): the URL segment is a secret, typing it in a form would spread it.
  var rescue=p.get('rescue');if(rescue){var cr=cleanRelay(rescue);if(validRelay(cr))config.rescue=cr;}
  storeConfig(config);
  // Strip the provisioning params from the address bar once adopted: the URL
  // carries the relay token in clear, and it would otherwise persist in the
  // browser history / share sheet / screenshots. The config now lives in
  // localStorage; preconnect.js already ran at parse time so it saw the param.
  try{history.replaceState(null,'',location.pathname);}catch(e){}
  return true;
}

// v8.59 — fades the only navigation this app has: out, swap, in. Stays INSTANT
// on the boot paint and when the source screen is already hidden. `done` runs
// once the target is up, so a caller can focus a field that is on screen.
var SCREEN_FADE_MS=180,screensShown=false;
function switchScreen(fromId,toId,done){
  var from=document.getElementById(fromId),to=document.getElementById(toId);
  var swap=function(){
    from.style.display='none';from.classList.remove('leaving');
    to.style.display='flex';
  };
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(!screensShown||reduce||from.style.display==='none'){
    screensShown=true;swap();to.classList.remove('leaving');if(done)done();return;
  }
  from.classList.add('leaving');
  setTimeout(function(){
    swap();
    // The reflow between the add and the remove is load-bearing (verified by
    // removing it: the pin goes red at opacity 1) — without it both collapse
    // into one style recalculation and nothing transitions.
    to.classList.add('leaving');
    void to.offsetWidth;
    to.classList.remove('leaving');
    if(done)done();
  },SCREEN_FADE_MS);
}

function showSettings(){
  switchScreen('mainScreen','settingsScreen',function(){
    document.getElementById('cfgHost').focus();
  });
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
      // v8.67 — wording realigned on what the tile ACTUALLY paints. v8.53 merged
      // the two blue labels into a bare « Éteint » (the scheduled/unscheduled
      // nuance moved into the sub); this hint kept promising « Éteint (prévu) »,
      // a string the app no longer renders anywhere. In-app help that describes a
      // label the user will never see is worse than no help: it makes them doubt
      // they are looking at the right screen.
      :'Si le serveur s\'éteint volontairement la nuit : hors plage, l\'arrêt s\'affiche « Éteint » en bleu avec l\'heure de réveil auto';
  }
  if(checkInterval)clearInterval(checkInterval);
  // (focus is done by switchScreen's callback above, once the field is on screen)
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
  var prevRescue=(config&&config.rescue)||'';
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
  if(prevRescue)config.rescue=prevRescue;
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
          if(Date.now()<serverReadyHintUntil&&document.getElementById('statusSub').textContent!==SUB_DEGRADED)
            showToast('⏳ Serveur démarré — patiente',false,TOAST_MS);
          return;
        }
        e.preventDefault();
        // During an active WoL boot the server is in transition, not "off" —
        // the generic "allume-le" message is misleading and frustrating
        // ("but I just did!"). Differentiate the two cases.
        if(wolSent||remoteWaking)showToast('⏳ Réveil en cours — patiente',true);
        else showToast('⚠ Serveur éteint — allume-le',true);
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
  // v8.50 — rescue-page link (only if provisioned via ?rescue=). Deliberately
  // NEVER server-dependent/greyed: its whole point is to stay clickable when
  // the home server (or its whole docker stack) is down. Top-level nav like
  // the app links — the page carries no login, but standalone _blank would
  // open it in the ephemeral in-app browser (see the app-link comment above).
  if(config.rescue){
    var resc=document.createElement('a');
    resc.className='link-btn';
    resc.href=config.rescue;
    var rescIcon=document.createElement('div');
    rescIcon.className='link-icon cfg';
    rescIcon.textContent='🛟';
    var rescText=document.createElement('div');
    rescText.className='link-text';
    rescText.textContent='Accès de secours';
    var rescSub=document.createElement('div');
    rescSub.className='link-sub';
    rescSub.textContent='marche même si le serveur est down';
    rescText.appendChild(rescSub);
    resc.appendChild(rescIcon);
    resc.appendChild(rescText);
    container.appendChild(resc);
  }
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
// v8.72 — `remoteWaking` counts as much as `wolSent`: a wake fired by the AM5's
// logon task is adopted here, paints the same countdown, and the user watches it
// for the same ~80 s. Keying only on wolSent let the screen lock mid-boot on
// exactly the flavour of wake the family sees most (reported 2026-08-01).
// Re-entrant by construction: enterRemoteWaking() runs on EVERY poll of an adopted
// wake, so without the held/pending guard each poll would mint a new lock and
// orphan the previous one (releaseWakeLock only ever knows the last).
var wakeLockPending=false;
function acquireWakeLock(){
  if(!('wakeLock' in navigator)||(!wolSent&&!remoteWaking))return;
  if(wakeLock||wakeLockPending)return;
  wakeLockPending=true;
  navigator.wakeLock.request('screen').then(function(l){
    wakeLockPending=false;
    if(!wolSent&&!remoteWaking){l.release().catch(function(){});return;}
    wakeLock=l;
  }).catch(function(){wakeLockPending=false;});
}
function releaseWakeLock(){
  wakeLockPending=false;
  if(wakeLock){wakeLock.release().catch(function(){});wakeLock=null;}
}

function startApp(){
  switchScreen('settingsScreen','mainScreen');
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
  isOnline=false;wolSent=false;remoteWaking=false;checking=false;checkStartedAt=0;relayReachable=true;relayMissStreak=0;hasConfirmedState=false;wakeFailed=false;cardKind='none';
  // v8.28 — restore the persisted relay-served ETA so a wake fired right after an
  // offline open still seeds a shared-value countdown before the first poll lands.
  relayEtaMs=(config&&typeof config.eta==='number'&&config.eta*1000>=BOOT_MIN_MS&&config.eta*1000<=BOOT_MAX_MS)?config.eta*1000:0;
  downStreak=0;if(downRecheckTimer){clearTimeout(downRecheckTimer);downRecheckTimer=null;}
  // Reuse the localStorage cache (<60 s) for an instant paint so back-to-back
  // reopens don't strobe orange. v8.7: only an "up" cache is pre-painted (the
  // confident green) — a cached "down" is NOT pre-painted red (a stale cache must
  // never show a confident red); THAT path leaves hasConfirmedState=false so
  // checkStatus() shows the orange "Vérification…" until the probe settles.
  // v8.67 — the pre-paint itself does NOT: setOnline() sets hasConfirmedState=true
  // and stamps lastVerdictAtMs, on purpose (a cached verdict was measured, unlike
  // the schedule presumptions — see sealAsPresumption). The old comment here
  // claimed the opposite and had been wrong for a while; it is the reason this
  // path was nearly "unified" with the presumptions, which would have silently
  // dropped the "vérifié il y a…" line.
  var cached=readLocalStatus();
  if(cached&&cached.up&&!cached.degraded){
    relayReachable=cached.relayOk!==false;
    setOnline();
    logPaint('online','cache-prepaint-open','cache='+Math.round((Date.now()-cached.t)/1000)+'s');
  }else if(cached&&cached.up){
    // v8.72 — a degraded "up" is not a confident green: the host answers but the
    // app the user is about to tap does not. Fall through to the orange probe
    // rather than pre-paint a green the live path would itself withhold.
    hasConfirmedState=false;
    logPaint('checking','cache-prepaint-declined-degraded','cache='+Math.round((Date.now()-cached.t)/1000)+'s');
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
  // The install hint used to be revealed here, from a 3 s timeout — which is
  // what made it re-centre the page under the user's eyes. It is now decided
  // before the first paint by install-hint.js + CSS (v8.72); nothing to do
  // at runtime.
}

// v7.0 — relay-as-oracle. One fetch to the relay's /status answers both
// "is the relay reachable?" and "is the home server up?". On relay
// timeout we retry once; if both fail we fall back to a direct no-cors
// fetch against the home so up/down detection survives a GCP outage.
function readLocalStatus(maxAgeMs){
  try{
    var raw=localStorage.getItem(STATUS_LOCAL_KEY);if(!raw)return null;
    var d=JSON.parse(raw);
    if(!d||typeof d!=='object'||typeof d.t!=='number')return null;
    if(Date.now()-d.t>(maxAgeMs||STATUS_LOCAL_TTL_MS))return null;
    return d;
  }catch(e){return null;}
}
// v8.72 — `degraded` is part of the cached verdict, not a detail of the live
// one. Without it the cache said "up, confident" at the exact moment the code
// was refusing to show green (the wake hold below), and any pre-paint promoted
// that to the confident green it had just withheld. Cached shape is versioned
// only by its keys: an old entry simply has no `degraded` and reads falsy,
// which is the pre-v8.72 behaviour for the one TTL it survives.
function writeLocalStatus(up,relayOk,degraded){
  try{localStorage.setItem(STATUS_LOCAL_KEY,JSON.stringify({up:!!up,relayOk:relayOk!==false,degraded:!!degraded,t:Date.now()}));}catch(e){}
}

// See PAINT_LOG_KEY. `card` = what the user sees (online/offline/checking/
// unknown/waking/no-network), `why` = the branch that decided it, `detail` =
// the evidence it decided on (relay source + age, prior age…). Never throws:
// localStorage can be unavailable (private mode) and a diagnostic must not be
// able to break the app it observes.
// v8.71 — the shape of a detail string, numbers blanked. Collapsing on the raw
// string was defeated by the very field that changes every tick: the 07:53
// wake-up of 2026-07-30 burned 14 of the 40 slots on ONE decision
// ("verdict-down src=hb age=2329s… age=2374s waking"), so the ring held ~1 h of
// history instead of a night. Blanking digits collapses those; it deliberately
// does NOT collapse a change of KIND (src=hb vs src=pull, the appearance of
// "waking"), which is evidence, not noise.
function paintDetailShape(d){return (d||'').replace(/\d+/g,'#');}
function logPaint(card,why,detail){
  try{
    var raw=localStorage.getItem(PAINT_LOG_KEY),a=raw?JSON.parse(raw):[];
    if(!Array.isArray(a))a=[];
    var last=a.length?a[a.length-1]:null,now=Date.now();
    if(last&&last.c===card&&last.w===why&&paintDetailShape(last.d)===paintDetailShape(detail)){
      last.n=(last.n||1)+1;last.t=now;
      // Same decision on the same evidence, only the numbers moved. Keep the
      // last values too: "age=2329s → age=2374s ×14" says the heartbeat kept
      // ageing (a real outage), where a frozen age would say the opposite.
      if(detail&&detail!==last.d)last.d2=detail;
    }else{
      var e={t:now,t0:now,c:card,w:why};
      if(detail)e.d=detail;
      a.push(e);
    }
    if(a.length>PAINT_LOG_MAX)a=a.slice(a.length-PAINT_LOG_MAX);
    localStorage.setItem(PAINT_LOG_KEY,JSON.stringify(a));
  }catch(e){}
}
// Relay evidence in one short string, so a journal line answers "who said this"
// without a second lookup: source (home's own declaration vs relay pull), the
// age it claimed, and the flags that steer the card.
function relayEvidence(res){
  if(!res)return '';
  var p=[];
  p.push(res.declared?'src=hb':'src=poll');
  if(res.confirmed)p.push('confirmed');
  if(typeof res.ageS==='number')p.push('age='+res.ageS+'s');
  // 2026-07-31 — the age alone cannot say WHEN the relay computed it. Observed
  // on a cold open: age=578s at 11:30:04, then age=6s one second later, from a
  // single-worker relay whose age is monotonic — impossible from two live
  // answers. The documented suspect is right here in checkStatus: a probe
  // FROZEN mid-fetch by Android that only resolves on resume carries an age
  // computed minutes earlier. The round-trip separates the two stories at a
  // glance (rt=350ms = the relay really said this; rt=570000ms = a thawed
  // probe), and it is already measured — it was just never surfaced.
  // Benign here (the home was genuinely off), but the symmetric case is a
  // FALSE GREEN: a thawed probe carrying age=9s would paint online. Logged,
  // not "fixed": the fix has to wait for the journal to name the culprit.
  if(typeof res.rtMs==='number')p.push('rt='+res.rtMs+'ms');
  if(res.degraded)p.push('degraded');
  if(res.waking)p.push('waking');
  if(res.wakeFailedRemote)p.push('wake_failed');
  if(!res.relayReachable)p.push('relay-unreachable');
  return p.join(' ');
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
  // v8.51 — stamp the request round-trip so time-sensitive fields
  // (wake_age_s) can be transit-compensated by the consumer: the age is
  // computed server-side at response build and is stale by ~RTT/2 + download
  // by the time it's applied here (~0.3-0.5 s measured on the e2-micro leg).
  var t0=Date.now();
  return fetchOnce(config.relay+'/status',opts).then(function(r){
    if(!r.ok)return answered('HTTP '+r.status);
    return r.json().catch(function(){return answered('bad json');});
  }).then(function(j){
    if(!j||typeof j.up!=='boolean')return answered('bad shape');
    j._rtMs=Date.now()-t0;
    return j;
  });
}

// ⚠️ ONE caller, and it is not the one this used to have: the RELAY-LESS branch
// of probe(). With a relay configured this function is never called — v8.65
// removed it from that path because an opaque response identifies nothing (a
// captive portal, or the still-powered box in front of a shut-down host, both
// fulfil it). The old comment here still promised the opposite ("enough to flip
// the up/down state when the relay is dead"), which is precisely the reasoning
// that produced a false green IRL. Left standing, that sentence is an invitation
// to re-wire this into the relayed path.
//
// Where it IS the oracle — a fork with no relay — it is the only thing there is,
// and the same weakness applies; the fork accepts it knowingly. Covered by the
// three `no-relay-*` scenarios in cold-radio-e2e.py, which assert zero relay
// calls precisely so "this branch was never entered" cannot pass silently.
function fetchHomeDirectly(){
  // no-cors: the response is opaque — a fulfilled promise says only that
  // SOMETHING completed a handshake at that name, never that it was the home.
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
  // the card text is left UNTOUCHED and the poll runs silently. v8.53 — there is
  // no in-flight indicator any more (the refresh button that carried it is gone);
  // freshness is communicated by the "vérifié …" age line, which is what the user
  // actually needs to judge the verdict. v8.29 — we used to flip the sub to
  // "vérification…" on every
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
  // v8.71 — the schedule is a prior, and a MEASURED verdict outranks it. IRL
  // 2026-07-30: home woken by hand at 07:56 (window 13h50-00h10), app reopened
  // at 07:59 — the card flashed "Éteint (prévu)" before the probe corrected to
  // green 1 s later. The persisted "up" was 3 min old and simply never
  // consulted here, while the in-window branch below had been reading it since
  // v8.49. Two conditions, both needed (see windowEndedAtMs): the prior must be
  // younger than PRESUME_STALE_MAX_MS *and* stamped after the window closed —
  // otherwise a 00h20 reopen would flash green off a 00h05 verdict, the exact
  // mirror of the red flash banned in v8.7, on the most common nightly case.
  var prior=readLocalStatus(PRESUME_STALE_MAX_MS);
  var inWin=inUptimeWindow();
  var windowEnd=windowEndedAtMs();
  var priorOutranksSchedule=!!(prior&&prior.up&&downStreak===0&&
                               windowEnd!==null&&prior.t>=windowEnd);
  // !presumptionRefuted on BOTH branches, and no else: when the relay has
  // already refuted a presumption this session, the pre-paint paints NOTHING
  // and leaves the unknown card standing. Gating only the presumption would
  // have swapped one oscillation for another — the `checking` fallback below
  // would then repaint "Vérification…" every tick instead.
  if(!hasConfirmedState&&!presumptionRefuted&&!wolSent&&!remoteWaking&&
     navigator.onLine&&inWin===false&&!priorOutranksSchedule){
    setOffline();
    sealAsPresumption();
    // Why the schedule won, so the journal answers it without a second look:
    // no prior at all, a persisted down, or an up that predates the close.
    logPaint('offline','presume-off-window','window='+((config&&config.window)||'none')+
             ' prior='+(!prior?'none':(!prior.up?'down':
               (downStreak?'refuted':'pre-close'))));
  }else if(!hasConfirmedState&&!presumptionRefuted&&!wolSent&&!remoteWaking){
    // v8.49 — inside the window, presume the LAST PERSISTED verdict instead of
    // orange when one exists (bounded by PRESUME_STALE_MAX_MS). The relay knows
    // the answer instantly (heartbeat-primary), but the PWA→relay fetch still
    // pays a cold-radio TLS handshake (~3-8 s) — during which the family stared
    // at "Vérification…" for a state that almost never changed since last open.
    // Same PRESUMPTION contract as the off-window branch above: only an "up"
    // prior is painted (a stale red must never flash — v8.7 doctrine),
    // hasConfirmedState stays false (no "vérifié il y a…" claimed, spinner
    // runs), and the settling probe corrects within one cycle.
    // v8.52 — never re-presume while a down-confirmation is in flight: a live
    // "down" has already refuted the stale prior, and setOnline() here would
    // reset downStreak on every re-check cycle, making red unreachable for up
    // to PRESUME_STALE_MAX_MS (deterministic e2e failure: clockjump-wake-
    // stale-green-demoted stuck on "Vérification..." forever).
    // v8.60 — the SCHEDULE is a prior too, symmetrically with the off-window
    // branch above: inside the window the home is up unless something failed
    // (a missed RTC wake, itself covered by home-watch's auto-WoL). So a cold
    // open with NO usable cache — first install, or a cache older than
    // PRESUME_STALE_MAX_MS — no longer stares at orange for the whole cold-radio
    // handshake; it renders green at once and the probe corrects if wrong.
    // The one prior we do NOT overrule is a fresh PERSISTED down: during a real
    // outage the family re-opens the app repeatedly, and flashing green on each
    // open would be the mirror of the red flash v8.7 banned.
    var presumeUp=navigator.onLine&&downStreak===0&&
                  ((prior&&prior.up&&(inWin!==false||priorOutranksSchedule))||
                   (inWin===true&&!(prior&&!prior.up)));
    if(presumeUp){
      setOnline();
      sealAsPresumption();
      logPaint('online',
               inWin===false?'presume-prior-outranks-window':'presume-in-window',
               prior&&prior.up?('prior-up '+Math.round((Date.now()-prior.t)/1000)+'s'):'schedule-only');
    }else{
      cardKind='checking';
      document.getElementById('statusDot').className='status-dot checking';
      document.getElementById('statusCard').className='status-card';
      paintTile('Vérification...','interrogation du relais…');
      setButtonChecking();
      logPaint('checking','no-usable-prior',
               prior?(prior.up?'prior-up':'prior-down'):'no-prior');
    }
  }
  probe().then(function(res){
    // A newer probe (e.g. a resume re-probe) superseded this one — drop the
    // stale verdict without touching `checking`, which the newer probe owns.
    if(gen!==probeGen)return;
    checking=false;
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
    // v8.65 — no oracle answered: we do not know. An unknown is NOT a verdict,
    // so it commits nothing (no green, no red, no cache write, no streak move,
    // no hasConfirmedState) and it never overwrites something better already on
    // screen: a confirmed verdict keeps its card and lets the "vérifié il y a…"
    // age line carry the growing doubt, and a presumption (the blue scheduled
    // "Éteint (prévu)", or a reused fresh cache) keeps its own honest framing.
    // It paints only when the alternative is spinning on "Vérification…"
    // forever — the one case where the user is owed an answer we don't have.
    // One rule, deliberately blunt: the card shows what the RELAY said. When it
    // said nothing, the card says so — including over a presumption (the
    // schedule's blue "Éteint (prévu)", a reused cache), because a prior that no
    // probe could confirm is exactly the "fausse indication" this version is
    // about. Two exceptions, and only two:
    //   - a wake owns the card (the countdown is its own honest state);
    //   - the phone has NO network — that is a fact we hold first-hand, and
    //     setOffline() renders it as the hollow "Pas de connexion" card.
    // A verdict that is still fresh is kept (the poll runs silently under it);
    // once it ages past the staleness guard it is demoted here like the rest.
    // Idempotent: a second unknown leaves the unknown card alone.
    // The relay answered something usable -> a new episode may presume again.
    if(!res.unknown)presumptionRefuted=false;
    if(res.unknown){
      if(wolSent||remoteWaking)return;
      if(!navigator.onLine){setOffline();logPaint('no-network','probe-unknown-offline');return;}
      if(!(cardKind==='verdict'&&hasConfirmedState)){
        setUnknown();
        // The relay has now contradicted whatever we presumed: stop replaying it.
        presumptionRefuted=true;
        logPaint('unknown','relay-silent',relayEvidence(res));
      }else{
        logPaint('kept-verdict','relay-silent',relayEvidence(res));
      }
      return;
    }
    // v8.69 — the relay says the last wake FAILED (its campaign ran bursts +
    // grace without the home ever answering). Two things this buys, neither of
    // which the client could compute on its own:
    //   - SPEED for the device that tapped. Its own verdict was WOL_TIMEOUT_MS
    //     (5 min) of countdown-then-nothing; the relay knows at ~150 s. So the
    //     countdown is cut short here rather than run out.
    //   - AGREEMENT for every other open device. They never learned a wake had
    //     failed at all: two phones in the same room, one red one blue.
    // Since v8.68 wakeFailed is the sole input to the alarming red, so setting
    // it here is exactly what makes the card escalate on all of them.
    //
    // The freshness guard is the race this branch would otherwise lose: a probe
    // launched BEFORE our own tap can resolve after it, still carrying the
    // previous attempt's failure, and would kill a wake that just started. The
    // relay retracts the flag on the /wol itself, so anything older than one
    // poll cycle is stale by construction.
    if(res.wakeFailedRemote&&!res.up&&
       !(wolSent&&Date.now()-wolStartTime<WOL_POLL_MS)){
      if(wolSent||remoteWaking){
        wolSent=false;remoteWaking=false;wolStartTime=0;
        clearWolPoll();stopCountdown();releaseWakeLock();
        if(navigator.vibrate)navigator.vibrate(300);
        showToast('⚠ Pas démarré — réessaie',true,TOAST_LONG_MS);
      }
      wakeFailed=true;
      // No orange re-confirmation detour on the way to this red: the relay
      // watched the home ignore a full campaign, which is a stronger statement
      // than the two agreeing probes DOWN_CONFIRM asks for.
      downStreak=DOWN_CONFIRM;
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
      // v8.49 — during an active wake, a DEGRADED up is not "démarré": the host
      // answers HTTP but the apps (Seerr…) are still starting — exactly the
      // ~20 s the old countdown missed ("Serveur démarré" then a 502 on tap).
      // Keep the Démarrage… countdown running until the first non-degraded
      // poll; bounded (ETA + warm-up grace) so a genuinely broken app can't
      // hold the wake UI hostage — past the bound we settle green + degraded
      // sub, same as before. The relay's ETA sample is gated the same way
      // (services-ready), so the shared countdown now lands on this instant.
      if(res.degraded&&(wolSent||remoteWaking)&&wolStartTime&&
         Date.now()-wolStartTime<getEta()+APP_WARMUP_MS){
        downStreak=0;if(downRecheckTimer){clearTimeout(downRecheckTimer);downRecheckTimer=null;}
        writeLocalStatus(true,relayReachable,true);
        return;
      }
      writeLocalStatus(true,relayReachable,res.degraded);
      setOnline(res.degraded);
      logPaint('online','verdict-up',relayEvidence(res));
    }else if(res.waking&&!wolSent){
      // v8.25 — a wake fired elsewhere (another device, or an earlier session of
      // ours) is in progress per the relay. Show the boot countdown without
      // firing our own POSTs; the normal poll flips to green when the home
      // answers, or to the down path once the relay's waking signal expires.
      // Takes priority over the down-confirmation: waking is a confident
      // "it's coming up" signal, so don't paint red underneath it.
      enterRemoteWaking(res.wakeAgeS);
      logPaint('waking','adopted-remote-wake',relayEvidence(res));
    // v8.71 — `confirmed` earns the same shortcut as `declared`, on the same
    // grounds: the relay demoted a stale "up" beat only after a pull that had
    // itself survived STATUS_DOWN_CONFIRM_POLLS. Re-confirming it here would add
    // two 8 s cycles of orange to a verdict two independent legs already agree
    // on — which would move the false-green window rather than close it.
    }else if(res.declared||res.confirmed||++downStreak>=DOWN_CONFIRM){
      // v8.48 — a heartbeat-sourced "down" is the home's own last words (clean
      // shutdown last-gasp), not a flaky probe: commit red at once instead of
      // the orange re-confirmation detour. Covers "extinction avec app ouverte"
      // — the card flips to Éteint on the next poll, no Vérification… dance.
      // v8.68 — `declared` keeps THIS job (skip the orange detour) and only this
      // one: it no longer feeds the card's colour, which now keys on wakeFailed.
      downStreak=DOWN_CONFIRM;
      writeLocalStatus(false,relayReachable);
      setOffline();
      logPaint('offline','verdict-down',relayEvidence(res));
    }else{
      setRechecking();
      logPaint('checking','down-unconfirmed',relayEvidence(res)+' streak='+downStreak);
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
  // During an active wake, keep the "Démarrage…" state — a re-check card would
  // contradict the wake-in-progress UI (mirrors setOffline's wolSent guard).
  // v8.53 — remoteWaking is guarded too, not just wolSent. An ADOPTED wake (the
  // relay's `waking`, e.g. the AM5 logon task) whose boot outlives the relay's
  // WAKE_SIGNAL_TTL_S stops being advertised while remoteWaking is still true
  // here and the countdown is still ticking: the next non-waking "down" landed
  // in this function and painted "Vérification…" over the card while the power
  // label kept counting "Démarrage long…". Two widgets, two contradicting
  // stories. tests/README.md documented the repaint as a trap to write tests
  // AROUND ("the status card is repainted in ~200 ms while the countdown keeps
  // ticking") — it was the bug, not a fixture quirk.
  if(wolSent||remoteWaking){setStarting();return;}
  cardKind='checking';
  document.getElementById('statusDot').className='status-dot checking';
  document.getElementById('statusCard').className='status-card';
  paintTile('Vérification...','nouvelle tentative…');
  setButtonChecking();
}

// v8.7 follow-up (user feedback 2026-06-07) — the power button must not keep a
// stale confident green while the card is showing an orange check. Paint a
// neutral "Vérification…" button whenever the card is orange (cold-open check or
// a down being re-confirmed). NOT during a WoL wake — the button owns the
// "Démarrage…" / progress UI then — nor without a configured MAC (no wake to offer).
function setButtonChecking(){
  // v8.53 — remoteWaking added alongside wolSent: during ANY wake the button
  // owns the countdown UI, whether this device fired it or adopted it.
  if(!config||!config.mac||wolSent||remoteWaking)return;
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
//                                         down (the fallback link is promoted).
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
    // v8.69 — and `wake_failed`, its mirror: the relay's campaign ran its full
    // course without the home ever answering. See the branch in checkStatus.
    // ageS is carried for the paint journal only (see relayEvidence): "green,
    // src=hb, age=549s" is a different story from "green, src=poll, age=2s", and
    // that distinction is precisely what the 2026-07-30 report was missing.
    function(j){return {up:j.up,ageS:(typeof j.age_s==='number'?j.age_s:null),rtMs:(typeof j._rtMs==='number'?j._rtMs:null),relayReachable:true,window:(typeof j.window==='string'?j.window:null),waking:j.waking===true,wakeAgeS:(typeof j.wake_age_s==='number'?j.wake_age_s+((j._rtMs||0)/2000):0),etaS:(typeof j.eta_s==='number'?j.eta_s:0),degraded:j.degraded===true,declared:j.source==='heartbeat',confirmed:j.confirmed===true,wakeFailedRemote:j.wake_failed===true};},
    function(err){
      var relayUp=!!(err&&err.answered);
      // v8.65 — the direct-home fallback no longer produces a VERDICT.
      //
      // IRL bug (2026-07-29, wifi d'une autre box, homelab éteint hors fenêtre):
      // carte bleue "Éteint (prévu)" → VERT quasi instantané → "Vérification…"
      // 2,5 s → éteint. The green was a real setOnline(): first cycle on a cold
      // radio timed out the relay, the fallback fetch *fulfilled*, and that was
      // promoted to {up:true}.
      //
      // A `no-cors` response is OPAQUE: a fulfilled promise says only "something
      // accepted the TCP/TLS handshake at that name" — it identifies NOTHING. On
      // a foreign wifi (captive portal, DNS interception) or against the still-
      // powered box in front of a shut-down host, that "something" is not the
      // home. Symmetrically, a rejection can be the wifi blocking us rather than
      // the home being off. Neither direction is evidence, so neither is painted.
      // The relay stays the ONLY oracle; when it doesn't answer we say we don't
      // know (see the res.unknown branch in checkStatus).
      //
      // Since neither outcome carries information, the fallback fetch is not
      // made at all on this path: it only bought a HOME_FALLBACK_TIMEOUT_MS wait
      // before we could admit we don't know. (fetchHomeDirectly survives for the
      // relay-less mode below, where it is the only oracle there is.)
      return Promise.resolve({unknown:true,up:false,relayReachable:relayUp});
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
// Style/wording chosen so a relay outage is visible even while the server is
// up — otherwise the issue only surfaces the next time WoL is needed. v8.53
// widened it past the admin-only case: ANY failed wake promotes the link.
function setFallbackState(){
  if(!config||!config.mac)return;
  var link=document.getElementById('fallbackLink');
  var a=document.getElementById('fallbackLinkA');
  link.classList.remove('promoted','warn');
  // v8.53 — promote on ANY failed wake, not just an unreachable relay: a wake
  // that timed out, a 401/403 or a 502 are exactly when the manual page helps
  // most, and none of them used to promote it.
  if(!relayReachable||wakeFailed){
    if(isOnline){
      link.classList.add('warn');
      a.textContent='⚠ Réveil manuel';
    }else{
      link.classList.add('promoted');
      a.textContent='Réveil manuel — comment faire';
    }
  }else{
    a.textContent='Réveil ne marche pas ? Réveil manuel';
  }
}

function setOnline(degraded){
  // v8.27 — capture whether we got here off the back of a wake (local or remote)
  // BEFORE the flags are cleared below: if so, arm the app-warm-up grace so a tap
  // in the next APP_WARMUP_MS gets the "apps still starting" heads-up.
  if(wolSent||remoteWaking)serverReadyHintUntil=Date.now()+APP_WARMUP_MS;
  isOnline=true;
  remoteWaking=false;
  // The home is up — however it got there. Any earlier wake failure is moot.
  wakeFailed=false;
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
  // with a <60 s verdict, reused while the re-check runs) or from a live probe
  // settle.
  // Both are treated as "up"; a contradicting probe corrects to red within ~1
  // probe (see hasConfirmedState note).
  cardKind='verdict';
  document.getElementById('statusDot').className='status-dot online';
  document.getElementById('statusCard').className='status-card online';
  // v8.54 — undo the no-network hide (setOffline may have set it before the
  // radio came back): never leave the button hidden on a state that can arm
  // it. Same inline-style mechanism, same wolReady() guard as startApp().
  document.getElementById('powerSection').style.display=wolReady()?'flex':'none';
  // v8.48 — surface the relay's `degraded` on the card itself: host up but the
  // apps (Seerr…) still starting. Green stays (no pointless wake) but the sub
  // says WHY a tapped app may spin — the toast hint alone was invisible until
  // the user actually tapped a link. Self-corrects: the next non-degraded poll
  // repaints the normal sub.
  // v8.54 — silent when nominal. "serveur accessible" restated what the green
  // card and the "En ligne" label already said, in the state the family sees
  // most often. The degraded sub stays: that one carries information (a tapped
  // app may still spin) and self-corrects on the next non-degraded poll.
  paintTile('En ligne',degraded?SUB_DEGRADED:'');
  updateVerdictAge();
  if(config.mac){
    var pBtn=document.getElementById('powerBtn'),pLbl=document.getElementById('powerLabel');
    pBtn.className='power-btn online';
    pLbl.textContent='Serveur allumé';pLbl.className='power-label sent';
    setFallbackState();
  }
  if(wolSent){
    // v8.53 — no local boot sample is recorded any more: the relay measures the
    // wake it actually served (and to services-ready, which this client-side
    // timing never could — it only sees the host answering).
    wolStartTime=0;
    showToast('✓ Serveur démarré avec succès',false,TOAST_LONG_MS);
    if(navigator.vibrate)navigator.vibrate([100,50,100]);
    wolSent=false;
    setTimeout(releaseWakeLock,10000);
  }else{
    releaseWakeLock();
  }
}

// v8.65 — the honest non-answer. Reached when the relay (the only oracle we can
// authenticate) neither answered nor could be reached. It deliberately looks
// like NO state: dashed, dimmed, no coloured dot — a card that claims nothing,
// so it can never be mistaken for the green or the red at a glance.
//
// Nothing internal moves: isOnline is left as-is (an unknown does not "turn the
// server off"), hasConfirmedState stays false, downStreak is untouched and no
// cache is written. Only the picture changes, and the next poll can still settle
// it either way.
//
// The wake button stays ARMED (same reasoning as v8.53's relay-down case): not
// knowing is precisely when the family should still be able to act, and a magic
// packet sent to a host that is already up is ignored by the NIC. The manual-
// wake link is promoted by setFallbackState() through relayReachable.
function setUnknown(){
  cardKind='unknown';
  document.getElementById('statusDot').className='status-dot unknown';
  document.getElementById('statusCard').className='status-card unknown';
  document.getElementById('powerSection').style.display=wolReady()?'flex':'none';
  // Copy kept short — narrow phones (256-300 px CSS) truncated longer subs in
  // v8.13/v8.14/v8.54.
  // v8.68 — say WHAT happened, not WHO is at fault. "relais injoignable" named a
  // culprit the app cannot actually identify: this state is reached whenever the
  // /status fetch fails, and the most frequent cause is the PHONE's own last mile
  // — a foreign wifi, a captive portal, a tunnel re-establishing — none of which
  // flips navigator.onLine, all of which look identical from here. Blaming a
  // component that is very often perfectly healthy is both wrong and useless to
  // a family reader, who has no model of "the relay" anyway.
  paintTile('Statut inconnu','impossible de vérifier');
  updateVerdictAge();
  if(wolReady()){
    var btn=document.getElementById('powerBtn'),lbl=document.getElementById('powerLabel');
    btn.className='power-btn';lbl.className='power-label';
    lbl.textContent='Allumer le serveur';
    setFallbackState();
  }
}

function setStarting(){
  cardKind='wake';
  document.getElementById('statusDot').className='status-dot checking';
  document.getElementById('statusCard').className='status-card';
  paintTile('Démarrage…','réveil en cours');
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
  acquireWakeLock();
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
  // !important: under prefers-reduced-motion the blanket `*{transition-duration:
  // .01ms!important}` rule (index.html) would otherwise beat this inline value
  // and snap the bar straight to 100% — a full, static bar that reads as "done"
  // mid-boot (Windows PC with animations off, 2026-08-01). An inline !important
  // outranks a stylesheet !important, so the real ETA-length transition wins.
  // The wake bar is essential feedback, exempt from motion reduction (WCAG 2.3.3).
  bar.style.setProperty('transition','width '+((etaMs-elapsedMs)/1000)+'s linear','important');
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
  // v8.51 — ticks aligned on countdownEndsAt's whole-second boundaries
  // instead of a free-running 1 s interval: two devices sharing the same
  // anchor now repaint the same remaining-seconds number at the same wall
  // instant (a free-running interval phase added up to ~1 s of perceived
  // cross-device offset). stopCountdown's clearInterval also clears timeouts
  // (shared handle pool), so the existing teardown keeps working.
  var schedule=function(){
    var d=(countdownEndsAt-Date.now())%1000;
    if(d<=0)d+=1000;
    countdownTimer=setTimeout(function(){tick();schedule();},d);
  };
  schedule();
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
    wolSent=false;wolStartTime=0;wakeFailed=true;stopCountdown();clearWolPoll();releaseWakeLock();
    // v8.53 — name the case. These are genuinely different situations for the
    // reader: 401/403 and 502 are the admin's problem and retrying is pointless,
    // 429 clears on its own, and only "réessaie" is honest for the rest.
    var msg;
    if(r.status===401||r.status===403)msg='Relais : accès refusé (config)';
    else if(r.status===429)msg='Trop d\'essais — patiente';
    else if(r.status===502)msg='Relais : serveur introuvable';
    else msg='Erreur relais HTTP '+r.status;
    if(navigator.vibrate)navigator.vibrate(300);
    showToast('⚠ '+msg,true,TOAST_LONG_MS);
    setOffline();
  }).catch(function(){
    wolSent=false;wolStartTime=0;wakeFailed=true;stopCountdown();clearWolPoll();releaseWakeLock();
    // Flip relayReachable manually — a checkStatus() right now would race
    // the WoL POST, and we already know the relay just failed. This is a
    // CONFIRMED failure (the user actually tried to wake), so bypass the
    // miss-streak debounce and surface it immediately; pin the streak at the
    // confirmed-down ceiling so a following miss keeps it down.
    relayReachable=false;relayMissStreak=RELAY_DOWN_MISSES;
    if(navigator.vibrate)navigator.vibrate(300);
    showToast('⚠ Relais injoignable',true,TOAST_LONG_MS);
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
  // v8.54 — three painted states instead of four, on ONE rule: the colour now
  // answers "what do I do?", not "what is the internal state?".
  //   hollow  — the PHONE has no network. The app knows nothing and no tap can
  //             help, so the card is unlit and the button is hidden.
  //   blue    — off. The button below IS the answer.
  //   red     — the button was pressed and the wake FAILED. Only then does the
  //             app INSTRUCT: "contacte l'administrateur".
  // v8.68 — the blue/red split no longer tries to guess whether a stop was
  // "orderly". It couldn't: the only evidence for that (the relay's last-gasp,
  // `declared`) lives 45 s (HEARTBEAT_TTL_S), so every nominal evening shutdown
  // turned red a minute later and told the family to call the admin about a
  // server that had simply gone to bed. And the guess was never actionable
  // anyway — off is off, and pressing the button is the same gesture either way.
  // What IS actionable is a wake that didn't work: that, and only that, is worth
  // escalating. A relay we cannot reach no longer lands on the red by itself —
  // it shows the honest "Statut inconnu" (setUnknown) until a tap actually
  // fails, which is when it starts costing the family something.
  var noNet=!navigator.onLine;
  var sleeping=!noNet&&!wakeFailed;
  var paint=noNet?'nonet':(sleeping?'sleep':'offline');
  cardKind='verdict';
  document.getElementById('statusDot').className='status-dot '+paint;
  document.getElementById('statusCard').className='status-card '+paint;
  // The wake button is hidden ONLY here: navigator.onLine=false is a fact, not
  // the presumption that v8.53 stopped disarming the button on.
  // ⚠️ Drive the INLINE style, not a class: startApp() sets style.display on this
  // same element, and an inline style beats any stylesheet rule — a `.hidden`
  // class silently did nothing here. Keep the wolReady() guard so we never
  // reveal a power section that a status-only device is meant to be without.
  document.getElementById('powerSection').style.display=
    (noNet||!wolReady())?'none':'flex';
  if(noNet){
    // Sub says what the user can actually do — the one actionable thing left.
    // Kept SHORT on purpose: "vérifie ton wifi ou tes données mobiles" was
    // truncated to "…données m…" at 360 px in tests/screenshots. Third time
    // this tile has had to cut copy for narrow phones (v8.13, v8.14).
    paintTile('Pas de connexion','vérifie ta connexion');
  }else if(sleeping){
    // v8.15 — "En veille" implied a suspend; the box actually powers OFF
    // (autoshutdown + RTC wake). "Éteint" matches reality while the blue card
    // keeps the calm "this is expected" framing.
    // v8.54 — sub carries the auto-wake time when the schedule knows it.
    // v8.68 — and NOTHING otherwise. It used to say "arrêt normal du serveur",
    // which was only ever backed by the 45 s last-gasp; now that this blue also
    // covers stops we cannot explain, asserting "normal" would be inventing.
    // Silence costs nothing here — the button underneath already reads
    // "Allumer le serveur", which is the whole of what the family has to do.
    // Keep any copy short: v8.13/v8.14 both had to cut subs that wrapped on
    // narrow phones (S24) once Android font scaling kicked in.
    paintTile('Éteint',(inWin===false)?'réveil auto à '+windowStartLabel():'');
  }else{
    paintTile('Hors ligne','contacte l\'administrateur');
  }
  updateVerdictAge();
  if(wolReady()){
    var btn=document.getElementById('powerBtn'),lbl=document.getElementById('powerLabel');
    // v8.53 — the button stays ARMED even when the relay is presumed unreachable.
    // It used to render `.unavailable`, which is `pointer-events:none` — a dead
    // button, on a presumption drawn from status-poll misses that are as often the
    // phone's own connectivity as the relay's health (see the sendWol comment).
    // The relay-down warning is not lost: setFallbackState() promotes the manual
    // wake link to a red, full-size call to action, which is the actionable half
    // of the old message. A tap that really can't reach the relay fails in one
    // round-trip with an explicit toast.
    btn.className='power-btn';lbl.className='power-label';
    // v8.54 — always the plain label. "(relais incertain)" was admin vocabulary
    // on the one control the family uses: it named a component they have no
    // model of, and it changed nothing about the gesture (v8.53 already arms the
    // button either way, and a tap that truly can't reach the relay fails in one
    // round-trip with an explicit toast). The uncertainty belongs on the status
    // card — which now says "contacte l'administrateur" — not on the action.
    lbl.textContent='Allumer le serveur';
    setFallbackState();
  }
}

function sendWol(){
  if(isOnline||wolSent||!wolReady())return;
  // v8.53 — a tap is no longer refused on the PRESUMED relay state. relayReachable
  // is inferred from consecutive /status misses, and those misses include the
  // phone's own connectivity blips (tunnel re-establishing, radio handover): the
  // relay can be perfectly fine. Refusing meant the user had to wait for 3 clean
  // polls (~24 s) before the button re-armed, on a wake that would have worked.
  // Try instead: postWol answers in one round-trip and its failure path already
  // paints the definitive "Relais injoignable" + manual-fallback toast. Optimistic
  // action, then correct on evidence — the cost of being wrong is one round-trip,
  // the cost of the old guard was a dead button.
  if(navigator.vibrate)navigator.vibrate(50);
  wolSent=true;
  logPaint('waking','local-tap');
  // A fresh attempt clears the previous failure: the promoted fallback link
  // would otherwise stay shouting through a wake that is going fine.
  wakeFailed=false;
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
      wolSent=false;wolStartTime=0;wakeFailed=true;clearWolPoll();stopCountdown();releaseWakeLock();checkStatus();
      if(navigator.vibrate)navigator.vibrate(300);
      // Surface the timeout — silent failure (vibration + flip to red) used to
      // leave family members wondering whether the app was broken. Toast tells
      // them what happened and points to the manual fallback.
      showToast('⚠ Pas démarré — réessaie',true,TOAST_LONG_MS);
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
  // only an "up" cache is pre-painted (the confident green); a cached "down" is
  // NOT pre-painted red — it falls
  // through to the orange "Vérification…" like a stale/empty cache. The
  // background checkStatus() below confirms or corrects within ~1 probe.
  var cached=readLocalStatus();
  // v8.72 — the IRL bug of 2026-08-03, and the reason this guard is TWO guards.
  // The AM5 woke the home; this page adopted the wake and the v8.49 hold was
  // correctly withholding green on `degraded` polls — while caching a bare
  // "up". A resume (trivial to trigger on desktop Chrome: any click back into
  // the window) then pre-painted that cache, and setOnline() does more than
  // paint: it clears `remoteWaking` and stops the countdown, so every later
  // degraded poll fell out of the hold too. Green landed 29 s before Seerr
  // actually answered, and stayed. So: a degraded cache is never a confident
  // green (guard 1), and a pre-paint never ends a wake that is still in flight
  // (guard 2) — the live probe below is what gets to settle it.
  if(cached&&cached.up&&!cached.degraded&&!((wolSent||remoteWaking)&&countdownTimer)){
    relayReachable=cached.relayOk!==false;
    setOnline();
    logPaint('online','cache-prepaint-resume','cache='+Math.round((Date.now()-cached.t)/1000)+'s');
  } else if((wolSent||remoteWaking)&&countdownTimer){
    // Wake in flight: the countdown on screen is the honest state. Leave the
    // card alone (hasConfirmedState untouched, so no orange flash over it).
    logPaint('waking','prepaint-held-wake-in-flight',
             'cache='+(cached?Math.round((Date.now()-cached.t)/1000)+'s':'none')+(cached&&cached.degraded?' degraded':''));
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
  if(wolSent||remoteWaking)acquireWakeLock();
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
        // Same reason as startCountdown(): inline !important so the blanket
        // prefers-reduced-motion `*{transition-duration:.01ms!important}` rule
        // can't beat this value and snap the bar to 100% mid-boot. This resync
        // path was missed by the 2026-08-01b fix — on a Windows PC with
        // animations off, the first focus/visibilitychange after the wake
        // re-armed the bar here WITHOUT !important and it jumped straight to
        // full, reading as "finished" while the server was still booting.
        bar.style.setProperty('transition','width '+(remainingMs/1000)+'s linear','important');
        bar.style.width='100%';
      }
    }
  }
}
// v8.65 — react to the ONE thing we know first-hand. Losing the radio used to be
// noticed only when the next 8 s poll happened to settle, so the card could sit
// on a green (or on "Statut inconnu") while the phone had no network at all —
// and recovery waited for the poll too. Both are facts, not verdicts, so they
// are painted / re-probed at once. Guarded on a wake, which owns the card.
window.addEventListener('offline',function(){
  if(!config||wolSent||remoteWaking)return;
  hasConfirmedState=false;setOffline();
  logPaint('no-network','radio-lost');
});
window.addEventListener('online',function(){if(config)checkStatus();});
window.addEventListener('focus',onForeground);
document.addEventListener('visibilitychange',function(){if(!document.hidden)onForeground();});
// v7.9 — fast-path visibility-transition poll (1 s). Reads document.hidden
// directly and triggers onForeground on a hidden→visible flip. Absolute safety
// net for the Android PWA standalone case where neither focus nor
// visibilitychange fires reliably on app-switcher resume — the IRL bug behind
// "il faut attendre au moins 15 s pour voir le statut passer à rouge". The
// The STATUS_POLL_INTERVAL_MS (8 s) self-healing tick is still the eventual
// catch-up; this poll cuts the worst case from 8 s down to ~1 s without depending
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

// Wire up the button handlers (migrated from inline onclick="..." attributes
// so the CSP can drop 'unsafe-inline' from script-src — see <meta http-equiv
// "Content-Security-Policy"> in index.html).
document.getElementById('testRelayBtn').addEventListener('click',function(){testRelay(this);});
document.getElementById('cancelBtn').addEventListener('click',cancelSettings);
document.getElementById('backBtn').addEventListener('click',cancelSettings);
document.getElementById('saveBtn').addEventListener('click',saveConfig);
document.getElementById('powerBtn').addEventListener('click',sendWol);

// Footer version, derived from the active SW cache name (see version.js — the
// same parser serves the debug and manual-wake pages; it used to be copied into
// all three). Leaves the placeholder alone when no cache of ours exists, rather
// than claiming a version we cannot read.
if(window.withCacheLabel){
  withCacheLabel(function(label){
    var el=document.getElementById('footerVersion');
    if(el)el.textContent=label;
  });
}

// Init: URL params > localStorage > settings screen
if(!readUrlParams())config=loadConfig();
if(config&&config.host)startApp();
else showSettings();
