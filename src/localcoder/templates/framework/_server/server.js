const express = require('express');
const path = require('path');
const app = express();

app.use(express.json({ limit: '50mb' }));

// ── Config: swap provider with env vars ──
const API_BASE = process.env.LLM_API_BASE || 'http://127.0.0.1:8089/v1';
const API_KEY  = process.env.LLM_API_KEY  || 'no-key-required';
const MODEL    = process.env.LLM_MODEL    || 'local';
const PORT     = process.env.PORT         || 3000;

// ── System prompt: replaced per app ──
const SYSTEM_PROMPT = `{{SYSTEM_PROMPT}}`;

// ── Serve frontend ──
app.get('/', (req, res) => res.sendFile(path.join(__dirname, 'index.html')));

// ── AI Analysis Endpoint ──
app.post('/api/analyze', async (req, res) => {
  const { message, image, audio, history } = req.body;

  // Build user content based on input type
  let userContent;
  if (image) {
    // Vision: image + optional text
    userContent = [
      { type: 'text', text: message || 'Analyze this image.' },
      { type: 'image_url', image_url: { url: image } }
    ];
  } else if (audio) {
    // Audio: transcribe context (for models with audio support)
    userContent = [
      { type: 'text', text: message || 'Transcribe and analyze this audio.' },
      { type: 'input_audio', input_audio: { data: audio.split(',')[1] || audio, format: 'webm' } }
    ];
  } else {
    // Text only
    userContent = message;
  }

  try {
    const priorMessages = Array.isArray(history)
      ? history
          .filter(item => item && (item.role === 'user' || item.role === 'assistant'))
          .map(item => ({
            role: item.role,
            content: Array.isArray(item.content) ? item.content : String(item.content || '')
          }))
      : [];

    const response = await fetch(`${API_BASE}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${API_KEY}`,
      },
      body: JSON.stringify({
        model: MODEL,
        stream: false,
        max_tokens: 2048,
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          ...priorMessages,
          { role: 'user', content: userContent }
        ]
      })
    });

    if (!response.ok) {
      const err = await response.text();
      return res.status(response.status).json({ error: `API Error: ${err}` });
    }

    const data = await response.json();
    const analysis = data.choices?.[0]?.message?.content || 'No response from AI';
    res.json({ analysis });

  } catch (error) {
    console.error('AI Error:', error.message);
    res.status(500).json({ error: error.message });
  }
});

app.listen(PORT, () => console.log(`✨ Running at http://localhost:${PORT}`));
