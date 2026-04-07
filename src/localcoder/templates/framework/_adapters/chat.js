/* ── Chat Adapter: Multi-turn conversation with message bubbles ── */

let chatHistory = [];

function addMessage(role, content) {
  chatHistory.push({ role, content });
  renderMessages();
}

function renderMessages() {
  const container = document.getElementById('messages');
  container.innerHTML = chatHistory.map((m, i) => {
    const isUser = m.role === 'user';
    const align = isUser ? 'flex-end' : 'flex-start';
    const bubbleClass = isUser ? 'bubble-user' : 'bubble-ai';
    const label = isUser ? 'You' : 'AI';
    const content = m.role === 'assistant' ? md(m.content) : escapeHtml(m.content);
    return `<div class="msg" style="align-self:${align}">
      <div class="msg-label">${label}</div>
      <div class="${bubbleClass}">${content}</div>
    </div>`;
  }).join('');
  container.scrollTop = container.scrollHeight;
}

function escapeHtml(text) {
  return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}

function updateLastAI(content) {
  if (chatHistory.length > 0 && chatHistory[chatHistory.length - 1].role === 'assistant') {
    chatHistory[chatHistory.length - 1].content = content;
    renderMessages();
  }
}

async function sendMessage() {
  const input = document.getElementById('chatInput');
  const msg = input.value.trim();
  const img = typeof getImageBase64 === 'function' ? getImageBase64() : null;
  const audio = typeof getAudioBase64 === 'function' ? getAudioBase64() : null;
  if (!msg && !img && !audio) return;

  // Add user message
  addMessage('user', msg || (img ? '📷 Image' : '🎙️ Audio'));
  input.value = '';
  input.style.height = 'auto';

  // Add placeholder AI message
  addMessage('assistant', '');
  const sendBtn = document.getElementById('sendBtn');
  sendBtn.disabled = true;

  // Show typing indicator
  updateLastAI('<div class="typing"><span></span><span></span><span></span></div>');

  try {
    const body = { message: msg || 'Analyze this', history: chatHistory.slice(0, -1) };
    if (img) body.image = img;
    if (audio) body.audio = audio;

    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    updateLastAI(data.analysis);
  } catch (err) {
    updateLastAI(`<span style="color:var(--danger)">⚠️ ${err.message}</span>`);
  } finally {
    sendBtn.disabled = false;
    if (typeof clearImage === 'function') clearImage();
    if (typeof clearAudio === 'function') clearAudio();
    input.focus();
  }
}
