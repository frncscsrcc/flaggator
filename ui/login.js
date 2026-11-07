// This concept page does not call the API yet. Stopping the submit
// keeps the browser from navigating away and discarding the fields.

const form = document.querySelector("#login");

form.addEventListener("submit", (event) => {
  event.preventDefault();
});
