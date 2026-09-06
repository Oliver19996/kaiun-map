const JAPAN = [36.2, 138.0];
const markers = new Map();
let selectedMmsi = null;
let highlightFilter = null;
let sendBboxTimer = null;
let socket;

const map = L.map("map", { worldCopyJump: true, minZoom: 3, maxZoom: 16 }).setView(JAPAN, 6);
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}", {
  attribution: "Tiles &copy; Esri | &copy; OpenStreetMap | AIS: AISStream",
  maxNativeZoom: 10,
  maxZoom: 10,
}).addTo(map);
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Reference/MapServer/tile/{z}/{y}/{x}", {
  attribution: "",
  maxNativeZoom: 10,
  maxZoom: 10,
}).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  minZoom: 11,
  maxZoom: 16,
}).addTo(map);

const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const aiNote = document.getElementById("ai-note");
const briefBox = document.getElementById("brief-box");
const selectedMeta = document.getElementById("selected-meta");
const briefOut = document.getElementById("brief-out");

function shipIcon(heading, selected) {
  const rot = Number.isFinite(heading) && heading >= 0 && heading < 360 ? heading : 0;
  return L.divIcon({
    className: "",
    html: `<div class="ship-icon${selected ? " selected" : ""}" style="transform:rotate(${rot}deg)"></div>`,
    iconSize: [12, 16],
    iconAnchor: [6, 8],
  });
}

function popupHtml(v) {
  const name = v.name || "船名未着（静的AIS待ち）";
  const updated = v.updated_at ? new Date(v.updated_at).toLocaleString("ja-JP") : "不明";
  return `
    <strong>${escapeHtml(name)}</strong><br>
    MMSI ${v.mmsi}<br>
    速力 ${fmt(v.sog)} kn / 針路 ${fmt(v.cog)}°<br>
    船種 ${escapeHtml(v.ship_type_label || "不明")}<br>
    最終更新 ${escapeHtml(updated)}
  `;
}

function fmt(n) {
  return n == null || Number.isNaN(n) ? "—" : Number(n).toFixed(1);
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function upsertVessel(v) {
  if (v.lat == null || v.lon == null) return;
  if (highlightFilter && !passesHighlight(v)) {
    const existing = markers.get(v.mmsi);
    if (existing) {
      map.removeLayer(existing);
      markers.delete(v.mmsi);
    }
    return;
  }
  const selected = v.mmsi === selectedMmsi;
  let marker = markers.get(v.mmsi);
  if (!marker) {
    marker = L.marker([v.lat, v.lon], { icon: shipIcon(v.heading, selected) }).addTo(map);
    marker.on("click", () => selectVessel(v));
    markers.set(v.mmsi, marker);
  } else {
    marker.setLatLng([v.lat, v.lon]);
    marker.setIcon(shipIcon(v.heading, selected));
  }
  marker.vessel = v;
  marker.bindPopup(popupHtml(v));
}

function passesHighlight(v) {
  if (!highlightFilter) return true;
  if (highlightFilter.query && !JSON.stringify(v).toLowerCase().includes(highlightFilter.query.toLowerCase())) {
    return false;
  }
  if (highlightFilter.codes && (v.ship_type == null || !highlightFilter.codes.includes(v.ship_type))) {
    return false;
  }
  return true;
}

function replaceSnapshot(vessels) {
  const keep = new Set();
  vessels.forEach((v) => {
    keep.add(v.mmsi);
    upsertVessel(v);
  });
  for (const [mmsi, marker] of markers) {
    if (!keep.has(mmsi)) {
      map.removeLayer(marker);
      markers.delete(mmsi);
    }
  }
}

function selectVessel(v) {
  selectedMmsi = v.mmsi;
  briefBox.hidden = false;
  selectedMeta.textContent = `${v.name || "船名未着"} / MMSI ${v.mmsi}`;
  briefOut.textContent = "";
  const marker = markers.get(v.mmsi);
  if (marker) {
    marker.setIcon(shipIcon(v.heading ?? marker.vessel?.heading, true));
    map.panTo(marker.getLatLng());
    marker.openPopup();
  }
}

function setStatus(payload) {
  const labels = {
    connected: "AIS 接続中",
    connecting: "AIS 接続試行中",
    disconnected: "AIS 切断。再接続します",
    no_key: "AISSTREAM_API_KEY 未設定。地図は動きますが船は流れません",
    idle: "起動中",
  };
  const label = labels[payload.status] || payload.status || "不明";
  statusEl.textContent = `${label} · キャッシュ ${payload.count ?? "—"} 隻 · 表示 ${markers.size} 隻`;
}

function currentBboxMessage() {
  const b = map.getBounds();
  return {
    type: "bbox",
    min_lat: b.getSouth(),
    min_lon: b.getWest(),
    max_lat: b.getNorth(),
    max_lon: b.getEast(),
  };
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws/ais`);
  socket.addEventListener("open", () => {
    socket.send(JSON.stringify(currentBboxMessage()));
  });
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "snapshot") {
      replaceSnapshot(payload.vessels || []);
    } else if (payload.type === "update") {
      (payload.vessels || []).forEach(upsertVessel);
    }
    setStatus(payload);
  });
  socket.addEventListener("close", () => {
    statusEl.textContent = "サーバとの接続が切れました。再接続します…";
    setTimeout(connect, 2000);
  });
}

map.on("moveend", () => {
  clearTimeout(sendBboxTimer);
  sendBboxTimer = setTimeout(() => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(currentBboxMessage()));
    }
  }, 400);
});

document.getElementById("search-btn").addEventListener("click", runSearch);
document.getElementById("search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch();
});
document.getElementById("ai-btn").addEventListener("click", runAiSearch);
document.getElementById("ai-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runAiSearch();
});
document.getElementById("brief-btn").addEventListener("click", runBrief);

async function runSearch() {
  highlightFilter = null;
  const q = document.getElementById("search").value.trim();
  const res = await fetch(`/api/vessels?q=${encodeURIComponent(q)}`);
  const data = await res.json();
  resultsEl.innerHTML = "";
  (data.vessels || []).forEach((v) => {
    const li = document.createElement("li");
    li.textContent = `${v.name || "船名未着"} · ${v.mmsi}`;
    li.addEventListener("click", () => {
      if (v.lat != null) map.setView([v.lat, v.lon], 10);
      upsertVessel(v);
      selectVessel(v);
    });
    resultsEl.appendChild(li);
  });
  if (!data.vessels?.length) {
    const li = document.createElement("li");
    li.textContent = "キャッシュ内に一致なし（表示中の海域の船だけが対象です）";
    resultsEl.appendChild(li);
  }
}

async function runAiSearch() {
  const q = document.getElementById("ai-q").value.trim();
  if (!q) return;
  aiNote.textContent = "解釈中…";
  const res = await fetch("/api/ai/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ q }),
  });
  const data = await res.json();
  if (!res.ok) {
    aiNote.textContent = data.detail || "AI 検索に失敗しました";
    return;
  }
  if (data.clarification && !data.bbox) {
    aiNote.textContent = data.clarification;
    return;
  }
  highlightFilter = {
    query: data.query || "",
    codes: hintCodes(data.ship_type_hint),
  };
  if (data.bbox) {
    const [minLat, minLon, maxLat, maxLon] = data.bbox;
    map.fitBounds(
      [
        [minLat, minLon],
        [maxLat, maxLon],
      ],
      { maxZoom: 10, padding: [24, 24] }
    );
  }
  replaceSnapshot(data.vessels || []);
  const n = (data.vessels || []).length;
  aiNote.textContent = `${data.place_name || "海域"} ${data.ship_type_label || ""} · ${n} 隻（${data.source}）`;
}

function hintCodes(hint) {
  const table = {
    tanker: range(80, 90),
    cargo: range(70, 80),
    container: range(70, 80),
    passenger: range(60, 70),
    fishing: [30],
    tug: [31, 32, 52],
    pleasure: [36, 37],
  };
  return hint ? table[hint] : null;
}

function range(a, b) {
  return Array.from({ length: b - a }, (_, i) => a + i);
}

async function runBrief() {
  if (!selectedMmsi) return;
  briefOut.textContent = "生成中…";
  const res = await fetch("/api/ai/brief", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mmsi: selectedMmsi }),
  });
  const data = await res.json();
  if (!res.ok) {
    briefOut.textContent = data.detail || "解説に失敗しました";
    return;
  }
  briefOut.innerHTML = `
    <div class="facts"><strong>事実</strong>\n${escapeHtml(data.facts || "")}</div>
    <div class="speculation"><strong>推測</strong>\n${escapeHtml(data.speculation || "")}</div>
  `;
}

connect();
