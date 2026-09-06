const JAPAN = [36.2, 138.0];
const markers = new Map();
let selectedMmsi = null;
let selectedVessel = null;
let highlightFilter = null;
let sendBboxTimer = null;
let socket;
let currentProjectId = localStorage.getItem("kaiun-project") || "";
let currentProject = null;

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

function shipIcon(heading, selected, inProject) {
  const rot = Number.isFinite(heading) && heading >= 0 && heading < 360 ? heading : 0;
  const cls = `ship-icon${selected ? " selected" : ""}${inProject ? " in-project" : ""}`;
  return L.divIcon({
    className: "",
    html: `<div class="${cls}" style="transform:rotate(${rot}deg)"></div>`,
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
  const inProject = vesselInProject(v);
  if (currentProject && !inProject) {
    const existing = markers.get(v.mmsi);
    if (existing) {
      map.removeLayer(existing);
      markers.delete(v.mmsi);
    }
    return;
  }
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
    marker = L.marker([v.lat, v.lon], { icon: shipIcon(v.heading, selected, inProject) }).addTo(map);
    marker.on("click", () => selectVessel(v));
    markers.set(v.mmsi, marker);
  } else {
    marker.setLatLng([v.lat, v.lon]);
    marker.setIcon(shipIcon(v.heading, selected, inProject));
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
  selectedVessel = v;
  briefBox.hidden = false;
  selectedMeta.textContent = `${v.name || "船名未着"} / MMSI ${v.mmsi}${v.call_sign ? ` / ${v.call_sign}` : ""}`;
  briefOut.textContent = "";
  const marker = markers.get(v.mmsi);
  if (marker) {
    marker.setIcon(shipIcon(v.heading ?? marker.vessel?.heading, true, vesselInProject(v)));
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
loadProjects();

document.getElementById("project-create").addEventListener("click", createProject);
document.getElementById("project-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") createProject();
});
document.getElementById("project-select").addEventListener("change", (e) => {
  currentProjectId = e.target.value;
  localStorage.setItem("kaiun-project", currentProjectId);
  loadCurrentProject();
});
document.getElementById("project-delete").addEventListener("click", deleteCurrentProject);
document.getElementById("ship-add").addEventListener("click", addShipToProject);
document.getElementById("ship-add-selected").addEventListener("click", addSelectedToProject);

function projectNote(text) {
  document.getElementById("project-note").textContent = text || "";
}

function matchShip(saved, live) {
  if (saved.mmsi && live.mmsi && Number(saved.mmsi) === Number(live.mmsi)) return true;
  const sc = (saved.call_sign || "").trim().toUpperCase();
  const lc = (live.call_sign || "").trim().toUpperCase();
  if (sc && lc && sc === lc) return true;
  const sn = (saved.name || "").trim().toUpperCase();
  const ln = (live.name || "").trim().toUpperCase();
  return Boolean(sn && ln && sn === ln);
}

function vesselInProject(v) {
  return Boolean(currentProject?.ships?.some((ship) => matchShip(ship, v)));
}

async function loadProjects() {
  const res = await fetch("/api/projects");
  const data = await res.json();
  const select = document.getElementById("project-select");
  const previous = currentProjectId;
  select.innerHTML = '<option value="">（すべての船）</option>';
  (data.projects || []).forEach((project) => {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name}（${project.ship_count}隻）`;
    select.appendChild(option);
  });
  if (previous && [...select.options].some((opt) => opt.value === previous)) {
    select.value = previous;
    currentProjectId = previous;
  } else {
    currentProjectId = "";
    localStorage.setItem("kaiun-project", "");
  }
  await loadCurrentProject();
}

async function loadCurrentProject() {
  const fleet = document.getElementById("fleet");
  const del = document.getElementById("project-delete");
  if (!currentProjectId) {
    currentProject = null;
    fleet.hidden = true;
    del.hidden = true;
    projectNote("");
    refreshMapFilter();
    return;
  }
  const res = await fetch(`/api/projects/${currentProjectId}`);
  if (!res.ok) {
    currentProject = null;
    fleet.hidden = true;
    del.hidden = true;
    projectNote("プロジェクトを読み込めませんでした。");
    return;
  }
  currentProject = await res.json();
  fleet.hidden = false;
  del.hidden = false;
  renderFleet();
  refreshMapFilter();
  fitProjectShips();
}

function renderFleet() {
  const list = document.getElementById("fleet-list");
  list.innerHTML = "";
  const ships = currentProject?.ships || [];
  if (!ships.length) {
    const li = document.createElement("li");
    li.textContent = "まだ船がありません。下の欄から追加してください。";
    list.appendChild(li);
    return;
  }
  ships.forEach((ship) => {
    const li = document.createElement("li");
    const meta = document.createElement("div");
    meta.className = "meta";
    const live = ship.live;
    const title = ship.name || live?.name || "名称未設定";
    const parts = [
      title,
      ship.call_sign ? `呼出 ${ship.call_sign}` : "",
      ship.mmsi ? `MMSI ${ship.mmsi}` : "",
      live ? "地図上" : "未検出",
    ].filter(Boolean);
    meta.textContent = parts.join(" · ");
    if (ship.notes) {
      meta.textContent += ` / ${ship.notes}`;
    }
    meta.addEventListener("click", () => {
      if (live) {
        upsertVessel(live);
        selectVessel(live);
      }
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "外す";
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      removeShip(ship.id);
    });
    li.append(meta, remove);
    list.appendChild(li);
  });
}

function refreshMapFilter() {
  const remembered = [...markers.values()].map((marker) => marker.vessel).filter(Boolean);
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(currentBboxMessage()));
  }
  remembered.forEach(upsertVessel);
}

function fitProjectShips() {
  const points = (currentProject?.ships || [])
    .map((ship) => ship.live)
    .filter((live) => live && live.lat != null && live.lon != null)
    .map((live) => [live.lat, live.lon]);
  if (points.length === 1) {
    map.setView(points[0], 9);
  } else if (points.length > 1) {
    map.fitBounds(points, { maxZoom: 10, padding: [32, 32] });
  }
}

async function createProject() {
  const input = document.getElementById("project-name");
  const name = input.value.trim();
  if (!name) {
    projectNote("プロジェクト名を入力してください。");
    return;
  }
  const res = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const data = await res.json();
  if (!res.ok) {
    projectNote(data.detail || "作成に失敗しました。");
    return;
  }
  input.value = "";
  currentProjectId = data.id;
  localStorage.setItem("kaiun-project", currentProjectId);
  projectNote(`「${data.name}」を作成しました。`);
  await loadProjects();
}

async function deleteCurrentProject() {
  if (!currentProjectId) return;
  if (!window.confirm("このプロジェクトを削除しますか？登録船も消えます。")) return;
  const res = await fetch(`/api/projects/${currentProjectId}`, { method: "DELETE" });
  if (!res.ok) {
    projectNote("削除できませんでした。");
    return;
  }
  currentProjectId = "";
  localStorage.setItem("kaiun-project", "");
  projectNote("プロジェクトを削除しました。");
  await loadProjects();
}

async function addShipToProject() {
  if (!currentProjectId) return;
  const body = {
    name: document.getElementById("ship-name").value.trim(),
    call_sign: document.getElementById("ship-call").value.trim(),
    notes: document.getElementById("ship-notes").value.trim(),
  };
  const mmsi = document.getElementById("ship-mmsi").value.trim();
  if (mmsi) body.mmsi = Number(mmsi);
  const res = await fetch(`/api/projects/${currentProjectId}/ships`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    projectNote(data.detail || "追加できませんでした。");
    return;
  }
  document.getElementById("ship-name").value = "";
  document.getElementById("ship-call").value = "";
  document.getElementById("ship-mmsi").value = "";
  document.getElementById("ship-notes").value = "";
  currentProject = data.project;
  projectNote("船を登録しました。");
  await loadProjects();
}

async function addSelectedToProject() {
  if (!currentProjectId) {
    projectNote("先にプロジェクトを選ぶか作成してください。");
    return;
  }
  if (!selectedVessel) {
    projectNote("地図上の船を選んでから追加してください。");
    return;
  }
  const res = await fetch(`/api/projects/${currentProjectId}/ships`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: selectedVessel.name || "",
      call_sign: selectedVessel.call_sign || "",
      mmsi: selectedVessel.mmsi,
    }),
  });
  const data = await res.json();
  if (!res.ok) {
    projectNote(data.detail || "追加できませんでした。");
    return;
  }
  currentProject = data.project;
  projectNote("選択中の船をプロジェクトに入れました。");
  await loadProjects();
}

async function removeShip(shipId) {
  const res = await fetch(`/api/projects/${currentProjectId}/ships/${shipId}`, { method: "DELETE" });
  if (!res.ok) {
    projectNote("外せませんでした。");
    return;
  }
  await loadCurrentProject();
  await loadProjects();
}
