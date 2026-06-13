const state = {
  csrfToken: null,
  authenticated: false,
  panel: "overview",
};

const titleByPanel = {
  overview: "Overview",
  shared: "Shared Views",
  memory: "Memory",
  insights: "Insights",
  activity: "Activity",
  status: "Status",
  settings: "Settings",
};

async function fetchJson(path, options = {}) {
  const headers = {
    "content-type": "application/json",
    ...(options.headers || {}),
  };
  if (options.method && options.method !== "GET" && state.csrfToken) {
    headers["x-csrf-token"] = state.csrfToken;
  }
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json();
  if (!response.ok) {
    const detail = payload.detail || {};
    throw new Error(detail.message || payload.message || `Request failed: ${response.status}`);
  }
  return payload;
}

function setMessage(text) {
  document.querySelector("#message").textContent = text || "";
}

function setAuthenticated(authenticated) {
  state.authenticated = authenticated;
  document.querySelector("#login-panel").hidden = authenticated;
  document.querySelector("#app-panel").hidden = !authenticated;
}

function renderOverview(data) {
  const issues = data.issues || [];
  const watches = data.watches || [];
  const shared = data.shared_views || {};
  return `
    <div class="summary-grid">
      <section class="panel">
        <h2>Git</h2>
        <p>${escapeHtml(data.git?.summary || "unavailable")}</p>
      </section>
      <section class="panel">
        <h2>Watches</h2>
        <p>${watches.length} managed watch${watches.length === 1 ? "" : "es"}</p>
      </section>
      <section class="panel">
        <h2>Issues</h2>
        <p>${issues.length ? `${issues.length} recent issue${issues.length === 1 ? "" : "s"}` : "No recent issues"}</p>
      </section>
      <section class="panel">
        <h2>Shared Views</h2>
        <p>${Number(shared.provider_view_count || 0)} shared, ${Number(shared.connection_count || 0)} used</p>
      </section>
      <section class="panel">
        <h2>Activity</h2>
        <p>${Number(shared.note_count || 0)} notes, ${Number(shared.inbox_count || 0)} inbox records</p>
      </section>
    </div>
    <section class="panel wide">
      <h2>Recent Issues</h2>
      ${issues.length ? `<ul>${issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>` : "<p>No recent issues.</p>"}
    </section>
  `;
}

function renderItems(items, emptyText = "No records.") {
  if (!items.length) {
    return `<p>${escapeHtml(emptyText)}</p>`;
  }
  return `<ul class="item-list">${items.map((item) => `<li>${escapeHtml(item.label || item.id || item.view_id || item.heading_id || item.message || JSON.stringify(item))}</li>`).join("")}</ul>`;
}

function renderJsonPanel(title, value) {
  return `<section class="panel wide"><h2>${escapeHtml(title)}</h2><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></section>`;
}

async function renderSharedViews() {
  const payload = await fetchJson("/api/share/views");
  const providerViews = payload.data.provider_views || [];
  const connections = payload.data.connections || [];
  return `
    <div class="two-column">
      <section class="panel">
        <h2>Views I Share</h2>
        ${renderItems(providerViews.map((view) => ({ label: view.title || view.view_id || view.error })))}
      </section>
      <section class="panel">
        <h2>Views I Use</h2>
        ${renderItems(connections.map((connection) => ({ label: `${connection.heading_id} (${connection.relationship})` })))}
      </section>
    </div>
  `;
}

async function renderMemory() {
  const payload = await fetchJson("/api/memory/files");
  return `<section class="panel wide"><h2>Memory Files</h2>${renderItems(payload.data.files || [])}</section>`;
}

async function renderInsights() {
  const payload = await fetchJson("/api/insights");
  return `<section class="panel wide"><h2>Insight Logs</h2>${renderItems(payload.data.insights || [])}</section>`;
}

async function renderActivity() {
  const payload = await fetchJson("/api/activity");
  const notes = payload.data.notes || [];
  const inbox = payload.data.inbox || [];
  return `
    <div class="two-column">
      <section class="panel">
        <h2>Notes</h2>
        ${renderItems(notes)}
      </section>
      <section class="panel">
        <h2>Inbox</h2>
        ${renderItems(inbox)}
      </section>
    </div>
  `;
}

async function renderStatus() {
  const payload = await fetchJson("/api/status");
  return renderJsonPanel("Status", payload.data);
}

function renderSettings() {
  return `
    <section class="panel wide">
      <h2>Active Root</h2>
      <form id="active-root-form" class="settings-form">
        <input id="active-root-input" name="root" value="${escapeHtml(document.querySelector("#active-root").textContent || "")}">
        <button type="submit">Save</button>
      </form>
    </section>
  `;
}

async function loadPanel() {
  document.querySelector("#panel-title").textContent = titleByPanel[state.panel] || "Overview";
  let html = "";
  if (state.panel === "overview") {
    const payload = await fetchJson("/api/overview");
    document.querySelector("#active-root").textContent = payload.data.active_root || "";
    html = renderOverview(payload.data);
  } else if (state.panel === "shared") {
    html = await renderSharedViews();
  } else if (state.panel === "memory") {
    html = await renderMemory();
  } else if (state.panel === "insights") {
    html = await renderInsights();
  } else if (state.panel === "activity") {
    html = await renderActivity();
  } else if (state.panel === "status") {
    html = await renderStatus();
  } else if (state.panel === "settings") {
    html = renderSettings();
  }
  document.querySelector("#content").innerHTML = html;
  attachPanelHandlers();
}

async function loadSession() {
  const session = await fetchJson("/api/session");
  state.csrfToken = session.csrf_token || null;
  setAuthenticated(session.authenticated);
  document.querySelector("#active-root").textContent = session.active_root || "";
  if (session.authenticated) {
    await loadPanel();
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const token = new FormData(event.currentTarget).get("token");
    const payload = await fetchJson("/api/login", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    state.csrfToken = payload.data.csrf_token;
    setAuthenticated(true);
    setMessage(payload.message);
    await loadPanel();
  } catch (error) {
    setMessage(error.message);
  }
});

document.querySelector("#refresh-button").addEventListener("click", async () => {
  try {
    await loadPanel();
    setMessage("Refreshed.");
  } catch (error) {
    setMessage(error.message);
  }
});

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.panel = button.dataset.panel;
    try {
      await loadPanel();
    } catch (error) {
      setMessage(error.message);
    }
  });
});

function attachPanelHandlers() {
  const activeRootForm = document.querySelector("#active-root-form");
  if (activeRootForm) {
    activeRootForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const root = new FormData(event.currentTarget).get("root");
        const payload = await fetchJson("/api/active-root", {
          method: "POST",
          body: JSON.stringify({ root }),
        });
        state.csrfToken = payload.data.csrf_token || state.csrfToken;
        document.querySelector("#active-root").textContent = payload.data.active_root || "";
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }
}

loadSession().catch((error) => {
  setAuthenticated(false);
  setMessage(error.message);
});
