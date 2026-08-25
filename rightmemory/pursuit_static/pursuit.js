const state = {
  token: null,
  workspace: null,
  tasks: [],
  reconciliations: [],
  taskRevision: null,
  selectedId: null,
  operations: [],
  baseRevision: null,
  preview: null,
};

function bootstrapToken() {
  const url = new URL(window.location.href);
  const queryToken = url.searchParams.get("token");
  if (queryToken) {
    localStorage.setItem("rightmemory-pursuit-token", queryToken);
    url.searchParams.delete("token");
    history.replaceState({}, "", url.pathname + url.search + url.hash);
  }
  state.token = queryToken || localStorage.getItem("rightmemory-pursuit-token");
  if (!state.token) {
    throw new Error("Missing Pursuit Studio token. Launch through `rightmemory pursuit studio`.");
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${state.token}`,
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    const detail = payload.detail || payload.message || `Request failed: ${response.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload.data;
}

function setMessage(text, isError = false) {
  const target = document.querySelector("#message");
  target.textContent = text || "";
  target.style.color = isError ? "#a73737" : "#8a4b12";
}

async function loadWorkspace({ preserveSelection = true } = {}) {
  const selected = preserveSelection ? state.selectedId : null;
  const data = await api("/api/workspace");
  state.workspace = data.workspace;
  state.tasks = data.tasks || [];
  state.reconciliations = data.reconciliations || [];
  state.taskRevision = data.task_revision;
  state.baseRevision = data.workspace.revision;
  state.operations = [];
  state.preview = null;
  state.selectedId = selected && findNode(selected) ? selected : null;
  renderAll();
}

function renderAll() {
  document.querySelector("#root-label").textContent = `${state.workspace.nodes.length} Pursuits · revision ${state.workspace.revision.slice(0, 10)}`;
  renderMap();
  renderEditor();
  renderReconciliations();
  syncToolbar();
}

function syncToolbar() {
  const dirty = state.operations.length > 0;
  document.querySelector("#apply").disabled = !dirty;
  document.querySelector("#preview").disabled = !dirty;
  document.querySelector("#discard").disabled = !dirty;
  document.querySelector("#apply").textContent = dirty ? `Apply Changes (${state.operations.length})` : "Apply Changes";
}

function findNode(id) {
  return (state.workspace?.nodes || []).find((node) => node.id === id) || null;
}

function nodeMap() {
  return new Map((state.workspace?.nodes || []).map((node) => [node.id, node]));
}

function renderMap() {
  const map = document.querySelector("#map");
  const byId = nodeMap();
  const query = document.querySelector("#search").value.trim().toLowerCase();
  const visible = new Set();
  if (query) {
    for (const node of byId.values()) {
      const haystack = `${node.id} ${node.title} ${node.objective} ${node.state}`.toLowerCase();
      if (haystack.includes(query)) {
        let current = node;
        while (current) {
          visible.add(current.id);
          current = current.parent_id ? byId.get(current.parent_id) : null;
        }
      }
    }
  }
  const roots = (state.workspace.roots || []).filter((id) => byId.has(id));
  if (!roots.length) {
    map.innerHTML = '<p>No live Pursuits yet.</p>';
    return;
  }
  map.innerHTML = `<div class="map-root">${roots.map((id) => renderBranch(id, byId, visible, query)).join("")}</div>`;
  map.querySelectorAll(".map-node[data-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedId = button.dataset.id;
      renderMap();
      renderEditor();
    });
  });
}

function renderBranch(id, byId, visible, query) {
  const node = byId.get(id);
  if (!node || (query && !visible.has(id))) {
    return "";
  }
  const children = (node.children || []).filter((child) => byId.has(child));
  const classes = ["map-node"];
  if (state.selectedId === id) classes.push("selected");
  if (node.focused) classes.push("focused");
  if (node.parked) classes.push("parked");
  const badges = [];
  if (node.focused) badges.push('<span class="badge focus">focus</span>');
  if (node.parked) badges.push('<span class="badge">parked</span>');
  if (node.backing) badges.push('<span class="badge file">F#</span>');
  if ((node.tasks || []).length) badges.push(`<span class="badge task">${node.tasks.length} task${node.tasks.length === 1 ? "" : "s"}</span>`);
  return `
    <div class="map-branch">
      <div class="map-node-wrap">
        <button class="${classes.join(" ")}" type="button" data-id="${escapeHtml(id)}">
          <strong>${escapeHtml(node.title)}</strong>
          <small>${escapeHtml(node.state || node.objective || "No current state")}</small>
          <span class="node-meta">${badges.join("")}</span>
        </button>
      </div>
      ${children.length ? `<div class="map-children">${children.map((child) => renderBranch(child, byId, visible, query)).join("")}</div>` : ""}
    </div>
  `;
}

function renderEditor() {
  const empty = document.querySelector("#empty-selection");
  const section = document.querySelector("#editor-section");
  const node = state.selectedId ? findNode(state.selectedId) : null;
  empty.hidden = Boolean(node);
  section.hidden = !node;
  if (!node) return;

  document.querySelector("#editor-heading").textContent = node.title;
  document.querySelector("#editor-id").textContent = node.id;
  document.querySelector("#editor-badges").innerHTML = [
    node.focused ? '<span class="badge focus">focus</span>' : "",
    node.parked ? '<span class="badge">parked</span>' : "",
    node.backing ? '<span class="badge file">F# backing</span>' : "",
  ].join("");
  const form = document.querySelector("#editor-form");
  form.elements.title.value = node.title || "";
  form.elements.objective.value = node.objective || "";
  form.elements.state.value = node.state || "";
  form.elements.next.value = (node.next || []).map((item) => `${item.kind}: ${item.text}`).join("\n");
  form.elements.done_when.value = node.done_when || "";
  form.elements.status.value = node.status || "active";
  form.elements.edges.value = (node.edges || []).map((edge) => `${edge.type}:${edge.target}`).join("\n");

  document.querySelector("#toggle-focus").textContent = node.focused ? "Remove from Focus" : "Add to Focus";
  document.querySelector("#toggle-backing").textContent = node.backing ? "Inline F# Children" : "Split Children to F#";

  const parent = document.querySelector("#move-parent");
  const options = ['<option value="">Top level</option>'];
  for (const candidate of state.workspace.nodes) {
    if (candidate.id !== node.id) {
      options.push(`<option value="${escapeHtml(candidate.id)}"${candidate.id === node.parent_id ? " selected" : ""}>${escapeHtml(candidate.title)} (${escapeHtml(candidate.id)})</option>`);
    }
  }
  parent.innerHTML = options.join("");
  renderTasks(node);
}

function renderTasks(node) {
  const target = document.querySelector("#task-list");
  const tasks = state.tasks.filter((task) => (task.pursuit_ids || []).includes(node.id));
  if (!tasks.length) {
    target.innerHTML = "<p>No linked tasks.</p>";
    return;
  }
  target.innerHTML = tasks.map((task) => `
    <article class="mini-card">
      <strong>${escapeHtml(task.title)}</strong>
      <small>${escapeHtml(task.task_id)} · ${escapeHtml(task.status)}${task.thread_id ? ` · thread ${escapeHtml(task.thread_id)}` : ""}</small>
      ${task.action ? `<p>${escapeHtml(task.action)}</p>` : ""}
      ${task.result ? `<details><summary>Result</summary><p>${escapeHtml(task.result)}</p></details>` : ""}
      <div class="button-row">
        ${task.status === "planned" ? `<button class="run-task" type="button" data-task-id="${escapeHtml(task.task_id)}">Run</button>` : ""}
        <button class="unlink-task" type="button" data-task-id="${escapeHtml(task.task_id)}">Unlink</button>
      </div>
    </article>
  `).join("");
  target.querySelectorAll(".run-task").forEach((button) => button.addEventListener("click", () => runTask(button.dataset.taskId)));
  target.querySelectorAll(".unlink-task").forEach((button) => button.addEventListener("click", () => unlinkTask(button.dataset.taskId, node.id)));
}

function renderReconciliations() {
  const target = document.querySelector("#reconciliation-list");
  const records = state.reconciliations.filter((record) => record.status === "pending");
  if (!records.length) {
    target.innerHTML = "<p>No pending task reconciliation.</p>";
    return;
  }
  target.innerHTML = records.map((record) => `
    <article class="mini-card">
      <strong>${escapeHtml(record.summary)}</strong>
      <small>${escapeHtml(record.reconciliation_id)} · task ${escapeHtml(record.task_id)}</small>
      <p>${escapeHtml(JSON.stringify(record.operations, null, 2))}</p>
      <div class="button-row">
        <button class="apply-reconciliation primary" type="button" data-id="${escapeHtml(record.reconciliation_id)}">Apply</button>
        <button class="dismiss-reconciliation" type="button" data-id="${escapeHtml(record.reconciliation_id)}">Dismiss</button>
      </div>
    </article>
  `).join("");
  target.querySelectorAll(".apply-reconciliation").forEach((button) => button.addEventListener("click", () => applyReconciliation(button.dataset.id)));
  target.querySelectorAll(".dismiss-reconciliation").forEach((button) => button.addEventListener("click", () => dismissReconciliation(button.dataset.id)));
}

async function stageOperation(operation) {
  const previousOperations = [...state.operations];
  state.operations.push(operation);
  try {
    const preview = await api("/api/preview", {
      method: "POST",
      body: JSON.stringify({ operations: state.operations, revision: state.baseRevision }),
    });
    state.preview = preview;
    state.workspace = preview.snapshot;
    if (state.selectedId && !findNode(state.selectedId)) state.selectedId = null;
    setMessage(`${state.operations.length} staged operation${state.operations.length === 1 ? "" : "s"}.`);
    renderAll();
  } catch (error) {
    state.operations = previousOperations;
    setMessage(error.message, true);
  }
}

async function applyDraft() {
  if (!state.operations.length) return;
  try {
    const data = await api("/api/apply", {
      method: "POST",
      body: JSON.stringify({
        operations: state.operations,
        revision: state.baseRevision,
        commit: document.querySelector("#commit").checked,
      }),
    });
    setMessage(data.commit ? `Applied and committed ${data.commit.slice(0, 12)}.` : "Applied Pursuit changes.");
    await loadWorkspace();
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function showDiff() {
  if (!state.operations.length) return;
  try {
    if (!state.preview) {
      state.preview = await api("/api/preview", {
        method: "POST",
        body: JSON.stringify({ operations: state.operations, revision: state.baseRevision }),
      });
    }
    document.querySelector("#diff-output").textContent = state.preview.diff || "No Markdown changes.";
    document.querySelector("#diff-dialog").showModal();
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function discardDraft() {
  state.selectedId = state.selectedId;
  await loadWorkspace();
  setMessage("Discarded staged changes.");
}

function requireCleanDraft() {
  if (state.operations.length) {
    setMessage("Apply or discard staged Pursuit edits before changing task links.", true);
    return false;
  }
  return true;
}

async function runTask(taskId, project = null) {
  if (!requireCleanDraft()) return;
  setMessage("Running Codex task. This request remains open until the task turn completes.");
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/run`, {
      method: "POST",
      body: JSON.stringify(project ? { project } : {}),
    });
    await loadWorkspace();
    setMessage("Codex task completed and its result was recorded.");
  } catch (error) {
    setMessage(error.message, true);
    await loadWorkspace();
  }
}

async function unlinkTask(taskId, pursuitId) {
  if (!requireCleanDraft()) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/unlink`, {
      method: "POST",
      body: JSON.stringify({ pursuit_id: pursuitId }),
    });
    await loadWorkspace();
    setMessage("Task link removed.");
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function applyReconciliation(id) {
  if (!requireCleanDraft()) return;
  try {
    await api(`/api/reconciliations/${encodeURIComponent(id)}/apply`, {
      method: "POST",
      body: JSON.stringify({ commit: document.querySelector("#commit").checked }),
    });
    await loadWorkspace();
    setMessage("Task result reconciled into Pursuit.");
  } catch (error) {
    setMessage(error.message, true);
  }
}

async function dismissReconciliation(id) {
  if (!requireCleanDraft()) return;
  try {
    await api(`/api/reconciliations/${encodeURIComponent(id)}/dismiss`, { method: "POST", body: "{}" });
    await loadWorkspace();
    setMessage("Reconciliation dismissed.");
  } catch (error) {
    setMessage(error.message, true);
  }
}

function openNodeDialog(parentId, title) {
  const form = document.querySelector("#node-form");
  form.reset();
  form.elements.parent_id.value = parentId || "";
  document.querySelector("#node-dialog-title").textContent = title;
  document.querySelector("#node-dialog").showModal();
}

function parseLines(value) {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
}

document.querySelector("#search").addEventListener("input", renderMap);
document.querySelector("#apply").addEventListener("click", applyDraft);
document.querySelector("#preview").addEventListener("click", showDiff);
document.querySelector("#discard").addEventListener("click", discardDraft);
document.querySelector("#close-diff").addEventListener("click", () => document.querySelector("#diff-dialog").close());
document.querySelector("#add-root").addEventListener("click", () => openNodeDialog(null, "New Top-Level Pursuit"));
document.querySelector("#cancel-node").addEventListener("click", () => document.querySelector("#node-dialog").close());

document.querySelector("#editor-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const node = findNode(state.selectedId);
  if (!node) return;
  const form = new FormData(event.currentTarget);
  await stageOperation({
    op: "update",
    id: node.id,
    title: form.get("title"),
    objective: form.get("objective"),
    state: form.get("state"),
    next: parseLines(String(form.get("next") || "")),
    done_when: form.get("done_when"),
    status: form.get("status"),
    edges: parseLines(String(form.get("edges") || "")),
  });
});

document.querySelector("#toggle-focus").addEventListener("click", async () => {
  const node = findNode(state.selectedId);
  if (!node) return;
  const ids = [...state.workspace.focus_ids];
  const index = ids.indexOf(node.id);
  if (index >= 0) ids.splice(index, 1); else ids.push(node.id);
  await stageOperation({ op: "set_focus", ids });
});

document.querySelector("#toggle-backing").addEventListener("click", async () => {
  const node = findNode(state.selectedId);
  if (!node) return;
  await stageOperation({ op: node.backing ? "inline_file" : "split_file", id: node.id });
});

document.querySelector("#move-node").addEventListener("click", async () => {
  const node = findNode(state.selectedId);
  if (!node) return;
  const rawIndex = document.querySelector("#move-index").value;
  const operation = {
    op: "move",
    id: node.id,
    parent_id: document.querySelector("#move-parent").value || null,
  };
  if (rawIndex !== "") operation.index = Number(rawIndex);
  await stageOperation(operation);
});

document.querySelector("#add-child").addEventListener("click", () => {
  const node = findNode(state.selectedId);
  if (node) openNodeDialog(node.id, `New Child of ${node.title}`);
});
document.querySelector("#add-sibling").addEventListener("click", () => {
  const node = findNode(state.selectedId);
  if (node) openNodeDialog(node.parent_id, `New Sibling of ${node.title}`);
});

document.querySelector("#delete-node").addEventListener("click", async () => {
  const node = findNode(state.selectedId);
  if (!node) return;
  if ((node.tasks || []).length) {
    setMessage("Unlink this Pursuit from its tasks before deleting it.", true);
    return;
  }
  if (!window.confirm(`Remove ${node.title} and all of its children from live Pursuit? Git history remains available.`)) return;
  await stageOperation({ op: "delete", id: node.id, cascade: true });
});

document.querySelector("#node-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const next = String(form.get("next") || "").trim();
  document.querySelector("#node-dialog").close();
  await stageOperation({
    op: "create",
    id: form.get("id"),
    title: form.get("title"),
    parent_id: form.get("parent_id") || null,
    objective: form.get("objective"),
    next: next ? [next] : [],
  });
  state.selectedId = String(form.get("id"));
  renderAll();
});

document.querySelector("#link-task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireCleanDraft()) return;
  const node = findNode(state.selectedId);
  if (!node) return;
  const form = new FormData(event.currentTarget);
  try {
    await api("/api/tasks/link", {
      method: "POST",
      body: JSON.stringify({
        pursuit_ids: [node.id],
        provider: "codex",
        thread_id: form.get("thread_id"),
        title: form.get("title"),
        project: form.get("project"),
        task_revision: state.taskRevision,
      }),
    });
    event.currentTarget.reset();
    await loadWorkspace();
    setMessage("Codex thread linked.");
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.querySelector("#plan-task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!requireCleanDraft()) return;
  const node = findNode(state.selectedId);
  if (!node) return;
  const form = new FormData(event.currentTarget);
  const mode = event.submitter?.value || "plan";
  try {
    const task = await api("/api/tasks/plan", {
      method: "POST",
      body: JSON.stringify({
        pursuit_id: node.id,
        action: form.get("action"),
        title: form.get("title"),
        project: form.get("project"),
      }),
    });
    if (mode === "run") {
      await runTask(task.task_id, String(form.get("project") || "") || null);
    } else {
      await loadWorkspace();
      setMessage("Planned task created and linked.");
    }
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.querySelector("#undo").addEventListener("click", async () => {
  if (!requireCleanDraft()) return;
  try {
    await api("/api/undo", { method: "POST", body: JSON.stringify({ commit: document.querySelector("#commit").checked }) });
    await loadWorkspace();
    setMessage("Undid the latest Pursuit Studio edit.");
  } catch (error) {
    setMessage(error.message, true);
  }
});

document.querySelector("#redo").addEventListener("click", async () => {
  if (!requireCleanDraft()) return;
  try {
    await api("/api/redo", { method: "POST", body: JSON.stringify({ commit: document.querySelector("#commit").checked }) });
    await loadWorkspace();
    setMessage("Redid the latest Pursuit Studio edit.");
  } catch (error) {
    setMessage(error.message, true);
  }
});

(async () => {
  try {
    bootstrapToken();
    await loadWorkspace({ preserveSelection: false });
  } catch (error) {
    setMessage(error.message, true);
  }
})();
