// Sign-in uses the shared request helper. The message is written as text
// so an error string from the API cannot become markup.

import { ApiError, login } from "./api.js";

const form = document.querySelector("#login");
const message = document.querySelector("#login-message");
const button = form.querySelector("button");

form.addEventListener("submit", (event) => {
  event.preventDefault();
  signIn();
});

async function signIn() {
  message.textContent = "";
  message.classList.remove("is-error");
  button.disabled = true;
  try {
    await login(form.email.value, form.password.value);
    window.location.assign("index.html");
  } catch (error) {
    message.classList.add("is-error");
    if (error instanceof ApiError) {
      message.textContent = error.message;
    } else {
      message.textContent = "The request failed.";
    }
  } finally {
    button.disabled = false;
  }
}
