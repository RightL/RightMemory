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
  const runningWatches = watches.filter((watch) => String(watch.state || "").startsWith("running")).length;
  return `
    <div class="summary-grid">
      ${metricPanel("Git", data.git?.summary || "unavailable")}
      ${metricPanel("Watches", `${runningWatches}/${watches.length} running`)}
      ${metricPanel("Issues", issues.length ? `${issues.length} recent` : "Clear")}
      ${metricPanel("Shared Views", `${Number(shared.provider_view_count || 0)} shared, ${Number(shared.connection_count || 0)} used`)}
      ${metricPanel("Activity", `${Number(shared.note_count || 0)} notes, ${Number(shared.inbox_count || 0)} inbox`)}
    </div>
    <section class="panel wide">
      <div class="section-heading">
        <h2>Recent Issues</h2>
      </div>
      ${issues.length ? `<ul class="item-list">${issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>` : "<p>No recent issues.</p>"}
    </section>
  `;
}

function metricPanel(title, value) {
  return `
    <section class="panel metric">
      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(value)}</p>
    </section>
  `;
}

function renderItems(items, emptyText = "No records.") {
  if (!items.length) {
    return `<p>${escapeHtml(emptyText)}</p>`;
  }
  return `<ul class="item-list">${items.map((item) => `<li>${escapeHtml(item.label || item.id || item.view_id || item.heading_id || item.message || JSON.stringify(item))}</li>`).join("")}</ul>`;
}

function renderOptions(items, selectedValue = "") {
  if (!items.length) {
    return `<option value="">No items yet</option>`;
  }
  return items
    .map((item) => {
      const value = item.value || item.id || item.view_id || item.heading_id || "";
      const selected = selectedValue && value === selectedValue ? " selected" : "";
      return `<option value="${escapeHtml(value)}"${selected}>${escapeHtml(item.label || value)}</option>`;
    })
    .join("");
}

function renderJsonPanel(title, value) {
  return `<section class="panel wide"><h2>${escapeHtml(title)}</h2><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></section>`;
}

async function renderSharedViews() {
  const payload = await fetchJson("/api/share/views");
  const providerViews = payload.data.provider_views || [];
  const connections = payload.data.connections || [];
  const providerOptions = renderOptions(
    providerViews.map((view) => ({
      value: view.view_id,
      label: view.title ? `${view.title} (${view.view_id})` : view.view_id || view.error,
    })),
  );
  const connectionOptions = renderOptions(
    connections.map((connection) => ({
      value: connection.heading_id,
      label: `${connection.heading_id} (${connection.relationship || "shared view"})`,
    })),
  );

  return `
    <div class="flow-layout">
      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">1</span>
          <div>
            <h2>Create a View</h2>
            <p>Name the collaboration surface and describe what memory should be included.</p>
          </div>
        </div>
        <form id="define-view-form" class="guided-form">
          <label>
            View name
            <input name="title" placeholder="Auth API with Alice" required>
          </label>
          <label>
            Memory focus
            <textarea name="filter_terms" placeholder="auth, tokens, oauth"></textarea>
          </label>
          <label class="inline-choice">
            <input name="include_all" type="checkbox">
            Include broad memory when the focus is intentionally open
          </label>
          <details class="advanced">
            <summary>Naming and audience</summary>
            <label>
              View id
              <input name="view_id" placeholder="auto from name">
            </label>
            <label>
              Audience
              <input name="audience" placeholder="Alice, team, external collaborator">
            </label>
            <label>
              Description
              <textarea name="description" placeholder="What this view is for"></textarea>
            </label>
            <label>
              Source globs
              <textarea name="source_globs" placeholder="shared_views/auth/*.md"></textarea>
            </label>
          </details>
          <div class="button-row">
            <button class="primary" type="submit">Create View</button>
          </div>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">2</span>
          <div>
            <h2>Publish or Refresh</h2>
            <p>Pick an existing view, then build or publish it without retyping the view id.</p>
          </div>
        </div>
        <form id="provider-view-form" class="guided-form">
          <label>
            View
            <select name="view_id">${providerOptions}</select>
          </label>
          <label>
            Focus query
            <input name="query" placeholder="optional query for this build">
          </label>
          <div class="button-row">
            <button class="primary" name="action" value="build" type="submit"${providerViews.length ? "" : " disabled"}>Build</button>
          </div>
          <details class="advanced">
            <summary>Publish targets</summary>
            <label>
              Local hub folder
              <input name="target" placeholder="/path/to/shared hub or package folder">
            </label>
            <div class="button-row">
              <button name="action" value="export" type="submit"${providerViews.length ? "" : " disabled"}>Export Package</button>
              <button name="action" value="publish-mounted" type="submit"${providerViews.length ? "" : " disabled"}>Publish Local</button>
            </div>
            <label>
              HTTP hub URL
              <input name="hub_url" placeholder="https://hub.example.test">
            </label>
            <label>
              Credential id
              <input name="credential_id" placeholder="auto from view id">
            </label>
            <div class="button-row">
              <button name="action" value="publish-http" type="submit"${providerViews.length ? "" : " disabled"}>Publish HTTP</button>
            </div>
          </details>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">3</span>
          <div>
            <h2>Accept a View</h2>
            <p>Paste an invitation URL or package folder. RightMemory can infer the rest in ordinary cases.</p>
          </div>
        </div>
        <form id="accept-invite-form" class="guided-form">
          <label>
            Invitation
            <textarea name="invitation" placeholder="https://.../i/token or /path/to/package" required></textarea>
          </label>
          <details class="advanced">
            <summary>Connection naming</summary>
            <label>
              Heading id
              <input name="heading_id" placeholder="auto from invitation">
            </label>
            <label>
              Title
              <input name="title" placeholder="shown in memory">
            </label>
            <label>
              Relationship
              <input name="relationship" placeholder="uses, depends on, collaborates with">
            </label>
          </details>
          <div class="button-row">
            <button class="primary" type="submit">Accept View</button>
          </div>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">4</span>
          <div>
            <h2>Use a Connected View</h2>
            <p>Retrieve context or send feedback from a selected connection.</p>
          </div>
        </div>
        <form id="consumer-view-form" class="guided-form">
          <label>
            Connection
            <select name="heading_id">${connectionOptions}</select>
          </label>
          <label>
            Ask the view
            <input name="query" placeholder="What context do I need?">
          </label>
          <div class="button-row">
            <button class="primary" name="action" value="retrieve" type="submit"${connections.length ? "" : " disabled"}>Retrieve</button>
          </div>
          <details class="advanced">
            <summary>Send a note</summary>
            <label>
              Message
              <textarea name="message" placeholder="What should the provider know?"></textarea>
            </label>
            <label class="inline-choice">
              <input name="confirmed" type="checkbox">
              Confirm provider-visible note
            </label>
            <div class="button-row">
              <button name="action" value="note" type="submit"${connections.length ? "" : " disabled"}>Send Note</button>
            </div>
          </details>
        </form>
      </section>
    </div>

    <section class="panel wide">
      <div class="section-heading">
        <h2>Current Shared Views</h2>
      </div>
      <div class="two-column">
        <div>
          <h3>Views I Share</h3>
          ${renderItems(providerViews.map((view) => ({ label: view.title || view.view_id || view.error })))}
        </div>
        <div>
          <h3>Views I Use</h3>
          ${renderItems(connections.map((connection) => ({ label: `${connection.heading_id} (${connection.relationship})` })))}
        </div>
      </div>
    </section>

    <section class="panel wide result-panel" id="shared-view-result" hidden>
      <div class="section-heading">
        <h2>Result</h2>
      </div>
      <pre></pre>
    </section>
  `;
}

async function renderMemory() {
  const payload = await fetchJson("/api/memory/files");
  return renderArtifactBrowser("Memory Files", payload.data.files || [], "memory", "/api/memory/files");
}

async function renderInsights() {
  const payload = await fetchJson("/api/insights");
  return renderArtifactBrowser("Insight Logs", payload.data.insights || [], "insight", "/api/insights");
}

async function renderActivity() {
  const activity = await fetchJson("/api/activity");
  const logs = await fetchJson("/api/logs");
  const notes = activity.data.notes || [];
  const inbox = activity.data.inbox || [];
  return `
    <div class="two-column">
      <section class="panel">
        <div class="section-heading">
          <h2>Notes</h2>
        </div>
        ${renderItems(notes)}
      </section>
      <section class="panel">
        <div class="section-heading">
          <h2>Inbox</h2>
        </div>
        ${renderItems(inbox)}
      </section>
    </div>
    ${renderArtifactBrowser("Runtime Logs", logs.data.logs || [], "log", "/api/logs")}
  `;
}

async function renderStatus() {
  const payload = await fetchJson("/api/status");
  const status = payload.data;
  const watches = status.watches || [];
  return `
    <div class="summary-grid">
      ${metricPanel("Root", status.root || "unknown")}
      ${metricPanel("Git", status.git?.summary || "unavailable")}
      ${metricPanel("Issues", (status.issues || []).length ? `${status.issues.length} recent` : "Clear")}
    </div>
    <section class="panel wide">
      <div class="section-heading">
        <h2>Managed Watches</h2>
      </div>
      ${
        watches.length
          ? `<div class="status-list">${watches.map(renderStatusRow).join("")}</div>`
          : "<p>No watches configured.</p>"
      }
    </section>
    <div class="two-column">
      ${renderStatusSection("Dreamer", status.dreamer)}
      ${renderStatusSection("Insight", status.insight)}
    </div>
    ${renderStatusSection("Async Update", status.update, true)}
  `;
}

function renderStatusRow(item) {
  return `
    <div class="status-row">
      <strong>${escapeHtml(item.name || "unknown")}</strong>
      <span>${escapeHtml(item.state || "")}</span>
      ${item.last ? `<small>${escapeHtml(item.last)}</small>` : ""}
    </div>
  `;
}

function renderStatusSection(title, section, wide = false) {
  if (!section) {
    return `<section class="panel${wide ? " wide" : ""}"><h2>${escapeHtml(title)}</h2><p>No data.</p></section>`;
  }
  return `
    <section class="panel${wide ? " wide" : ""}">
      <div class="section-heading">
        <h2>${escapeHtml(title)}</h2>
      </div>
      <p>${escapeHtml(section.state || "")}</p>
      ${section.detail ? `<pre>${escapeHtml(section.detail)}</pre>` : ""}
      ${section.last ? `<p>${escapeHtml(section.last)}</p>` : ""}
    </section>
  `;
}

async function renderSettings() {
  const payload = await fetchJson("/api/settings");
  const settings = payload.data;
  return `
    <section class="panel wide">
      <div class="section-heading">
        <h2>Active Root</h2>
      </div>
      <form id="active-root-form" class="settings-form">
        <label>
          Memory root
          <input id="active-root-input" name="root" value="${escapeHtml(settings.active_root || "")}">
        </label>
        <div class="button-row">
          <button class="primary" type="submit">Save Root</button>
        </div>
      </form>
    </section>

    <section class="panel wide">
      <div class="section-heading">
        <h2>Runtime Settings</h2>
      </div>
      <div class="settings-grid">
        ${settingTile("Config", settings.config_exists ? "rightmemory.toml found" : "Using defaults", settings.config_path)}
        ${runtimeTile("Review", settings.runtime?.review)}
        ${runtimeTile("Update Queue", settings.runtime?.update)}
        ${runtimeTile("Dreamer", settings.runtime?.dreamer_watch)}
        ${runtimeTile("Insight", settings.runtime?.insight_watch)}
        ${runtimeTile("Pruner", settings.runtime?.pruner)}
        ${runtimeTile("Sync", settings.runtime?.sync)}
      </div>
    </section>

    <section class="panel wide">
      <div class="section-heading">
        <h2>Role Executors</h2>
      </div>
      ${renderRoleTable(settings.roles || [])}
    </section>

    <section class="panel wide">
      <div class="section-heading">
        <h2>HTTP Hub Credential</h2>
      </div>
      <form id="credential-form" class="settings-form">
        <label>
          Credential id
          <input name="credential_id" placeholder="alice-publish">
        </label>
        <label>
          Hub URL
          <input name="hub_url" placeholder="https://hub.example.test">
        </label>
        <label>
          Provider id
          <input name="provider_id" placeholder="provider id">
        </label>
        <label>
          View id
          <input name="view_id" placeholder="optional scope">
        </label>
        <label>
          Kind
          <select name="kind">
            <option value="http-publish">Publish to HTTP hub</option>
            <option value="http-connection">Use HTTP shared view</option>
          </select>
        </label>
        <label>
          Token
          <input name="token" type="password" placeholder="token">
        </label>
        <div class="button-row">
          <button class="primary" type="submit">Save Credential</button>
        </div>
      </form>
    </section>
  `;
}

function settingTile(title, value, detail = "") {
  return `
    <div class="setting-tile">
      <strong>${escapeHtml(title)}</strong>
      <span>${escapeHtml(value || "not configured")}</span>
      ${detail ? `<small>${escapeHtml(detail)}</small>` : ""}
    </div>
  `;
}

function runtimeTile(title, wrapper) {
  if (!wrapper || !wrapper.ok) {
    return settingTile(title, "Needs attention", wrapper?.error || "");
  }
  const value = wrapper.value || {};
  if (typeof value.enabled === "boolean") {
    return settingTile(title, value.enabled ? "Enabled" : "Disabled", `stale pull: ${value.stale_pull_after_hours || "default"}h`);
  }
  if (typeof value.trigger_points !== "undefined") {
    return settingTile(title, `${value.trigger_points} trigger points`, `check every ${value.check_interval_seconds}s`);
  }
  if (typeof value.generation_commits !== "undefined") {
    return settingTile(title, `${value.generation_commits} commits`, `${value.revival_grace_checkpoints} revival checkpoints`);
  }
  if (typeof value.batch_size !== "undefined") {
    return settingTile(title, `${value.batch_size} sessions`, `${value.since_days} day review window`);
  }
  if (typeof value.target_batch_candidates !== "undefined") {
    return settingTile(title, `${value.target_batch_candidates} candidates`, `${value.max_wait_seconds}s max wait`);
  }
  return settingTile(title, "Configured");
}

function renderRoleTable(roles) {
  if (!roles.length) {
    return "<p>No role settings loaded.</p>";
  }
  return `
    <div class="role-table">
      <div class="role-head">Role</div>
      <div class="role-head">Executor</div>
      <div class="role-head">Details</div>
      ${roles.map(renderRoleRow).join("")}
    </div>
  `;
}

function renderRoleRow(role) {
  if (!role.ok) {
    return `
      <div>${escapeHtml(role.role)}</div>
      <div>Needs config</div>
      <div>${escapeHtml(role.error || "")}</div>
    `;
  }
  const executor = role.executor || {};
  const mode = executor.mode || "unknown";
  const detail =
    mode === "cli-agent"
      ? `${executor.provider || "provider"}${executor.model ? ` / ${executor.model}` : ""}`
      : `${executor.model_id || "model"}${executor.api_base ? ` / ${executor.api_base}` : ""}`;
  return `
    <div>${escapeHtml(role.role)}</div>
    <div>${escapeHtml(mode)}</div>
    <div>${escapeHtml(detail)}</div>
  `;
}

function renderArtifactBrowser(title, items, type, endpoint) {
  return `
    <section class="panel wide artifact-browser">
      <div class="section-heading">
        <h2>${escapeHtml(title)}</h2>
      </div>
      ${
        items.length
          ? `<div class="browser-grid">
              <div class="artifact-list">
                ${items.map((item) => renderArtifactButton(item, type, endpoint)).join("")}
              </div>
              <article class="artifact-preview" id="${escapeHtml(type)}-preview">
                <p>Select an item to preview it.</p>
              </article>
            </div>`
          : "<p>No records.</p>"
      }
    </section>
  `;
}

function renderArtifactButton(item, type, endpoint) {
  const missing = item.exists === false ? " missing" : "";
  return `
    <button class="list-button${missing}" type="button" data-artifact-type="${escapeHtml(type)}" data-endpoint="${escapeHtml(endpoint)}" data-id="${escapeHtml(item.id)}">
      <strong>${escapeHtml(item.label || item.id)}</strong>
      <small>${escapeHtml(item.kind || "")}</small>
    </button>
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
    html = await renderSettings();
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

function splitTerms(value) {
  return String(value || "")
    .split(/[\n,]/)
    .map((term) => term.trim())
    .filter(Boolean);
}

function slugify(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
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
  attachSharedViewHandlers();
  attachSettingsHandlers();
  attachArtifactHandlers();
}

function attachSharedViewHandlers() {
  const defineViewForm = document.querySelector("#define-view-form");
  if (defineViewForm) {
    defineViewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const title = String(form.get("title") || "").trim();
        const viewId = String(form.get("view_id") || "").trim() || slugify(title);
        const payload = await fetchJson("/api/share/views", {
          method: "POST",
          body: JSON.stringify({
            view_id: viewId,
            title,
            description: form.get("description"),
            audience: form.get("audience"),
            source_globs: splitTerms(form.get("source_globs")),
            filter_terms: splitTerms(form.get("filter_terms")),
            include_all: form.get("include_all") === "on",
          }),
        });
        setMessage(payload.message);
        await loadPanel();
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const providerViewForm = document.querySelector("#provider-view-form");
  if (providerViewForm) {
    providerViewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const action = event.submitter?.value;
        const viewId = String(form.get("view_id") || "").trim();
        const query = String(form.get("query") || "").trim();
        let path = `/api/share/views/${encodeURIComponent(viewId)}/build`;
        let body = query ? { query } : {};
        if (action === "export") {
          path = `/api/share/views/${encodeURIComponent(viewId)}/export`;
          body = { target: form.get("target"), replace: true, ...(query ? { query } : {}) };
        } else if (action === "publish-mounted") {
          path = `/api/share/views/${encodeURIComponent(viewId)}/publish`;
          body = { kind: "mounted", hub: form.get("target"), replace: true, ...(query ? { query } : {}) };
        } else if (action === "publish-http") {
          path = `/api/share/views/${encodeURIComponent(viewId)}/publish`;
          body = {
            kind: "http",
            hub_url: form.get("hub_url"),
            credential_id: String(form.get("credential_id") || "").trim() || `${viewId}-publish`,
            ...(query ? { query } : {}),
          };
        }
        const payload = await fetchJson(path, { method: "POST", body: JSON.stringify(body) });
        setMessage(payload.message);
        if (action === "build") {
          await loadPanel();
        }
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const acceptInviteForm = document.querySelector("#accept-invite-form");
  if (acceptInviteForm) {
    acceptInviteForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const payload = await fetchJson("/api/use/accept-invite", {
          method: "POST",
          body: JSON.stringify({
            invitation: form.get("invitation"),
            heading_id: form.get("heading_id"),
            title: form.get("title"),
            relationship: form.get("relationship"),
          }),
        });
        setMessage(payload.message);
        await loadPanel();
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const consumerViewForm = document.querySelector("#consumer-view-form");
  if (consumerViewForm) {
    consumerViewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const action = event.submitter?.value;
        const headingId = String(form.get("heading_id") || "").trim();
        const path =
          action === "note"
            ? `/api/use/connections/${encodeURIComponent(headingId)}/note`
            : `/api/use/connections/${encodeURIComponent(headingId)}/retrieve`;
        const body =
          action === "note"
            ? {
                message: form.get("message"),
                confirmed: form.get("confirmed") === "on",
              }
            : { query: form.get("query") };
        const payload = await fetchJson(path, { method: "POST", body: JSON.stringify(body) });
        showSharedViewResult(payload.data?.text || payload.message);
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }
}

function attachSettingsHandlers() {
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
        await loadPanel();
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const credentialForm = document.querySelector("#credential-form");
  if (credentialForm) {
    credentialForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const payload = await fetchJson("/api/share/credentials", {
          method: "POST",
          body: JSON.stringify({
            credential_id: form.get("credential_id"),
            kind: form.get("kind"),
            hub_url: form.get("hub_url"),
            provider_id: form.get("provider_id"),
            view_id: form.get("view_id"),
            token: form.get("token"),
          }),
        });
        event.currentTarget.reset();
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }
}

function attachArtifactHandlers() {
  document.querySelectorAll(".list-button[data-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const endpoint = button.dataset.endpoint;
        const id = button.dataset.id;
        const type = button.dataset.artifactType;
        const payload = await fetchJson(`${endpoint}/${encodeURIComponent(id)}`);
        const preview = document.querySelector(`#${CSS.escape(type)}-preview`);
        if (preview) {
          preview.innerHTML = `
            <h3>${escapeHtml(payload.data.label || id)}</h3>
            <small>${escapeHtml(payload.data.path || "")}</small>
            <pre>${escapeHtml(payload.data.text || "")}</pre>
          `;
        }
      } catch (error) {
        setMessage(error.message);
      }
    });
  });
}

function showSharedViewResult(text) {
  const panel = document.querySelector("#shared-view-result");
  if (!panel) {
    return;
  }
  panel.hidden = false;
  panel.querySelector("pre").textContent = text || "";
}

loadSession().catch((error) => {
  setAuthenticated(false);
  setMessage(error.message);
});
