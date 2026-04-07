/* ── Text Adapter: Textarea + Chat Input ── */

function getTextInput(id = 'input') {
  const el = document.getElementById(id);
  return el ? el.value.trim() : '';
}

function clearTextInput(id = 'input') {
  const el = document.getElementById(id);
  if (el) el.value = '';
}

function autoResize(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 300) + 'px';
}
