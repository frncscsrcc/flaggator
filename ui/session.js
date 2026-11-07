// The access token is kept for this tab. Each console page is its own
// document, so a variable alone would disappear on the way from sign-in
// to the next page. sessionStorage clears when the tab closes.
// localStorage is not used.

const STORAGE_KEY = "flaggator.access_token";

let accessToken = readStored();

export function saveToken(value) {
  accessToken = value;
  writeStored(value);
}

export function token() {
  return accessToken;
}

export function clearToken() {
  accessToken = "";
  writeStored(null);
}

function readStored() {
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY);
    return stored === null ? "" : stored;
  } catch (_error) {
    return "";
  }
}

function writeStored(value) {
  try {
    if (value === null || value === "") {
      sessionStorage.removeItem(STORAGE_KEY);
    } else {
      sessionStorage.setItem(STORAGE_KEY, value);
    }
  } catch (_error) {
    // A private window can refuse storage. The variable still covers this page.
  }
}
