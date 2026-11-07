(function (global) {
  const STOP = new Set(["a", "an", "the", "of", "in", "at", "for", "to", "me", "it", "is", "s", "what", "whats", "time", "now", "current", "local", "clock", "show"]);
  const SHORT = new Set(["us", "uk", "uae", "la", "sf", "kl", "hk", "ksa", "usa", "nyc", "drc"]);
  const GROUPS = [
    ["usa", "us", "united states", "united states of america", "america"],
    ["uk", "united kingdom", "britain", "great britain"],
    ["uae", "united arab emirates", "emirates"],
    ["ksa", "saudi arabia"],
    ["south korea", "korea", "republic of korea"],
    ["czechia", "czech republic"],
    ["netherlands", "holland"],
    ["myanmar", "burma"],
    ["eswatini", "swaziland"],
    ["north macedonia", "macedonia"],
    ["turkiye", "turkey"],
    ["cote d ivoire", "ivory coast"],
    ["timor leste", "east timor"],
    ["cabo verde", "cape verde"],
    ["democratic republic of the congo", "dr congo", "drc"],
    ["vatican city", "vatican", "holy see"],
    ["new york city", "new york", "nyc"],
    ["los angeles", "la"],
    ["san francisco", "sf"],
    ["kuala lumpur", "kl"],
    ["hong kong", "hk"],
    ["mumbai", "bombay"],
    ["kolkata", "calcutta"],
    ["chennai", "madras"],
    ["bengaluru", "bangalore"],
    ["yangon", "rangoon"],
    ["kyiv", "kiev"],
    ["beijing", "peking"],
    ["ho chi minh city", "saigon"],
    ["istanbul", "constantinople"],
    ["cologne", "koln"],
    ["saint petersburg", "st petersburg", "leningrad"],
    ["gothenburg", "goteborg"],
    ["munich", "munchen"],
    ["florence", "firenze"],
    ["rome", "roma"],
    ["naples", "napoli"],
    ["venice", "venezia"],
    ["turin", "torino"],
    ["vienna", "wien"],
    ["prague", "praha"],
    ["warsaw", "warszawa"],
    ["lisbon", "lisboa"],
    ["moscow", "moskva"],
    ["athens", "athina"],
    ["copenhagen", "kobenhavn"],
    ["brussels", "bruxelles"],
    ["seville", "sevilla"],
    ["belgrade", "beograd"],
    ["bucharest", "bucuresti"],
    ["geneva", "geneve"],
    ["the hague", "den haag"],
    ["kathmandu", "katmandu"],
    ["mecca", "makkah"],
    ["medina", "madinah"],
    ["sanaa", "sana a"],
    ["tel aviv", "tel aviv yafo"],
    ["xian", "xi an"]
  ];
  const ALIAS = new Map();
  GROUPS.forEach(g => g.forEach(m => ALIAS.set(m, g.filter(o => o !== m))));
  const LEADING = [
    "what is the current time in ", "what is the time in ", "what time is it in ",
    "current local time in ", "current time in ", "local time in ",
    "show me cities in ", "show cities in ", "find cities in ", "list cities in ",
    "cities in ", "city in ", "places in ", "show me ", "look up ", "search for ",
    "find ", "search ", "show ", "time in ", "clock in ", "the time in "
  ];

  const state = {
    ready: false, promise: null, n: 0, names: [], c: null, r: null, z: null, la: null, lo: null,
    countries: [], regions: [], zones: [], hay: [], countryRows: [], countryIndex: new Map(),
    zoneNorm: [], fuzzy: null
  };

  function normalize(text) {
    return String(text).normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
      .replace(/[_\-\/.,'’]/g, " ").split(/\s+/).filter(Boolean).join(" ");
  }

  async function load(url, onProgress) {
    if (state.promise) return state.promise;
    state.promise = (async () => {
      const res = await fetch(url || "data/places.json");
      if (!res.ok) throw new Error("The city database could not be downloaded.");
      const d = await res.json();
      const n = d.n.length;
      state.names = d.n;
      state.n = n;
      state.c = Uint16Array.from(d.c);
      state.r = Uint16Array.from(d.r);
      state.z = Uint16Array.from(d.z);
      state.la = Float32Array.from(d.la);
      state.lo = Float32Array.from(d.lo);
      state.countries = d.countries;
      state.regions = d.regions;
      state.zones = d.zones;
      const cn = d.countries.map(normalize);
      const rn = d.regions.map(normalize);
      state.zoneNorm = d.zones.map(normalize);
      state.countryRows = d.countries.map(() => []);
      state.countryIndex = new Map();
      cn.forEach((v, i) => { if (v && !state.countryIndex.has(v)) state.countryIndex.set(v, i); });
      const hay = new Array(n);
      const step = 20000;
      for (let s = 0; s < n; s += step) {
        const e = Math.min(n, s + step);
        for (let i = s; i < e; i++) {
          hay[i] = " " + normalize(d.n[i]) + " " + rn[d.r[i]] + " " + cn[d.c[i]];
          state.countryRows[d.c[i]].push(i);
        }
        if (onProgress) onProgress(e / n);
        await new Promise(r => setTimeout(r, 0));
      }
      state.hay = hay;
      state.ready = true;
    })();
    state.promise.catch(() => { state.promise = null; });
    return state.promise;
  }

  function whenReady() { return state.ready ? Promise.resolve() : (state.promise || load()); }
  function isReady() { return state.ready; }
  function info() { return { places: state.n, countries: state.countryIndex.size }; }

  function place(i) {
    const la = state.la[i], lo = state.lo[i];
    return {
      city: state.names[i],
      country: state.countries[state.c[i]],
      zone: state.zones[state.z[i]],
      region: state.regions[state.r[i]],
      latitude: la > 90 ? null : Math.round(la * 1e4) / 1e4,
      longitude: la > 90 ? null : Math.round(lo * 1e4) / 1e4
    };
  }

  function cleanQuery(raw) {
    let text = normalize(raw);
    for (const phrase of LEADING) {
      if (text.startsWith(phrase)) { text = text.slice(phrase.length).trim(); break; }
    }
    return text.split(" ").filter(t => t && t !== "cities" && t !== "city" && t !== "places").join(" ");
  }

  function variants(tokens) {
    let base = tokens.filter(t => !STOP.has(t));
    if (!base.length) base = tokens;
    const phrase = base.join(" ");
    const out = [[base, SHORT.has(phrase) ? 3 : 0]];
    const seen = new Set([base.join("|")]);
    const alts = (ALIAS.get(phrase) || []).map(a => a.split(" "));
    base.forEach((token, pos) => {
      (ALIAS.get(token) || []).forEach(a => alts.push(base.slice(0, pos).concat(a.split(" "), base.slice(pos + 1))));
    });
    for (const t of alts) {
      const key = t.join("|");
      if (!seen.has(key) && out.length < 8) { seen.add(key); out.push([t, 0]); }
    }
    return out;
  }

  function lev(a, b) {
    let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i];
      for (let j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      prev = cur;
    }
    return prev[b.length];
  }

  function closest(token, candidates, count, cutoff) {
    const scored = [];
    for (const c of candidates) {
      if (Math.abs(c.length - token.length) > 2) continue;
      const ratio = 1 - lev(token, c) / Math.max(token.length, c.length);
      if (ratio >= cutoff) scored.push([ratio, c]);
    }
    scored.sort((x, y) => y[0] - x[0]);
    return scored.slice(0, count).map(x => x[1]);
  }

  function buildFuzzy() {
    if (state.fuzzy) return state.fuzzy;
    const byLetter = new Map(), nameIndex = new Map();
    for (let i = 0; i < state.n; i++) {
      const nm = normalize(state.names[i]);
      if (!nm) continue;
      let arr = nameIndex.get(nm);
      if (!arr) {
        arr = [];
        nameIndex.set(nm, arr);
        let b = byLetter.get(nm[0]);
        if (!b) { b = []; byLetter.set(nm[0], b); }
        b.push(nm);
      }
      if (arr.length < 3) arr.push(i);
    }
    state.fuzzy = { byLetter, nameIndex };
    return state.fuzzy;
  }

  function search(query, limit) {
    limit = limit || 30;
    if (!state.ready) return [];
    if (String(query).indexOf("/") !== -1) {
      const text = normalize(query);
      const zoneSet = new Set();
      state.zoneNorm.forEach((z, i) => { if (z.indexOf(text) !== -1) zoneSet.add(i); });
      const found = [];
      for (let i = 0; i < state.n && found.length < limit; i++) if (zoneSet.has(state.z[i])) found.push(place(i));
      return found;
    }
    const text = cleanQuery(query);
    if (!text) return [];
    const options = variants(text.split(" "));
    const pool = new Map();
    const put = (i, level) => { const old = pool.get(i); if (old === undefined || level < old) pool.set(i, level); };
    for (const [tokens, penalty] of options) {
      const joined = tokens.join(" ");
      const ci = state.countryIndex.get(joined);
      if (ci !== undefined) {
        const rows = state.countryRows[ci];
        for (let k = 0; k < Math.min(300, rows.length); k++) put(rows[k], -1);
      }
      const needles = tokens.map(t => " " + t);
      let count = 0;
      for (let i = 0; i < state.n; i++) {
        const hay = state.hay[i];
        let ok = true;
        for (let k = 0; k < needles.length; k++) { if (hay.indexOf(needles[k]) === -1) { ok = false; break; } }
        if (ok) {
          const name = normalize(state.names[i]);
          put(i, (name === joined ? 0 : name.startsWith(tokens[0]) ? 1 : 2) + penalty);
          if (++count >= 800) break;
        }
      }
    }
    const base = options[0][0];
    if (!pool.size && base.length > 1) {
      const needles = base.map(t => " " + t);
      let best = 0, found = [];
      for (let i = 0; i < state.n; i++) {
        let hits = 0;
        for (let k = 0; k < needles.length; k++) if (state.hay[i].indexOf(needles[k]) !== -1) hits++;
        if (hits > best) { best = hits; found = [i]; }
        else if (hits === best && hits > 0 && found.length < 300) found.push(i);
      }
      found.forEach(i => put(i, 5 + base.length - best));
    }
    if (!pool.size) {
      const token = base.reduce((a, b) => (b.length > a.length ? b : a), "");
      if (token.length >= 3) {
        const fz = buildFuzzy();
        for (const nm of closest(token, fz.byLetter.get(token[0]) || [], 6, 0.74)) {
          (fz.nameIndex.get(nm) || []).slice(0, 3).forEach(i => put(i, 6));
        }
        for (const cname of closest(token, Array.from(state.countryIndex.keys()), 3, 0.78)) {
          state.countryRows[state.countryIndex.get(cname)].slice(0, 200).forEach(i => put(i, 7));
        }
      }
    }
    const ranked = Array.from(pool.entries()).sort((a, b) => a[1] - b[1] || a[0] - b[0]);
    const results = [], seen = new Set();
    for (const [i] of ranked) {
      const p = place(i);
      const key = p.city + "|" + p.country + "|" + p.zone + "|" + p.region;
      if (!seen.has(key)) { seen.add(key); results.push(p); }
      if (results.length >= limit) break;
    }
    return results;
  }

  global.TZSearch = { load, whenReady, isReady, search, info, normalize };
})(typeof window !== "undefined" ? window : globalThis);