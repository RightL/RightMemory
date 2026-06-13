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
    </div>
    <section class="panel wide">
      <h2>Recent Issues</h2>
      ${issues.length ? `<ul>${issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>` : "<p>No recent issues.</p>"}
    </section>
  `;
}

async function loadPanel() {
  document.querySelector("#panel-title").textContent = titleByPanel[state.panel] || "Overview";
  if (state.panel !== "overview") {
    document.querySelector("#content").innerHTML = `<section class="panel wide"><h2>${titleByPanel[state.panel]}</h2><p>This workspace area is ready for the next Web Studio API slice.</p></section>`;
    return;
  }
  const payload = await fetchJson("/api/overview");
  document.querySelector("#active-root").textContent = payload.data.active_root || "";
  document.querySelector("#content").innerHTML = renderOverview(payload.data);
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

loadSession().catch((error) => {
  setAuthenticated(false);
  setMessage(error.message);
});
