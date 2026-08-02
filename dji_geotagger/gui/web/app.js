/* Interface wiring.
 *
 * Resolve Base and Run both call the real pipeline on a worker thread; results
 * and progress come back through window.onPipelineEvent.
 *
 * Plain DOM, no framework: the whole surface is four forms and a list, and a
 * build step would cost more than it saves for that. */

'use strict';

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  baseFile: null,      // {path, name, kind}
  sumFile: null,
  roots: [],           // folders searched, in the order they were added
  flights: [],         // [{path, name, root, photos, mrk, on}]
  outFile: null,
  outFileExplicit: false,   // true once the user has chosen one themselves
  crs: null,
  basePosition: null,
  tracks: {},          // flight path -> track, cached so toggling never refetches
  coverage: {},        // flight path -> base-coverage result
  emails: [],          // addresses that have worked, most recent first
  running: false,
  resolving: false,
  cancellable: false,
  cancelled: false,
};

/* ----------------------------- logging ------------------------------- */

/* The level arrives as its own field, so it is rendered as a badge rather than
   trusted to be in the text. Much of the library writes "[INFO]" into the
   message itself and much of it does not, which is why the log used to look
   half-prefixed; any such prefix is stripped here and replaced consistently. */
const LEVEL_ALIAS = { WARNING: 'WARN', CRITICAL: 'ERROR' };
const PREFIX = /^\s*\[(INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL)\]\s*/i;

function log(text, level) {
  const el = $('#log');

  let body = String(text);
  const found = PREFIX.exec(body);
  if (found) {
    if (!level) level = found[1].toUpperCase();
    body = body.slice(found[0].length);
  }

  const shown = LEVEL_ALIAS[level] || level || 'INFO';

  /* Stuck to the bottom only while the user is already there - otherwise
     scrolling back to read something is undone by the next line. */
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;

  const line = document.createElement('div');
  line.className = 'logline lvl-' + shown.toLowerCase();

  const tag = document.createElement('span');
  tag.className = 'loglevel';
  tag.textContent = shown;

  const message = document.createElement('span');
  message.className = 'logmsg';
  message.textContent = body;

  line.append(tag, message);
  el.appendChild(line);

  if (atBottom) el.scrollTop = el.scrollHeight;
}

/* A thrown error in a handler, or a rejected bridge call, otherwise leaves no
   trace on screen at all - the control simply stops responding and there is
   nothing to report but "it does nothing". Both end up in the Log instead. */
window.addEventListener('error', (ev) => {
  log('[ERROR] ' + ev.message + '  (' + (ev.filename || '?').split('/').pop() +
      ':' + ev.lineno + ')');
});

window.addEventListener('unhandledrejection', (ev) => {
  log('[ERROR] unhandled: ' + (ev.reason && ev.reason.message || ev.reason));
});

/* ----------------------------- progress ------------------------------ */

/* Two bars, because the pipeline genuinely has two answers to "how far":
 *
 *   overall - where the whole run is, the number that predicts the finish
 *   step    - where the thing running right now is, the number that shows
 *             it has not hung
 *
 * `overall` and `step` are fractions in [0,1], or null for "running, length
 * unknown" - a CSRS-PPP queue has no length, and neither does image reading
 * before the count is known.
 */
function progress({ stage, label, message, overall, step, mode }) {
  const foot = $('.panel-foot');
  const bar = $('#progress-bar');
  const overallTrack = $('#overall-track');
  const stepTrack = $('#step-track');
  const stepBar = $('#step-bar');

  /* Nothing has been started, so there is nothing to show. An empty track
     sitting above an un-pressed Run implies work is under way. */
  overallTrack.classList.toggle('hidden', mode === 'idle' || !mode);

  $('#prog-stage').textContent = stage || 'Idle';
  $('#prog-count').textContent = label || '';
  $('#status').textContent = message || '';

  bar.classList.remove('indeterminate', 'done', 'failed');
  stepBar.classList.remove('indeterminate');
  foot.classList.toggle('busy', mode === 'busy');
  foot.classList.toggle('idle',
    mode !== 'busy' && mode !== 'done' && mode !== 'failed');

  if (mode === 'done' || mode === 'failed') {
    bar.classList.add(mode);
    bar.style.width = '100%';
    stepTrack.classList.add('hidden');
    $('#step-count').textContent = '';
    return;
  }

  if (mode !== 'busy') {                                      // idle
    bar.style.width = '0';
    stepTrack.classList.add('hidden');
    $('#prog-count').textContent = '';
    $('#step-count').textContent = '';
    return;
  }

  if (overall == null) {
    bar.classList.add('indeterminate');
    if (!label) $('#prog-count').textContent = '';
  } else {
    const fraction = Math.min(1, Math.max(0, overall));
    bar.style.width = (fraction * 100) + '%';
    if (!label) $('#prog-count').textContent = Math.round(fraction * 100) + '%';
  }

  /* The step bar appears only when there is a step to show, so a single-stage
     operation does not sit next to an empty second bar. */
  if (step == null) {
    stepTrack.classList.add('hidden');
    $('#step-count').textContent = '';
  } else {
    const fraction = Math.min(1, Math.max(0, step));
    stepTrack.classList.remove('hidden');
    stepBar.style.width = (fraction * 100) + '%';
    $('#step-count').textContent = Math.round(fraction * 100) + '%';
  }
}

function idle(message) {
  progress({ stage: '', message: message || 'Ready.', mode: 'idle' });
}

/* ----------------------------- splitter ------------------------------ */

/* Panel width is a working preference - a wide left column while setting a run
   up, a wide map while checking one - so it is dragged, not fixed, and kept
   between sessions. */
const PANEL_MIN = 420;
const PANEL_MAX_MARGIN = 360;   // always leave this much for the map

function setPanelWidth(px) {
  const limit = Math.max(PANEL_MIN, window.innerWidth - PANEL_MAX_MARGIN);
  const width = Math.round(Math.min(limit, Math.max(PANEL_MIN, px)));
  document.documentElement.style.setProperty('--panel-w', width + 'px');
  return width;
}

(function initSplitter() {
  const splitter = $('#splitter');
  let dragging = false;

  splitter.addEventListener('pointerdown', (ev) => {
    dragging = true;
    splitter.setPointerCapture(ev.pointerId);
    splitter.classList.add('dragging');
    document.body.classList.add('resizing');
  });

  splitter.addEventListener('pointermove', (ev) => {
    if (dragging) setPanelWidth(ev.clientX);
  });

  splitter.addEventListener('pointerup', (ev) => {
    if (!dragging) return;
    dragging = false;
    splitter.releasePointerCapture(ev.pointerId);
    splitter.classList.remove('dragging');
    document.body.classList.remove('resizing');
    MapView.refresh();
    try {
      localStorage.setItem('panelWidth', String(setPanelWidth(ev.clientX)));
    } catch (err) { /* private mode, or storage disabled - width just resets */ }
  });

  /* Double-click restores the default rather than making the user hunt for it
     after dragging the panel somewhere unusable. */
  splitter.addEventListener('dblclick', () => {
    setPanelWidth(560);
    try { localStorage.removeItem('panelWidth'); } catch (err) { /* ignore */ }
  });

  let stored = null;
  try { stored = localStorage.getItem('panelWidth'); } catch (err) { /* ignore */ }
  if (stored) setPanelWidth(parseInt(stored, 10));

  /* Shrinking the window must not push the map out of existence. */
  window.addEventListener('resize', () => {
    setPanelWidth(parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue('--panel-w')));
    MapView.refresh();
  });
})();

/* --------------------------- mode switching --------------------------- */

/* Base position mode. Panes are swapped rather than disabled so that only the
   fields belonging to the chosen source are on screen at all. */
$$('input[name=base-mode]').forEach((radio) => {
  radio.addEventListener('change', () => {
    $$('.mode-pane').forEach((pane) => {
      pane.classList.toggle('hidden', pane.dataset.mode !== radio.value);
    });
    refresh();
  });
});

/* A fixed delivery epoch is a NAD83 concept - CSRS-PPP offers it only for that
   frame, so the control appears with it. */
$('#ppp-frame').addEventListener('change', (ev) => {
  $('#row-epoch').classList.toggle('hidden', ev.target.value !== 'NAD83');
});

/* Tabs. */
$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    $$('.tab-pane').forEach((p) => {
      p.classList.toggle('active', p.dataset.tab === tab.dataset.tab);
    });
    MapView.refresh();
  });
});

/* Basemap switch. */
$$('.bm').forEach((button) => {
  button.addEventListener('click', () => {
    $$('.bm').forEach((b) => b.classList.toggle('active', b === button));
    MapView.setBasemap(button.dataset.basemap);
  });
});

/* ------------------------------ actions ------------------------------- */

const actions = {

  async 'pick-base'() {
    const picked = await pywebview.api.pick_base_file();
    if (!picked) return;

    /* A different base invalidates everything downstream of it - the solved
       position, which flights were checked against its observation window,
       which target frames are reachable, and any result already written.
       Leaving those on screen would show a survey assembled from two
       different base stations. */
    if (state.baseFile && state.baseFile.path !== picked.path) {
      resetBelowBase();
      log('[INFO] Base changed; steps 2 to 4 cleared.');
    }

    state.baseFile = picked;
    $('#base-file').value = picked.path;

    /* Antenna height is consumed during raw conversion. A RINEX file already
       carries it in the header, so offering the field again would invite a
       second, silent application of the same correction. */
    const isRaw = picked.kind === 'raw';
    $('#row-antenna').classList.toggle('hidden', !isRaw);

    if (picked.kind === 'unknown') {
      log('[WARN] Unrecognised extension: ' + picked.name +
          ' - expected .dat, .obs, .rnx or a RINEX 2 .??o file.');
    } else {
      log('[INFO] Base observations: ' + picked.name + ' (' + picked.kind + ')');
    }

    /* A RINEX header already states roughly where the receiver was, so the
       map can show the site immediately instead of waiting minutes for
       CSRS-PPP to say the same thing to eight more decimal places. */
    if (!state.basePosition) {
      const approx = await pywebview.api.approximate_base(picked.path);
      if (approx) {
        MapView.setBase(approx.lat, approx.lon,
                        'Base station (approximate)<br>from RINEX header',
                        { provisional: true });
        log('[INFO] Approximate base position from header: ' +
            approx.lat.toFixed(6) + ', ' + approx.lon.toFixed(6) +
            ' - metre level, superseded once resolved.');
      } else if (picked.kind === 'raw') {
        log('[INFO] Position becomes readable after conversion; the antenna ' +
            'height is applied then, so it waits for Resolve.');
      }
    }
    refresh();
  },

  async 'pick-sum'() {
    const picked = await pywebview.api.pick_sum_file();
    if (!picked) return;
    state.sumFile = picked;
    $('#sum-file').value = picked.path;
    log('[INFO] Summary file: ' + picked.name);
    refresh();
  },

  async 'pick-survey'() {
    const found = await pywebview.api.pick_survey_folder();
    if (!found) return;

    if (found.error) log('[ERROR] ' + found.error);

    found.roots.forEach((root) => {
      if (!state.roots.includes(root)) state.roots.push(root);
    });

    /* Appending, not replacing: a survey split over two cards is a second
       click rather than a second run. Paths already present are dropped, so
       re-picking an overlapping parent cannot double-count a flight. */
    const known = new Set(state.flights.map((f) => f.path));
    const added = found.flights.filter((f) => !known.has(f.path));

    /* Flights without an MRK start unchecked. They cannot be processed, and
       silently including them would only produce a failure later. */
    state.flights.push(...added.map((f) => ({ ...f, on: Boolean(f.mrk) })));
    state.flights.sort((a, b) => (a.root + a.name).localeCompare(b.root + b.name));

    if (added.length === 0) {
      log('[WARN] No new flights under ' + found.roots.join(', ') +
          ' - a flight folder holds an MRK file next to its photos.');
    } else {
      log('[INFO] ' + added.length + ' flight(s) found under ' + found.roots.join(', '));
      added.forEach((f) => {
        log(f.mrk ? '[INFO]   ' + f.name + ': ' + f.photos + ' photos, ' + f.mrk
                  : '[WARN]   ' + f.name + ': no MRK file, excluded.');
      });
    }

    /* Fill in an output path so the run is not blocked on a second dialog.
       Only while the user has not chosen one themselves - overwriting an
       explicit choice because another folder was added would be worse than
       leaving the field empty. */
    if (!state.outFileExplicit && found.suggested_output) {
      state.outFile = found.suggested_output;
      $('#out-file').value = state.outFile;
    }

    renderSurveyField();
    flightsChanged();
  },

  'clear-survey'() {
    state.roots = [];
    state.flights = [];
    renderSurveyField();
    flightsChanged();
  },

  async 'copy-log'() {
    const button = $('#btn-copy-log');
    /* Rebuilt with the level in front of each line: pasted into an email or an
       issue, the badges are gone but the levels still have to be readable. */
    const text = $$('#log .logline').map((line) =>
      '[' + line.querySelector('.loglevel').textContent + '] ' +
      line.querySelector('.logmsg').textContent).join('\n');
    let ok = false;

    /* The clipboard API needs a secure context, which pywebview provides by
       serving the page over localhost - but it can still be refused, so the
       old selection-based route stays as a fallback. */
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (err) {
      const scratch = document.createElement('textarea');
      scratch.value = text;
      scratch.style.position = 'fixed';
      scratch.style.opacity = '0';
      document.body.appendChild(scratch);
      scratch.select();
      try { ok = document.execCommand('copy'); } catch (err2) { ok = false; }
      scratch.remove();
    }

    /* Copying is silent by nature; without this there is no way to tell it
       worked. */
    button.textContent = ok ? 'Copied' : 'Failed';
    setTimeout(() => { button.textContent = 'Copy'; }, 1400);
  },

  'open-output'() {
    if (state.finished) pywebview.api.reveal(state.finished);
  },

  async 'statistics'() { showStatistics(); },

  'start-over'() {
    state.baseFile = null;
    $('#base-file').value = '';
    $('#row-antenna').classList.remove('hidden');
    resetBelowBase();
    $('#log').textContent = '';
    log('Ready.');
  },

  async 'reexport'() {
    const confidence = $('#confidence');
    const started = await pywebview.api.reexport({
      targetCrs: state.crs,
      k: parseFloat(confidence.value),
      confidenceLabel: confidence.selectedOptions[0].textContent.split(' (')[0],
      outFile: state.outFile,
    });
    if (!started) { log('Nothing to re-export.', 'WARN'); return; }

    state.running = true;
    state.cancellable = false;
    refresh();
    progress({ stage: 'Export', message: 'Writing with the new settings...',
               overall: null, mode: 'busy' });
  },

  'close-modal'() {
    $('#modal').classList.add('hidden');
  },

  'toggle-detail'() {
    const card = $('#base-result');
    const open = card.classList.toggle('hidden');
    $('#btn-detail').textContent = open ? 'Detail' : 'Hide';
  },

  'pick-crs'() { openCrsPicker(); },

  'close-crs'() { $('#crs-modal').classList.add('hidden'); },

  'why-crs'() { explainCrs(); },

  async 'about'() { showAbout(); },

  'accept-crs'() {
    state.crs = crs.selected;
    state.crsName = crs.selectedName || crs.selected;

    /* The name is what identifies the system to a person; the code is what
       goes in a delivery note. Both, with the name first. */
    const field = $('#crs');
    field.value = state.crs
      ? state.crsName + '   ·   ' + state.crs
      : '';
    field.title = field.value;

    $('#crs-modal').classList.add('hidden');
    log('Target CRS: ' + state.crsName + ' (' + state.crs + ')');
    markExportDirty();
    refresh();
  },

  'define-crs'() {
    $('#crs-define').classList.remove('hidden');
    $('#def-name').focus();
  },

  'cancel-define'() {
    $('#crs-define').classList.add('hidden');
  },

  async 'save-define'() {
    /* The two systems that have no EPSG code of their own: a UTM zone on a
       chosen datum, and a published grid lifted onto a versioned one. */
    const kind = $('input[name=crs-kind]:checked').value;
    const name = $('#def-name').value.trim();
    if (!name) { crsVerdict('Give the coordinate system a name.', false); return; }

    const spec = kind === 'utm'
      ? { kind: 'utm', name: name, zone: $('#def-zone').value,
          datum: $('#def-datum').value.trim(), south: $('#def-south').checked }
      : { kind: 'rebase', name: name,
          projected: $('#def-proj').value.trim(),
          datum: $('#def-datum2').value.trim() };

    try {
      const made = await pywebview.api.define_crs(spec);
      if (!made.ok) { crsVerdict('✗  ' + made.reason, false); return; }
      log('[INFO] Defined ' + made.entry.code);
      crs.open['User-Defined'] = true;
      await openCrsPicker();
    } catch (err) {
      crsVerdict('Could not create it.\n' + err, false);
    }
  },

  async 'pick-output'() {
    const path = await pywebview.api.pick_output_file();
    if (!path) return;
    state.outFile = path;
    state.outFileExplicit = true;
    $('#out-file').value = path;
    markExportDirty();
    refresh();
  },

  async 'resolve'() {
    const mode = $('input[name=base-mode]:checked').value;
    state.resolveToken = (state.resolveToken || 0) + 1;
    const started = await pywebview.api.resolve_base({
      token: state.resolveToken,
      mode: mode,
      baseFile: state.baseFile,
      antennaHeight: parseFloat($('#antenna-height').value) || 0,
      email: $('#ppp-email').value,
      frame: $('#ppp-frame').value,
      processType: $('#ppp-type').value,
      epoch: $('#ppp-epoch').value,
      force: $('#ppp-force').checked,
      sumFile: state.sumFile && state.sumFile.path,
      k: parseFloat($('#confidence').value),
      manual: {
        lat: $('#man-lat').value, lon: $('#man-lon').value,
        hgt: $('#man-hgt').value, frame: $('#man-frame').value,
        epoch: $('#man-epoch').value,
        sh: $('#man-sh').value, sv: $('#man-sv').value,
      },
    });
    if (!started) { log('[WARN] Something is already running.'); return; }

    state.running = true;
    state.cancellable = false;
    state.resolving = true;
    setResolveState('busy');
    refresh();

    /* Deliberately not touching the footer bar: that one tracks the run, and
       moving it here reads as "the run has started". */
    resolveNote(mode === 'online'
      ? 'Submitting to CSRS-PPP.\nUsually 30 to 60 seconds, longer when NRCan '
        + 'has a queue. See the Log tab for detail.'
      : 'Reading the base station position...');
  },

  async 'run'() {
    const flights = selectedFlights();
    const confidence = $('#confidence');

    const started = await pywebview.api.run({
      flights: flights.map((f) => f.path),
      baseFile: state.baseFile,
      antennaHeight: parseFloat($('#antenna-height').value) || 0,
      targetCrs: state.crs,
      k: parseFloat(confidence.value),
      confidenceLabel: confidence.selectedOptions[0].textContent.split(' (')[0],
      workers: parseInt($('#workers').value, 10) || 1,
      outFile: state.outFile,
    });
    if (!started) { log('[WARN] Something is already running.'); return; }

    state.running = true;
    state.cancellable = true;
    state.cancelled = false;
    outer = null;              /* flight counter belongs to one run only */
    beginRun(flights);
    refresh();
    log('[INFO] Run started: ' + flights.length + ' flight(s), ' +
        flights.reduce((n, f) => n + f.photos, 0) + ' photos.');
    progress({ stage: 'Starting', message: 'Preparing...', mode: 'busy' });
  },

  'cancel'() {
    state.cancelled = true;
    log('[INFO] Cancelling...');
    if (window.pywebview) pywebview.api.cancel();
  },
};

document.addEventListener('click', (ev) => {
  const btn = ev.target.closest('[data-action]');
  if (!btn || btn.disabled) return;
  /* Some triggers are links, for the affordance rather than the navigation. */
  if (btn.tagName === 'A') ev.preventDefault();
  const handler = actions[btn.dataset.action];
  if (handler) handler();
});

/* ---------------------------- flight list ----------------------------- */

const leaf = (path) => path.replace(/[\\/]+$/, '').split(/[\\/]/).pop();

function renderSurveyField() {
  const field = $('#survey-folder');
  field.value = state.roots.length === 1
    ? state.roots[0]
    : state.roots.map(leaf).join('  +  ');
  field.title = state.roots.join('\n');
}

function renderFlights() {
  const list = $('#flight-list');
  const head = $('#flight-head');
  list.textContent = '';

  head.classList.toggle('hidden', state.flights.length === 0);

  if (state.flights.length === 0) {
    /* Nothing chosen yet needs no announcement - the empty field above says
       it. Only the surprising case gets words. */
    if (state.roots.length) {
      const empty = document.createElement('div');
      empty.className = 'list-empty';
      empty.textContent = 'No flights found there.';
      list.appendChild(empty);
    }
    list.classList.toggle('hidden', !state.roots.length);
    updateTally();
    return;
  }

  /* With one search root the relative path is unambiguous. With several it is
     not - two cards can hold identically named missions - so the root's own
     name comes back in front. */
  const prefixed = state.roots.length > 1;

  list.classList.remove('hidden');

  state.flights.forEach((flight, i) => {
    const coverage = state.coverage[flight.path];
    const flagged = coverage && coverage.outside > 0;

    const row = document.createElement('div');
    row.className = 'list-item' + (flight.on ? '' : ' off') +
                    (flagged ? ' warn' : '');

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = flight.on;
    box.style.accentColor = 'var(--accent)';

    const name = document.createElement('span');
    name.className = 'fname';
    name.textContent = prefixed ? leaf(flight.root) + '\\' + flight.name
                                : flight.name;
    name.title = flight.path;

    const count = document.createElement('span');
    count.className = 'fcount';
    count.textContent = flight.photos + ' photos';

    const mrk = document.createElement('span');
    mrk.className = 'fmrk ' + (flight.mrk ? 'ok' : 'bad');
    mrk.textContent = flight.mrk ? 'MRK ✓' : 'MRK ✗';
    if (!flight.mrk) mrk.title = 'No MRK file - timestamps cannot be matched.';

    row.append(box, name, count);

    /* Directly after the count, because it is a statement about those
       photos - not a separate status alongside the MRK check. */
    if (flagged) {
      const warn = document.createElement('button');
      warn.className = 'fwarn';
      warn.textContent = '▲';
      warn.title = coverage.outside + ' of ' + coverage.exposures +
                   ' exposures outside base coverage - click for details';
      warn.addEventListener('click', (ev) => {
        ev.stopPropagation();      /* not a row toggle */
        showCoverage(flight);
      });
      row.appendChild(warn);
    }

    row.appendChild(mrk);

    /* The whole row toggles, not just the box - a 14 px target in a list this
       dense is needlessly fiddly. */
    row.addEventListener('click', (ev) => {
      if (ev.target !== box) flight.on = !flight.on;
      else flight.on = box.checked;
      flightsChanged();
    });

    list.appendChild(row);
  });

  updateTally();
}

function selectedFlights() {
  return state.flights.filter((f) => f.on);
}

/* ------------------------------- modal -------------------------------- */

function openModal(title, bodyNodes) {
  $('#modal-title').textContent = title;
  const body = $('#modal-body');
  body.textContent = '';
  bodyNodes.forEach((node) => body.appendChild(node));
  $('#modal').classList.remove('hidden');
}

function para(text, bold) {
  const p = document.createElement('p');
  p.style.margin = '0 0 6px';
  if (bold) { const b = document.createElement('b'); b.textContent = text; p.appendChild(b); }
  else p.textContent = text;
  return p;
}

/* Failures are shown, not merely logged: the step that follows a failed one
   is always a button the user is about to press in vain. */
function showError(title, message) {
  const body = para(message || 'No detail was reported.');
  body.style.margin = '0';
  openModal(title, [body]);
}

$('#modal').addEventListener('click', (ev) => {
  if (ev.target === $('#modal')) $('#modal').classList.add('hidden');
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') $('#modal').classList.add('hidden');
});

/* ------------------------------ coverage ------------------------------ */

/* PPK can only correct an exposure the base station was observing at the
   time. Asking now costs a MRK read; finding out during the solve costs the
   ephemeris download and minutes of RTKLIB first. */
async function checkCoverage() {
  if (!state.flights.length) return;

  const result = await pywebview.api.check_coverage(
    state.flights.map((f) => f.path));

  if (!result.available) { state.coverage = {}; renderFlights(); return; }

  state.coverage = {};
  let flagged = 0;
  result.flights.forEach((f) => {
    state.coverage[f.path] = f;
    if (f.outside > 0) flagged++;
  });

  log('[INFO] Base observations cover ' + result.base_start + ' to ' +
      result.base_end + '.');
  if (flagged) {
    log('[WARN] ' + flagged + ' flight(s) have exposures outside that window.');
  }
  renderFlights();
}

function showCoverage(flight) {
  const info = state.coverage[flight.path];
  if (!info) return;

  const nodes = [para(flight.name, true)];

  /* Which window failed matters: one is somebody else's receiver, the other
     is this aircraft, and they are fixed in completely different ways. */
  if (info.outside_base) {
    nodes.push(para(info.outside_base + ' of ' + info.exposures +
      ' exposures fall outside the base station observation window - there is '
      + 'no simultaneous base data to correct them against.'));
  }
  if (info.outside_rover) {
    nodes.push(para(info.outside_rover + ' of ' + info.exposures +
      ' fall outside the aircraft\'s own observation window - the shutter '
      + 'fired before or after its GNSS logging, so there is no trajectory to '
      + 'interpolate onto. Common for a test shot taken at power-up.'));
  }
  nodes.push(para('Affected images come out with NaN positions.'));

  if (info.names) {
    const box = document.createElement('div');
    box.className = 'names';
    box.textContent = info.names.join('\n') +
      (info.truncated ? '\n... and more' : '');
    nodes.push(box);
  } else {
    nodes.push(para('Photo count does not match the MRK record count, so ' +
                    'filenames cannot be attributed reliably.'));
  }

  openModal('Outside base coverage', nodes);
}

/* ------------------------------ settings ------------------------------ */

/* Addresses are remembered only after a submission the service accepted. An
   address that never worked is not worth offering back, and a typo caught at
   validation would otherwise sit in the list forever. */
const MAX_REMEMBERED_EMAILS = 8;

async function loadSettings() {
  const settings = await pywebview.api.load_settings();

  /* Worker count offered up to the machine's own sensible ceiling, not an
     arbitrary list - one core stays free, and past four the disk is the
     limit anyway. */
  const cpu = await pywebview.api.suggest_workers();
  const select = $('#workers');
  select.textContent = '';
  for (let n = 1; n <= cpu.max; n++) {
    const option = document.createElement('option');
    option.value = n;
    option.textContent = n === 1 ? 'off (one at a time)' : n + ' flights at once';
    select.appendChild(option);
  }
  select.value = String(settings.workers || cpu.suggested);
  $('#workers-hint').textContent =
    cpu.cores + ' cores  ·  up to ' + cpu.max;
  select.addEventListener('change', () =>
    pywebview.api.save_settings({ workers: parseInt(select.value, 10) }));

  /* Remembered, not prefilled. The field starts empty and the list appears
     when the field is clicked - filling it in would put an address into a
     submission the user never looked at. */
  state.emails = settings.emails || [];
  renderEmailHistory();

  /* Free text here was unanswerable: the module accepts a fixed set of tokens
     and nothing on screen said which. Offered from that same table so the two
     cannot drift apart. */
  const frames = await pywebview.api.list_frames();
  const frameSelect = $('#man-frame');
  frameSelect.textContent = '';
  frames.forEach((frame) => {
    const option = document.createElement('option');
    option.value = frame.token;
    option.textContent = frame.token + '  —  ' + frame.name +
                         (frame.ambiguous ? '  (no realization named)' : '');
    frameSelect.appendChild(option);
  });
  frameSelect.value = 'NAD83';
}

/* Own dropdown rather than a <datalist>: the native one is drawn by the
   browser in its own style and looks pasted on. This one also allows an
   address to be forgotten, which a datalist cannot offer at all. */
let suggestIndex = -1;

/* Populates the list; visibility is the caller's decision. Conflating the two
   is what made the dropdown appear on start-up, over a section that was still
   locked - loading the saved addresses is not the user asking to see them. */
function renderEmailHistory() {
  const box = $('#email-suggest');
  const typed = $('#ppp-email').value.trim().toLowerCase();
  const matches = state.emails.filter(
    (a) => !typed || (a.toLowerCase().includes(typed) && a.toLowerCase() !== typed));

  box.textContent = '';
  if (!matches.length) { box.classList.add('hidden'); return; }

  matches.forEach((address, i) => {
    const item = document.createElement('li');
    if (i === suggestIndex) item.classList.add('active');

    const text = document.createElement('span');
    text.textContent = address;

    const forget = document.createElement('button');
    forget.className = 'forget';
    forget.textContent = '✕';
    forget.title = 'Forget this address';
    forget.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      state.emails = state.emails.filter((a) => a !== address);
      pywebview.api.save_settings({ emails: state.emails });
      showSuggestions();
    });

    item.append(text, forget);
    /* mousedown, not click: blur would close the list first. */
    item.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      chooseEmail(address);
    });
    box.appendChild(item);
  });
}

/* Only ever from a deliberate action on the field itself. */
function showSuggestions() {
  renderEmailHistory();
  if ($('#email-suggest').children.length) {
    $('#email-suggest').classList.remove('hidden');
  }
}

function chooseEmail(address) {
  $('#ppp-email').value = address;
  hideSuggestions();
  refresh();
}

function hideSuggestions() {
  suggestIndex = -1;
  $('#email-suggest').classList.add('hidden');
}

(function initEmailSuggestions() {
  const field = $('#ppp-email');

  field.addEventListener('focus', () => { suggestIndex = -1; showSuggestions(); });
  field.addEventListener('input', () => { suggestIndex = -1; showSuggestions(); });
  field.addEventListener('blur', hideSuggestions);

  field.addEventListener('keydown', (ev) => {
    const items = $$('#email-suggest li');
    if (!items.length || $('#email-suggest').classList.contains('hidden')) return;

    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      const step = ev.key === 'ArrowDown' ? 1 : -1;
      suggestIndex = (suggestIndex + step + items.length + 1) % (items.length + 1) - 1;
      renderEmailHistory();
    } else if (ev.key === 'Enter' && suggestIndex >= 0) {
      ev.preventDefault();
      chooseEmail(items[suggestIndex].firstChild.textContent);
    } else if (ev.key === 'Escape') {
      hideSuggestions();
    }
  });
})();

function rememberEmail(address) {
  const cleaned = (address || '').trim();
  if (!cleaned) return;
  /* Most recent first, no duplicates. */
  state.emails = [cleaned, ...state.emails.filter((e) => e !== cleaned)]
    .slice(0, MAX_REMEMBERED_EMAILS);
  renderEmailHistory();
  pywebview.api.save_settings({ emails: state.emails });
}

/* ------------------------------- about -------------------------------- */

/* The disclaimer is the substantive half. BSD-2-Clause already says there is
   no warranty; what a surveyor needs told is narrower and specific to these
   numbers - what the uncertainties are, and what they are not. */
const REPO_URL = 'https://github.com/geo-raypan/dji-geotagger';

async function showAbout() {
  const info = await pywebview.api.about();
  const nodes = [];

  const para2 = (html) => {
    const p = document.createElement('p');
    p.style.margin = '0 0 7px';
    p.innerHTML = html;
    return p;
  };
  const head = (text) => {
    const h = document.createElement('p');
    h.className = 'kv-group';
    h.textContent = text;
    return h;
  };

  /* Title row, with the repository on the right. The mark is inlined rather
     than fetched: the page is served from localhost and nothing here should
     depend on being online. */
  const title = document.createElement('div');
  title.className = 'about-head';
  title.innerHTML =
    '<b>dji-geotagger ' + info.version + '</b>'
    + '<a class="gh" href="#" title="' + REPO_URL + '">'
    + '<svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true">'
    + '<path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 '
    + '5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-'
    + '2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.'
    + '58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-'
    + '.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.'
    + '21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 '
    + '2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-'
    + '3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.'
    + '38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg></a>';

  title.querySelector('.gh').addEventListener('click', (ev) => {
    ev.preventDefault();
    pywebview.api.open_url(REPO_URL);
  });

  nodes.push(title);
  nodes.push(para2(info.licence));

  nodes.push(head('What the uncertainties are'));
  nodes.push(para2(
    'They are <b>formal</b> uncertainties: the CSRS-PPP solution for the base '
    + 'combined with RTKLIB\'s own stochastic model for the flight. They '
    + 'describe the consistency of the solution, not its agreement with the '
    + 'ground.'));
  nodes.push(para2(
    'RTKLIB\'s figures are known to be optimistic, particularly for fixed '
    + 'ambiguities. A delivery that depends on the numbers should be checked '
    + 'against independent control.'));
  nodes.push(para2(
    'The base coordinate refers to the ground mark only if an antenna height '
    + 'was entered; otherwise it refers to the antenna reference point.'));

  nodes.push(head('No warranty'));
  nodes.push(para2(
    'Provided as is, without warranty of any kind. The author is not liable '
    + 'for any claim or damages arising from its use. Verifying that a result '
    + 'is fit for its purpose remains with whoever delivers it.'));

  nodes.push(head('Third-party components'));
  info.third_party.forEach((item) => {
    nodes.push(para2('<b>' + item.name + '</b> &mdash; ' + item.licence
      + '<br><span style="color:var(--ink-faint)">' + item.who + '</span>'
      + (item.where ? '<br><span style="color:var(--ink-faint);'
                      + 'font-family:Consolas,monospace;font-size:10px">'
                      + item.where + '</span>' : '')));
  });

  openModal('About', nodes);
}

/* ----------------------------- results -------------------------------- */

async function showResult() {
  const result = await pywebview.api.camera_points();

  /* The result is the thing to look at, so the map comes forward first.
     Fitting while the pane is still hidden makes Leaflet compute the zoom
     against a stale container size, and invalidateSize afterwards corrects
     the size without recomputing the fit - which is why the view came out
     too close no matter what the zoom ceiling was. */
  showTab('map');
  MapView.refresh();

  /* The MRK tracks were a preview of where the flights went; the corrected
     centres are the answer. Keeping both would draw two lines a metre apart
     and invite reading the wrong one. */
  MapView.setTracks([], false);
  MapView.setCameras(result.points);

  if (result.shown < result.total) {
    log('Map shows ' + result.shown + ' of ' + result.total +
        ' camera centres; the rest would be under the same pixels.');
  }
}

function showTab(name) {
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  $$('.tab-pane').forEach((p) => p.classList.toggle('active', p.dataset.tab === name));
}

/* Percentiles, not a mean: a survey is judged on its worst usable fraction,
   and an average hides a bad segment inside a good number. */
async function showStatistics() {
  const s = await pywebview.api.run_statistics();
  if (!s) return;

  const nodes = [];
  const section = (title, rows) => {
    const head = document.createElement('p');
    head.className = 'kv-group';
    head.textContent = title;
    nodes.push(head);
    const list = document.createElement('dl');
    list.className = 'kv';
    rows.forEach(([k, v]) => {
      const dt = document.createElement('dt');
      dt.textContent = k;
      const dd = document.createElement('dd');
      dd.textContent = v;
      list.append(dt, dd);
    });
    nodes.push(list);
  };

  const cm = (v) => (v == null ? '—' : (v * 100).toFixed(1) + ' cm');

  section('Output', [
    ['Camera centres', s.rows.toLocaleString()],
    ['Without a position', s.missing
      ? s.missing + '  (outside the base observation window)' : '0'],
    ['Flights', String(Object.keys(s.flights).length)],
    ['File', s.output || '—'],
  ]);

  const axes = [['East', 'E'], ['North', 'N'], ['Up', 'U']];
  section('Uncertainty  ·  ' + s.sigma_label.replace('sigma_E', '1σ')
          .replace('_', ' '), axes.map(([label, key]) => {
    const v = s.sigma[key];
    return [label, v ? 'median ' + cm(v.median) + '   ·   95th ' + cm(v.p95)
                       + '   ·   worst ' + cm(v.max) : '—'];
  }));

  const status = Object.entries(s.status);
  if (status.length) {
    const total = status.reduce((n, [, v]) => n + v, 0);
    section('Onboard RTK at exposure', status.map(([k, v]) =>
      [k, v.toLocaleString() + '   (' + (100 * v / total).toFixed(1) + '%)']));
  }

  const flights = Object.entries(s.flights);
  if (flights.length > 1) {
    section('Per flight', flights.map(([k, v]) => [k, v.toLocaleString()]));
  }

  openModal('Run statistics', nodes);
}

/* ---------------------------- CRS picker ------------------------------ */

/* Two things make this more than a list of seven thousand EPSG codes:
 *
 *  - it is filtered to systems covering the resolved base station, and
 *  - highlighting one runs the real transform_coordinates against it, so a
 *    system that would throw after a ten-minute run cannot be chosen at all.
 *
 * The verdict is what the description pane shows, and OK stays disabled until
 * something has passed. */
const crs = {
  groups: {},
  open: { Projected: true },
  selected: null,
  checking: 0,
  usable: {},        // code -> true/false once checked
  onlyUsable: true,
};

/* Everything the picker needs is known as soon as the base is resolved, so it
   is worked out then rather than when the dialog opens. Checking 150 systems
   takes a few seconds; spending them while the user is choosing flights costs
   nothing, spending them after a click is a dialog that hangs. */
async function loadCrs(nearBase, onProgress) {
  const listing = await pywebview.api.list_crs(nearBase);
  crs.groups = listing.groups;
  crs.usable = {};
  crs.forArea = nearBase;

  /* In batches, so a folder scan or a track read can get a turn on the bridge
     between them rather than waiting for the whole sweep. */
  const order = Object.keys(crs.groups)
    .sort((a, b) => (crs.open[b] ? 1 : 0) - (crs.open[a] ? 1 : 0));

  for (const title of order) {
    const codes = crs.groups[title].map((e) => e.code);
    for (let i = 0; i < codes.length; i += 20) {
      const batch = codes.slice(i, i + 20);
      Object.assign(crs.usable, await pywebview.api.validate_many(batch));
      if (onProgress) onProgress();
    }
  }
  crs.ready = true;
}

/* Why a system a user can see in QGIS is greyed out here. Written out rather
   than left to the per-entry verdict, because the two causes are general and
   the answer is usually "use the versioned twin" or "you already have it". */
function explainCrs() {
  const nodes = [];

  const section = (title, lines) => {
    const head = document.createElement('p');
    head.className = 'kv-group';
    head.textContent = title;
    nodes.push(head);
    lines.forEach((text) => {
      const p = document.createElement('p');
      p.style.margin = '0 0 7px';
      p.innerHTML = text;
      nodes.push(p);
    });
  };

  section('Two reasons a system is refused', [
    '<b>1 &mdash; It names a datum ensemble.</b> "WGS 84" is not one datum but '
    + 'a family (G730 &hellip; G2296) whose members differ by metres; EPSG '
    + 'states the ensemble accuracy as 2 m. Asking to transform "into WGS 84" '
    + 'does not say which member, so PROJ picks a candidate, and here it picks '
    + 'one that shifts the coordinates by 1.60 m.',

    '<b>2 &mdash; The datum names no realization.</b> "NAD83 / UTM zone 12N" '
    + 'and "NAD83(CSRS) / UTM zone 12N" have no rigorous path from ITRF, so '
    + 'PROJ falls back to a <i>ballpark</i> shift: it returns the coordinates '
    + 'essentially unchanged while relabelling them. The error is the size of '
    + 'the datum difference &mdash; 1.63 m at this site &mdash; with nothing in '
    + 'the output to show it.',
  ]);

  section('WGS 84 / World Mercator, specifically', [
    'The correct ITRF2020 &rarr; WGS 84 operation displaces <b>0.000 m</b>: '
    + 'WGS 84(G2296) is aligned to ITRF2020 at the centimetre level by '
    + 'definition.',

    'So the <code>cam_lat</code> / <code>cam_lon</code> already in the output '
    + '<b>are</b> WGS 84 &mdash; and better than the 2 m the ensemble itself '
    + 'claims. For Google Earth, QGIS or Metashape, use them directly; there '
    + 'is nothing to convert.',

    'What is blocked is not the transformation but PROJ choosing the wrong '
    + 'candidate for it.',
  ]);

  section('What to use instead', [
    '<b>Versioned works, generic does not.</b> EPSG:2956 is refused, '
    + 'EPSG:22812 &mdash; the same grid on NAD83(CSRS)v8 &mdash; is not. '
    + 'ETRS89 as 4936 is refused; ETRF2020 as 10571 is not.',

    'If the grid you need exists only against an unversioned datum &mdash; '
    + 'Alberta 3TM is published that way &mdash; use <b>Define&hellip;</b> to '
    + 'lift it onto a versioned one. Verified against NRCan: rebased northing '
    + 'matched exactly, while EPSG:3780 used directly was out by 1.60 m.',

    'Or export in the solved frame. It is tagged with its frame and epoch, '
    + 'which is lossless &mdash; any other system can still be derived from it '
    + 'later.',
  ]);

  openModal('Why some coordinate systems are unavailable', nodes);
}

function crsSummary() {
  const total = Object.keys(crs.usable).length;
  const usable = Object.values(crs.usable).filter(Boolean).length;
  return usable + ' of ' + total + ' can be used from this frame. The rest '
    + 'would need a ballpark shift or sit on a datum ensemble.';
}

/* Started after a resolve; failures are logged and simply leave the picker to
   do the work itself when it opens. */
async function prewarmCrs() {
  crs.ready = false;
  try {
    await loadCrs(true, null);
    log('[INFO] Coordinate systems checked: ' + crsSummary());
  } catch (err) {
    log('[WARN] Could not pre-check coordinate systems: ' + err);
  }
}

async function openCrsPicker() {
  $('#crs-modal').classList.remove('hidden');
  $('#crs-define').classList.add('hidden');
  crs.selected = null;
  $('#crs-ok').disabled = true;

  /* Without a base there is no source frame, so nothing can be judged and the
     list would be a page of greyed-out entries with no explanation. */
  if (!state.basePosition) {
    $('#crs-tree').textContent = '';
    crsVerdict('Resolve the base station first. Whether a target can be '
               + 'reached depends on the frame the data is in, so there is '
               + 'nothing to check against yet.', false);
    return;
  }

  const nearBase = !$('#crs-all').checked;
  if (crs.ready && crs.forArea === nearBase) {
    renderCrsTree();
    crsVerdict(crsSummary(), null);
    return;
  }

  $('#crs-tree').textContent = '';
  crsVerdict('Checking which coordinate systems can be used...', null);

  /* Anything thrown across the bridge would otherwise vanish into an
     unhandled rejection, leaving a dialog that simply does nothing. */
  try {
    await loadCrs(nearBase, renderCrsTree);
    renderCrsTree();
    crsVerdict(crsSummary(), null);
  } catch (err) {
    crsVerdict('Could not list coordinate systems.\n' + err, false);
    log('[ERROR] list_crs: ' + err);
  }
}

/* Empty means hidden, not an empty box: a pane reserving eighty pixels to say
   "select something" is telling the user what they were already doing. */
function crsVerdict(text, ok) {
  const pane = $('#crs-verdict');
  pane.textContent = text || '';
  pane.classList.toggle('hidden', !text);
  pane.classList.toggle('ok', ok === true);
  pane.classList.toggle('bad', ok === false);
}

function renderCrsTree() {
  const tree = $('#crs-tree');
  const needle = $('#crs-search').value.trim().toLowerCase();
  tree.textContent = '';
  let shown = 0;

  /* Systems that cannot be reached from this frame are hidden by default -
     with most of a regional list refused, showing them buries the few that
     work. The toggle brings them back for anyone hunting a specific code and
     wanting to know why it is missing. */
  const showBad = $('#crs-show-bad').checked;

  Object.entries(crs.groups).forEach(([title, entries]) => {
    const matches = entries.filter((e) =>
      (!needle || e.name.toLowerCase().includes(needle) ||
       e.code.toLowerCase().includes(needle)) &&
      (showBad || crs.usable[e.code] !== false));
    if (!matches.length) return;
    shown += matches.length;

    /* A search implies you want to see what matched. */
    const expanded = needle ? true : Boolean(crs.open[title]);

    const head = document.createElement('div');
    head.className = 'crs-group';
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = expanded ? '▼' : '▶';
    const label = document.createElement('span');
    label.textContent = title;
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = matches.length;
    head.append(caret, label, count);
    head.addEventListener('click', () => {
      crs.open[title] = !expanded;
      renderCrsTree();
    });
    tree.appendChild(head);

    if (!expanded) return;
    matches.forEach((entry) => {
      const usable = crs.usable[entry.code];

      /* Refused entries stay visible but marked. Hiding them would leave the
         user hunting for a code they can see in QGIS and wondering why it is
         missing; the point is to say why it cannot be used. */
      const item = document.createElement('div');
      item.className = 'crs-item' +
        (crs.selected === entry.code ? ' selected' : '') +
        (usable === false ? ' unusable' : '');

      const mark = document.createElement('span');
      mark.className = 'cmark';
      mark.textContent = usable === true ? '✓' : usable === false ? '✗' : '';

      const name = document.createElement('span');
      name.className = 'cname';
      name.textContent = entry.name;
      name.title = entry.name;

      const code = document.createElement('span');
      code.className = 'ccode';
      code.textContent = entry.code;

      item.append(mark, name, code);
      item.addEventListener('click', () => selectCrs(entry));
      tree.appendChild(item);
    });
  });

  if (!shown) {
    const empty = document.createElement('div');
    empty.className = 'crs-empty';
    empty.textContent = 'Nothing matches that filter.';
    tree.appendChild(empty);
  }
}

async function selectCrs(entry) {
  crs.selected = entry.code;
  crs.selectedName = entry.name;
  renderCrsTree();
  $('#crs-ok').disabled = true;
  crsVerdict('Checking ' + entry.code + '...', null);

  /* Verdicts arrive out of order if the user keeps clicking; only the newest
     one is allowed to write to the pane. */
  const ticket = ++crs.checking;
  let result;
  try {
    result = await pywebview.api.validate_crs(entry.code);
    log('[INFO] verdict ' + entry.code + ': ' + JSON.stringify(result));
  } catch (err) {
    crsVerdict('Check failed.\n' + err, false);
    log('[ERROR] validate_crs(' + entry.code + '): ' + err);
    return;
  }
  if (ticket !== crs.checking) return;

  if (!result || !result.ok) {
    crsVerdict('✗  ' + ((result && result.reason) || 'no verdict returned'), false);
    return;
  }

  const lines = ['✓  ' + result.name];
  if (result.operation) lines.push('Operation: ' + result.operation);
  if (result.accuracy != null) {
    lines.push('Stated accuracy: ' + Number(result.accuracy).toFixed(3) + ' m');
  }
  if (result.shift != null) {
    lines.push('Datum shift at the base: ' + Number(result.shift).toFixed(3) + ' m');
  }
  if (result.epoch != null) lines.push('Epoch: ' + Number(result.epoch).toFixed(4));
  if (result.sigma_transformed) lines.push('Uncertainty is rotated with the grid.');
  crsVerdict(lines.join('\n'), true);
  $('#crs-ok').disabled = false;
}

$('#crs-search').addEventListener('input', renderCrsTree);
$('#crs-show-bad').addEventListener('change', renderCrsTree);
$('#crs-all').addEventListener('change', openCrsPicker);

$$('input[name=crs-kind]').forEach((radio) => {
  radio.addEventListener('change', () => {
    $$('.def-pane').forEach((pane) => {
      pane.classList.toggle('hidden', pane.dataset.kind !== radio.value);
    });
  });
});

/* --------------------------- base details ----------------------------- */

async function showBaseDetails() {
  const base = await pywebview.api.base_details();
  if (!base) return;

  const k = parseFloat($('#confidence').value);
  const label = $('#confidence').selectedOptions[0].textContent.split(' (')[0];

  const metres = (v) => (v == null ? '—' : v.toFixed(4) + ' m');

  const groups = [
    ['Solution', [
      ['Frame', base.coord_sys || '—'],
      ['Epoch', (base.epoch || '—') +
        (base.epoch_decimal_year ? '   ' + base.epoch_decimal_year.toFixed(5) : '')],
      ['Epoch propagated', base.epoch_propagated
        ? 'yes — ' + (base.velocity_model || 'velocity model') : 'no'],
      ['Source', base.source_detail || base.source || '—'],
    ]],
    ['Position', [
      ['Latitude', dms(base.lat_dd, 'N', 'S')],
      ['Longitude', dms(base.lon_dd, 'E', 'W')],
      ['Ellipsoidal height', metres(base.hgt)],
      ['ECEF X', metres(base.X)],
      ['ECEF Y', metres(base.Y)],
      ['ECEF Z', metres(base.Z)],
    ]],
    ['Uncertainty  ·  ' + label, [
      ['East', base.sigma_ENU ? metres(base.sigma_ENU[0] * k) : '—'],
      ['North', base.sigma_ENU ? metres(base.sigma_ENU[1] * k) : '—'],
      ['Up', base.sigma_ENU ? metres(base.sigma_ENU[2] * k) : '—'],
      ['From the solution', base.uncertainty_available ? 'yes' : 'no — assumed'],
    ]],
  ];

  const nodes = [];
  groups.forEach(([title, rows]) => {
    const heading = document.createElement('p');
    heading.className = 'kv-group';
    heading.textContent = title;
    nodes.push(heading);

    const list = document.createElement('dl');
    list.className = 'kv';
    rows.forEach(([key, value]) => {
      const dt = document.createElement('dt');
      dt.textContent = key;
      const dd = document.createElement('dd');
      dd.textContent = value;
      list.append(dt, dd);
    });
    nodes.push(list);
  });

  if (base.report) {
    const open = document.createElement('button');
    open.className = 'btn';
    open.style.marginTop = '12px';
    open.textContent = 'Open PDF report';
    open.addEventListener('click', () => pywebview.api.open_path(base.report));
    nodes.push(open);
  } else {
    nodes.push(para('No PDF report alongside the summary file.'));
  }

  openModal('Base station', nodes);
}

/* ------------------------------- tracks ------------------------------- */

/* MRK positions are the drone's own RTK fixes, so they need no processing at
   all - which is the point. Seeing the flight lines is the earliest possible
   check that the right folders were picked. */
async function drawTracks() {
  const wanted = selectedFlights();
  const missing = wanted.filter((f) => !(f.path in state.tracks));

  if (missing.length) {
    const fetched = await pywebview.api.flight_tracks(missing.map((f) => f.path));
    fetched.forEach((track) => { state.tracks[track.path] = track; });

    /* Remember the misses too, or every redraw re-reads a folder whose MRK
       could not be parsed. */
    missing.forEach((f) => {
      if (!(f.path in state.tracks)) state.tracks[f.path] = null;
    });
  }

  MapView.setTracks(
    wanted
      .map((f) => {
        const track = state.tracks[f.path];
        return track && { ...track, name: f.name };
      })
      .filter(Boolean),
    missing.length > 0        /* fit only when something new arrived */
  );
}

function updateTally() {
  const on = selectedFlights();
  const photos = on.reduce((sum, f) => sum + f.photos, 0);
  $('#flight-tally').textContent = state.flights.length
    ? on.length + ' of ' + state.flights.length + ' selected  ·  ' +
      photos.toLocaleString() + ' photos'
    : '';

  const all = $('#chk-all');
  all.checked = on.length === state.flights.length && state.flights.length > 0;
  all.indeterminate = on.length > 0 && on.length < state.flights.length;
}

$('#chk-all').addEventListener('change', (ev) => {
  state.flights.forEach((f) => { f.on = ev.target.checked; });
  flightsChanged();
});

function flightsChanged() {
  renderFlights();
  refresh();
  drawTracks();
  if (state.basePosition) checkCoverage();
}

/* ------------------------------ enabling ------------------------------ */

/* A run needs base observations, a resolvable base position and at least one
   selected flight. Checking it here keeps the reason for a disabled button in
   one place rather than scattered through the handlers. */
function baseSourceReady() {
  const mode = $('input[name=base-mode]:checked').value;
  if (mode === 'sum')    return state.sumFile !== null;
  if (mode === 'manual') return $('#man-lat').value !== '' &&
                                $('#man-lon').value !== '' &&
                                $('#man-hgt').value !== '';
  return $('#ppp-email').value.trim() !== '';
}

/* Steps run in order, and a later one configured against a missing earlier one
   is a run that fails minutes in. Prerequisites are stated on the card rather
   than left for the user to deduce from a greyed-out control. */
function setStepLock(step, reason) {
  const card = document.querySelector(`.card[data-step="${step}"]`);
  if (!card) return;

  card.classList.toggle('locked', Boolean(reason));

  let hint = card.querySelector('.lock-hint');
  if (reason) {
    if (!hint) {
      hint = document.createElement('span');
      hint.className = 'lock-hint';
      card.querySelector('h2').appendChild(hint);
    }
    hint.textContent = reason;
  } else if (hint) {
    hint.remove();
  }
}

function refresh() {
  /* Everything is frozen while anything is going, resolves included. The job
     took its settings when it started; leaving them editable invites changing
     one and believing the result reflects it. Cancel, where it applies, stays
     available in the footer. */
  if (state.running) {
    const why = state.resolving ? 'resolving' : 'running';
    [1, 2, 3, 4].forEach((step) => setStepLock(step, why));
  } else if (state.finished) {
    /* The solve is done and its inputs are settled. Delivery settings are
       not: a different CRS or confidence level is a second of work from the
       result already in memory, so step 4 stays open and the others close. */
    [1, 2, 3].forEach((step) => setStepLock(step, 'run complete'));
    setStepLock(4, null);
  } else {
    setStepLock(1, null);
    setStepLock(2, state.baseFile ? null : 'needs base observations');
    setStepLock(3, state.basePosition ? null : 'needs a resolved base');
    setStepLock(4, !state.basePosition ? 'needs a resolved base'
                  : selectedFlights().length === 0 ? 'needs at least one flight'
                  : null);
  }

  $('[data-action=resolve]').disabled =
    state.running || !baseSourceReady() ||
    ($('input[name=base-mode]:checked').value === 'online' && !state.baseFile);

  /* Run needs a base position that a human has already looked at. That is the
     whole point of the two-button split, so it is enforced rather than
     resolved silently on the way past. */
  $('[data-action=run]').disabled =
    state.running || !state.basePosition ||
    selectedFlights().length === 0 || !state.outFile;

  /* Cancel is shown only where it can actually take effect. resolve_base_position()
     has no progress hook, so there is no checkpoint to stop at during a PPP
     wait, and a button that silently does nothing is worse than no button. */
  $('[data-action=cancel]').classList.toggle(
    'hidden', !(state.running && state.cancellable));

  /* Run gives way to the result once there is one. Changing any input clears
     it, because then the file on disk no longer matches what is on screen. */
  const finished = Boolean(state.finished) && !state.running;
  $('#btn-restart').classList.toggle('hidden', !finished);
  $('#btn-stats').classList.toggle('hidden', !finished);
  $('#btn-run').classList.toggle('hidden', finished);

  /* Re-export takes the place of Open until the file on disk matches the
     settings on screen again. */
  $('#btn-reexport').classList.toggle('hidden', !(finished && state.exportDirty));
  $('#btn-open').classList.toggle('hidden', !(finished && !state.exportDirty));
}

$$('#ppp-email, #man-lat, #man-lon, #man-hgt').forEach((el) => {
  el.addEventListener('input', refresh);
});

/* Antenna height is a tape measurement, so it reads in centimetres: the wheel
   and arrows move by 0.1 m and the value settles at two decimals. Formatting
   on `change` rather than `input` leaves typing alone - reformatting mid-entry
   would rewrite "2." to "2.00" before the user reaches the decimals. Finer
   values can still be typed; only the step is coarse. */
$('#antenna-height').addEventListener('change', (ev) => {
  const value = parseFloat(ev.target.value);
  ev.target.value = Number.isFinite(value) ? value.toFixed(2) : '';
});

/* --------------------------- pipeline events --------------------------- */

/* The pipeline reports at two granularities at once: geotag() counts flights,
   and inside each one rnx2rtkp counts epochs. Taken literally the bar would
   alternate between "3 of 19" and "PPK 45%" and jump on every message, so the
   inner fraction is folded into the outer position here:
 *
 *     overall = (flights done + fraction of the current one) / flights
 *
 * Composition belongs in the display. The library is right to report both -
 * a caller that wants only the coarse count still gets it. */
/* Where each stage sits within one flight. Proportions from a measured run:
   conversion and image reading are seconds, the RTKLIB solve is minutes. */
const STAGE_SPAN = {
  convert: [0.00, 0.06],
  ppk:     [0.06, 0.88],
  images:  [0.88, 1.00],
};

let outer = null;
let workers = {};        // flight name -> fraction, for flights in progress
let weights = {};        // flight name -> exposures, its share of the work
let doneWeight = 0;
let totalWeight = 0;

/* Flights are not equal - a survey holds 999-photo lines next to 21-photo
   ones - so counting them finished says little about time remaining. The
   second bar weights each flight by its exposure count instead.
 *
 * Exposures rather than observation epochs: the rover RINEX does not exist
 * until its worker converts the raw log, so epoch counts cannot be summed
 * before the run, whereas the MRK was already read to draw the tracks. The
 * two are proportional - both measure how long the aircraft was flying. */
function beginRun(flights) {
  workers = {};
  weights = {};
  doneWeight = 0;
  flights.forEach((f) => {
    const key = leaf(f.path);
    weights[key] = (state.coverage[f.path] || {}).exposures || f.photos || 1;
  });
  totalWeight = Object.values(weights).reduce((a, b) => a + b, 0) || 1;
}

function weightedProgress() {
  const running = Object.keys(workers)
    .reduce((sum, k) => sum + workers[k] * (weights[k] || 0), 0);
  return Math.min(1, (doneWeight + running) / totalWeight);
}

function compose(ev) {
  const fraction = ev.total ? ev.current / ev.total : null;

  /* geotag() counts flights; that is the run's own measure of "how far". */
  if (ev.stage === 'flight') {
    outer = ev.total ? { done: ev.current, total: ev.total } : null;

    /* A finished flight stops being a worker. Matching on the message keeps
       this in one place rather than adding an event type for it. */
    const finished = /^(Finished|Skipped) (.+)$/.exec(ev.message || '');
    if (finished) {
      const key = finished[2];
      delete workers[key];
      doneWeight += weights[key] || 0;
    }

    return {
      stage: 'Flights',
      label: outer ? outer.done + ' of ' + outer.total : '',
      message: ev.message,
      overall: fraction,
      step: weightedProgress(),
      mode: 'busy',
    };
  }

  /* Concurrent flights each report under their own key. Their rows replace
     the single step bar, which means nothing when four are running. */
  /* Work reported against a flight. The top bar counts flights, the second
     tracks the weighted share of the survey actually done - so a run does not
     appear stuck at "3 of 19" for the twenty minutes a large line takes. */
  if (ev.key != null) {
    /* A flight is several stages, each counting from zero. Taken at face
       value the bar reaches 90% on the PPK solve and then drops back to 0
       when image reading starts. Each stage drives its own slice instead,
       sized by how long it actually takes - the solve dominates. */
    const span = STAGE_SPAN[ev.stage];
    if (span && fraction != null) {
      workers[ev.key] = span[0] + (span[1] - span[0]) * fraction;
    } else if (span) {
      workers[ev.key] = Math.max(workers[ev.key] || 0, span[0]);
    }

    const running = Object.keys(workers).length;
    return {
      stage: ev.stage === 'ppk' ? 'PPK' : ev.stage,
      label: outer ? outer.done + ' of ' + outer.total : '',
      message: running > 1 ? running + ' flights solving' : ev.message,
      overall: outer ? outer.done / outer.total : null,
      step: weightedProgress(),
      mode: 'busy',
    };
  }

  return {
    stage: ev.stage,
    message: ev.message,
    overall: outer ? outer.done / outer.total : fraction,
    step: outer ? weightedProgress() : fraction,
    mode: 'busy',
  };
}

/* Everything the Python side has to say arrives here: log records forwarded
   from the library's own logger, progress reports, and terminal results. */
window.onPipelineEvent = function (ev) {
  switch (ev.kind) {

    case 'log':
      log(ev.text, ev.level);
      break;

    case 'progress':
      /* While step 2 is working, its reports stay in step 2. */
      if (state.resolving) {
        if (ev.message) resolveNote(ev.message);
      } else {
        progress(compose(ev));
      }
      break;

    case 'base':
      /* A result for a base station that is no longer the chosen one. It
         cannot be shown - it would put the previous site's coordinates under
         the current file - and it cannot be recovered either. */
      if (ev.token !== state.resolveToken) {
        log('[INFO] Discarded a base position for a superseded selection.');
        break;
      }
      state.basePosition = ev.base;
      state.running = false;
      state.resolving = false;
      renderBase(ev.base);
      setResolveState('ok');
      resolveNote('');
      refresh();
      if ($('input[name=base-mode]:checked').value === 'online') {
        rememberEmail($('#ppp-email').value);
      }
      /* Only now is the base observation window known - if it came from a raw
         log it did not exist as RINEX until a moment ago. */
      checkCoverage();

      /* And only now can a target frame be judged, so the picker's work
         starts here rather than when it is opened. */
      prewarmCrs();
      /* The footer belongs to the run. Step 2 reports in its own card, so
         nothing here touches it - a finished bar above an un-pressed Run
         reads as though the run had already happened. */
      break;

    case 'done':
      state.running = false;
      /* The next thing anyone does after a run is look at the file. Pressing
         Run again is not it, and doing so by reflex would redo twenty
         minutes of work. */
      state.finished = ev.path;
      state.exportDirty = false;
      refresh();
      progress({ stage: 'Complete',
                 message: ev.rows + ' rows written to ' + ev.path, mode: 'done' });
      showResult();
      break;

    case 'cancelled':
      state.running = false;
      refresh();
      progress({ stage: 'Cancelled', message: ev.message, mode: 'failed' });
      break;

    case 'failed':
      if (ev.token !== undefined && ev.token !== state.resolveToken) {
        log('[INFO] Discarded a failure for a superseded selection.');
        break;
      }
      state.running = false;
      log('[ERROR] ' + ev.message);

      /* A failure has to be seen. Left as an inline note it can scroll out of
         view, and the next thing the user does is press a button that cannot
         work. */
      showError(ev.stage || 'Failed', ev.message);

      if (state.resolving) {
        state.resolving = false;
        setResolveState('failed');
        resolveNote(ev.message, true);
        refresh();
        break;                       /* the footer never showed this step */
      }
      refresh();
      progress({ stage: ev.stage || 'Failed', message: ev.message, mode: 'failed' });
      log('[ERROR] ' + ev.message);
      break;
  }
};

/* ------------------------ resolve button states ----------------------- */

/* The button carries the state of the step it starts, so there is no separate
   indicator to keep in step with it:
 *
 *   idle    "Resolve Base"
 *   busy    "Resolving..." with a spinner, disabled
 *   ok      gone, replaced by a one-line summary and a Detail toggle
 *   failed  "Retry", and the reason is already in the footer and the log
 */
/* Everything that only makes sense relative to one base station. Called when
   the base file itself is replaced. */
function resetBelowBase() {
  state.basePosition = null;
  state.sumFile = null;
  state.roots = [];
  state.flights = [];
  state.coverage = {};
  state.tracks = {};
  state.crs = null;
  state.crsName = null;
  state.finished = null;
  if (!state.outFileExplicit) state.outFile = null;

  crs.ready = false;
  crs.groups = {};
  crs.usable = {};

  $('#sum-file').value = '';
  $('#survey-folder').value = '';
  $('#crs').value = '';
  $('#out-file').value = state.outFile || '';

  setResolveState('idle');
  resolveNote('');
  renderSurveyField();
  renderFlights();

  MapView.setTracks([], false);
  MapView.setCameras([]);
  MapView.setBase(null, null);

  idle();                 /* also takes the progress bar off screen */
  refresh();
}

/* One line of state for step 2, shown next to its own button. */
function resolveNote(text, bad) {
  const note = $('#resolve-note');
  note.textContent = text || '';
  note.classList.toggle('hidden', !text);
  note.classList.toggle('bad', Boolean(bad));
}

function setResolveState(mode) {
  const button = $('#btn-resolve');
  const detail = $('#btn-detail');
  const line = $('#resolved-line');

  button.classList.remove('working', 'retry');
  button.disabled = false;

  if (mode === 'busy') {
    button.textContent = 'Resolving...';
    button.classList.add('working');
    button.disabled = true;
    button.classList.remove('hidden');
    detail.classList.add('hidden');
    line.classList.add('hidden');
    return;
  }

  if (mode === 'ok') {
    button.classList.add('hidden');
    detail.classList.remove('hidden');
    line.classList.remove('hidden');
    return;
  }

  if (mode === 'failed') {
    button.textContent = 'Retry';
    button.classList.add('retry');
    button.classList.remove('hidden');
    detail.classList.add('hidden');
    line.classList.add('hidden');
    return;
  }

  button.textContent = 'Resolve Base';
  button.classList.remove('hidden');
  detail.classList.add('hidden');
  line.classList.add('hidden');
  $('#base-result').classList.add('hidden');
}

/* Editing any input the solution depends on invalidates it - the button comes
   back rather than leaving a stale result looking current. */
$$('#ppp-email, #ppp-frame, #ppp-type, #ppp-epoch, #base-file, #sum-file, ' +
   '#antenna-height, #man-lat, #man-lon, #man-hgt, #man-frame, #man-epoch, ' +
   '#man-sh, #man-sv').forEach((el) => {
  el.addEventListener('input', () => {
    if (state.basePosition && !state.running) {
      state.basePosition = null;
      state.finished = null;
      /* The verdicts were computed against the old frame; keeping them would
         offer systems that may no longer be reachable. */
      crs.ready = false;
      setResolveState('idle');
      refresh();
    }
  });
});

/* ---------------------------- base result ----------------------------- */

const dms = (value, positive, negative) => {
  const hemisphere = value >= 0 ? positive : negative;
  const abs = Math.abs(value);
  const d = Math.floor(abs);
  const m = Math.floor((abs - d) * 60);
  const s = (abs - d - m / 60) * 3600;
  return `${d}° ${String(m).padStart(2, '0')}' ${s.toFixed(5).padStart(8, '0')}" ${hemisphere}`;
};

function renderBase(base) {
  const epoch = base.epoch_decimal_year
    ? base.epoch_decimal_year.toFixed(4)
    : (base.epoch || '');
  $('#res-frame').textContent = (base.coord_sys || '?') + (epoch ? ' @ ' + epoch : '');

  /* A propagated epoch is worth calling out: it is the one number in the
     solution that came from a velocity model rather than the observations. */
  const tier = $('#res-tier');
  tier.textContent = base.epoch_propagated
    ? 'propagated · ' + (base.velocity_model || 'velocity model')
    : (base.mode || base.source || '');
  tier.className = 'badge' + (base.epoch_propagated ? ' warn' : '');
  tier.classList.toggle('hidden', !tier.textContent);

  $('#res-lat').textContent = dms(base.lat_dd, 'N', 'S');
  $('#res-lon').textContent = dms(base.lon_dd, 'E', 'W');
  $('#res-hgt').textContent = base.hgt.toFixed(4) + ' m';

  const k = parseFloat($('#confidence').value);
  const label = $('#confidence').selectedOptions[0].textContent.split(' (')[0];
  $('#sigma-label').textContent = label;

  const cells = ['#res-se', '#res-sn', '#res-su'];
  if (base.sigma_ENU) {
    base.sigma_ENU.forEach((s, i) => {
      $(cells[i]).textContent = (s * k).toFixed(3);
    });
  } else {
    cells.forEach((c) => { $(c).textContent = '—'; });
  }

  /* Collapsed summary: frame, epoch and the uncertainty, in centimetres
     because that is the unit the number is argued about in. */
  const sigma = base.sigma_ENU
    ? '  ·  ' + label + ' ' +
      base.sigma_ENU.map((s) => (s * k * 100).toFixed(1)).join(' / ') + ' cm'
    : '';
  $('#resolved-line').textContent =
    '✓ ' + (base.coord_sys || '?') + (epoch ? ' @ ' + epoch : '') + sigma;

  MapView.setBase(base.lat_dd, base.lon_dd,
                  'Base station<br>' + (base.coord_sys || '') +
                  '<br><i>click for details</i>',
                  { onClick: showBaseDetails });
}

/* The multiplier depends on how many components the statement covers, so the
   caveat has to name the level actually selected. Pinned to 95%, it read as
   contradicting the dropdown whenever anything else was chosen. */
const DIMENSION_K = {
  '1': ['68.3% of a single component',
        '39.4% of a horizontal ellipse',
        '19.9% of a spatial ellipsoid'],
  '1.960': ['95% per component (k = 1.960)',
            'a 2-D horizontal ellipse at 95% needs k = 2.448',
            'a 3-D spatial one at 95% needs k = 2.796'],
  '2.576': ['99% per component (k = 2.576)',
            'a 2-D horizontal ellipse at 99% needs k = 3.035',
            'a 3-D spatial one at 99% needs k = 3.368'],
};

/* The icon lives inside its label, so without this a click on it is forwarded
   to the control and opens the dropdown it is there to explain. */
$('.help').addEventListener('click', (ev) => {
  ev.preventDefault();
  ev.stopPropagation();
  ev.target.focus();
});

function updateConfidenceHelp() {
  const rows = DIMENSION_K[$('#confidence').value];
  if (!rows) return;
  $('.help').dataset.tip =
    'Reported figure covers ' + rows[0] + '.\n' +
    'Not a horizontal radius: ' + rows[1] + '.\n' +
    'For a full 3-D statement, ' + rows[2] + '.';
}

/* Changing the confidence level rescales a result already on screen, rather
   than leaving a number labelled with the wrong multiplier. */
$('#confidence').addEventListener('change', () => {
  updateConfidenceHelp();
  if (state.basePosition) renderBase(state.basePosition);
  markExportDirty();
});

/* Anything that changes what the delivered file should contain leaves the one
   on disk out of date - said with a button rather than left for the user to
   remember. */
function markExportDirty() {
  if (!state.finished) return;
  state.exportDirty = true;
  refresh();
}

/* ------------------------------- start -------------------------------- */

window.addEventListener('pywebviewready', () => {
  log('[INFO] Ready.');
  loadSettings();
  refresh();
});

MapView.init();
updateConfidenceHelp();
idle();
setResolveState('idle');
renderSurveyField();
renderFlights();
refresh();
