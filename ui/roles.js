// Reads the roles claim for display. The API ignores this claim and loads
// the roles from the user row, so a missing or stale claim only changes
// what the page shows.

import { token } from "./session.js";

export function roles() {
  const claim = claims().roles;
  if (!Array.isArray(claim)) {
    return [];
  }
  return claim.filter((role) => typeof role === "string");
}

export function subject() {
  const sub = claims().sub;
  return typeof sub === "string" ? sub : "";
}

export function hasRole(role) {
  const held = roles();
  if (held.includes("admin")) {
    return true;
  }
  return held.includes(role);
}

function claims() {
  const parts = token().split(".");
  if (parts.length < 2) {
    return {};
  }
  try {
    const json = JSON.parse(decodeBase64Url(parts[1]));
    if (json !== null && typeof json === "object") {
      return json;
    }
  } catch (_error) {
    return {};
  }
  return {};
}

function decodeBase64Url(segment) {
  const padded = segment + "=".repeat((4 - (segment.length % 4)) % 4);
  const base64 = padded.replace(/-/g, "+").replace(/_/g, "/");
  return atob(base64);
}
