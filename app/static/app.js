const JAPAN = [36.2, 138.0];
const markers = new Map();
let selectedMmsi = null;
let selectedVessel = null;
let highlightFilter = null;
let sendBboxTimer = null;
let socket;
const storedProject = localStorage.getItem("kaiun-project");
let currentProjectId = storedProject === null ? "sample" : storedProject;
let currentProject = null;
let selectedShipId = null;
let sampleRetry = 0;
let isGuest = true;

const ROUTE_COLORS = {
  here: "#2ee6ff",
  load: "#3de0c5",
  trans: "#c084fc",
  call: "#7ab8ff",
  disc: "#ff5a1f",
  bl: "#b388ff",
  live: "#2ee6ff",
};
const seaRouteCache = new Map();
let routeDrawSeq = 0;
const map = L.map("map", { worldCopyJump: true, minZoom: 3, maxZoom: 16 }).setView(JAPAN, 6);
const routeLayer = L.layerGroup().addTo(map);
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
const briefBox = document.getElementById("brief-box");
const selectedMeta = document.getElementById("selected-meta");
const briefOut = document.getElementById("brief-out");

function shipIcon(heading, selected, inProject) {
  const rot = Number.isFinite(heading) && heading >= 0 && heading < 360 ? heading : 0;
  const cls = `ship-icon${selected ? " selected" : ""}${inProject ? " in-project" : ""}`;
  return L.divIcon({
    className: "",
    html: `<div class="${cls}" style="transform:rotate(${rot}deg)"></div>`,
    iconSize: selected ? [16, 22] : [12, 16],
    iconAnchor: selected ? [8, 11] : [6, 8],
  });
}

function popupHtml(v) {
  const name = v.name || "船名未着（静的AIS待ち）";
    const updated = v.updated_at ? new Date(v.updated_at).toLocaleString("ja-JP") : "不明";
  let port = "";
  if (v.in_port) {
    const since = v.in_port_since ? new Date(v.in_port_since).toLocaleString("ja-JP") : "不明";
    port = `<br>港滞在: ${escapeHtml(v.port_name || "港内")} / ${since} から ${v.stay_hours ?? "—"} 時間`;
    if (v.delay_hours != null) {
      port += `<br>遅延: ${v.delay_hours} 時間`;
    }
    if (v.delay_reason) {
      port += `<br>${escapeHtml(v.delay_reason)}`;
    }
  }
  return `
    <strong>${escapeHtml(name)}</strong><br>
    MMSI ${v.mmsi}<br>
    速力 ${fmt(v.sog)} kn / 針路 ${fmt(v.cog)}°<br>
    船種 ${escapeHtml(v.ship_type_label || "不明")}<br>
    最終更新 ${escapeHtml(updated)}${port}
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

function projectLiveMmsis() {
  return new Set(
    (currentProject?.ships || [])
      .map((ship) => Number(ship.live?.mmsi || ship.mmsi))
      .filter((mmsi) => Number.isFinite(mmsi) && mmsi > 0)
  );
}

function pinProjectShips() {
  (currentProject?.ships || []).forEach((ship) => {
    if (ship.live?.lat != null) upsertVessel(ship.live);
  });
}

function matchingProjectShip(v) {
  return currentProject?.ships?.find((ship) => matchShip(ship, v)) || null;
}

function replaceSnapshot(vessels) {
  const keep = projectLiveMmsis();
  vessels.forEach((v) => {
    keep.add(v.mmsi);
    upsertVessel(v);
  });
  pinProjectShips();
  for (const [mmsi, marker] of markers) {
    if (!keep.has(mmsi)) {
      map.removeLayer(marker);
      markers.delete(mmsi);
    }
  }
}

function selectVessel(v, shipId) {
  selectedMmsi = v.mmsi;
  selectedVessel = v;
  const matched = shipId ? currentProject?.ships?.find((s) => s.id === shipId) : matchingProjectShip(v);
  selectedShipId = matched?.id || shipId || null;
  briefBox.hidden = false;
  selectedMeta.textContent = `${v.name || matched?.name || "船名未着"} / MMSI ${v.mmsi}${v.call_sign ? ` / ${v.call_sign}` : ""}`;
  briefOut.textContent = "";
  const marker = markers.get(v.mmsi);
  if (marker) {
    marker.setIcon(shipIcon(v.heading ?? marker.vessel?.heading, true, vesselInProject(v)));
    map.panTo(marker.getLatLng());
    marker.openPopup();
  } else if (v.lat != null) {
    map.panTo([v.lat, v.lon]);
  }
  drawRoutes();
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
      pinProjectShips();
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
document.getElementById("brief-btn").addEventListener("click", runBrief);
document.getElementById("chat-send").addEventListener("click", sendChat);
document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});
document.getElementById("chat-role").addEventListener("change", saveProfile);
document.getElementById("chat-company").addEventListener("change", saveProfile);
document.getElementById("chat-doc").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (file) uploadDoc(file);
});
document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});
document.querySelectorAll(".chat-ex").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.getElementById("chat-input").value = btn.dataset.q || "";
    sendChat();
  });
});

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

boot();

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
document.getElementById("logout-btn").addEventListener("click", logout);

async function boot() {
  const res = await fetch("/api/auth/me");
  const user = res.ok ? await res.json() : { guest: true };
  isGuest = Boolean(user.guest);
  const who = document.getElementById("whoami");
  const logoutBtn = document.getElementById("logout-btn");
  const loginLink = document.getElementById("login-link");
  if (isGuest) {
    who.textContent = `ゲスト（${user.role_label || "調査・アナリスト"}）`;
    logoutBtn.hidden = true;
    loginLink.hidden = false;
  } else {
    who.textContent = `${user.user_id}（${user.role_label || ""}）`;
    logoutBtn.hidden = false;
    loginLink.hidden = true;
  }
  await fillRoleSelect(user);
  await loadDocs();
  await loadPlaces();
  connect();
  loadProjects();
  setTimeout(() => map.invalidateSize(), 50);
}

async function logout() {
  await fetch("/api/auth/logout", { method: "POST" });
  location.href = "/";
}

async function loadPlaces() {
  const res = await fetch("/api/places");
  if (!res.ok) return;
  const data = await res.json();
  ["ship-origin", "ship-dest", "ship-transship", "ship-call-port"].forEach((id) => {
    const select = document.getElementById(id);
    if (!select) return;
    (data.places || []).forEach((place) => {
      const option = document.createElement("option");
      option.value = place.id;
      option.textContent = place.name;
      select.appendChild(option);
    });
  });
}

function blPoints(ship) {
  const points = [];
  if (ship.origin_lat != null) {
    points.push({ lat: ship.origin_lat, lon: ship.origin_lon, name: ship.origin_name, kind: "load" });
  }
  (ship.transship_places || []).forEach((place) => {
    if (place.lat != null) points.push({ lat: place.lat, lon: place.lon, name: place.name, kind: "trans" });
  });
  if (ship.dest_lat != null) {
    points.push({ lat: ship.dest_lat, lon: ship.dest_lon, name: ship.dest_name, kind: "disc" });
  }
  return points;
}

async function fetchSeaPath(points) {
  const usable = (points || []).filter((p) => p && p.lat != null && p.lon != null);
  if (usable.length < 2) return usable.map((p) => [p.lat, p.lon]);
  const key = usable.map((p) => `${p.lat.toFixed(3)},${p.lon.toFixed(3)}`).join(">");
  if (seaRouteCache.has(key)) return seaRouteCache.get(key);
  try {
    const res = await fetch("/api/searoute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ waypoints: usable.map((p) => [p.lat, p.lon]) }),
    });
    const data = await res.json();
    const path = data.path && data.path.length >= 2 ? data.path : usable.map((p) => [p.lat, p.lon]);
    seaRouteCache.set(key, path);
    return path;
  } catch {
    const fallback = usable.map((p) => [p.lat, p.lon]);
    seaRouteCache.set(key, fallback);
    return fallback;
  }
}

async function drawRoutes() {
  const seq = ++routeDrawSeq;
  if (!currentProject) {
    routeLayer.clearLayers();
    return;
  }
  const hasSelection = Boolean(selectedShipId || selectedMmsi);
  const planned = await Promise.all(
    (currentProject.ships || []).map(async (ship) => {
      const selected = ship.id === selectedShipId || (selectedMmsi && Number(ship.mmsi) === Number(selectedMmsi));
      const bl = blPoints(ship);
      const live = ship.live;
      const next =
        (ship.call_places && ship.call_places[0]) ||
        (ship.dest_lat != null ? { lat: ship.dest_lat, lon: ship.dest_lon, name: ship.dest_name } : null);
      const [blPath, livePath] = await Promise.all([
        fetchSeaPath(bl),
        live?.lat != null && next?.lat != null ? fetchSeaPath([live, next]) : Promise.resolve(null),
      ]);
      return { ship, selected, bl, blPath, live, next, livePath };
    })
  );
  if (seq !== routeDrawSeq) return;
  routeLayer.clearLayers();
  planned.forEach(({ ship, selected, bl, blPath, live, next, livePath }) => {
    const dim = hasSelection && !selected;
    const opacity = dim ? 0.22 : 1;
    if (blPath && blPath.length >= 2) {
      L.polyline(blPath, {
        color: selected ? ROUTE_COLORS.bl : "#8661c7",
        weight: selected ? 5 : 2.5,
        opacity,
      })
        .bindPopup(
          `${escapeHtml(ship.name || "海上航路")}<br>B/L: ${escapeHtml(ship.origin_name || "船積未設定")} → ${escapeHtml(
            (ship.transship_places || []).map((p) => p.name).join(" → ") || "積み替えなし"
          )} → ${escapeHtml(ship.dest_name || "船卸未設定")}`
        )
        .addTo(routeLayer);
    }
    bl.forEach((point) => {
      const isDisc = point.kind === "disc";
      L.circleMarker([point.lat, point.lon], {
        radius: selected ? (isDisc ? 9 : 7) : isDisc ? 7 : 6,
        color: ROUTE_COLORS[point.kind],
        fillColor: ROUTE_COLORS[point.kind],
        fillOpacity: 0.95 * opacity,
        weight: isDisc ? 3 : selected ? 3 : 1,
        opacity,
      })
        .bindTooltip(`${point.kind === "load" ? "船積" : point.kind === "trans" ? "積み替え" : "船卸"} ${point.name || ""}`)
        .addTo(routeLayer);
    });
    (ship.call_places || []).forEach((place) => {
      if (place.lat == null) return;
      L.circleMarker([place.lat, place.lon], {
        radius: selected ? 7 : 5,
        color: ROUTE_COLORS.call,
        fillColor: "#071018",
        fillOpacity: 0.9,
        weight: 2,
        opacity,
      })
        .bindTooltip(`寄港（B/L外） ${place.name || ""}`)
        .addTo(routeLayer);
    });
    if (livePath && livePath.length >= 2) {
      L.polyline(livePath, {
        color: ROUTE_COLORS.live,
        weight: selected ? 4 : 2,
        dashArray: "1 8",
        opacity,
      })
        .bindPopup(`${escapeHtml(ship.name || "現在地")} → ${escapeHtml(next?.name || "次地点")}`)
        .addTo(routeLayer);
    }
    if (live?.lat != null) {
      L.circleMarker([live.lat, live.lon], {
        radius: selected ? 9 : 6,
        color: ROUTE_COLORS.here,
        fillColor: ROUTE_COLORS.here,
        fillOpacity: 0.35,
        weight: 3,
        opacity,
      })
        .bindTooltip(`現在位置 ${ship.name || live.name || ""}`)
        .addTo(routeLayer);
      if (selected) {
        L.marker([live.lat, live.lon], {
          icon: L.divIcon({ className: "", html: '<div class="here-ring"></div>', iconSize: [28, 28], iconAnchor: [14, 14] }),
          interactive: false,
        }).addTo(routeLayer);
      }
    }
  });
}

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
  } else if ([...select.options].some((opt) => opt.value === "sample")) {
    select.value = "sample";
    currentProjectId = "sample";
    localStorage.setItem("kaiun-project", "sample");
  } else {
    currentProjectId = "";
    localStorage.setItem("kaiun-project", "");
  }
  await loadCurrentProject();
}

async function loadCurrentProject() {
  const fleet = document.getElementById("fleet");
  const del = document.getElementById("project-delete");
  const form = document.getElementById("fleet-form");
  if (!currentProjectId) {
    currentProject = null;
    fleet.hidden = true;
    del.hidden = true;
    form.hidden = true;
    projectNote(isGuest ? "未ログインの作成分は、ブラウザを閉じると消えます。" : "");
    selectedShipId = null;
    drawRoutes();
    refreshMapFilter();
    return;
  }
  const res = await fetch(`/api/projects/${currentProjectId}`);
  if (!res.ok) {
    currentProject = null;
    fleet.hidden = true;
    del.hidden = true;
    form.hidden = true;
    projectNote("プロジェクトを読み込めませんでした。");
    return;
  }
  currentProject = await res.json();
  const readonly = Boolean(currentProject.readonly);
  fleet.hidden = false;
  del.hidden = readonly;
  form.hidden = readonly;
  renderFleet();
  pinProjectShips();
  drawRoutes();
  refreshMapFilter();
  pinProjectShips();
  fitProjectShips();
  const liveCount = (currentProject.ships || []).filter((s) => s.live?.lat != null).length;
  if (readonly) {
    projectNote(currentProject.notes || "サンプルです。変更・削除はできません。");
    if (liveCount < 2 && sampleRetry < 8) {
      sampleRetry += 1;
      setTimeout(loadCurrentProject, 2500);
    } else {
      sampleRetry = 0;
    }
  } else {
    sampleRetry = 0;
    if (isGuest) projectNote("未ログインの作成分は、ブラウザを閉じると消えます。");
  }
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
      live?.lat != null ? "現在位置あり" : "未検出",
      ship.origin_name ? `船積 ${ship.origin_name}` : "",
      (ship.transship_places || []).length ? `積み替え ${(ship.transship_places || []).map((p) => p.name).join("・")}` : "",
      (ship.call_places || []).length ? `寄港 ${(ship.call_places || []).map((p) => p.name).join("・")}` : "",
      ship.dest_name ? `船卸 ${ship.dest_name}` : "",
    ].filter(Boolean);
    if (live?.in_port) {
      parts.push(`港 ${live.port_name || ""} ${live.stay_hours ?? "—"}h`);
      if (live.delay_hours != null) parts.push(`遅延 ${live.delay_hours}h`);
    }
    meta.textContent = parts.join(" · ");
    if (ship.notes) {
      meta.textContent += ` / ${ship.notes}`;
    }
    meta.addEventListener("click", () => {
      if (live?.lat != null) {
        upsertVessel(live);
        selectVessel(live, ship.id);
      } else {
        selectedShipId = ship.id;
        selectedMmsi = ship.mmsi || null;
        selectedVessel = live || { mmsi: ship.mmsi, name: ship.name };
        drawRoutes();
        fitProjectShips();
      }
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.textContent = "外す";
    if (currentProject?.readonly) {
      remove.hidden = true;
    } else {
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        removeShip(ship.id);
      });
    }
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
  pinProjectShips();
}

function fitProjectShips() {
  const points = [];
  (currentProject?.ships || []).forEach((ship) => {
    if (ship.live && ship.live.lat != null) points.push([ship.live.lat, ship.live.lon]);
    if (ship.origin_lat != null) points.push([ship.origin_lat, ship.origin_lon]);
    if (ship.dest_lat != null) points.push([ship.dest_lat, ship.dest_lon]);
    (ship.transship_places || []).forEach((place) => {
      if (place.lat != null) points.push([place.lat, place.lon]);
    });
    (ship.call_places || []).forEach((place) => {
      if (place.lat != null) points.push([place.lat, place.lon]);
    });
  });
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

function selectedPlaceIds(id) {
  return [...document.getElementById(id).selectedOptions].map((opt) => opt.value).filter(Boolean);
}

async function addShipToProject() {
  if (!currentProjectId) return;
  const body = {
    name: document.getElementById("ship-name").value.trim(),
    call_sign: document.getElementById("ship-call").value.trim(),
    notes: document.getElementById("ship-notes").value.trim(),
    origin_place_id: document.getElementById("ship-origin").value || null,
    dest_place_id: document.getElementById("ship-dest").value || null,
    transship_place_ids: selectedPlaceIds("ship-transship"),
    call_place_ids: selectedPlaceIds("ship-call-port"),
    planned_arrival_at: document.getElementById("ship-eta").value || null,
    planned_departure_at: document.getElementById("ship-etd").value || null,
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
  document.getElementById("ship-origin").value = "";
  document.getElementById("ship-dest").value = "";
  document.getElementById("ship-eta").value = "";
  document.getElementById("ship-etd").value = "";
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
      origin_place_id: document.getElementById("ship-origin").value || null,
      dest_place_id: document.getElementById("ship-dest").value || null,
      transship_place_ids: selectedPlaceIds("ship-transship"),
      call_place_ids: selectedPlaceIds("ship-call-port"),
      planned_arrival_at: document.getElementById("ship-eta").value || null,
      planned_departure_at: document.getElementById("ship-etd").value || null,
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

async function fillRoleSelect(user) {
  const sel = document.getElementById("chat-role");
  const company = document.getElementById("chat-company");
  if (!sel.options.length) {
    const res = await fetch("/api/auth/roles");
    const data = res.ok ? await res.json() : { roles: [] };
    for (const role of data.roles || []) {
      const opt = document.createElement("option");
      opt.value = role.id;
      opt.textContent = role.label;
      sel.appendChild(opt);
    }
  }
  sel.value = user.role || "analyst";
  company.value = user.company || "";
}

async function saveProfile() {
  const role = document.getElementById("chat-role").value;
  const company = document.getElementById("chat-company").value;
  const res = await fetch("/api/auth/profile", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, company }),
  });
  const user = await res.json();
  if (!res.ok) return;
  const who = document.getElementById("whoami");
  if (user.guest) {
    who.textContent = `ゲスト（${user.role_label || ""}）`;
  } else {
    who.textContent = `${user.user_id}（${user.role_label || ""}）`;
  }
}

async function loadDocs() {
  const res = await fetch("/api/docs");
  const data = res.ok ? await res.json() : { docs: [] };
  const list = document.getElementById("chat-docs");
  list.innerHTML = "";
  for (const doc of data.docs || []) {
    const li = document.createElement("li");
    li.append(document.createTextNode(doc.filename || "資料"));
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "×";
    btn.addEventListener("click", () => deleteDoc(doc.id));
    li.appendChild(btn);
    list.appendChild(li);
  }
}

async function uploadDoc(file) {
  const body = new FormData();
  body.append("file", file);
  appendChat("bot", `${file.name} を読み込んでいます…`);
  const pending = document.getElementById("chat-log").lastElementChild;
  const res = await fetch("/api/docs", { method: "POST", body });
  const data = await res.json();
  pending.textContent = res.ok
    ? `${data.doc.filename} をベクトル化しました（${data.doc.chunk_count} 断片）。以降の質問で参照します。`
    : data.detail || "アップロードできませんでした。";
  if (res.ok) await loadDocs();
}

async function deleteDoc(id) {
  await fetch(`/api/docs/${id}`, { method: "DELETE" });
  await loadDocs();
}

function appendChat(role, text, cites) {
  const log = document.getElementById("chat-log");
  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}`;
  bubble.textContent = text;
  if (cites && cites.length) {
    const cite = document.createElement("span");
    cite.className = "chat-cite";
    cite.textContent = `根拠資料: ${cites.filter(Boolean).join(" / ")}`;
    bubble.appendChild(cite);
  }
  log.appendChild(bubble);
  log.scrollTop = log.scrollHeight;
}

async function sendChat() {
  const input = document.getElementById("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  appendChat("user", message);
  appendChat("bot", "考えています…");
  const pending = document.getElementById("chat-log").lastElementChild;
  const res = await fetch("/api/ai/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      mmsi: selectedMmsi,
      project_id: currentProjectId || null,
      ship_id: selectedShipId,
    }),
  });
  const data = await res.json();
  pending.textContent = res.ok ? data.reply || data.facts || "回答を生成できませんでした。" : data.detail || "送信に失敗しました。";
  if (res.ok && data.used_docs && data.used_docs.length) {
    const cite = document.createElement("span");
    cite.className = "chat-cite";
    cite.textContent = `根拠資料: ${data.used_docs.filter(Boolean).join(" / ")}`;
    pending.appendChild(cite);
  }
}
