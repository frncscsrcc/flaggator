// One bar and one footer for every page after sign-in.
// The address on the right comes from GET /v1/users/{id}. The id is the
// sub claim. A missing token leaves that spot empty.

import { getUser } from "./api.js";
import { subject } from "./roles.js";
import { token } from "./session.js";

const pages = [
  { id: "dashboard", href: "index.html", label: "Dashboard" },
  { id: "projects", href: "projects.html", label: "Projects" },
  { id: "users", href: "users.html", label: "Users" },
];

const current = document.body.dataset.page;

function brandLink() {
  const link = document.createElement("a");
  link.className = "brand";
  link.href = "index.html";
  const mark = document.createElement("img");
  mark.src = "logo.png";
  mark.alt = "";
  mark.width = 1044;
  mark.height = 620;
  const name = document.createElement("span");
  name.append("Flaggator");
  const tld = document.createElement("span");
  tld.className = "tld";
  tld.textContent = ".dev";
  name.append(tld);
  link.append(mark, name);
  return link;
}

function navigation() {
  const nav = document.createElement("nav");
  nav.className = "nav";
  nav.setAttribute("aria-label", "Console");
  for (const page of pages) {
    const link = document.createElement("a");
    link.href = page.href;
    link.textContent = page.label;
    if (page.id === current) {
      link.setAttribute("aria-current", "page");
    }
    nav.append(link);
  }
  return nav;
}

const headerMount = document.querySelector("#shell-header");
const header = document.createElement("header");
header.className = "topbar";
const who = document.createElement("p");
who.className = "who";
header.append(brandLink(), navigation(), who);
if (token() !== "" && subject() !== "") {
  getUser(subject()).then((user) => {
    if (user !== null && typeof user.email === "string") {
      who.textContent = user.email;
    }
  }).catch(() => {
    who.textContent = "";
  });
}
headerMount.replaceChildren(header);

const footerMount = document.querySelector("#shell-footer");
const footer = document.createElement("footer");
footer.className = "footer";
const line = document.createElement("p");
line.append("Flaggator");
const tld = document.createElement("span");
tld.className = "tld";
tld.textContent = ".dev";
line.append(tld);
footer.append(line);
footerMount.replaceChildren(footer);
