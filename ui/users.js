// Loads, creates, edits, and disables users through the API.
// An empty password on edit is omitted, so the stored password stays.
// The cursor for the current page is remembered here because the API
// only returns the cursor for the following page.

import { ApiError, createUser, listServerAudit, listUsers, patchUser } from "./api.js";
import { hasRole } from "./roles.js";
import { token } from "./session.js";

const ROLE_LABELS = {
  reader: "reader",
  writer: "writer",
  protected_writer: "protected writer",
  admin: "admin",
};

const message = document.querySelector("#users-message");
const creator = document.querySelector(".creator");
const formTitle = document.querySelector("#user-form-title");
const form = document.querySelector("#user-form");
const email = document.querySelector("#email");
const nameInput = document.querySelector("#name");
const password = document.querySelector("#password");
const disabledField = document.querySelector("#disabled-field");
const disabledInput = document.querySelector("#disabled");
const saveButton = document.querySelector("#save-user");
const cancelButton = document.querySelector("#cancel-edit");
const finder = document.querySelector("#user-search");
const queryInput = document.querySelector("#user-query");
const rows = document.querySelector("#user-rows");
const panel = document.querySelector(".panel");
const pager = document.querySelector(".pager");
const previousButton = document.querySelector("#previous-page");
const nextButton = document.querySelector("#next-page");
const pageStatus = document.querySelector("#page-status");
const auditList = document.querySelector("#server-audit-rows");

const cursors = [null];
let pageIndex = 0;
let editingId = null;
let activeSearch = "";
let requestId = 0;
const canManage = hasRole("admin");

form.addEventListener("submit", (event) => {
  event.preventDefault();
  save();
});

// The list is not filtered while typing. Enter and the button both submit.
finder.addEventListener("submit", (event) => {
  event.preventDefault();
  activeSearch = queryInput.value.trim();
  cursors.length = 1;
  cursors[0] = null;
  loadPage(0);
});

cancelButton.addEventListener("click", () => {
  setCreateMode();
  creator.open = false;
});

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
  showGate("Sign in to see users.", "login.html", "Sign in");
} else {
  if (canManage) {
    creator.hidden = false;
  }
  loadPage(0);
  loadServerAudit();
}

async function loadServerAudit() {
  try {
    const page = await listServerAudit(null);
    auditList.replaceChildren();
    for (const item of page.items) {
      const row = document.createElement("li");
      const actor = item.actor.kind + " " + item.actor.id;
      row.textContent = item.at + " — " + item.action + " — " + actor;
      auditList.append(row);
    }
  } catch (error) {
    if (!(error instanceof ApiError)) {
      throw error;
    }
  }
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
    const page = await listUsers(cursors[index], activeSearch);
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
    renderRows(items);
    previousButton.disabled = index === 0;
    nextButton.disabled = cursors[index + 1] === undefined;
    if (!nextButton.disabled) {
      pageStatus.textContent = "More accounts follow.";
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

function renderRows(items) {
  rows.replaceChildren();
  if (items.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    if (activeSearch === "") {
      cell.textContent = "No accounts on this page.";
    } else {
      cell.textContent = "No accounts match this search.";
    }
    row.append(cell);
    rows.append(row);
    return;
  }
  for (const user of items) {
    if (user === null || typeof user !== "object" || typeof user.id !== "number") {
      continue;
    }
    rows.append(renderRow(user));
  }
}

function renderRow(user) {
  const disabled = user.disabled_at !== null && user.disabled_at !== undefined;
  const row = document.createElement("tr");
  if (disabled) {
    row.className = "is-disabled";
  }
  row.append(cell("Name", text(user.name)));
  const emailCell = cell("Email", text(user.email));
  emailCell.classList.add("email");
  row.append(emailCell);
  row.append(rolesCell(user.roles));
  const status = cell("Status", disabled ? "Disabled" : "Enabled");
  status.classList.add("status");
  row.append(status);
  row.append(actionsCell(user, disabled));
  return row;
}

function cell(label, value) {
  const item = document.createElement("td");
  item.dataset.label = label;
  item.textContent = value;
  return item;
}

function rolesCell(userRoles) {
  const item = document.createElement("td");
  item.dataset.label = "Roles";
  const list = document.createElement("span");
  list.className = "roles";
  const values = Array.isArray(userRoles) ? userRoles : [];
  for (const role of values) {
    if (typeof role !== "string") {
      continue;
    }
    const chip = document.createElement("span");
    chip.className = role === "admin" ? "role is-admin" : "role";
    chip.textContent = ROLE_LABELS[role] === undefined ? role : ROLE_LABELS[role];
    list.append(chip);
  }
  item.append(list);
  return item;
}

function actionsCell(user, disabled) {
  const item = document.createElement("td");
  item.dataset.label = "Actions";
  item.className = "actions";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "button quiet";
  edit.textContent = "Edit";
  edit.disabled = !canManage;
  edit.addEventListener("click", () => {
    setEditMode(user);
  });
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "button quiet";
  toggle.disabled = !canManage;
  toggle.textContent = disabled ? "Enable" : "Disable";
  toggle.addEventListener("click", () => {
    setDisabled(user.id, !disabled);
  });
  item.append(edit, toggle);
  return item;
}

function setEditMode(user) {
  if (!canManage) {
    return;
  }
  editingId = user.id;
  creator.hidden = false;
  creator.open = true;
  formTitle.textContent = "Edit user";
  email.value = text(user.email);
  email.disabled = true;
  nameInput.value = text(user.name);
  password.value = "";
  disabledField.hidden = false;
  disabledInput.checked = user.disabled_at !== null && user.disabled_at !== undefined;
  const selected = new Set(Array.isArray(user.roles) ? user.roles : []);
  for (const box of form.querySelectorAll('input[name="roles"]')) {
    box.checked = selected.has(box.value);
  }
  saveButton.textContent = "Save";
  cancelButton.hidden = false;
  clearMessage();
}

function setCreateMode() {
  editingId = null;
  form.reset();
  email.disabled = false;
  disabledField.hidden = true;
  disabledInput.checked = false;
  formTitle.textContent = "New user";
  saveButton.textContent = "Create";
  cancelButton.hidden = true;
}

async function save() {
  if (!canManage) {
    return;
  }
  clearMessage();
  saveButton.disabled = true;
  const roles = selectedRoles();
  try {
    if (editingId === null) {
      const body = { email: email.value, name: nameInput.value, roles };
      if (password.value !== "") {
        body.password = password.value;
      }
      await createUser(body);
      setCreateMode();
      creator.open = false;
      await loadPage(pageIndex);
      message.textContent = "User created.";
      return;
    } else {
      const body = {
        name: nameInput.value,
        roles,
        disabled: disabledInput.checked,
      };
      if (password.value !== "") {
        body.password = password.value;
      }
      await patchUser(editingId, body);
      setCreateMode();
      creator.open = false;
      await loadPage(pageIndex);
      message.textContent = "User saved.";
    }
  } catch (error) {
    report(error);
  } finally {
    saveButton.disabled = false;
  }
}

async function setDisabled(userId, disabled) {
  if (!canManage) {
    return;
  }
  clearMessage();
  try {
    await patchUser(userId, { disabled });
    await loadPage(pageIndex);
    message.textContent = disabled ? "User disabled." : "User enabled.";
  } catch (error) {
    report(error);
  }
}

function selectedRoles() {
  const values = [];
  for (const box of form.querySelectorAll('input[name="roles"]')) {
    if (box.checked) {
      values.push(box.value);
    }
  }
  return values;
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
