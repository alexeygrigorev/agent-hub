/* Compatibility adapter for bookmarks and inbound links to the former Fleet page. */
(() => {
  // Session launch and engine overrides are handled by the Sessions flow;
  // its policy requires a reason ("Engine override requires an explanation").
  const hash=location.hash.slice(1).toLowerCase();
  let target='/quota.html';
  if(['quota','usage','usage-quota'].includes(hash)) target='/quota.html';
  else if(['checks','registry','accounts','settings','policy','routing-policy'].includes(hash)) {
    const panel=hash==='checks'?'checks':hash==='registry'||hash==='accounts'?'accounts':hash==='policy'||hash==='routing-policy'?'policy':'';
    target=`/settings.html${panel?`?panel=${panel}`:''}`;
  }
  else if(hash==='launch') target='/sessions.html?new=1';
  else if(['live-sessions','sessions','work-activity','hub-agents','registered-agents','agents'].includes(hash)) target='/sessions.html';
  else if(['alerts','liveness'].includes(hash)) target='/sessions.html?view=attention';
  else if(['host-health','health','disk','resource-management'].includes(hash)) target='/machines.html#machine-history';
  else if(['machine-view','machines'].includes(hash)) target='/machines.html';
  const status=document.getElementById('fleet-redirect-status');
  if(status)status.textContent=`Opening ${target.startsWith('/quota')?'Quota':target.startsWith('/settings')?'Settings':'Machines'}…`;
  const destination=new URL(target,location.origin);
  new URLSearchParams(location.search).forEach((value,key)=>destination.searchParams.append(key,value));
  location.replace(destination.href);
})();
