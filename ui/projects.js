// Loads and creates projects, and edits environments, through the API.
// A project has no update route. An environment can change name and protected.
// The cursor for the current page is remembered here because the API
// only returns the cursor for the following page.

import { ApiError, createEnvironment, createProject, listEnvironments, listProjects, patchEnvironment } from "./api.js";
import { hasRole } from "./roles.js";
import { token } from "./session.js";

const message = document.querySelector("#projects-message");
const creator = document.querySelector(".creator");
const formTitle = document.querySelector("#project-form-title");
const form = document.querySelector("#project-form");
const keyInput = document.querySelector("#key");
const nameInput = document.querySelector("#name");
const environmentHint = document.querySelector("#environment-hint");
const saveButton = document.querySelector("#save-project");
const cancelButton = document.querySelector("#cancel-edit");
const environmentRows = document.querySelector("#environment-rows");
const addEnvironmentButton = document.querySelector("#add-environment");
const finder = document.querySelector("#project-search");
const queryInput = document.querySelector("#project-query");
const rows = document.querySelector("#project-rows");
const panel = document.querySelector(".panel");
const pager = document.querySelector(".pager");
const previousButton = document.querySelector("#previous-page");
const nextButton = document.querySelector("#next-page");
const pageStatus = document.querySelector("#page-status");

const cursors = [null];
let pageIndex = 0;
let activeSearch = "";
let requestId = 0;
const canManage = hasRole("admin");
const createHint = environmentHint.textContent;

let editingKey = null;
let environmentSeq = 0;

form.addEventListener("submit", (event) => {
  event.preventDefault();
  save();
});

addEnvironmentButton.addEventListener("click", () => {
  addEnvironmentRow();
});

cancelButton.addEventListener("click", () => {
  setCreateMode();
  creator.open = false;
});

// The list is not filtered while typing. Enter and the button both submit.
finder.addEventListener("submit", (event) => {
  event.preventDefault();
  activeSearch = queryInput.value.trim();
  cursors.length = 1;
  cursors[0] = null;
  loadPage(0);
});

addEnvironmentRow();

previousButton.addEventListener("click", () => {
  if (pageIndex > 0) {
    loadPage(pageIndex - 1);
  }
});

nextButton.addEventListener("click", () => {
  if (cursors[pageIndex + 1] !== undefined) {
    loadPage(pageIndex + 1);
  }
});

if (token() === "") {
  showGate("Sign in to see projects.", "login.html", "Sign in");
} else {
  if (canManage) {
    creator.hidden = false;
  }
  loadPage(0);
}

function showGate(text, href, linkText) {
  message.classList.add("is-error");
  message.textContent = text;
  if (href) {
    message.append(" ");
    const link = document.createElement("a");
    link.href = href;
    link.textContent = linkText;
    message.append(link);
  }
  creator.hidden = true;
  finder.hidden = true;
  panel.hidden = true;
  pager.hidden = true;
}

async function loadPage(index) {
  const id = requestId + 1;
  requestId = id;
  pageIndex = index;
  try {
    const page = await listProjects(cursors[index], activeSearch);
    if (id !== requestId) {
      return;
    }
    const items = page !== null && Array.isArray(page.items) ? page.items : [];
    const next = page !== null ? page.next_cursor : null;
    if (typeof next === "string" && next !== "") {
      cursors[index + 1] = next;
    } else {
      cursors.length = index + 1;
    }
    await renderRows(items, id);
    previousButton.disabled = index === 0;
    nextButton.disabled = cursors[index + 1] === undefined;
    if (!nextButton.disabled) {
      pageStatus.textContent = "More projects follow.";
    } else if (index > 0) {
      pageStatus.textContent = "Last page.";
    } else {
      pageStatus.textContent = "";
    }
  } catch (error) {
    if (id !== requestId) {
      return;
    }
    report(error);
  }
}

async function renderRows(items, id) {
  if (id !== requestId) {
    return;
  }
  rows.replaceChildren();
  if (items.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    if (activeSearch === "") {
      cell.textContent = "No projects on this page.";
    } else {
      cell.textContent = "No projects match this search.";
    }
    row.append(cell);
    rows.append(row);
    return;
  }
  for (const project of items) {
    if (project === null || typeof project !== "object" || typeof project.key !== "string") {
      continue;
    }
    const row = await renderRow(project);
    if (id !== requestId) {
      return;
    }
    rows.append(row);
  }
}

async function renderRow(project) {
  const row = document.createElement("tr");
  const keyCell = cell("Key", project.key);
  keyCell.classList.add("key");
  row.append(keyCell);
  row.append(cell("Name", text(project.name)));
  const environments = cell("Environments", "");
  row.append(environments);
  let listed = [];
  try {
    listed = await collectPages((cursor) => listEnvironments(project.key, cursor));
    environments.replaceChildren(environmentList(listed));
  } catch (error) {
    environments.textContent = error instanceof ApiError ? error.message : "The request failed.";
  }
  row.append(actionsCell(project, listed));
  return row;
}

function actionsCell(project, environments) {
  const item = document.createElement("td");
  item.dataset.label = "Actions";
  item.className = "actions";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "button quiet";
  edit.textContent = "Edit";
  edit.disabled = !canManage;
  edit.addEventListener("click", () => {
    setEditMode(project, environments);
  });
  const flags = document.createElement("a");
  flags.className = "button quiet";
  flags.href = "flags.html?project=" + encodeURIComponent(project.key);
  flags.textContent = "Flags";
  item.append(edit, flags);
  return item;
}

function setEditMode(project, environments) {
  if (!canManage) {
    return;
  }
  editingKey = project.key;
  creator.hidden = false;
  creator.open = true;
  formTitle.textContent = "Edit environments";
  keyInput.value = project.key;
  keyInput.disabled = true;
  nameInput.value = text(project.name);
  nameInput.disabled = true;
  environmentHint.textContent = "The project stays as it is. A saved environment can change its name and whether it is protected. A new row is created after those.";
  environmentRows.replaceChildren();
  const values = Array.isArray(environments) ? environments : [];
  let filled = 0;
  for (const environment of values) {
    if (environment === null || typeof environment !== "object" || typeof environment.key !== "string") {
      continue;
    }
    addEnvironmentRow(environment);
    filled += 1;
  }
  if (filled === 0) {
    addEnvironmentRow();
  }
  saveButton.textContent = "Save";
  cancelButton.hidden = false;
  clearMessage();
}

function setCreateMode() {
  editingKey = null;
  form.reset();
  keyInput.disabled = false;
  nameInput.disabled = false;
  formTitle.textContent = "New project";
  environmentHint.textContent = createHint;
  saveButton.textContent = "Create";
  cancelButton.hidden = true;
  resetEnvironmentRows();
}

function environmentList(items) {
  const list = document.createElement("span");
  list.className = "roles";
  const values = Array.isArray(items) ? items : [];
  if (values.length === 0) {
    list.textContent = "None";
    return list;
  }
  for (const environment of values) {
    if (environment === null || typeof environment !== "object") {
      continue;
    }
    const chip = document.createElement("span");
    chip.className = "role";
    chip.textContent = text(environment.name) === "" ? text(environment.key) : text(environment.name);
    if (environment.protected === true) {
      chip.classList.add("is-protected");
    }
    list.append(chip);
  }
  if (list.childElementCount === 0) {
    list.textContent = "None";
  }
  return list;
}

async function collectPages(load) {
  const items = [];
  const seen = new Set();
  let cursor = null;
  for (let page = 0; page < 20; page += 1) {
    const body = await load(cursor);
    const batch = body !== null && Array.isArray(body.items) ? body.items : [];
    items.push(...batch);
    const next = body !== null ? body.next_cursor : null;
    if (typeof next !== "string" || next === "" || seen.has(next)) {
      return items;
    }
    seen.add(next);
    cursor = next;
  }
  return items;
}

function cell(label, value) {
  const item = document.createElement("td");
  item.dataset.label = label;
  item.textContent = value;
  return item;
}

async function save() {
  if (!canManage) {
    return;
  }
  clearMessage();
  saveButton.disabled = true;
  const environments = environmentCommands();
  if (editingKey !== null) {
    await saveEnvironments(environments);
    return;
  }
  let projectKey = null;
  try {
    const created = await createProject({ key: keyInput.value, name: nameInput.value });
    projectKey = created !== null && typeof created.key === "string" ? created.key : keyInput.value;
    for (const environment of environments) {
      await createEnvironment(projectKey, environmentBody(environment));
    }
    setCreateMode();
    creator.open = false;
    await loadPage(pageIndex);
    message.textContent = "Project created.";
  } catch (error) {
    if (projectKey !== null) {
      await loadPage(pageIndex);
    }
    report(error);
    if (projectKey !== null) {
      message.textContent = "The project was created. " + message.textContent;
    }
  } finally {
    saveButton.disabled = false;
  }
}

async function saveEnvironments(environments) {
  let saved = false;
  try {
    for (const environment of environments) {
      if (environment.existing) {
        await patchEnvironment(editingKey, environment.key, {
          name: environment.name,
          protected: environment.protected,
        });
      } else {
        await createEnvironment(editingKey, environmentBody(environment));
      }
      saved = true;
    }
    setCreateMode();
    creator.open = false;
    await loadPage(pageIndex);
    message.textContent = saved ? "Environments saved." : "No environment to save.";
  } catch (error) {
    if (saved) {
      await loadPage(pageIndex);
    }
    report(error);
    if (saved) {
      message.textContent = "Some environments were saved. " + message.textContent;
    }
  } finally {
    saveButton.disabled = false;
  }
}

function environmentBody(environment) {
  return {
    key: environment.key,
    name: environment.name,
    protected: environment.protected,
  };
}

function environmentCommands() {
  const commands = [];
  for (const row of environmentRows.querySelectorAll(".env-row")) {
    const key = row.querySelector(".env-key").value.trim();
    const name = row.querySelector(".env-name").value.trim();
    if (key === "" && name === "") {
      continue;
    }
    commands.push({
      key,
      name,
      protected: row.querySelector(".env-protected-input").checked,
      existing: row.dataset.existing === "true",
    });
  }
  return commands;
}

function addEnvironmentRow(environment) {
  environmentSeq += 1;
  const keyId = "env-key-" + String(environmentSeq);
  const nameId = "env-name-" + String(environmentSeq);
  const protectedId = "env-protected-" + String(environmentSeq);
  const row = document.createElement("div");
  row.className = "env-row";
  const existing = environment !== undefined;
  if (existing) {
    row.dataset.existing = "true";
  }
  row.append(labeledInput(keyId, "Key", "env-key"));
  row.append(labeledInput(nameId, "Name", "env-name"));
  row.append(protectedField(protectedId));
  if (existing) {
    const key = row.querySelector(".env-key");
    key.value = text(environment.key);
    key.disabled = true;
    row.querySelector(".env-name").value = text(environment.name);
    row.querySelector(".env-protected-input").checked = environment.protected === true;
  } else {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button quiet";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => {
      row.remove();
    });
    row.append(remove);
  }
  environmentRows.append(row);
}

function protectedField(id) {
  const label = document.createElement("label");
  label.className = "env-protected";
  label.htmlFor = id;
  const input = document.createElement("input");
  input.id = id;
  input.className = "env-protected-input";
  input.type = "checkbox";
  label.append(input, document.createTextNode("Protected"));
  return label;
}

function labeledInput(id, label, className) {
  const wrap = document.createElement("div");
  const caption = document.createElement("label");
  caption.htmlFor = id;
  caption.textContent = label;
  const input = document.createElement("input");
  input.id = id;
  input.className = className;
  input.type = "text";
  input.autocomplete = "off";
  if (className === "env-key") {
    input.spellcheck = false;
  }
  wrap.append(caption, input);
  return wrap;
}

function resetEnvironmentRows() {
  environmentRows.replaceChildren();
  addEnvironmentRow();
}

function report(error) {
  message.classList.add("is-error");
  if (error instanceof ApiError) {
    message.textContent = error.message;
  } else {
    message.textContent = "The request failed.";
  }
}

function clearMessage() {
  message.textContent = "";
  message.classList.remove("is-error");
}

function text(value) {
  return typeof value === "string" ? value : "";
}
