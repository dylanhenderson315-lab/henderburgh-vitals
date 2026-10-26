/* Streaming services — shared by /hub (Now + Watch spaces) and /streaming.

   `open` is the service's universal link: on iPad it opens the installed app,
   and falls back to Safari when the app isn't installed, so a tap never dead-ends.
   `tv` is the app link sent to Home Assistant's Android TV Remote integration
   (remote.turn_on activity=<link>) on the Google TVs. It must be a link, not a
   package: the TVs ignore bare package names (verified 2026-10-25 on the Living
   Room TV — the package was a silent no-op, https://www.youtube.com opened
   YouTube). `pkg` is the Android package the TV then reports as its current
   activity, used to name what's on screen and to confirm a launch landed.
   `perk` marks the services expected via the Verizon myPlan bundle — still to
   be confirmed by the owner; flip `confirmed` to true as each one is checked. */
(function () {
  'use strict';
  window.HBStreaming = [
    { key: 'netflix',   name: 'Netflix',          tint: '229,9,20',    open: 'https://www.netflix.com/browse',      tv: 'https://www.netflix.com/title', pkg: 'com.netflix.ninja',                      perk: true,  confirmed: false },
    { key: 'max',       name: 'Max',              tint: '0,40,255',    open: 'https://play.max.com',                tv: 'https://play.max.com', pkg: 'com.wbd.stream',                         perk: true,  confirmed: false },
    { key: 'disney',    name: 'Disney+',          tint: '17,60,207',   open: 'https://www.disneyplus.com',          tv: 'https://www.disneyplus.com', pkg: 'com.disney.disneyplus',                  perk: true,  confirmed: false },
    { key: 'hulu',      name: 'Hulu',             tint: '28,231,131',  open: 'https://www.hulu.com/hub/home',       tv: 'https://www.hulu.com', pkg: 'com.hulu.livingroomplus',                perk: true,  confirmed: false },
    { key: 'espn',      name: 'ESPN',             tint: '204,0,0',     open: 'https://www.espn.com/watch/',         tv: 'https://www.espn.com/watch/', pkg: 'com.espn.gtv',                           perk: true,  confirmed: false },
    { key: 'peacock',   name: 'Peacock',          tint: '250,200,40',  open: 'https://www.peacocktv.com/watch/home', tv: 'https://www.peacocktv.com', pkg: 'com.peacocktv.peacockandroid',          perk: true,  confirmed: false },
    { key: 'paramount', name: 'Paramount+',       tint: '0,100,255',   open: 'https://www.paramountplus.com/home/', tv: 'https://www.paramountplus.com', pkg: 'com.cbs.ott',                            perk: true,  confirmed: false },
    { key: 'youtube',   name: 'YouTube',          tint: '255,0,0',     open: 'https://www.youtube.com',             tv: 'https://www.youtube.com', pkg: 'com.google.android.youtube.tv',          perk: true,  confirmed: false },
    { key: 'ytmusic',   name: 'YouTube Music',    tint: '255,40,40',   open: 'https://music.youtube.com',           tv: 'https://music.youtube.com', pkg: 'com.google.android.youtube.tvmusic',     perk: true,  confirmed: false },
    { key: 'applemusic',name: 'Apple Music',      tint: '250,45,85',   open: 'https://music.apple.com',             tv: null,                                     perk: true,  confirmed: false },
    // Free, legal, ad-supported (FMHY's "Free w/ Ads" list — the legal part of
    // that directory). Packages unconfirmed on these TVs, so a launch is
    // confirmed by the TV changing apps rather than by a package match.
    { key: 'tubi',      name: 'Tubi',             tint: '255,80,30',   open: 'https://tubitv.com',                  tv: 'https://tubitv.com', pkg: null, free: true, perk: false, confirmed: false },
    { key: 'pluto',     name: 'Pluto TV',         tint: '255,230,0',   open: 'https://pluto.tv/live-tv',            tv: 'https://pluto.tv', pkg: null, free: true, perk: false, confirmed: false },
    { key: 'roku',      name: 'The Roku Channel', tint: '110,40,200',  open: 'https://therokuchannel.roku.com',     tv: 'https://therokuchannel.roku.com', pkg: null, free: true, perk: false, confirmed: false },
    { key: 'plex',      name: 'Plex',             tint: '229,160,13',  open: 'https://watch.plex.tv',               tv: 'https://watch.plex.tv', pkg: null, free: true, perk: false, confirmed: false },
    { key: 'appletv',   name: 'Apple TV+',        tint: '200,200,210', open: 'https://tv.apple.com',                tv: 'https://tv.apple.com', pkg: 'com.apple.atve.androidtv.appletv',       perk: false, confirmed: false },
  ];

  // What a TV's reported activity means in words: a service name, or "Home
  // screen" for the TVs' own launchers / screensavers.
  const BY_PKG = Object.fromEntries(window.HBStreaming.filter((s) => s.pkg).map((s) => [s.pkg, s.name]));
  const HOME_APPS = /tvcenter|com\.tcl\.tv$|launcher|dreamx|screensaver/i;
  window.HBTvApp = function (app) {
    if (!app) return null;
    if (HOME_APPS.test(app)) return 'Home screen';
    return BY_PKG[app] || app.split('.').pop().replace(/^\w/, (c) => c.toUpperCase());
  };
})();
