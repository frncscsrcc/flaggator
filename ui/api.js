// One fetch path for the console. The token, when present, is sent as a
// bearer header. Failures become ApiError so a page can show the message
// as text and never has to parse the JSON error document itself.

import { saveToken, token } from "./session.js";

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export async function request(method, path, body) {
  const headers = { Accept: "application/json" };
  const current = token();
  if (current !== "") {
    headers.Authorization = "Bearer " + current;
  }
  const init = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (_error) {
    throw new ApiError(0, "network", "The service could not be reached.", []);
  }
  if (response.status === 204) {
    return null;
  }
  const payload = await _json(response);
  if (!response.ok) {
    throw _apiError(response.status, payload);
  }
  return payload;
}

export function listUsers(cursor, search) {
  const params = new URLSearchParams();
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    params.set("cursor", cursor);
  }
  if (typeof search === "string" && search !== "") {
    params.set("search", search);
  }
  const query = params.toString();
  if (query === "") {
    return request("GET", "/v1/users");
  }
  return request("GET", "/v1/users?" + query);
}

export function listServerAudit(cursor) {
  const params = new URLSearchParams();
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    params.set("cursor", cursor);
  }
  const query = params.toString();
  if (query === "") {
    return request("GET", "/v1/audit");
  }
  return request("GET", "/v1/audit?" + query);
}

export function getUser(userId) {
  return request("GET", "/v1/users/" + encodeURIComponent(String(userId)));
}

export function createUser(body) {
  return request("POST", "/v1/users", body);
}

export function patchUser(userId, body) {
  return request("PATCH", "/v1/users/" + encodeURIComponent(String(userId)), body);
}

export function listProjects(cursor, search) {
  const params = new URLSearchParams();
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    params.set("cursor", cursor);
  }
  if (typeof search === "string" && search !== "") {
    params.set("search", search);
  }
  const query = params.toString();
  if (query === "") {
    return request("GET", "/v1/projects");
  }
  return request("GET", "/v1/projects?" + query);
}

export function createProject(body) {
  return request("POST", "/v1/projects", body);
}

export function createEnvironment(projectKey, body) {
  return request(
    "POST",
    "/v1/projects/" + encodeURIComponent(projectKey) + "/environments",
    body,
  );
}

export function patchEnvironment(projectKey, environmentKey, body) {
  return request(
    "PATCH",
    "/v1/projects/" + encodeURIComponent(projectKey) + "/environments/" + encodeURIComponent(environmentKey),
    body,
  );
}

export function listEnvironments(projectKey, cursor) {
  return request("GET", _nested(projectKey, "environments", cursor));
}

export function listFlags(projectKey, cursor) {
  return request("GET", _nested(projectKey, "flags", cursor));
}

export function logout() {
  return request("POST", "/v1/auth/logout");
}

export async function login(email, password) {
  const payload = await request("POST", "/v1/auth/login", { email, password });
  const accessToken = payload === null ? undefined : payload.access_token;
  if (typeof accessToken !== "string" || accessToken === "") {
    throw new ApiError(200, "invalid_response", "The service did not return an access token.", []);
  }
  saveToken(accessToken);
  return payload;
}

function _nested(projectKey, collection, cursor) {
  const params = new URLSearchParams();
  params.set("limit", "200");
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    params.set("cursor", cursor);
  }
  return "/v1/projects/" + encodeURIComponent(projectKey) + "/" + collection + "?" + params.toString();
}

async function _json(response) {
  const text = await response.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch (_error) {
    throw new ApiError(
      response.status,
      "invalid_response",
      "The service returned a response that is not JSON.",
      [],
    );
  }
}

function _apiError(status, payload) {
  const error = payload !== null && typeof payload.error === "object" && payload.error !== null
    ? payload.error
    : {};
  const message = typeof error.message === "string" && error.message !== ""
    ? error.message
    : "The request failed.";
  const code = typeof error.code === "string" && error.code !== ""
    ? error.code
    : "unknown";
  const details = Array.isArray(error.details) ? error.details : [];
  return new ApiError(status, code, message, details);
}
