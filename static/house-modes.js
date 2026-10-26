/* House modes — the owner's REAL lighting routines, one tap each.
   Single source of truth for /home-assistant and /ipad (moved verbatim out of
   home-assistant.html; keep the history comments with the data).

   HBHouseModes.build({ allLightIds }) -> { key: { icon, name, desc, run } }
   run() returns steps: [entity_ids, payload] (light.turn_on) or
   [entity_ids, null, 'turn_off']. allLightIds is the caller's live list so
   "everything" always means the lights the page currently knows about. */
(function () {
  'use strict';

  const DESK_BARS = 'light.wiz_rgbw_tunable_ef497e';   // "Desk Light Bars"

  function build({ allLightIds }) {
    // Rebuilt from 32 days of real usage (see /insights) instead of guessed —
    // the original six modes were invented and several contradicted actual
    // behavior outright (Gaming's "rich violet" never once appears in the
    // data; real gaming sessions run Party, identical to the old Work Mode,
    // so those two collapsed into one honest button). Movie Time had no real
    // signal at all and is gone rather than kept as a guess. Wind-Down's
    // "dim everything to 30%" contradicted TWO separate real behaviors (warm
    // white at real 76-97% brightness in the evening, then fully OFF at
    // bedtime) — split into Evening + Bedtime instead of averaging them into
    // a state nobody actually uses.
    return {
      'desk-on':   { icon: 'fa-bolt',        name: 'Desk Lights On', desc: 'Party — Desk Bars, Bookshelf + Desk Lamp',
                     run: () => [[[DESK_BARS, 'light.bookshelf_lamp', 'light.office_lamp'], { effect: 'Party' }]] },
      'meeting':   { icon: 'fa-video',       name: 'Meeting Mode',   desc: 'Candlelight, dimmed — no colors shifting',
                     // Exact live values confirmed by the owner (Desk Bars +
                     // Bookshelf 58%, Desk Lamp 40%) — Candlelight on all three.
                     run: () => [[[DESK_BARS, 'light.bookshelf_lamp'], { effect: 'Candlelight', brightness_pct: 58 }],
                                 [['light.office_lamp'], { effect: 'Candlelight', brightness_pct: 40 }]] },
      'evening':   { icon: 'fa-couch',       name: 'Evening',        desc: 'Warm white — Hallway, Kitchen, Living Room',
                     run: () => [[['light.hallway_light_1', 'light.hallway_light_2', 'light.hallway_light_3', 'light.hallway_light_4'],
                                   { effect: 'Warm white', brightness_pct: 76 }],
                                 [['light.kitchen_light_left', 'light.kitchen_light_right'],
                                   { effect: 'Warm white', brightness_pct: 97 }],
                                 [['light.living_room_lamp'], { effect: 'Warm white', brightness_pct: 65 }]] },
      // The real bedtime state: everything off EXCEPT the two bedroom TV
      // lamps, which go to dim Fireplace — matches the owner's own
      // description exactly ("just the 2 lamps in very dim fireplace mode,
      // the only lights on") and the data (Tv Lamp 1/2 -> Fireplace 99-100%
      // of the time they're on, at 26-32% brightness).
      'bedtime':   { icon: 'fa-bed',         name: 'Bedtime',        desc: 'Everything off — Tv Lamps stay on, dim Fireplace',
                     run: () => [[allLightIds().filter(id => id !== 'light.tv_lamp_1' && id !== 'light.tv_lamp_2'), null, 'turn_off'],
                                 [['light.tv_lamp_1'], { effect: 'Fireplace', brightness_pct: 32 }],
                                 [['light.tv_lamp_2'], { effect: 'Fireplace', brightness_pct: 26 }]] },
      'lights-out':{ icon: 'fa-power-off',   name: 'Lights Out',     desc: 'Everything off, one tap',
                     run: () => [[allLightIds(), null, 'turn_off']] },
    };
  }

  window.HBHouseModes = { build, DESK_BARS };
})();
