const state = {
  token: window.localStorage.getItem("rightmemory.hub.adminToken") || "",
  views: []
};

const sections = ["overview", "providers", "views", "invitations", "connections", "inbox", "audit", "tokens"];

function $(selector) {
  return document.querySelector(selector);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showNotice(message, isError = false) {
  const node = $("#notice");
  node.hidden = false;
  node.textContent = message;
  node.classList.toggle("danger", isError);
}

async function api(path, options = {}) {
  if (!state.token) {
    throw new Error("Admin token is required.");
  }
  const response = await fetch(path, {
    ...options,
    headers: {
      "Authorization": `Bearer ${state.token}`,
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return body;
}

function renderTable(target, columns, rows, actions = () => "") {
  const node = $(target);
  if (!rows.length) {
    node.innerHTML = "<p>No records.</p>";
    return;
  }
  const header = columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join("");
  const body = rows.map((row) => {
    const cells = columns.map((column) => `<td>${column.render ? column.render(row) : escapeHtml(row[column.key])}</td>`).join("");
    return `<tr>${cells}<td>${actions(row)}</td></tr>`;
  }).join("");
  node.innerHTML = `<table><thead><tr>${header}<th>Actions</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderOverview(overview) {
  $("#health-line").textContent = `${overview.public_base_url} - ${overview.initialized ? "initialized" : "uninitialized"}`;
  const items = [
    ["Providers", overview.provider_count],
    ["Views", overview.view_count],
    ["Active Tokens", overview.active_token_count],
    ["Interactions", overview.interaction_count],
    ["Audit Events", overview.audit_event_count],
    ["Auth Failures", overview.recent_auth_failure_count],
    ["Storage", overview.storage_present ? "present" : "missing"],
    ["Max Package Bytes", overview.max_package_bytes]
  ];
  $("#overview-grid").innerHTML = items.map(([label, value]) => (
    `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
  )).join("");
}

async function loadOverview() {
  const data = await api("/api/admin/overview");
  renderOverview(data.overview);
}

async function loadProviders() {
  const data = await api("/api/admin/providers");
  renderTable("#providers-table", [
    {key: "provider_id", label: "Provider"},
    {key: "label", label: "Label"},
    {key: "view_count", label: "Views"},
    {key: "active_token_count", label: "Active Tokens"},
    {key: "updated_at", label: "Updated"}
  ], data.providers);
}

async function loadViews() {
  const data = await api("/api/admin/views");
  state.views = data.views;
  renderTable("#views-table", [
    {key: "view_id", label: "View"},
    {key: "provider_id", label: "Provider"},
    {key: "kind", label: "Kind"},
    {key: "title", label: "Title"},
    {key: "current_version_id", label: "Current Version"},
    {key: "question_base_url", label: "Question URL"},
    {key: "updated_at", label: "Updated"}
  ], data.views);
}

async function loadInvitations() {
  const rows = [];
  for (const view of state.views) {
    const data = await api(`/api/admin/views/${encodeURIComponent(view.view_id)}/invitations`);
    rows.push(...data.invitations);
  }
  renderTable("#invitations-table", [
    {key: "invitation_id", label: "Invitation"},
    {key: "view_id", label: "View"},
    {key: "token_id", label: "Token"},
    {key: "label", label: "Label"},
    {key: "accepted_count", label: "Accepted"},
    {key: "revoked_at", label: "Revoked"}
  ], rows, (row) => row.revoked_at ? "" : `<button data-revoke-invitation="${escapeHtml(row.token_id)}">Revoke</button>`);
}

async function loadConnections() {
  const data = await api("/api/admin/connections");
  renderTable("#connections-table", [
    {key: "connection_id", label: "Connection"},
    {key: "provider_id", label: "Provider"},
    {key: "view_id", label: "View"},
    {key: "consumer_label", label: "Consumer"},
    {key: "token_id", label: "Token"},
    {key: "revoked_at", label: "Revoked"}
  ], data.connections, (row) => row.revoked_at ? "" : `<button data-revoke-connection="${escapeHtml(row.token_id)}">Revoke</button>`);
}

async function loadInbox() {
  const data = await api("/api/admin/inbox");
  renderTable("#inbox-table", [
    {key: "interaction_id", label: "Interaction"},
    {key: "provider_id", label: "Provider"},
    {key: "view_id", label: "View"},
    {key: "connection_id", label: "Connection"},
    {key: "payload", label: "Message", render: (row) => escapeHtml(row.payload?.message || JSON.stringify(row.payload))},
    {key: "created_at", label: "Created"}
  ], data.interactions);
}

async function loadAudit() {
  const data = await api("/api/admin/audit");
  renderTable("#audit-table", [
    {key: "event_id", label: "Event"},
    {key: "kind", label: "Kind"},
    {key: "actor_id", label: "Actor"},
    {key: "provider_id", label: "Provider"},
    {key: "view_id", label: "View"},
    {key: "created_at", label: "Created"}
  ], data.events);
}

async function loadTokens() {
  const data = await api("/api/admin/tokens");
  renderTable("#tokens-table", [
    {key: "token_id", label: "Token"},
    {key: "action", label: "Action"},
    {key: "provider_id", label: "Provider"},
    {key: "view_id", label: "View"},
    {key: "label", label: "Label"},
    {key: "revoked_at", label: "Revoked"}
  ], data.tokens, (row) => row.revoked_at ? "" : `<button data-revoke-token="${escapeHtml(row.token_id)}">Revoke</button>`);
}

async function refreshAll() {
  await loadOverview();
  await loadProviders();
  await loadViews();
  await loadInvitations();
  await loadConnections();
  await loadInbox();
  await loadAudit();
  await loadTokens();
}

function activateTab(name) {
  for (const section of sections) {
    $(`#${section}`).classList.toggle("active", section === name);
    document.querySelector(`[data-tab="${section}"]`).classList.toggle("active", section === name);
  }
}

async function createProviderToken(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const providerId = form.get("provider_id");
  const payload = {label: form.get("label") || null};
  const data = await api(`/api/admin/providers/${encodeURIComponent(providerId)}/tokens`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
  const box = $("#provider-token-result");
  box.hidden = false;
  box.textContent = `Raw token for ${data.provider_id}: ${data.raw_token}`;
  await refreshAll();
}

async function createInvitation(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const viewId = form.get("view_id");
  const payload = {
    label: form.get("label") || null,
    expires_at: form.get("expires_at") || null
  };
  const data = await api(`/api/admin/views/${encodeURIComponent(viewId)}/invitations`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
  const box = $("#invitation-result");
  box.hidden = false;
  box.textContent = data.invitation_url;
  await refreshAll();
}

async function revokeByToken(path, tokenId) {
  await api(`${path}/${encodeURIComponent(tokenId)}/revoke`, {method: "POST", body: "{}"});
  await refreshAll();
}

document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (target.dataset.tab) {
    activateTab(target.dataset.tab);
    return;
  }
  try {
    if (target.dataset.revokeToken) {
      await revokeByToken("/api/admin/tokens", target.dataset.revokeToken);
    }
    if (target.dataset.revokeInvitation) {
      await revokeByToken("/api/admin/invitations", target.dataset.revokeInvitation);
    }
    if (target.dataset.revokeConnection) {
      await revokeByToken("/api/admin/connections", target.dataset.revokeConnection);
    }
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  state.token = $("#admin-token").value;
  window.localStorage.setItem("rightmemory.hub.adminToken", state.token);
  try {
    await refreshAll();
    showNotice("Connected.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#provider-token-form").addEventListener("submit", async (event) => {
  try {
    await createProviderToken(event);
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#invitation-form").addEventListener("submit", async (event) => {
  try {
    await createInvitation(event);
  } catch (error) {
    showNotice(error.message, true);
  }
});

if (state.token) {
  $("#admin-token").value = state.token;
  refreshAll().catch((error) => showNotice(error.message, true));
}
