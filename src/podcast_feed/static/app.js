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
