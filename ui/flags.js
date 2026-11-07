// The projects page links here with the project key in the query.
// The flag list is not on this page yet.

const project = new URLSearchParams(window.location.search).get("project");
const line = document.querySelector("#flags-project");

if (typeof project === "string" && project !== "") {
  line.textContent = project;
} else {
  line.textContent = "Open this page from a project.";
}
