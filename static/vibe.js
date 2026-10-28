/* "Tell the house" — the intent engine, shared by the lighting page and the hub.
   You don't operate lights, you say what you want. Deterministic, honest
   parsing (rooms + scenes + colors + moods + brightness + modes, "except …",
   "X and Y") -> a plan the page executes. Moved here unchanged from
   home-assistant.html (2026-10-27); each page passes a context:
     { rooms(): [{name}], roomLightIds(name): [ids], allLightIds(): [ids], modes: {key: {name}} }
   parse(text, ctx) -> plan | null;  compound(text, ctx) -> [planA, planB] | null. */
(function () {
  'use strict';
  const VIBE_FX = [
    { re: /(thunder|storm|rain)/i,     fx: 'thunderstorm', label: 'Thunderstorm ⛈' },
    { re: /(snow|winter|blizzard)/i,   fx: 'snowy-sky',    label: 'Snowy Sky ❄️' },
    { re: /(aurora|northern light)/i,  fx: 'aurora',       label: 'Aurora 🌌' },
    { re: /lava/i,                     fx: 'lava-lamp',    label: 'Lava Lamp 🌋' },
  ];
  const VIBE_EFFECTS = [
    { re: /(fireplace|campfire|cabin|fire)/i,   effect: 'Fireplace' },
    { re: /(ocean|sea|underwater|waves)/i,      effect: 'Ocean' },
    { re: /(deep ?dive|dive)/i,                 effect: 'Deep dive' },
    { re: /\bclub\b/i,                          effect: 'Club' },
    { re: /(party|dance)/i,                     effect: 'Party' },
    { re: /(christmas|xmas|holiday)/i,          effect: 'Christmas' },
    { re: /(halloween|spooky)/i,                effect: 'Halloween' },
    { re: /(romantic|romance|date night)/i,     effect: 'Romance' },
    { re: /candle/i,                            effect: 'Candlelight' },
    { re: /(forest|woods)/i,                    effect: 'Forest' },
    { re: /(jungle|rainforest)/i,               effect: 'Jungle' },
    { re: /(sunset|golden hour)/i,              effect: 'Sunset' },
    { re: /(sunrise|wake ?up)/i,                effect: 'Wake-up' },
    { re: /(movie|film|tv time)/i,              effect: 'TV time' },
    { re: /(mojito|tropical|beach)/i,           effect: 'Mojito' },
    { re: /pastel/i,                            effect: 'Pastel colors' },
    { re: /(steampunk|vintage)/i,               effect: 'Steampunk' },
    { re: /(pulse|heartbeat)/i,                 effect: 'Pulse' },
    { re: /diwali/i,                            effect: 'Diwali' },
    { re: /(plant|grow)/i,                      effect: 'Plantgrowth' },
    { re: /spring/i,                            effect: 'Spring' },
    { re: /summer/i,                            effect: 'Summer' },
    { re: /(fall|autumn)/i,                     effect: 'Fall' },
  ];
  const VIBE_COLORS = [
    { re: /(purple|violet)/i,      rgb: [140, 60, 235], name: 'purple' },
    { re: /(lavender|lilac)/i,     rgb: [190, 150, 255], name: 'lavender' },
    { re: /(crimson|blood)/i,      rgb: [180, 10, 40],  name: 'crimson' },
    { re: /\bred\b/i,              rgb: [255, 30, 30],  name: 'red' },
    { re: /(ice|icy) ?blue/i,      rgb: [160, 210, 255], name: 'ice blue' },
    { re: /\bblue\b/i,             rgb: [40, 90, 255],  name: 'blue' },
    { re: /(lime|neon green)/i,    rgb: [140, 255, 40], name: 'lime' },
    { re: /\bgreen\b/i,            rgb: [30, 200, 90],  name: 'green' },
    { re: /(magenta|fuchsia)/i,    rgb: [255, 30, 200], name: 'magenta' },
    { re: /(rose|blush)/i,         rgb: [255, 120, 150], name: 'rose' },
    { re: /\bpink\b/i,             rgb: [255, 80, 180], name: 'pink' },
    { re: /(orange|amber)/i,       rgb: [255, 140, 20], name: 'amber' },
    { re: /(teal|cyan|turquoise)/i, rgb: [20, 200, 200], name: 'teal' },
    { re: /(gold|yellow)/i,        rgb: [255, 200, 40], name: 'gold' },
    { re: /\bwhite\b/i,            kelvin: 4000,        name: 'white' },
  ];
  const VIBE_MOODS = [
    { re: /(cozy|calm|chill|relax(ing)?|warm|hygge)/i,      kelvin: 2700, bri: 35, name: 'warm & cozy' },
    { re: /(bright|crisp|energi[sz]e|awake|morning)/i,      kelvin: 5000, bri: 85, name: 'bright & crisp' },
    { re: /(meeting|neutral|steady|no colors?|on camera)/i, kelvin: 4200, bri: 75, name: 'steady white' },
    { re: /(focus|study|reading|concentrate)/i,             kelvin: 4600, bri: 80, name: 'focus' },
    { re: /(dim|dark|low|soft|moody|gloomy)/i,              bri: 14,      name: 'dim' },
    { re: /(sleep|bed ?time|night ?light|nighttime)/i,      kelvin: 2200, bri: 10, name: 'night-low' },
  ];
  const VIBE_LEVELS = [
    { re: /(max|full|100)/i,      pct: 100 }, { re: /(half|medium)/i, pct: 50 },
    { re: /(minimum|barely)/i,    pct: 5 },   { re: /quarter/i,       pct: 25 },
  ];

  // House Mode names, so you can just TYPE a mode ("work mode", "movie time").
  // Requires the word "mode"/distinct phrasing so it never steals plain moods
  // like "steady white for work" — only "work mode" triggers the House Mode.
  const MODE_ALIASES = [
    // Work Mode and Gaming Mode both route here — real data shows no
    // distinct pattern between them, both are just Party on the desk lights.
    { re: /\b(desk\s*lights?\s*on|work\s*mode|gaming\s*mode)\b/i, key: 'desk-on' },
    { re: /\bmeeting\s*mode\b/i,                                  key: 'meeting' },
    { re: /\bevening\b/i,                                         key: 'evening' },
    // "wind down" still works as a phrase — it now routes to the real
    // bedtime state instead of the old invented dim-everything guess.
    { re: /\b(bed\s*time|wind[\s-]?down)\b/i,                     key: 'bedtime' },
    { re: /\blights?\s*out\b/i,                                   key: 'lights-out' },
  ];

  // Target matcher: ALL matching rooms/groups are unioned ("kitchen and bedroom"
  // hits both). Returns empty when nothing matches (callers decide the default).
  function matchTargets(text, ctx) {
    const t = (text || '').toLowerCase();
    const ids = new Set(); const labels = [];
    if (/(desk|office)/.test(t)) { ['light.wiz_rgbw_tunable_ef497e', 'light.office_lamp'].forEach(i => ids.add(i)); labels.push('Desk lights'); }
    if (/tv lights?|backlight/.test(t)) { ['light.t_v_lights', 'light.living_room_tv_lights'].forEach(i => ids.add(i)); labels.push('TV lights'); }
    if (/nicole/.test(t)) { ctx.roomLightIds('3rd Bedroom').forEach(i => ids.add(i)); labels.push("3rd Bedroom"); }
    for (const room of (ctx.rooms() || [])) {
      if (t.includes((room.name || '').toLowerCase())) { ctx.roomLightIds(room.name).forEach(i => ids.add(i)); labels.push(room.name); }
    }
    return { ids, labels };
  }

  function parseVibe(text, ctx) {
    // House Mode by name — "work mode", "movie time", "lights out" all route
    // straight to the mode engine (which handles its own snapshot + undo).
    const modeHit = MODE_ALIASES.find(m => m.re.test(text || ''));
    if (modeHit && ctx.modes && ctx.modes[modeHit.key]) {
      return { mode: modeHit.key, whereLabel: 'Modes', label: ctx.modes[modeHit.key].name };
    }
    // Exclusion clause: "… except/but not/besides/excluding/other than/apart from X".
    // The action + targets are parsed from the MAIN clause only — otherwise
    // "turn off all lights except bedroom" would match Bedroom as the target
    // and do the exact opposite of the intent.
    const exSplit = text.split(/\b(?:except|besides|excluding|other than|apart from|but not)\b/i);
    const main = exSplit[0];
    const exclusion = exSplit.slice(1).join(' ');

    const mainTargets = matchTargets(main, ctx);
    let ids, whereLabel;
    if (mainTargets.ids.size) { ids = [...mainTargets.ids]; whereLabel = mainTargets.labels.join(' + '); }
    else { ids = ctx.allLightIds(); whereLabel = 'Whole house'; }

    if (exclusion.trim()) {
      const ex = matchTargets(exclusion, ctx);
      if (!ex.ids.size) return null;   // "except <something we don't know>" — be honest, don't guess
      ids = ids.filter(i => !ex.ids.has(i));
      whereLabel += ` except ${ex.labels.join(' + ')}`;
      if (!ids.length) return null;    // exclusion swallowed everything
    }
    const where = { ids, label: whereLabel };
    text = main;   // actions (off/on, colors, moods, scenes) come from the main clause
    if (!where.ids.length) return null;

    // on/off verbs first — the most common commands of all
    if (/\b(off|out|kill|shut)\b/i.test(text) && !/off\s*white/i.test(text)) {
      return { ids: where.ids, whereLabel: where.label, service: 'turn_off', label: 'off' };
    }
    const fx = VIBE_FX.find(v => v.re.test(text));
    if (fx) return { ids: where.ids, whereLabel: where.label, fx: fx.fx, label: fx.label };

    const eff = VIBE_EFFECTS.find(v => v.re.test(text));
    const color = VIBE_COLORS.find(v => v.re.test(text));
    const mood = VIBE_MOODS.find(v => v.re.test(text));
    const pctMatch = text.match(/(\d{1,3})\s*%/);
    const level = pctMatch ? null : VIBE_LEVELS.find(v => v.re.test(text));
    const payload = {};
    let parts = [];
    if (eff) { payload.effect = eff.effect; parts.push(eff.effect); }
    else if (color && color.rgb) { payload.rgb_color = color.rgb; parts.push(color.name); }
    else if (color && color.kelvin) { payload.color_temp_kelvin = color.kelvin; parts.push(color.name); }
    else if (mood && mood.kelvin) { payload.color_temp_kelvin = mood.kelvin; }
    if (mood) { if (!payload.effect && mood.kelvin && !color) parts.push(mood.name); if (mood.bri != null) payload.brightness_pct = mood.bri; }
    if (pctMatch) payload.brightness_pct = Math.max(1, Math.min(100, parseInt(pctMatch[1], 10)));
    else if (level) payload.brightness_pct = level.pct;
    if (payload.brightness_pct != null && (pctMatch || level || !parts.length)) parts.push(`${payload.brightness_pct}%`);
    // bare "turn on the kitchen" -> gentle warm on
    if (!Object.keys(payload).length && /\bon\b/i.test(text)) {
      return { ids: where.ids, whereLabel: where.label, payload: { color_temp_kelvin: 3200, brightness_pct: 60 }, label: 'on · warm' };
    }
    if (!Object.keys(payload).length) return null;
    return { ids: where.ids, whereLabel: where.label, payload, label: parts.join(' · ') };
  }

  // Compound: "turn all lights on and game room off" — two clauses joined by
  // "and" that hit DIFFERENT lights. Descriptors like "warm and cozy" hit the
  // same target set, so they fall through to the normal single-plan parse.
  function compound(text, ctx) {
    const andParts = text.split(/\s+and\s+/i);
    if (andParts.length !== 2) return null;
    const pa = parseVibe(andParts[0], ctx), pb = parseVibe(andParts[1], ctx);
    if (pa && pb && !pa.mode && !pb.mode && pa.ids && pb.ids &&
        JSON.stringify([...pa.ids].sort()) !== JSON.stringify([...pb.ids].sort())) return [pa, pb];
    return null;
  }

  window.HBVibe = { parse: parseVibe, compound, matchTargets, MODE_ALIASES, VIBE_FX };
})();
