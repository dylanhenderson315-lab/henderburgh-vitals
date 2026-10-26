/* Streaming services — shared by /ipad (What to watch row) and /streaming.

   `open` is the service's universal link: on iPad it opens the installed app,
   and falls back to Safari when the app isn't installed, so a tap never dead-ends.
   `tv` is the Android/Google TV package launched via Home Assistant's Android TV
   Remote integration (remote.turn_on activity=<package>) on the living room /
   theater / bedroom Google TVs.
   `perk` marks the services expected via the Verizon myPlan bundle — still to
   be confirmed by the owner; flip `confirmed` to true as each one is checked. */
(function () {
  'use strict';
  window.HBStreaming = [
    { key: 'netflix',   name: 'Netflix',          tint: '229,9,20',    open: 'https://www.netflix.com/browse',      tv: 'com.netflix.ninja',                      perk: true,  confirmed: false },
    { key: 'max',       name: 'Max',              tint: '0,40,255',    open: 'https://play.max.com',                tv: 'com.wbd.stream',                         perk: true,  confirmed: false },
    { key: 'disney',    name: 'Disney+',          tint: '17,60,207',   open: 'https://www.disneyplus.com',          tv: 'com.disney.disneyplus',                  perk: true,  confirmed: false },
    { key: 'hulu',      name: 'Hulu',             tint: '28,231,131',  open: 'https://www.hulu.com/hub/home',       tv: 'com.hulu.livingroomplus',                perk: true,  confirmed: false },
    { key: 'espn',      name: 'ESPN',             tint: '204,0,0',     open: 'https://www.espn.com/watch/',         tv: 'com.espn.gtv',                           perk: true,  confirmed: false },
    { key: 'peacock',   name: 'Peacock',          tint: '250,200,40',  open: 'https://www.peacocktv.com/watch/home', tv: 'com.peacocktv.peacockandroid',          perk: true,  confirmed: false },
    { key: 'paramount', name: 'Paramount+',       tint: '0,100,255',   open: 'https://www.paramountplus.com/home/', tv: 'com.cbs.ott',                            perk: true,  confirmed: false },
    { key: 'youtube',   name: 'YouTube',          tint: '255,0,0',     open: 'https://www.youtube.com',             tv: 'com.google.android.youtube.tv',          perk: true,  confirmed: false },
    { key: 'ytmusic',   name: 'YouTube Music',    tint: '255,40,40',   open: 'https://music.youtube.com',           tv: 'com.google.android.youtube.tvmusic',     perk: true,  confirmed: false },
    { key: 'applemusic',name: 'Apple Music',      tint: '250,45,85',   open: 'https://music.apple.com',             tv: null,                                     perk: true,  confirmed: false },
    { key: 'appletv',   name: 'Apple TV+',        tint: '200,200,210', open: 'https://tv.apple.com',                tv: 'com.apple.atve.androidtv.appletv',       perk: false, confirmed: false },
  ];
})();
