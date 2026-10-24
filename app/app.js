/* Sale Lookup: offline library search for the book sale.
 *
 * Two independent halves:
 *   SEARCH  runs only on the copy of the library saved in this phone's IndexedDB. It never waits for the network.
 *   SYNC    runs in the background; it can fail, stall or time out without ever touching the search.
 *
 * Sync in one paragraph: on open (and when the connection returns, and when you come back to the app after
 * a while) fetch the tiny version.json. If its hash equals the saved copy's hash, nothing else happens.
 * If it differs, download the snapshot it names, check it (size, SHA-256, item count, not suspiciously
 * smaller), and only then replace the saved copy in ONE transaction. Any failure keeps the old copy.
 */
(function () {
  'use strict';

  var CFG = Object.assign({
    dataBase: 'data/',
    versionTimeoutMs: 5000,       // the version file is ~200 bytes: if it is not back in 5 s the link is bad
    snapshotTimeoutMs: 60000,     // whole snapshot download (about 0.3 MB compressed for ~4,000 books)
    recheckMs: 10 * 60 * 1000,    // coming back to the app re-checks only if the last check is older than this
    staleDays: 30,                // warn when this phone has not synced for this long
    serverStaleDays: 14,          // warn when the library copy on the server is older than this at sync time
    minKeepRatio: 0.5             // refuse a new copy with under half the titles we already hold
  }, window.STORE_CONFIG || {});

  var $ = function (id) { return document.getElementById(id); };
  var DAY = 86400000;

  /* ------------------------------------------------------------ storage -- */
  var dbPromise = null;
  function db() {
    if (!dbPromise) {
      dbPromise = new Promise(function (resolve, reject) {
        var r = indexedDB.open('sale-lookup', 1);
        r.onupgradeneeded = function () { r.result.createObjectStore('kv'); };
        r.onsuccess = function () { resolve(r.result); };
        r.onerror = function () { reject(r.error); };
      });
    }
    return dbPromise;
  }
  function kvGet(key) {
    return db().then(function (d) {
      return new Promise(function (resolve, reject) {
        var t = d.transaction('kv').objectStore('kv').get(key);
        t.onsuccess = function () { resolve(t.result); };
        t.onerror = function () { reject(t.error); };
      });
    });
  }
  function kvPut(key, value) {
    return db().then(function (d) {
      return new Promise(function (resolve, reject) {
        var tx = d.transaction('kv', 'readwrite');
        tx.objectStore('kv').put(value, key);
        tx.oncomplete = function () { resolve(); };
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error || new Error('aborted')); };
      });
    });
  }

  /* ------------------------------------------------------------- search -- */
  var STOP = { the: 1, a: 1, an: 1, of: 1, and: 1 };

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function norm(s) { return String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/['’]/g, '').replace(/[^a-z0-9]+/g, ' ').trim(); }
  function toks(s) { var n = norm(s); return n ? n.split(' ') : []; }
  function compounds(t) { var o = [], i; for (i = 0; i < t.length - 1; i++) o.push(t[i] + t[i + 1]); return o; }

  /* edit distance with transposition, bails out early past `max` */
  function lev(a, b, max) {
    var m = a.length, n = b.length, pp = null, p = [], i, j, c, v, min;
    if (Math.abs(m - n) > max) return max + 1;
    for (j = 0; j <= n; j++) p[j] = j;
    for (i = 1; i <= m; i++) {
      c = [i]; min = i;
      for (j = 1; j <= n; j++) {
        v = Math.min(p[j] + 1, c[j - 1] + 1, p[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
        if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) v = Math.min(v, pp[j - 2] + 1);
        c[j] = v; if (v < min) min = v;
      }
      if (min > max) return max + 1;
      pp = p; p = c;
    }
    return p[n];
  }
  /* how well does typed token q match word w: exact > prefix > typo */
  function mt(q, w) {
    var allow, d;
    if (w === q) return 3;
    if (w.indexOf(q) === 0) return q.length >= 2 ? 2.5 : 1.5;
    if (q.length >= 4) {
      allow = q.length >= 8 ? 2 : 1;
      if (w.length >= q.length - 1 && lev(q, w.slice(0, q.length), 1) <= 1) return 1.6;
      if (w.length >= 4) { d = lev(q, w, allow); if (d <= allow) return d === 1 ? 1.5 : 1.2; }
    }
    return 0;
  }
  function prepare(it) {
    it.tt = toks(it.t); it.at = toks(it.a);
    it.tw = it.tt.concat(compounds(it.tt)); it.aw = it.at.concat(compounds(it.at));
    it.tk = (it.tt.length > 1 && STOP[it.tt[0]]) ? it.tt.slice(1) : it.tt;
    it.gw = toks((it.g || []).join(' ') + ' ' + (it.r || ''));       // tags + series: searchable, lower weight
    it.isMedia = it.m !== 'book';
    return it;
  }
  function queryTokens(raw) {
    var all = toks(raw), k = all.filter(function (t) { return !STOP[t]; });
    return k.length ? k : all;
  }
  function score(it, qt, relaxed) {
    var total = 0, inTitle = false, hits = 0, i, j, q, bt, ba, bg, best, tn, qn;
    for (i = 0; i < qt.length; i++) {
      q = qt[i]; bt = 0; ba = 0; bg = 0;
      for (j = 0; j < it.tw.length; j++) bt = Math.max(bt, mt(q, it.tw[j]));
      for (j = 0; j < it.aw.length; j++) ba = Math.max(ba, mt(q, it.aw[j]));
      for (j = 0; j < it.gw.length; j++) bg = Math.max(bg, mt(q, it.gw[j]));
      best = Math.max(bt * 1.4, ba, bg * 0.6);
      if (best === 0) { if (relaxed) continue; return null; }
      hits++;
      if (bt > 0 && bt * 1.4 >= Math.max(ba, bg * 0.6)) inTitle = true;
      total += best;
    }
    if (!hits) return null;
    tn = it.tk.join(' '); qn = qt.join(' ');
    if (tn === qn) total += 4; else if (tn.indexOf(qn) === 0) total += 2;
    return { total: total, inTitle: inTitle };
  }
  /* books first; within books, title matches before author/tag-only matches; then relevance */
  function search(items, raw) {
    var qt = queryTokens(raw), out = [], relaxed = false, pass;
    if (!qt.length) return { items: [], qt: qt, relaxed: false };
    for (pass = 0; pass < 2 && !out.length; pass++) {
      relaxed = pass === 1;
      items.forEach(function (x) {
        var r = score(x, qt, relaxed);
        if (r) out.push({ it: x, total: r.total, tier: r.inTitle ? 0 : 1 });
      });
    }
    out.sort(function (a, b) {
      return (a.it.isMedia - b.it.isMedia) || (a.tier - b.tier) || (b.total - a.total) ||
        a.it.t.localeCompare(b.it.t) || a.it.id - b.it.id;
    });
    if (relaxed) out = out.slice(0, 8);
    return { items: out.slice(0, 50).map(function (o) { return o.it; }), qt: qt, relaxed: relaxed };
  }
  /* mark the letters that matched, word by word, on the original text */
  function hl(text, qt) {
    var out = '', last = 0, re = /[A-Za-z0-9À-ɏ’']+/g, m, w, nw, i, s, bs, bq, n;
    while ((m = re.exec(text))) {
      out += esc(text.slice(last, m.index));
      w = m[0]; nw = norm(w).replace(/ /g, ''); bs = 0; bq = '';
      for (i = 0; i < qt.length; i++) { s = mt(qt[i], nw); if (s > bs) { bs = s; bq = qt[i]; } }
      if (bs > 0 && nw === bq) out += '<mark>' + esc(w) + '</mark>';
      else if (bs > 0 && nw.indexOf(bq) === 0) { n = Math.min(bq.length, w.length); out += '<mark>' + esc(w.slice(0, n)) + '</mark>' + esc(w.slice(n)); }
      else if (bs > 0) out += '<mark>' + esc(w) + '</mark>';
      else out += esc(w);
      last = m.index + w.length;
    }
    return out + esc(text.slice(last));
  }

  /* ------------------------------------------------------------- state --- */
  var state = {
    items: null,          // prepared items, or null when this phone has no library yet
    lib: null,            // {sha256, count, generated_at, downloaded_at}
    checkedAt: null,      // last time a version check succeeded
    syncing: false,
    lastAttemptAt: 0,
    problem: '',          // why the last sync attempt failed, in plain words ('' = fine)
    saved: null,          // true / false / null = unknown: is the library saved for offline use?
    persisted: null,
    open: {}
  };

  /* -------------------------------------------------------------- sync --- */
  function SyncError(kind, message) { this.kind = kind; this.message = message || kind; }

  function fetchBytes(url, ms) {
    var ac = new AbortController(), timer = setTimeout(function () { ac.abort(); }, ms);
    return fetch(url, { signal: ac.signal, cache: 'no-store', credentials: 'omit' })
      .then(function (res) {
        if (!res.ok) throw new SyncError('http', 'The library server answered with an error (' + res.status + ').');
        return res.arrayBuffer();           // the timer keeps running: it also covers the body download
      })
      .catch(function (e) {
        if (e instanceof SyncError) throw e;
        if (e && e.name === 'AbortError') throw new SyncError('timeout', 'The library server did not answer in time (slow or weak connection).');
        throw new SyncError('network', 'Could not reach the library server (no connection?).');
      })
      .then(function (buf) { clearTimeout(timer); return buf; }, function (e) { clearTimeout(timer); throw e; });
  }
  function sha256hex(buf) {
    return crypto.subtle.digest('SHA-256', buf).then(function (d) {
      return Array.prototype.map.call(new Uint8Array(d), function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
    });
  }
  function parseJSON(buf, what) {
    try { return JSON.parse(new TextDecoder('utf-8').decode(buf)); }
    catch (e) { throw new SyncError('rejected', 'The ' + what + ' file was not readable; keeping your saved copy.'); }
  }
  function checkVersion(v) {
    if (!v || v.format !== 1 || !/^[0-9a-f]{64}$/.test(v.sha256 || '') || !/^snapshot\.[0-9a-f]{12}\.json$/.test(v.file || '') ||
        !Number.isInteger(v.count) || v.count < 1) {
      throw new SyncError('rejected', 'The server\'s version file looks wrong; keeping your saved copy.');
    }
  }
  function checkSnapshot(snap, v) {
    var i, it;
    if (!snap || snap.format !== 1 || !Array.isArray(snap.items) || snap.items.length !== v.count) {
      throw new SyncError('rejected', 'The new library copy did not match its description; keeping your saved copy.');
    }
    for (i = 0; i < Math.min(snap.items.length, 200); i++) {
      it = snap.items[i];
      if (!it || typeof it.t !== 'string' || !Number.isInteger(it.id)) {
        throw new SyncError('rejected', 'The new library copy has damaged entries; keeping your saved copy.');
      }
    }
    if (state.items && snap.items.length < CFG.minKeepRatio * state.items.length) {
      throw new SyncError('rejected', 'The new library copy has far fewer titles (' + snap.items.length + ' vs ' + state.items.length + '); keeping your saved copy.');
    }
  }

  function checkForUpdate(reason) {
    if (state.syncing) return Promise.resolve();
    state.syncing = true; state.lastAttemptAt = Date.now(); renderStatus(); render();
    var version;
    return fetchBytes(CFG.dataBase + 'version.json?t=' + Date.now(), CFG.versionTimeoutMs)
      .then(function (buf) {
        version = parseJSON(buf, 'version'); checkVersion(version);
        if (state.lib && state.lib.sha256 === version.sha256) {
          state.checkedAt = Date.now();
          kvPut('checked', { at: state.checkedAt, sha256: version.sha256 }).catch(function () {});
          state.lib.generated_at = version.generated_at || state.lib.generated_at;
          return null;
        }
        return fetchBytes(CFG.dataBase + version.file, CFG.snapshotTimeoutMs).then(function (buf) {
          if (version.bytes && buf.byteLength !== version.bytes) throw new SyncError('rejected', 'The download was cut short; keeping your saved copy.');
          return sha256hex(buf).then(function (hex) {
            if (hex !== version.sha256) throw new SyncError('rejected', 'The download was damaged (checksum mismatch); keeping your saved copy.');
            var snap = parseJSON(buf, 'library'); checkSnapshot(snap, version);
            return snap;
          });
        });
      })
      .then(function (snap) {
        if (!snap) return;
        var before = state.items ? state.items.length : 0;
        var oldIds = {}; (state.items || []).forEach(function (x) { oldIds[x.id] = 1; });
        var added = snap.items.filter(function (x) { return !oldIds[x.id]; }).length;
        var rec = { sha256: version.sha256, count: snap.items.length, generated_at: version.generated_at, downloaded_at: Date.now(), items: snap.items };
        state.items = snap.items.map(prepare);
        state.lib = { sha256: rec.sha256, count: rec.count, generated_at: rec.generated_at, downloaded_at: rec.downloaded_at };
        state.checkedAt = Date.now();
        return kvPut('library', rec).then(function () { state.saved = true; }, function () { state.saved = false; })
          .then(function () { kvPut('checked', { at: state.checkedAt, sha256: rec.sha256 }).catch(function () {}); })
          .then(function () {
            if (before) toast('Library updated: ' + rec.count.toLocaleString() + ' titles' + (added ? ' (+' + added + ' new)' : ''));
          });
      })
      .then(function () { state.problem = ''; })
      .catch(function (e) { state.problem = (e && e.message) || 'Sync failed.'; })
      .then(function () { state.syncing = false; renderStatus(); render(); });
  }

  /* ---------------------------------------------------------------- UI --- */
  var BIND = {
    'hardcover': ['HARDCOVER', 'Hardcover', 'hc'], 'paperback': ['PAPERBACK', 'Paperback', 'pb'],
    'board book': ['BOARD BOOK', 'Board book', 'other'], 'flexibound': ['FLEXIBOUND', 'Flexibound', 'other'],
    'spiral-bound': ['SPIRAL', 'Spiral-bound', 'other'], 'hardcover-spiral': ['SPIRAL', 'Hardcover spiral', 'other'],
    'leather bound': ['LEATHER', 'Leather bound', 'other'], 'library binding': ['LIBRARY', 'Library binding', 'other'],
    'unknown binding': ['BINDING ?', 'Unknown', 'unk'], '': ['BINDING ?', 'Unknown', 'unk']
  };
  var MEDIA = { dvd: ['DVD', 'DVD'], video_game: ['GAME', 'Video game'], music: ['MUSIC', 'Music'], audiobook: ['AUDIO', 'Audiobook'] };
  var q = $('q'), results = $('results'), clearBtn = $('clear'), pill = $('pill'), panel = $('panel'), toastBox = $('toast'), toastTimer = null;

  function fmtDate(ms) { return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
  function fmtDateTime(ms) { return new Date(ms).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); }
  function syncedAt() { return (state.checkedAt || (state.lib && state.lib.downloaded_at)) || 0; }
  function toast(msg) {
    $('toastText').textContent = msg; toastBox.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { toastBox.hidden = true; }, 3500);
  }
  function hasBinding(it) { return !it.isMedia && !!it.b && it.b !== 'unknown binding'; }
  function badge(it) {
    var b;
    if (it.isMedia) return '<span class="badge other">' + esc((MEDIA[it.m] || [it.m.toUpperCase().slice(0, 7)])[0]) + '</span>';
    if (!hasBinding(it)) return '';                   // no binding on file: show nothing rather than a placeholder
    b = BIND[it.b] || [String(it.b).toUpperCase().slice(0, 9), it.b, 'other'];
    return '<span class="badge ' + b[2] + '">' + esc(b[0]) + '</span>';
  }

  function renderStatus() {
    var n = state.items ? state.items.length : 0, cls = '', ds, text, age, at, msg = '', bad = false;
    if (!state.items) {
      cls = 'bad'; ds = state.syncing ? 'syncing' : 'empty';
      text = state.syncing ? 'Downloading the library…' : 'No library data on this phone';
      if (state.problem) { msg = state.problem + ' Connect to your home network or Tailscale, then tap Update now.'; bad = true; }
    } else {
      at = syncedAt(); age = (Date.now() - at) / DAY;
      var count = n.toLocaleString() + ' titles', when = 'synced ' + fmtDate(at);
      if (state.syncing) { ds = 'syncing'; text = 'Checking for updates · ' + count + ' · ' + when; }
      else if (age > CFG.staleDays) { ds = 'stale'; cls = 'warn'; text = count + ' · ' + when + ' · ' + Math.floor(age) + ' days old'; }
      else { ds = 'ready'; text = 'Offline-ready · ' + count + ' · ' + when; }
      if (state.problem) msg = state.problem + ' Using the copy saved on this phone.';
      else if (state.lib && state.lib.generated_at && state.checkedAt &&
               (state.checkedAt - Date.parse(state.lib.generated_at)) / DAY > CFG.serverStaleDays) {
        msg = 'The library copy on the server is ' + Math.floor((state.checkedAt - Date.parse(state.lib.generated_at)) / DAY) +
          ' days old. Newly added books will not appear until the export job runs.'; cls = cls || 'warn';
      }
      if (state.saved === false) { msg = (msg ? msg + ' ' : '') + 'Could not save the library for offline use on this phone.'; cls = 'warn'; }
    }
    pill.className = 'pill ' + cls; pill.setAttribute('data-state', ds); $('pillText').textContent = text;
    $('pCount').textContent = state.items ? n.toLocaleString() + ' titles (' + state.items.filter(function (x) { return !x.isMedia; }).length.toLocaleString() + ' books)' : '0 titles';
    $('pSynced').textContent = state.items ? fmtDateTime(syncedAt()) : 'Never';
    $('pGenerated').textContent = state.lib && state.lib.generated_at ? fmtDateTime(Date.parse(state.lib.generated_at)) : '–';
    $('pStorage').textContent = !state.items ? '–' : state.saved === false ? 'No' :
      (state.persisted ? 'Yes, protected from clean-up' : 'Yes');
    $('pMsg').textContent = msg; $('pMsg').className = 'msg' + (bad ? ' bad' : '');
    $('update').disabled = state.syncing;
    q.disabled = !state.items;
  }

  function rowHTML(it, qt, dupes) {
    var isOpen = !!state.open[it.id], key = it.t + '|' + it.a + '|' + it.b + '|' + it.m, sub = '', det = '', tags = it.g || [], b,
        bd = badge(it);
    sub = '<span class="au">' + hl(it.a || '', qt) + '</span>';
    if (dupes[key] > 1) sub += ' · <span class="isbn">…' + esc(String(it.i || '').slice(-4)) + '</span>';
    if (tags.length) sub += ' · ' + esc(tags.join(' · '));
    if (isOpen) {
      b = it.isMedia ? [0, (MEDIA[it.m] || [0, it.m])[1]] : (BIND[it.b] || [0, it.b]);
      det = '<span class="detail">' +
        (it.isMedia || hasBinding(it) ? '<span class="k">Binding</span><span class="v">' + esc(b[1]) + '</span>' : '') +
        (it.s ? '<span class="k">Subtitle</span><span class="v">' + esc(it.s) + '</span>' : '') +
        (it.r ? '<span class="k">Series</span><span class="v">' + esc(it.r) + '</span>' : '') +
        '<span class="k">Tags</span><span class="v chips">' + tags.map(function (g) { return '<span class="chip">' + esc(g) + '</span>'; }).join('') + '</span>' +
        '<span class="k">Location</span><span class="v">' + esc((it.l || []).join(' · ') || 'Not set') + '</span>' +
        '<span class="k">ISBN</span><span class="v mono">' + esc(it.i || '') + '</span>' +
        '<span class="k">Type</span><span class="v">' + esc(it.isMedia ? (MEDIA[it.m] || [0, it.m])[1] : 'Book') + '</span></span>';
    }
    return '<button class="row' + (bd ? '' : ' nobadge') + '" type="button" data-id="' + it.id + '" aria-expanded="' + isOpen + '">' +
      '<span><span class="t">' + hl(it.t, qt) + '</span><span class="s">' + sub + '</span></span>' + bd + det + '</button>';
  }
  function render() {
    var raw = q.value, res, dupes = {}, html;
    clearBtn.hidden = !raw;
    if (!state.items) {
      results.innerHTML = '<div class="nodata">' + (state.syncing
        ? 'Downloading the library for the first time…'
        : 'Nothing is stored on this phone yet. Connect to your home network or Tailscale, then open the status pill and tap Update now.') + '</div>';
      return;
    }
    if (!queryTokens(raw).length) {
      results.innerHTML = '<div class="hint"><p>Type a title or an author. Partial words and typos are fine.</p></div>';
      return;
    }
    res = search(state.items, raw);
    if (!res.items.length) {
      results.innerHTML = '<p class="note">Nothing in the library matches “' + esc(raw.trim()) + '” as of ' + esc(fmtDate(syncedAt())) + '.</p>';
      return;
    }
    res.items.forEach(function (it) { var k = it.t + '|' + it.a + '|' + it.b + '|' + it.m; dupes[k] = (dupes[k] || 0) + 1; });
    html = (res.relaxed ? '<p class="note">No full match. Closest results:</p>' : '') +
      '<div class="list">' + res.items.map(function (it) { return rowHTML(it, res.qt, dupes); }).join('') + '</div>';
    results.innerHTML = html;
  }

  q.addEventListener('input', render);
  clearBtn.addEventListener('click', function () { q.value = ''; render(); q.focus(); });
  results.addEventListener('click', function (e) {
    var r = e.target.closest('.row');
    if (r) { var id = r.getAttribute('data-id'); if (state.open[id]) delete state.open[id]; else state.open[id] = true; render(); }
  });
  pill.addEventListener('click', function () {
    var show = panel.hidden; panel.hidden = !show; pill.setAttribute('aria-expanded', show ? 'true' : 'false');
  });
  $('update').addEventListener('click', function () { checkForUpdate('manual'); });
  window.addEventListener('online', function () { checkForUpdate('online'); });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && Date.now() - state.lastAttemptAt > CFG.recheckMs) checkForUpdate('visible');
  });

  /* -------------------------------------------------------------- boot --- */
  function boot() {
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(function () {});
    return Promise.all([kvGet('library').catch(function () { return null; }), kvGet('checked').catch(function () { return null; })])
      .then(function (r) {
        var lib = r[0], chk = r[1];
        if (lib && Array.isArray(lib.items) && lib.items.length) {
          state.items = lib.items.map(prepare); state.saved = true;
          state.lib = { sha256: lib.sha256, count: lib.count, generated_at: lib.generated_at, downloaded_at: lib.downloaded_at };
          if (chk && chk.sha256 === lib.sha256) state.checkedAt = chk.at;
        }
        renderStatus(); render();
        if (navigator.storage && navigator.storage.persist) {
          navigator.storage.persisted().then(function (p) { return p || navigator.storage.persist(); })
            .then(function (p) { state.persisted = !!p; renderStatus(); }).catch(function () {});
        }
        try { q.focus(); } catch (e) { /* ignore */ }
        return checkForUpdate('open');
      });
  }
  boot();
})();
