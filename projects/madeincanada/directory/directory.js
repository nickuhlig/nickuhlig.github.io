// Made in Canada directory: filters the brands in ../brands.json (built by Jekyll from _brands/*.md).
// Within a group, any ticked option matches; across groups, every group with something ticked must match.
(function () {
  'use strict';

  // filter groups, in the order they're shown; `key` is the field in each brand
  var GROUPS = [
    { key: 'items', legend: 'Item' },
    { key: 'audience', legend: 'For' },
    { key: 'style', legend: 'Style' },
    { key: 'business', legend: 'Business' },
    { key: 'province', legend: 'Province' },
    { key: 'city', legend: 'City', hint: 'Tick a province to choose cities in it.' },
    { key: 'made_in_canada', legend: 'How much is made in Canada' },
    { key: 'status', legend: 'Status', hint: 'Watchlist: still makes some things here, but moving production abroad. Acquired: bought by another company. Wall of shame: moved all production out of Canada. Defunct: no longer exists.' },
    { key: 'ownership', legend: 'Ownership' }
  ];
  var NOT_SET = '(not specified)';

  var vocab, brands, labels = {}, selected = {};
  var $ = function (id) { return document.getElementById(id); };

  function arr(v) {
    if (v === null || v === undefined || v === '') return [];
    return Array.isArray(v) ? v : [v];
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function label(key, id) { return (labels[key] && labels[key][id]) || id; }
  function textOf(html) { var d = document.createElement('div'); d.innerHTML = html || ''; return d.textContent.trim(); }

  // ---------------------------------------------------------------- data

  function load(data) {
    vocab = data.vocab;
    Object.keys(vocab).forEach(function (key) {
      labels[key] = {};
      vocab[key].forEach(function (o) { labels[key][o.id] = o.label; });
    });
    brands = data.brands.map(function (b) {
      // locations are "City, PROV" (or just "PROV"); province and city filters are derived from them,
      // and a city's id is the whole "City, PROV" so each city stays with its own province
      b.locations = arr(b.locations).map(String);
      b.province = [];
      b.city = [];
      b.locations.forEach(function (loc) {
        var m = loc.match(/^(?:(.+),\s*)?([A-Z]{2})$/);
        if (!m) { b.province.push(loc); return; }  // flagged by checkData
        if (b.province.indexOf(m[2]) === -1) b.province.push(m[2]);
        if (m[1]) b.city.push(loc);
      });
      GROUPS.forEach(function (g) {
        if (g.key !== 'province' && g.key !== 'city') b[g.key] = arr(b[g.key]).map(String);
      });
      b.searchText = (b.title + ' ' + textOf(b.description)).toLowerCase();
      return b;
    }).sort(function (a, b) { return a.title.localeCompare(b.title, 'en', { sensitivity: 'base' }); });
    checkData();
  }

  // any value a brand uses that isn't in _data/madeincanada.yml
  function checkData() {
    var problems = [];
    brands.forEach(function (b) {
      GROUPS.forEach(function (g) {
        if (!vocab[g.key]) return;
        b[g.key].forEach(function (v) {
          if (!labels[g.key][v]) problems.push(b.id + ': ' + g.key + ' "' + v + '"');
        });
      });
    });
    if (problems.length) {
      console.warn('Values not in _data/madeincanada.yml:', problems);
      $('data-check').textContent = 'Data check: ' + problems.length + ' value(s) not in _data/madeincanada.yml: ' + problems.join('; ');
    }
  }

  // options for a group: the vocabulary order, or (for city) the cities of the ticked provinces
  function optionsFor(key) {
    if (key === 'city') {
      var provs = selected.province || [];
      var seen = {};
      brands.forEach(function (b) {
        b.city.forEach(function (c) {
          if (provs.indexOf(c.slice(-2)) !== -1) seen[c] = true;
        });
      });
      // with one province ticked the province suffix is just noise
      return Object.keys(seen).sort(function (a, b) { return a.localeCompare(b); })
        .map(function (c) { return { id: c, label: provs.length === 1 ? c.replace(/,\s*[A-Z]{2}$/, '') : c }; });
    }
    // only options at least one brand uses (status always shows all, so the defaults stay visible)
    return (vocab[key] || []).filter(function (o) {
      return key === 'status' || brands.some(function (b) { return b[key].indexOf(o.id) !== -1; });
    });
  }

  // ---------------------------------------------------------------- state <-> URL

  function defaults() {
    selected = {};
    selected.status = vocab.status.filter(function (s) { return s.shown; }).map(function (s) { return s.id; });
  }

  function readHash() {
    defaults();
    var h = location.hash.replace(/^#/, '');
    if (!h) return;
    h.split('&').forEach(function (part) {
      var kv = part.split('=');
      var key = decodeURIComponent(kv[0]);
      var val = decodeURIComponent(kv[1] || '');
      if (key === 'q') $('search').value = val;
      else if (GROUPS.some(function (g) { return g.key === key; })) selected[key] = val ? val.split(',') : [];
    });
  }

  function writeHash() {
    var parts = [];
    GROUPS.forEach(function (g) {
      var v = selected[g.key] || [];
      var isDefaultStatus = g.key === 'status' &&
        v.slice().sort().join(',') === vocab.status.filter(function (s) { return s.shown; }).map(function (s) { return s.id; }).sort().join(',');
      if (isDefaultStatus) return;
      if (v.length || g.key === 'status') parts.push(g.key + '=' + v.map(encodeURIComponent).join(','));
    });
    var q = $('search').value.trim();
    if (q) parts.push('q=' + encodeURIComponent(q));
    history.replaceState(null, '', parts.length ? '#' + parts.join('&') : location.pathname);
  }

  // ---------------------------------------------------------------- filtering

  function matches(b, skipKey) {
    var q = $('search').value.trim().toLowerCase();
    if (q && b.searchText.indexOf(q) === -1) return false;
    return GROUPS.every(function (g) {
      if (g.key === skipKey) return true;
      var want = selected[g.key] || [];
      if (!want.length) return true;
      return b[g.key].some(function (v) { return want.indexOf(v) !== -1; });
    });
  }

  function filtered() { return brands.filter(function (b) { return matches(b); }); }

  // ---------------------------------------------------------------- rendering

  function renderFilters() {
    // drop ticked cities whose province is no longer ticked
    var cityOpts = optionsFor('city').map(function (o) { return o.id; });
    selected.city = (selected.city || []).filter(function (c) { return cityOpts.indexOf(c) !== -1; });

    var html = '';
    GROUPS.forEach(function (g) {
      var opts = optionsFor(g.key);
      var want = selected[g.key] || [];
      html += '<fieldset><legend> ' + esc(g.legend) + ' </legend>';
      opts.forEach(function (o) {
        // how many brands this option would show, given every other group's ticks
        var n = brands.filter(function (b) { return b[g.key].indexOf(o.id) !== -1 && matches(b, g.key); }).length;
        html += '<label class="opt"><input type="checkbox" data-key="' + esc(g.key) + '" value="' + esc(o.id) + '"' +
          (want.indexOf(o.id) !== -1 ? ' checked' : '') + '> ' + esc(o.label) + ' <span class="count">' + n + '</span></label>';
      });
      if (g.hint && (g.key !== 'city' || !opts.length)) html += '<p class="hint">' + esc(g.hint) + '</p>';
      html += '</fieldset>';
    });
    $('filters').innerHTML = html;
  }

  function badges(b) {
    var out = [];
    var mic = b.made_in_canada[0];
    if (mic) out.push('<span class="badge">Made in Canada: ' + esc(label('made_in_canada', mic).toLowerCase()) + '</span>');
    var st = b.status[0];
    if (st === 'acquired' && b.acquired_by) {
      // link to the buyer's entry here if it has one, else to its website
      var buyer = brands.filter(function (x) { return x.title.toLowerCase() === String(b.acquired_by).toLowerCase(); })[0];
      var href = buyer ? '#brand-' + buyer.id : b.acquired_by_website;
      var who = href ? '<a href="' + esc(href) + '">' + esc(b.acquired_by) + '</a>' : esc(b.acquired_by);
      out.push('<span class="badge acquired">Acquired by ' + who + '</span>');
    } else if (st && st !== 'confirmed') {
      out.push('<span class="badge ' + esc(st === 'unknown' ? 'unknown-status' : st) + '">' + esc(label('status', st)) + '</span>');
    }
    var own = b.ownership[0];
    if (own && own !== 'unknown') out.push('<span class="badge">' + esc(label('ownership', own)) + '-owned</span>');
    if (b.business.indexOf('manufacturer') !== -1) out.push('<span class="badge">' + esc(label('business', 'manufacturer')) + '</span>');
    return out.join('');
  }

  function where(b) {
    return b.locations.length ? b.locations.join('; ') : 'Location in Canada not known';
  }

  function card(b) {
    var name = b.website ? '<a href="' + esc(b.website) + '">' + esc(b.title) + '</a>' : esc(b.title);
    var tags = []
      .concat(b.items.map(function (v) { return label('items', v); }))
      .concat(b.audience.map(function (v) { return label('audience', v); }))
      .concat(b.style.map(function (v) { return label('style', v); }));
    return '<article class="brand" id="brand-' + esc(b.id) + '">' +
      '<h3>' + name + '</h3>' +
      '<p class="where">' + esc(where(b)) + '</p>' +
      '<div>' + badges(b) + '</div>' +
      (tags.length ? '<p class="tags">' + esc(tags.join(' · ')) + '</p>' : '') +
      '<div class="desc">' + (b.description || '') + '</div>' +
      (b.last_checked ? '<p class="checked">Last checked ' + esc(b.last_checked) + '</p>' : '') +
      '</article>';
  }

  // [{ name, brands }]: results under item headings; a brand with several items appears under each
  function groups(list) {
    var key = 'items';
    var order = (vocab[key] || []).map(function (o) { return o.id; });
    var want = selected[key] || [];
    var byId = {};
    list.forEach(function (b) {
      var vals = b[key].length ? b[key] : [NOT_SET];
      // grouping by something that's also filtered: only the ticked headings
      if (want.length) vals = vals.filter(function (v) { return want.indexOf(v) !== -1; });
      vals.forEach(function (v) { (byId[v] = byId[v] || []).push(b); });
    });
    return Object.keys(byId).sort(function (a, b) {
      var ia = order.indexOf(a), ib = order.indexOf(b);
      if (a === NOT_SET) return 1;
      if (b === NOT_SET) return -1;
      if (ia !== -1 || ib !== -1) return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib);
      return a.localeCompare(b);
    }).map(function (id) {
      return { name: id === NOT_SET ? 'Not specified' : label(key, id), brands: byId[id] };
    });
  }

  function renderResults() {
    var list = filtered();
    var gs = groups(list);
    var placed = gs.reduce(function (n, g) { return n + g.brands.length; }, 0);
    $('summary').textContent = list.length + ' of ' + brands.length + ' brands' +
      (placed > list.length ? ' (some are listed under more than one heading)' : '');
    if (!list.length) {
      $('results').innerHTML = '<p class="hint">Nothing matches. Try unticking something.</p>';
      return;
    }
    $('results').innerHTML = gs.map(function (g) {
      return (g.name ? '<h2>' + esc(g.name) + ' <span class="count">' + g.brands.length + '</span></h2>' : '') +
        g.brands.map(card).join('');
    }).join('');
  }

  function update() {
    renderFilters();
    renderResults();
    writeHash();
  }

  // ---------------------------------------------------------------- exports

  function download(name, type, text) {
    var blob = new Blob([text], { type: type + ';charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  function csvCell(v) {
    v = String(v === null || v === undefined ? '' : v);
    return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }

  function exportCsv() {
    var cols = ['title', 'website', 'locations', 'items', 'audience', 'style', 'business',
      'made_in_canada', 'status', 'acquired_by', 'ownership', 'last_checked', 'description'];
    var rows = [cols.join(',')];
    filtered().forEach(function (b) {
      rows.push(cols.map(function (c) {
        if (c === 'description') return csvCell(textOf(b.description));
        if (Array.isArray(b[c])) {
          var vocabKey = vocab[c] ? c : null;
          return csvCell(b[c].map(function (v) { return vocabKey ? label(c, v) : v; }).join('; '));
        }
        return csvCell(b[c]);
      }).join(','));
    });
    download('made-in-canada.csv', 'text/csv', '﻿' + rows.join('\r\n') + '\r\n');
  }

  function exportMarkdown() {
    var out = ['# Made in Canada', '', 'From ' + location.href, ''];
    groups(filtered()).forEach(function (g) {
      if (g.name) out.push('## ' + g.name, '');
      g.brands.forEach(function (b) {
        var name = b.website ? '[' + b.title + '](' + b.website + ')' : b.title;
        var notes = [label('made_in_canada', b.made_in_canada[0] || 'unknown').toLowerCase() + ' made in Canada'];
        if (b.status[0] === 'acquired' && b.acquired_by) notes.push('acquired by ' + b.acquired_by);
        else if (b.status[0] && b.status[0] !== 'confirmed') notes.push(label('status', b.status[0]).toLowerCase());
        out.push('- **' + name + '** (' + where(b) + '; ' + notes.join(', ') + ')  ', '  ' + textOf(b.description));
      });
      out.push('');
    });
    download('made-in-canada.md', 'text/markdown', out.join('\n'));
  }

  // ---------------------------------------------------------------- wiring

  function init(data) {
    load(data);
    readHash();
    update();

    $('filters').addEventListener('change', function (e) {
      var box = e.target;
      if (!box.dataset || !box.dataset.key) return;
      var key = box.dataset.key;
      var cur = selected[key] || [];
      selected[key] = box.checked ? cur.concat([box.value]) : cur.filter(function (v) { return v !== box.value; });
      update();
    });
    var typing;
    $('search').addEventListener('input', function () { clearTimeout(typing); typing = setTimeout(update, 150); });
    $('reset').addEventListener('click', function () { $('search').value = ''; defaults(); update(); });
    $('export-csv').addEventListener('click', exportCsv);
    $('export-md').addEventListener('click', exportMarkdown);
    $('export-print').addEventListener('click', function () { window.print(); });
  }

  fetch('../brands.json')
    .then(function (r) { if (!r.ok) throw new Error(r.status + ' ' + r.statusText); return r.json(); })
    .then(init)
    .catch(function (err) {
      $('filters').innerHTML = '<p class="hint">Couldn\'t load the brand list (' + esc(err.message) + ').</p>';
    });
})();
