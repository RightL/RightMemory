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

function credentialLabel(credential) {
  const parts = [credential.credential_id || ""];
  if (credential.kind) {
    parts.push(credential.kind);
  }
  if (credential.base_url) {
    parts.push(credential.base_url);
  }
  return parts.filter(Boolean).join(" | ");
}

function renderCredentialOptions(credentials, blankLabel = "") {
  const options = [];
  if (blankLabel) {
    options.push(`<option value="">${escapeHtml(blankLabel)}</option>`);
  }
  if (!credentials.length && !blankLabel) {
    return `<option value="">No credentials yet</option>`;
  }
  credentials.forEach((credential) => {
    options.push(`
      <option
        value="${escapeHtml(credential.credential_id || "")}"
        data-base-url="${escapeHtml(credential.base_url || "")}"
        data-provider-id="${escapeHtml(credential.provider_id || "")}"
      >${escapeHtml(credentialLabel(credential))}</option>
    `);
  });
  return options.join("");
}

function renderJsonPanel(title, value) {
  return `<section class="panel wide"><h2>${escapeHtml(title)}</h2><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></section>`;
}

function renderProviderInbox(interactions) {
  if (!interactions.length) {
    return `<p>No provider inbox records.</p>`;
  }
  const groups = new Map();
  interactions.forEach((interaction) => {
    const viewId = interaction.view_id || "unknown-view";
    if (!groups.has(viewId)) {
      groups.set(viewId, []);
    }
    groups.get(viewId).push(interaction);
  });
  return Array.from(groups.entries()).map(([viewId, records]) => `
    <div class="record-group">
      <h3>${escapeHtml(viewId)}</h3>
      <div class="record-list">
        ${records.map(renderInboxRecord).join("")}
      </div>
    </div>
  `).join("");
}

function renderInboxRecord(record) {
  const payload = record.payload || {};
  const message = payload.message || record.message || "";
  const task = payload.task_context || record.task_context || "";
  return `
    <article class="record-card">
      <strong>${escapeHtml(message || record.interaction_id || "Inbox record")}</strong>
      <small>${escapeHtml(record.created_at || "")}</small>
      <small>${escapeHtml(record.connection_id || "")}${record.actor_id ? ` | ${escapeHtml(record.actor_id)}` : ""}</small>
      ${task ? `<p>${escapeHtml(task)}</p>` : ""}
    </article>
  `;
}

function renderPublishEvents(events) {
  if (!events.length) {
    return `<p>No publish events.</p>`;
  }
  return `
    <div class="record-list">
      ${events.map((event) => `
        <article class="record-card">
          <strong>${escapeHtml(event.view_id || "unknown-view")} ${escapeHtml(event.status || "")}</strong>
          <small>${escapeHtml(event.created_at || "")}${event.trigger ? ` | ${escapeHtml(event.trigger)}` : ""}</small>
          <p>${escapeHtml(event.message || "")}</p>
        </article>
      `).join("")}
    </div>
  `;
}

function renderShareRelationships(relationships) {
  if (!relationships.length) {
    return `<p>No shares yet.</p>`;
  }
  return `
    <div class="record-list">
      ${relationships.map((share) => `
        <article class="record-card share-card">
          <strong>${escapeHtml(share.title || share.share_id || "Share")}</strong>
          <small>${escapeHtml(share.share_id || "")} | ${escapeHtml(share.role || "")} | ${escapeHtml(share.state || "")} | ${escapeHtml(share.transport || "http")} | ${escapeHtml(share.capability || "auto")}</small>
          ${renderShareTransportSummary(share)}
          <p>${escapeHtml(renderSharePartsSummary(share))}</p>
          ${renderShareActions(share)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderShareTransportSummary(share) {
  const details = [];
  if (share.git_url) {
    details.push(`repo: ${share.git_url}`);
  }
  if (share.git_branch) {
    details.push(`branch: ${share.git_branch}`);
  }
  if (share.invitation_url) {
    details.push(`join: ${share.invitation_url}`);
  }
  return details.length ? `<small>${escapeHtml(details.join(" | "))}</small>` : "";
}

function renderShareActions(share) {
  const actions = [];
  const canPublish = share.role === "provider" && ["approved", "published"].includes(share.state);
  if (share.role === "provider") {
    actions.push(`
      <button class="share-publish" type="button" data-share-id="${escapeHtml(share.share_id || "")}"${canPublish ? "" : " disabled"}>
        Publish to ${escapeHtml(share.transport === "git" ? "Git" : "Hub")}
      </button>
    `);
  }
  if (share.invitation_url) {
    actions.push(`
      <button class="copy-join-url" type="button" data-join-url="${escapeHtml(share.invitation_url)}">
        Copy Join URL
      </button>
    `);
  }
  return actions.length ? `<div class="button-row share-actions">${actions.join("")}</div>` : "";
}

function renderSharePartsSummary(share) {
  const parts = [];
  if (share.file) {
    parts.push(`file: ${share.file.view_id || share.file.heading_id || "pending"}`);
  }
  if (share.question) {
    parts.push(`questions: ${share.question.view_id || share.question.heading_id || "pending"}`);
  }
  return parts.join(" | ") || "No configured parts.";
}

function formatShareOperationResult(result) {
  const lines = [`${result.share_id || "share"} ${result.role || ""} ${result.state || ""} capability=${result.capability || "auto"}`.trim()];
  if (result.builder_final_message) {
    lines.push("", "Builder summary:", result.builder_final_message);
  }
  if (Array.isArray(result.statuses) && result.statuses.length) {
    lines.push("", "Status:");
    result.statuses.forEach((status) => {
      const artifact = status.artifact_id || "-";
      const message = status.message ? `: ${status.message}` : "";
      lines.push(`${status.capability || "share"} ${artifact} ${status.status || "unknown"}${message}`);
    });
  }
  if (result.invitation_url) {
    lines.push("", `invitation_url\t${result.invitation_url}`);
  }
  if (result.next_action) {
    lines.push("", "Next:", result.next_action);
  }
  return lines.join("\n").trim();
}

async function renderSharedViews() {
  const [payload, sharePayload] = await Promise.all([
    fetchJson("/api/share/views"),
    fetchJson("/api/share/relationships"),
  ]);
  const providerViews = payload.data.provider_views || [];
  const connections = payload.data.connections || [];
  const credentials = payload.data.credentials || [];
  const relationships = sharePayload.data.relationships || [];
  const credentialOptions = renderCredentialOptions(credentials);
  const optionalCredentialOptions = renderCredentialOptions(credentials, "Use recipe default");
  const relationshipOptions = renderOptions(
    relationships.map((share) => ({
      value: share.share_id,
      label: `${share.share_id} (${share.state || "share"}, ${share.capability || "auto"})`,
    })),
  );
  const providerOptions = renderOptions(
    providerViews.map((view) => ({
      value: view.view_id,
      label: `${view.view_id || view.error} (${view.type || "view"}${view.approved ? ", approved" : ""})`,
    })),
  );
  const fileProviderOptions = renderOptions(
    providerViews
      .filter((view) => view.type === "file")
      .map((view) => ({
        value: view.view_id,
        label: `${view.view_id || view.error}${view.approved ? " (approved)" : ""}`,
      })),
  );
  const questionProviderOptions = renderOptions(
    providerViews
      .filter((view) => view.type === "question")
      .map((view) => ({
        value: view.view_id,
        label: `${view.view_id || view.error}${view.approved ? " (approved)" : ""}`,
      })),
  );
  const connectionOptions = renderOptions(
    connections.map((connection) => ({
      value: connection.heading_id,
      label: `${connection.heading_id} (${connection.type || connection.view_type || "shared view"})`,
    })),
  );
  const fileConnectionOptions = renderOptions(
    connections
      .filter((connection) => (connection.type || connection.view_type) === "file")
      .map((connection) => ({
        value: connection.heading_id,
        label: connection.heading_id,
      })),
  );
  const questionConnectionOptions = renderOptions(
    connections
      .filter((connection) => (connection.type || connection.view_type) === "question")
      .map((connection) => ({
        value: connection.heading_id,
        label: connection.heading_id,
      })),
  );
  const hasFileConnections = connections.some((connection) => (connection.type || connection.view_type) === "file");
  const hasQuestionConnections = connections.some((connection) => (connection.type || connection.view_type) === "question");
  const hasFileProviderViews = providerViews.some((view) => view.type === "file");
  const hasQuestionProviderViews = providerViews.some((view) => view.type === "question");
  const hasCredentials = credentials.length > 0;
  const hasRelationships = relationships.length > 0;

  return `
    <section class="panel wide share-console">
      <div class="section-heading">
        <h2>Shares</h2>
      </div>
      ${renderShareRelationships(relationships)}
    </section>

    <div class="flow-layout">
      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">1</span>
          <div>
            <h2>Create Share</h2>
          </div>
        </div>
        <form id="create-share-form" class="guided-form">
          <label>
            Share id
            <input name="share_id" placeholder="auth-api" required>
          </label>
          <label>
            Title
            <input name="title" placeholder="Auth API">
          </label>
          <label>
            Transport
            <select name="transport">
              <option value="http">HTTP Hub</option>
              <option value="git">Git Repo</option>
            </select>
          </label>
          <label data-transport-field="http">
            Credential
            <select class="credential-select" name="credential_id">${credentialOptions}</select>
          </label>
          <label>
            Provider id
            <input name="provider_id" placeholder="alice" required>
          </label>
          <label data-transport-field="http">
            HTTP hub URL
            <input name="hub_url" placeholder="https://hub.example.test">
          </label>
          <label data-transport-field="git" hidden>
            Git repo URL
            <input name="git_url" placeholder="https://github.com/user/rightmemory-shares.git">
          </label>
          <label data-transport-field="git" hidden>
            Branch
            <input name="git_branch" placeholder="default branch">
          </label>
          <label data-capability-field>
            Capability
            <select name="capability">
              <option value="auto">Auto</option>
              <option value="file-context">File context</option>
              <option value="live-questions">Live questions</option>
              <option value="both">Both</option>
            </select>
          </label>
          <label>
            Request
            <textarea name="request" placeholder="Share the auth API integration context with the frontend project." required></textarea>
          </label>
          <label data-question-field>
            Question base URL
            <input name="question_base_url" placeholder="only needed for live questions">
          </label>
          <div class="button-row">
            <button class="primary" type="submit">Create Share</button>
          </div>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">2</span>
          <div>
            <h2>Revise Share</h2>
          </div>
        </div>
        <form id="revise-share-form" class="guided-form">
          <label>
            Share
            <select name="share_id">${relationshipOptions}</select>
          </label>
          <label>
            Revision
            <textarea name="revision" placeholder="Narrow this share to token refresh behavior." required></textarea>
          </label>
          <label>
            Capability
            <select name="capability">
              <option value="">Keep current</option>
              <option value="auto">Auto</option>
              <option value="file-context">File context</option>
              <option value="live-questions">Live questions</option>
              <option value="both">Both</option>
            </select>
          </label>
          <label>
            Question base URL
            <input name="question_base_url" placeholder="only if changing live questions">
          </label>
          <div class="button-row">
            <button class="primary" type="submit"${hasRelationships ? "" : " disabled"}>Revise Share</button>
          </div>
        </form>
      </section>
    </div>

    <details id="advanced-shared-view-tools" class="advanced-tools wide">
      <summary>Advanced MF/MQ tools</summary>
      <div class="flow-layout">
      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">A</span>
          <div>
            <h2>Build File View</h2>
          </div>
        </div>
        <form id="build-file-view-form" class="guided-form">
          <label>
            View id
            <input name="view_id" placeholder="auth-api-files" required>
          </label>
          <label>
            Title
            <input name="title" placeholder="Auth API Files" required>
          </label>
          <label>
            Intent
            <textarea name="intent" placeholder="Expose auth API integration context" required></textarea>
          </label>
          <label>
            HTTP hub URL
            <input name="hub_url" placeholder="https://hub.example.test" required>
          </label>
          <label>
            Credential
            <select class="credential-select" name="credential_id" required>${credentialOptions}</select>
          </label>
          <div class="button-row">
            <button class="primary" type="submit"${hasCredentials ? "" : " disabled"}>Build File View</button>
          </div>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">B</span>
          <div>
            <h2>Build Question View</h2>
          </div>
        </div>
        <form id="build-question-view-form" class="guided-form">
          <label>
            View id
            <input name="view_id" placeholder="auth-api-ask" required>
          </label>
          <label>
            Title
            <input name="title" placeholder="Auth API Questions" required>
          </label>
          <label>
            Intent
            <textarea name="intent" placeholder="Let frontend agents ask auth API questions" required></textarea>
          </label>
          <div class="button-row">
            <button class="primary" type="submit">Build Question View</button>
          </div>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">C</span>
          <div>
            <h2>Approve View</h2>
          </div>
        </div>
        <form id="approve-view-form" class="guided-form">
          <label>
            View
            <select name="view_id">${providerOptions}</select>
          </label>
          <label>
            Type
            <select name="type">
              <option value="file">File</option>
              <option value="question">Question</option>
            </select>
          </label>
          <div class="button-row">
            <button class="primary" type="submit"${providerViews.length ? "" : " disabled"}>Approve</button>
          </div>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">D</span>
          <div>
            <h2>Create File Invitation</h2>
          </div>
        </div>
        <form id="invite-file-view-form" class="guided-form">
          <label>
            File view
            <select name="view_id">${fileProviderOptions}</select>
          </label>
          <details class="advanced">
            <summary>Hub override</summary>
            <label>
              HTTP hub URL
              <input name="hub_url" placeholder="from recipe">
            </label>
            <label>
              Credential
              <select class="credential-select" name="credential_id">${optionalCredentialOptions}</select>
            </label>
            <label>
              Label
              <input name="label" placeholder="frontend">
            </label>
            <label>
              Expires at
              <input name="expires_at" placeholder="2026-07-01T00:00:00+00:00">
            </label>
          </details>
          <div class="button-row">
            <button class="primary" type="submit"${hasFileProviderViews ? "" : " disabled"}>Create Invitation</button>
          </div>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">E</span>
          <div>
            <h2>Publish Question Invitation</h2>
          </div>
        </div>
        <form id="publish-question-view-form" class="guided-form">
          <label>
            Question view
            <select name="view_id">${questionProviderOptions}</select>
          </label>
          <label>
            HTTP hub URL
            <input name="hub_url" placeholder="https://hub.example.test" required>
          </label>
          <label>
            Credential
            <select class="credential-select" name="credential_id" required>${credentialOptions}</select>
          </label>
          <label>
            Question base URL
            <input name="question_base_url" placeholder="https://provider.example.test" required>
          </label>
          <details class="advanced">
            <summary>Invitation options</summary>
            <label>
              Label
              <input name="label" placeholder="frontend">
            </label>
            <label>
              Expires at
              <input name="expires_at" placeholder="2026-07-01T00:00:00+00:00">
            </label>
          </details>
          <div class="button-row">
            <button class="primary" type="submit"${hasQuestionProviderViews && hasCredentials ? "" : " disabled"}>Publish Invitation</button>
          </div>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">F</span>
          <div>
            <h2>Accept a View</h2>
          </div>
        </div>
        <form id="accept-invite-form" class="guided-form">
          <label>
            Invitation
            <textarea name="invitation" placeholder="https://.../i/token or https://github.com/user/rightmemory-shares.git#share=auth-api" required></textarea>
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
          <span class="step-badge">G</span>
          <div>
            <h2>Use a Connected View</h2>
          </div>
        </div>
        <form id="file-connection-form" class="guided-form">
          <label>
            File connection
            <select name="heading_id">${fileConnectionOptions}</select>
          </label>
          <div class="button-row">
            <button class="primary" name="action" value="pull" type="submit"${hasFileConnections ? "" : " disabled"}>Pull</button>
            <button name="action" value="status" type="submit"${hasFileConnections ? "" : " disabled"}>Status</button>
            <button id="pull-all-connections" type="button"${hasFileConnections ? "" : " disabled"}>Pull All</button>
            <button id="status-all-connections" type="button"${connections.length ? "" : " disabled"}>Status All</button>
          </div>
        </form>
        <form id="question-connection-form" class="guided-form">
          <label>
            Question connection
            <select name="heading_id">${questionConnectionOptions}</select>
          </label>
          <label>
            Question
            <input name="question" placeholder="How do tokens refresh?">
          </label>
          <div class="button-row">
            <button class="primary" type="submit"${hasQuestionConnections ? "" : " disabled"}>Ask</button>
          </div>
        </form>
        <form id="consumer-note-form" class="guided-form">
          <details class="advanced">
            <summary>Send a note</summary>
            <label>
              Connection
              <select name="heading_id">${connectionOptions}</select>
            </label>
            <label>
              Message
              <textarea name="message" placeholder="What should the provider know?"></textarea>
            </label>
            <label class="inline-choice">
              <input name="confirmed" type="checkbox">
              Confirm provider-visible note
            </label>
            <div class="button-row">
              <button type="submit"${connections.length ? "" : " disabled"}>Send Note</button>
            </div>
          </details>
        </form>
      </section>

      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">H</span>
          <div>
            <h2>Provider Inbox</h2>
          </div>
        </div>
        <form id="provider-inbox-form" class="guided-form">
          <label>
            Credential
            <select class="credential-select" name="credential_id" required>${credentialOptions}</select>
          </label>
          <details class="advanced">
            <summary>Provider override</summary>
            <label>
              HTTP hub URL
              <input name="hub_url" placeholder="from credential">
            </label>
            <label>
              Provider id
              <input name="provider_id" placeholder="from credential">
            </label>
          </details>
          <div class="button-row">
            <button class="primary" type="submit"${hasCredentials ? "" : " disabled"}>Load Inbox</button>
          </div>
        </form>
        <div id="provider-inbox-list" class="record-output">
          <p>No provider inbox loaded.</p>
        </div>
      </section>
      </div>
    </details>

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

    <section class="panel wide" id="publish-events-panel">
      <div class="section-heading">
        <h2>Auto-Publish Events</h2>
      </div>
      <div id="publish-events-list" class="record-output"><p>Load events to inspect recent file-view publishing.</p></div>
      <div class="button-row">
        <button id="load-publish-events" type="button">Load Events</button>
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
    return settingTile(
      title,
      `${value.trigger_candidates} trigger / ${value.target_batch_candidates} target`,
      `${value.max_wait_seconds}s max wait`,
    );
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
  attachCredentialSelectHandlers();

  const createShareForm = document.querySelector("#create-share-form");
  if (createShareForm) {
    const transportSelect = createShareForm.querySelector('select[name="transport"]');
    if (transportSelect) {
      transportSelect.addEventListener("change", () => syncCreateShareTransport(createShareForm));
    }
    syncCreateShareTransport(createShareForm);
    createShareForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const transport = String(form.get("transport") || "http");
        const body = {
          share_id: form.get("share_id"),
          title: form.get("title"),
          provider_id: form.get("provider_id"),
          transport,
          request: form.get("request"),
        };
        if (transport === "git") {
          body.git_url = form.get("git_url");
          body.git_branch = form.get("git_branch");
          body.capability = "file-context";
        } else {
          body.hub_url = form.get("hub_url");
          body.credential_id = form.get("credential_id");
          body.capability = form.get("capability");
          body.question_base_url = form.get("question_base_url");
        }
        const payload = await fetchJson("/api/share/relationships", {
          method: "POST",
          body: JSON.stringify(body),
        });
        const resultText = formatShareOperationResult(payload.data || {});
        setMessage(payload.message);
        await loadPanel();
        showSharedViewResult(resultText);
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  document.querySelectorAll(".share-publish").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const shareId = button.dataset.shareId || "";
        const payload = await fetchJson(`/api/share/relationships/${encodeURIComponent(shareId)}/publish`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        setMessage(payload.message);
        await loadPanel();
        showSharedViewResult(payload.data?.message || payload.message);
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  });

  document.querySelectorAll(".copy-join-url").forEach((button) => {
    button.addEventListener("click", async () => {
      const value = button.dataset.joinUrl || "";
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(value);
          setMessage("Join URL copied.");
        } else {
          showSharedViewResult(value);
          setMessage("Join URL ready.");
        }
      } catch (error) {
        showSharedViewResult(value);
        setMessage("Join URL ready.");
      }
    });
  });

  const reviseShareForm = document.querySelector("#revise-share-form");
  if (reviseShareForm) {
    reviseShareForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const shareId = String(form.get("share_id") || "").trim();
        const payload = await fetchJson(`/api/share/relationships/${encodeURIComponent(shareId)}/revise`, {
          method: "POST",
          body: JSON.stringify({
            revision: form.get("revision"),
            capability: form.get("capability"),
            question_base_url: form.get("question_base_url"),
          }),
        });
        const resultText = formatShareOperationResult(payload.data || {});
        setMessage(payload.message);
        await loadPanel();
        showSharedViewResult(resultText);
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const buildFileViewForm = document.querySelector("#build-file-view-form");
  if (buildFileViewForm) {
    buildFileViewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const payload = await fetchJson("/api/share/views/build-file", {
          method: "POST",
          body: JSON.stringify({
            view_id: form.get("view_id"),
            title: form.get("title"),
            intent: form.get("intent"),
            hub_url: form.get("hub_url"),
            credential_id: form.get("credential_id"),
          }),
        });
        setMessage(payload.message);
        await loadPanel();
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const buildQuestionViewForm = document.querySelector("#build-question-view-form");
  if (buildQuestionViewForm) {
    buildQuestionViewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const payload = await fetchJson("/api/share/views/build-question", {
          method: "POST",
          body: JSON.stringify({
            view_id: form.get("view_id"),
            title: form.get("title"),
            intent: form.get("intent"),
          }),
        });
        setMessage(payload.message);
        await loadPanel();
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const approveViewForm = document.querySelector("#approve-view-form");
  if (approveViewForm) {
    approveViewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const viewId = String(form.get("view_id") || "").trim();
        const payload = await fetchJson(`/api/share/views/${encodeURIComponent(viewId)}/approve`, {
          method: "POST",
          body: JSON.stringify({
            type: form.get("type"),
          }),
        });
        setMessage(payload.message);
        await loadPanel();
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const inviteFileViewForm = document.querySelector("#invite-file-view-form");
  if (inviteFileViewForm) {
    inviteFileViewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const viewId = String(form.get("view_id") || "").trim();
        const payload = await fetchJson(`/api/share/views/${encodeURIComponent(viewId)}/invite`, {
          method: "POST",
          body: JSON.stringify({
            hub_url: form.get("hub_url"),
            credential_id: form.get("credential_id"),
            label: form.get("label"),
            expires_at: form.get("expires_at"),
          }),
        });
        showSharedViewResult(payload.message);
        setMessage(payload.message);
        await loadPanel();
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const publishQuestionViewForm = document.querySelector("#publish-question-view-form");
  if (publishQuestionViewForm) {
    publishQuestionViewForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const viewId = String(form.get("view_id") || "").trim();
        const payload = await fetchJson(`/api/share/views/${encodeURIComponent(viewId)}/publish-question`, {
          method: "POST",
          body: JSON.stringify({
            hub_url: form.get("hub_url"),
            credential_id: form.get("credential_id"),
            question_base_url: form.get("question_base_url"),
            label: form.get("label"),
            expires_at: form.get("expires_at"),
          }),
        });
        showSharedViewResult(payload.message);
        setMessage(payload.message);
        await loadPanel();
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

  const fileConnectionForm = document.querySelector("#file-connection-form");
  if (fileConnectionForm) {
    fileConnectionForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const action = event.submitter?.value;
        const headingId = String(form.get("heading_id") || "").trim();
        const path =
          action === "status"
            ? `/api/use/connections/${encodeURIComponent(headingId)}/status`
            : `/api/use/connections/${encodeURIComponent(headingId)}/pull`;
        const payload = await fetchJson(path, { method: action === "status" ? "GET" : "POST" });
        showSharedViewResult(action === "status" ? JSON.stringify(payload.data, null, 2) : payload.message);
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const pullAllButton = document.querySelector("#pull-all-connections");
  if (pullAllButton) {
    pullAllButton.addEventListener("click", async () => {
      try {
        const payload = await fetchJson("/api/use/connections/pull-all", { method: "POST" });
        showSharedViewResult(JSON.stringify(payload.data.results || [], null, 2));
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const statusAllButton = document.querySelector("#status-all-connections");
  if (statusAllButton) {
    statusAllButton.addEventListener("click", async () => {
      try {
        const payload = await fetchJson("/api/use/connections/status-all");
        showSharedViewResult(JSON.stringify(payload.data.statuses || [], null, 2));
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const questionConnectionForm = document.querySelector("#question-connection-form");
  if (questionConnectionForm) {
    questionConnectionForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const headingId = String(form.get("heading_id") || "").trim();
        const payload = await fetchJson(`/api/use/connections/${encodeURIComponent(headingId)}/ask`, {
          method: "POST",
          body: JSON.stringify({ question: form.get("question") }),
        });
        showSharedViewResult(payload.data?.text || payload.message);
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const consumerNoteForm = document.querySelector("#consumer-note-form");
  if (consumerNoteForm) {
    consumerNoteForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const headingId = String(form.get("heading_id") || "").trim();
        const payload = await fetchJson(`/api/use/connections/${encodeURIComponent(headingId)}/note`, {
          method: "POST",
          body: JSON.stringify({
            message: form.get("message"),
            confirmed: form.get("confirmed") === "on",
          }),
        });
        showSharedViewResult(payload.message);
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const providerInboxForm = document.querySelector("#provider-inbox-form");
  if (providerInboxForm) {
    providerInboxForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const payload = await fetchJson("/api/share/provider-inbox", {
          method: "POST",
          body: JSON.stringify({
            credential_id: form.get("credential_id"),
            hub_url: form.get("hub_url"),
            provider_id: form.get("provider_id"),
          }),
        });
        const target = document.querySelector("#provider-inbox-list");
        if (target) {
          target.innerHTML = renderProviderInbox(payload.data.interactions || []);
        }
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const loadPublishEventsButton = document.querySelector("#load-publish-events");
  if (loadPublishEventsButton) {
    loadPublishEventsButton.addEventListener("click", async () => {
      try {
        const payload = await fetchJson("/api/share/publish-events");
        const target = document.querySelector("#publish-events-list");
        if (target) {
          target.innerHTML = renderPublishEvents(payload.data.events || []);
        }
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }
}

function syncCreateShareTransport(form) {
  const transport = form.querySelector('select[name="transport"]')?.value || "http";
  const isGit = transport === "git";
  form.querySelectorAll("[data-transport-field]").forEach((field) => {
    field.hidden = field.dataset.transportField !== transport;
  });
  form.querySelectorAll('[name="credential_id"], [name="hub_url"], [name="git_url"]').forEach((field) => {
    field.required = false;
  });
  const credential = form.querySelector('[name="credential_id"]');
  const hubUrl = form.querySelector('[name="hub_url"]');
  const gitUrl = form.querySelector('[name="git_url"]');
  if (isGit) {
    if (gitUrl) {
      gitUrl.required = true;
    }
  } else {
    if (credential) {
      credential.required = true;
    }
    if (hubUrl) {
      hubUrl.required = true;
    }
  }
  const capability = form.querySelector('[name="capability"]');
  if (capability) {
    capability.disabled = isGit;
    if (isGit) {
      capability.value = "file-context";
    }
  }
  const capabilityField = form.querySelector("[data-capability-field]");
  if (capabilityField) {
    capabilityField.hidden = isGit;
  }
  const questionField = form.querySelector("[data-question-field]");
  if (questionField) {
    questionField.hidden = isGit;
  }
  const questionBaseUrl = form.querySelector('[name="question_base_url"]');
  if (questionBaseUrl && isGit) {
    questionBaseUrl.value = "";
  }
}

function attachCredentialSelectHandlers() {
  document.querySelectorAll(".credential-select").forEach((select) => {
    const update = () => fillCredentialDefaults(select);
    select.addEventListener("change", update);
    update();
  });
}

function fillCredentialDefaults(select) {
  const option = select.selectedOptions ? select.selectedOptions[0] : null;
  if (!option || !option.value) {
    return;
  }
  const form = select.closest("form");
  if (!form) {
    return;
  }
  const hubInput = form.querySelector('input[name="hub_url"]');
  if (hubInput && !hubInput.value && option.dataset.baseUrl) {
    hubInput.value = option.dataset.baseUrl;
  }
  const providerInput = form.querySelector('input[name="provider_id"]');
  if (providerInput && !providerInput.value && option.dataset.providerId) {
    providerInput.value = option.dataset.providerId;
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
