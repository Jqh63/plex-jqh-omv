function setText(id,val,cls){
  var el=document.getElementById(id);
  if(!el)return;
  el.textContent=val;
  if(cls)el.className=cls;
}

setText('appVersion','v2.28');

if('serviceWorker' in navigator){
  navigator.serviceWorker.getRegistration().then(function(reg){
    if(reg){
      var sw=reg.active||reg.waiting||reg.installing;
      setText('swState',sw?sw.state:'aucun',sw&&sw.state==='activated'?'ok':'');
    } else {
      setText('swState','non enregistré','warn');
    }
  }).catch(function(){setText('swState','erreur lecture','warn');});
  if(window.caches){
    caches.keys().then(function(names){
      var ours=names.filter(function(n){return n.indexOf('plex-jqh-omv')===0;});
      setText('swCache',ours.join(', ')||'aucun');
    });
  }
} else {
  setText('swState','API absente','warn');
}

try {
  var cfg=JSON.parse(localStorage.getItem('plex-jqh-omv-cfg')||'{}');
  setText('cfgHost',cfg.host||'—');
  setText('cfgMac',cfg.mac?cfg.mac.match(/.{2}/g).join(':').toUpperCase():'—');
  setText('cfgPort',cfg.port||'9');
  setText('cfgApps',cfg.apps||'(défaut)');
} catch(e){
  setText('cfgHost','erreur localStorage','warn');
}

setText('navOnline',navigator.onLine?'oui':'non',navigator.onLine?'ok':'warn');
setText('displayMode',matchMedia('(display-mode:standalone)').matches?'standalone (PWA installée)':'browser');
setText('viewport',window.innerWidth+' × '+window.innerHeight);
setText('lang',navigator.language||'—');
setText('ua',navigator.userAgent);
