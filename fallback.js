var p = new URLSearchParams(window.location.search);
var mac = p.get('mac') || '';
var host = p.get('host') || '';
var port = p.get('port') || '9';

function formatMac(m) {
  if (!m || !/^[0-9a-fA-F]{12}$/.test(m)) return m || '—';
  return m.match(/.{2}/g).join(':').toUpperCase();
}

var macFmt = formatMac(mac);
var macEl = document.getElementById('paramMac');
var hostEl = document.getElementById('paramHost');
var portEl = document.getElementById('paramPort');

macEl.textContent = macFmt;
macEl.dataset.copy = macFmt;
hostEl.textContent = host || '—';
hostEl.dataset.copy = host || '';
portEl.textContent = port;
portEl.dataset.copy = port;

// Update the Windows PowerShell command
var psLine = document.getElementById('psLine');
if (psLine) {
  var psMac = /^[0-9a-fA-F]{12}$/.test(mac) ? mac.toUpperCase() : 'AABBCCDDEEFF';
  var psHost = host || 'myserver.example.com';
  psLine.textContent = "$mac=[byte[]]-split('" + psMac + "' -replace '..','0x$0 ');"
    + "$u=New-Object Net.Sockets.UdpClient;$u.Connect('" + psHost + "'," + port + ");"
    + "$u.Send(([byte[]](,0xFF*6)+($mac*16)),102)|Out-Null";
}

// Update the Linux/macOS command line
var cmdLine = document.getElementById('cmdLine');
if (cmdLine) {
  var cmdMac = macFmt && macFmt !== '—' ? macFmt : 'AA:BB:CC:DD:EE:FF';
  var cmdHost = host || 'myserver.example.com';
  cmdLine.textContent = 'wakeonlan -i ' + cmdHost + ' -p ' + port + ' ' + cmdMac;
}

// Click-to-copy on parameter values.
// On clipboard failure (API unavailable, permission denied, insecure context),
// surface a visible ✕ + hint so the user knows to select manually instead of
// silently doing nothing.
function flagCopyFail(el) {
  el.classList.add('copy-fail');
  el.parentNode.classList.add('copy-failed');
  setTimeout(function() {
    el.classList.remove('copy-fail');
    el.parentNode.classList.remove('copy-failed');
  }, 3000);
}

// Derive footer version from the active SW cache name. Single source
// of truth (sw.js) — mirrors the pattern in app.js and debug.js.
if(window.caches){
  caches.keys().then(function(names){
    var ours=names.filter(function(n){return n.indexOf('plex-jqh-omv')===0;});
    var m=ours[0]&&ours[0].match(/-v(\d+\.\d+)$/);
    var el=document.getElementById('footerVersion');
    if(el&&m)el.textContent='v'+m[1];
  }).catch(function(){});
}

document.querySelectorAll('.param code').forEach(function(el) {
  el.addEventListener('click', function() {
    var value = el.dataset.copy;
    if (!value || value === '—') return;
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      flagCopyFail(el);
      return;
    }
    navigator.clipboard.writeText(value).then(function() {
      el.classList.add('copied');
      setTimeout(function() { el.classList.remove('copied'); }, 1500);
    }).catch(function() {
      flagCopyFail(el);
    });
  });
});
