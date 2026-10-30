// The few things the admin pages do besides htmx's polling. A static file, so the pages carry
// no inline script.

function flash(button, text) {
  button.dataset.label = button.dataset.label || button.textContent;
  button.textContent = text;
  setTimeout(() => { button.textContent = button.dataset.label; }, 1500);
}

document.addEventListener("click", (event) => {
  // "Copy" puts the feed address, which the button carries in data-copy, on the clipboard.
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    navigator.clipboard.writeText(copy.dataset.copy).then(
      () => flash(copy, "Copied"),
      () => {
        // No clipboard access (plain http, an old browser): select the address for Ctrl+C.
        const field = copy.parentElement.querySelector("input");
        if (field) field.select();
        flash(copy, "Press Ctrl+C");
      },
    );
  }
  // A button with data-confirm asks before it submits its form.
  const ask = event.target.closest("[data-confirm]");
  if (ask && !window.confirm(ask.dataset.confirm)) event.preventDefault();
});

// The episode table is swapped every 3 s while episodes are unfinished. Do not swap away an edit
// form that is open: whatever is typed into it would be lost.
document.addEventListener("htmx:beforeSwap", (event) => {
  if (event.detail.target.querySelector("details[open]")) event.detail.shouldSwap = false;
});

// "Upload a file". The file is the body of the request, not a form field, so that the server can
// write it to disk as it arrives; XMLHttpRequest because only it reports upload progress. The
// name and title go in headers (percent-encoded), which request logs do not show. Without script
// there is no way to send it, so the section stays hidden until here.
const uploadForm = document.getElementById("upload");
if (uploadForm) {
  document.getElementById("upload-section").hidden = false;
  const progress = document.getElementById("upload-progress");
  const button = uploadForm.querySelector("button");

  const showError = (text) => {
    let alert = document.getElementById("upload-error");
    if (!alert) {
      alert = document.createElement("p");
      alert.id = "upload-error";
      alert.className = "alert";
      alert.setAttribute("role", "alert");
      uploadForm.before(alert);
    }
    alert.textContent = text;
  };

  const encoded = (text) => {
    try { return encodeURIComponent(text); } catch (error) { return ""; }  // a lone surrogate
  };

  const failed = (text) => {
    showError(text);
    progress.hidden = true;
    button.disabled = false;
  };

  uploadForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const file = uploadForm.elements.file.files[0];
    if (!file) return;
    // The server refuses too (and Cloudflare in front of it, at 100 MB), but only after the
    // browser has sent the file: say so first.
    if (file.size > Number(uploadForm.dataset.maxBytes)) {
      showError(uploadForm.dataset.tooLarge);
      return;
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", uploadForm.dataset.action);
    const name = encoded(file.name);
    if (name) xhr.setRequestHeader("X-Upload-Name", name);
    const title = encoded(uploadForm.elements.title.value.trim());
    if (title) xhr.setRequestHeader("X-Upload-Title", title);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) progress.value = e.loaded / e.total; };
    xhr.onload = () => {
      if (xhr.status === 204) {
        location.reload();  // the new episode is in the table
        return;
      }
      // Our own refusals are plain text; anything else (Cloudflare's page for a body that is too big) is not.
      const ours = (xhr.getResponseHeader("Content-Type") || "").startsWith("text/plain");
      if (ours && xhr.responseText) failed(xhr.responseText);
      else if (xhr.status === 413) failed(uploadForm.dataset.tooLarge);
      else failed("The upload failed (HTTP " + xhr.status + ").");
    };
    xhr.onerror = () => failed("The upload was interrupted. Try again.");
    document.getElementById("upload-error")?.remove();
    progress.value = 0;
    progress.hidden = false;
    button.disabled = true;
    xhr.send(file);
  });
}
