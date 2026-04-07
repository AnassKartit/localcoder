/* ══════════════════════════════════════════════════════════════
   LOCAL AI — Shared Utilities
   ══════════════════════════════════════════════════════════════ */

// ── Markdown Renderer ──
function md(text) {
  if (!text) return '';
  return text
    .replace(/### (.+)/g, '<h3>$1</h3>')
    .replace(/## (.+)/g, '<h2>$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^\* (.+)$/gm, '<li>$1</li>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/\|(.+)\|/g, (m) => {
      const cells = m.split('|').filter(Boolean).map(c => `<td>${c.trim()}</td>`);
      return `<tr>${cells.join('')}</tr>`;
    })
    .replace(/---+/g, '<hr>')
    .replace(/\n\n/g, '<br><br>')
    .replace(/\n/g, '<br>');
}

// ── Loading State ──
function showLoading(el, message = 'Analyzing') {
  el.classList.add('scanning');
  el.innerHTML = `<div class="loading"><div class="spinner"></div>${message}<span class="dots"></span></div>`;
}

function hideLoading(el) {
  el.classList.remove('scanning');
}

// ── API Call ──
async function callAI(message, image = null) {
  const body = { message };
  if (image) body.image = image;

  const res = await fetch('/api/analyze', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });

  if (!res.ok) {
    const err = await res.text();
    throw new Error(err);
  }

  const data = await res.json();
  return data.analysis;
}

// ── Display Result ──
function showResult(el, text) {
  hideLoading(el);
  el.innerHTML = md(text);
}

function showError(el, msg) {
  hideLoading(el);
  el.innerHTML = `<div style="color:var(--danger)">⚠️ ${msg}</div>`;
}
