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
    { key: 'appletv',   name: 'Apple TV+',        tint: '200,200,210', open: 'https://tv.apple.com',                tv: 'https://tv.apple.com', pkg: 'com.apple.atve.androidtv.appletv',       perk: false, confirmed: false },
  ];
})();
