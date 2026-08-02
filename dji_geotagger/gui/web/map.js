/* Map panel.
 *
 * Leaflet 1.7.1 is vendored under web/vendor - the same copy RTKLIB ships - so
 * the window has no network dependency of its own. Tiles do need the internet;
 * without it the basemap stays blank and the tracks are drawn on plain grey,
 * which is the right way round, because the tracks are the content and the
 * basemap is context.
 */

'use strict';

const MapView = (function () {

  const BASEMAPS = {
    satellite: {
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/' +
           'World_Imagery/MapServer/tile/{z}/{y}/{x}',
      attribution: 'Esri, Maxar, Earthstar Geographics',
      maxZoom: 19,
    },
    osm: {
      url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      attribution: '&copy; OpenStreetMap contributors',
      maxZoom: 19,
    },
    light: {
      url: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',
      attribution: '&copy; OpenStreetMap, &copy; CARTO',
      subdomains: 'abcd', maxZoom: 20,
    },
    dark: {
      url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
      attribution: '&copy; OpenStreetMap, &copy; CARTO',
      subdomains: 'abcd', maxZoom: 20,
    },
  };

  /* Amber marks a flight the drone itself could not hold a fixed ambiguity
     through. It is not a failure - PPK may well recover it - but it is the
     first thing worth looking at, so it is visible before any processing. */
  const TRACK_OK    = { color: '#2f7fd4', weight: 2.5, opacity: 0.9 };
  const TRACK_FLOAT = { color: '#d08a00', weight: 2.5, opacity: 0.95 };

  /* Horizontal uncertainty bands, in metres. Fixed rather than relative to
     the run's own spread: "worse than the rest of this flight" is not the
     question a survey asks, "good enough to deliver" is. */
  const QUALITY = [
    { limit: 0.02, colour: '#1a7f4b', label: 'better than 2 cm' },
    { limit: 0.05, colour: '#8bbf3f', label: '2 to 5 cm' },
    { limit: 0.10, colour: '#d08a00', label: '5 to 10 cm' },
    { limit: Infinity, colour: '#b3261e', label: 'worse than 10 cm' },
  ];

  /* Generous margin, and a ceiling on how far in a fit may go. Without the
     ceiling a compact survey fills the pane edge to edge at maximum zoom,
     which shows the flight lines but not where they are - and where they are
     is most of what a first look is for. */
  const FIT = { padding: [56, 56], maxZoom: 16 };

  let map = null;
  let tileLayer = null;
  let trackLayer = null;
  let cameraLayer = null;
  let baseMarker = null;
  let warnedOffline = false;

  function init() {
    map = L.map('map', { zoomControl: true, attributionControl: true })
           .setView([51.05, -114.07], 4);
    trackLayer = L.layerGroup().addTo(map);
    setBasemap('satellite');

    /* Bound once, on the map. Attaching it to each marker instead relied on
       Leaflet re-firing popupopen on the source layer, and it did not run -
       the preview simply stayed on "loading" with nothing logged. The map
       always fires it. */
    map.on('popupopen', loadThumbnail);
    return map;
  }

  /* The image is fetched only when its point is actually opened: a survey is
     thousands of 20 MB frames and this is one preview. */
  async function loadThumbnail(ev) {
    const say = (msg) => {
      if (typeof log === 'function') log('Thumbnail: ' + msg, 'WARN');
    };

    const popup = ev.popup;
    const p = popup && popup._point;
    if (!p) return;                            /* not a camera popup */
    if (p._thumb !== undefined) return;        /* already resolved once */

    if (!p.path) { p._thumb = null; return; }

    /* Rebuilt through setContent, not by editing the popup's DOM: Leaflet
       keeps the content as a string and rewrites innerHTML from it on every
       update, so an inserted <img> was being replaced by the placeholder it
       had just been put in place of. */
    try {
      const data = await pywebview.api.thumbnail(p.path);
      p._thumb = data || null;
      if (!data) say('none returned for ' + p.path);
    } catch (err) {
      p._thumb = null;
      say(String(err));
    }
    popup.setContent(popupHtml(p));
  }

  function setBasemap(name) {
    const spec = BASEMAPS[name];
    if (!spec || !map) return;
    if (tileLayer) map.removeLayer(tileLayer);
    tileLayer = L.tileLayer(spec.url, {
      attribution: spec.attribution,
      subdomains: spec.subdomains || 'abc',
      maxZoom: spec.maxZoom || 19,
    });
    tileLayer.on('tileerror', () => {
      if (warnedOffline) return;
      warnedOffline = true;
      note('Basemap tiles unavailable - offline, or the tile server is ' +
           'unreachable. Tracks are still drawn.');
    });
    tileLayer.addTo(map);
  }

  function note(text) {
    const el = document.getElementById('map-note');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('hidden', !text);
  }

  /* Replace every track. Called whenever the flight selection changes, so it
     rebuilds rather than diffing - a few hundred polylines is nothing.
   *
   * `fit` only when tracks arrive that were not on the map before. Refitting
   * on every checkbox would throw away a view the user had just panned and
   * zoomed to, which is exactly when they are comparing two flights.
   */
  function setTracks(tracks, fit) {
    if (!map) return;
    trackLayer.clearLayers();

    const bounds = [];
    tracks.forEach((track) => {
      if (!track.points || track.points.length === 0) return;
      const style = track.fixed === track.exposures ? TRACK_OK : TRACK_FLOAT;
      const line = L.polyline(track.points, style);

      const floats = track.exposures - track.fixed;
      line.bindTooltip(
        track.name + '<br>' + track.exposures + ' exposures' +
        (floats ? '<br>' + floats + ' not fixed' : ''),
        { sticky: true });

      line.addTo(trackLayer);
      track.points.forEach((p) => bounds.push(p));
    });

    if (fit && bounds.length) {
      map.fitBounds(L.latLngBounds(bounds), FIT);
    }
  }

  /* The base station. Zooms to it, because whether it came from a RINEX
     header on selection or from a finished solve, it is the thing the user is
     looking at when it appears.
   *
   * A provisional position - the receiver's own single-point fix, metres out -
   * is drawn hollow and grey. It must not be mistaken for the solution, so it
   * does not get to look like one. */
  function setBase(lat, lon, label, opts) {
    if (!map) return;
    const options = opts || {};

    /* Null clears it. A stale marker from the previous base station is worse
       than none: it is a plausible-looking position for the wrong site. */
    if (baseMarker) map.removeLayer(baseMarker);
    if (lat == null || lon == null) { baseMarker = null; return; }

    baseMarker = L.circleMarker([lat, lon], options.provisional
      ? { radius: 6, color: '#8a93a0', weight: 2, fillColor: '#ffffff',
          fillOpacity: 0.85, dashArray: '3 3' }
      : { radius: 7, color: '#ffffff', weight: 2,
          fillColor: '#b3261e', fillOpacity: 1 });

    baseMarker.bindTooltip(label || 'Base station', { direction: 'top' });
    if (options.onClick) baseMarker.on('click', options.onClick);

    baseMarker.addTo(map);
    map.setView([lat, lon], options.provisional ? 13 : 15);
  }

  /* Corrected camera centres, coloured by their own horizontal uncertainty.
     Drawn on a canvas: a survey is thousands of points, and that many SVG
     nodes make panning unusable. */
  function setCameras(points) {
    if (!map) return;
    if (cameraLayer) map.removeLayer(cameraLayer);
    if (!points || !points.length) { cameraLayer = null; legend(false); return; }

    const canvas = L.canvas({ padding: 0.3 });
    cameraLayer = L.layerGroup().addTo(map);

    /* The path first, so the points sit on top of it. Thin and pale: it is
       there to show the shape of the flight, not to compete with the colour
       that carries the actual information. */
    const byFlight = {};
    points.forEach((p) => {
      (byFlight[p.flight || ''] = byFlight[p.flight || ''] || []).push([p.lat, p.lon]);
    });
    Object.values(byFlight).forEach((line) => {
      L.polyline(line, {
        renderer: canvas, color: '#ffffff', weight: 1, opacity: 0.55,
      }).addTo(cameraLayer);
    });

    const bounds = [];
    points.forEach((p) => {
      const band = QUALITY.find((q) => (p.h == null ? Infinity : p.h) <= q.limit)
                   || QUALITY[QUALITY.length - 1];

      /* A dark ring around every point. Over satellite imagery the green band
         is very close to vegetation, and an unoutlined dot disappears into
         exactly the terrain these surveys are flown over. */
      const dot = L.circleMarker([p.lat, p.lon], {
        renderer: canvas,
        radius: 4,
        weight: 1,
        color: 'rgba(20, 24, 30, 0.75)',
        fillColor: p.h == null ? '#8a93a0' : band.colour,
        fillOpacity: 1,
      });

      dot.bindPopup(popupHtml(p), { minWidth: 250, maxWidth: 300 });
      /* Carried on the popup itself so the handler can rebuild its content
         rather than reach into Leaflet's own DOM. */
      dot.getPopup()._point = p;

      dot.addTo(cameraLayer);
      bounds.push([p.lat, p.lon]);
    });

    legend(true);
    if (bounds.length) {
      map.fitBounds(L.latLngBounds(bounds), FIT);
    }
  }

  /* Labelled rows rather than a run of numbers: "E 0.7 cm" alone does not say
     whether that is a coordinate, an offset or an uncertainty. */
  /* Three states, all of them said out loud: not yet fetched, fetched, and
     fetched but unavailable. Silence was what made this hard to diagnose. */
  function thumbHtml(p) {
    if (!p.path) return 'No image on file for this row.';
    if (p._thumb === undefined) return 'loading preview&hellip;';
    if (p._thumb === null) return 'Preview unavailable.';
    return '<img src="' + p._thumb + '" alt="">';
  }

  function popupHtml(p) {
    const cm = (v) => (v == null ? '&mdash;' : (v * 100).toFixed(1) + ' cm');
    const row = (k, v) => '<tr><th>' + k + '</th><td>' + v + '</td></tr>';

    return '<div class="pop">'
      + '<div class="pop-name">' + (p.name || '(unnamed)') + '</div>'
      + (p.flight ? '<div class="pop-sub">' + p.flight + '</div>' : '')
      + '<div class="pop-thumb">' + thumbHtml(p) + '</div>'
      + '<table class="pop-table">'
      + (p.time ? row('Exposure', p.time) : '')
      + row('Latitude', p.lat.toFixed(7) + '&deg;')
      + row('Longitude', p.lon.toFixed(7) + '&deg;')
      + (p.hgt != null ? row('Ellipsoidal height', p.hgt.toFixed(3) + ' m') : '')
      + row('Uncertainty E', cm(p.sE))
      + row('Uncertainty N', cm(p.sN))
      + row('Uncertainty U', cm(p.sU))
      + row('Horizontal', cm(p.h))
      + (p.status ? row('Onboard RTK', p.status) : '')
      + '</table></div>';
  }

  function legend(show) {
    const el = document.getElementById('map-legend');
    if (!el) return;
    el.classList.toggle('hidden', !show);
    if (!show) return;
    el.textContent = '';
    QUALITY.forEach((q) => {
      const row = document.createElement('div');
      row.className = 'legend-row';
      const swatch = document.createElement('span');
      swatch.className = 'swatch';
      swatch.style.background = q.colour;
      const text = document.createElement('span');
      text.textContent = q.label;
      row.append(swatch, text);
      el.appendChild(row);
    });
  }

  /* Leaflet caches the container size, so it has to be told after the splitter
     moves or the pane is revealed - otherwise half the map stays grey. */
  function refresh() {
    if (map) map.invalidateSize();
  }

  return { init, setBasemap, setTracks, setBase, setCameras, refresh, note };
})();
