---
name: web-app-patterns
description: Reference patterns for building web applications with a lightweight backend. Use when the user asks for a web app, Express server, or a project that needs a backend.
---

# Web App Patterns

Reference patterns for building web applications. Load this skill when the user asks to build a web app, Express server, or any project that needs a backend.

## When to Use
- User asks to build an AI-powered app (analyzer, chatbot, scanner)
- User asks for a server/backend
- NOT for simple static pages (those are just index.html)

## 3-File Pattern (Express + LLM)

Always create 3 files in the SAME directory:

### package.json
```json
{"name":"app","scripts":{"start":"node server.js"},"dependencies":{"express":"^4"}}
```

### server.js
```javascript
const express = require('express');
const app = express();
app.use(express.json({limit:'50mb'}));
const API_BASE = process.env.LLM_API_BASE || 'http://127.0.0.1:8089/v1';
const MODEL = process.env.LLM_MODEL || 'local';
app.get('/', (req,res) => res.sendFile(__dirname+'/index.html'));
app.post('/api/analyze', async (req,res) => {
  const {message, image} = req.body;
  const userContent = image
    ? [{type:'text',text:message}, {type:'image_url',image_url:{url:image}}]
    : message;
  const r = await fetch(API_BASE+'/chat/completions', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({model:MODEL, stream:false, max_tokens:2048,
      messages:[{role:'system',content:SYSTEM_PROMPT}, {role:'user',content:userContent}]})});
  const data = await r.json();
  res.json({analysis: data.choices[0].message.content});
});
app.listen(3000);
```

### index.html
Single file with inline CSS+JS. Dark theme.

## Frontend Design System
- Background: `#0a0a14`
- Card: `background:rgba(255,255,255,0.04); backdrop-filter:blur(20px); border:1px solid rgba(255,255,255,0.08); border-radius:24px`
- Buttons: `border-radius:14px; background:linear-gradient(135deg,#6366f1,#8b5cf6); color:white; font-weight:600; padding:14px 28px`
- Title gradient: `background:linear-gradient(to right,#f97316,#22c55e); -webkit-background-clip:text; color:transparent`
- Result area: `background:rgba(255,255,255,0.03); border-left:3px solid #22c55e; border-radius:16px; padding:24px`
- Font: `system-ui`. Transitions on all interactive elements.

## Image Upload Pattern
```javascript
let imageBase64 = null;
function uploadImage() {
  const input = document.createElement('input');
  input.type='file'; input.accept='image/*';
  input.onchange = e => {
    const file = e.target.files[0]; if(!file) return;
    const reader = new FileReader();
    reader.onload = ev => { imageBase64 = ev.target.result;
      document.getElementById('preview').src = imageBase64;
      document.getElementById('preview').style.display = 'block'; };
    reader.readAsDataURL(file); };
  input.click();
}
```

## Camera Capture Pattern
```javascript
async function openCamera() {
  const stream = await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});
  const video = document.getElementById('camVideo');
  video.srcObject = stream; video.style.display='block'; video.play();
}
function capturePhoto() {
  const video = document.getElementById('camVideo');
  const canvas = document.createElement('canvas');
  canvas.width=video.videoWidth; canvas.height=video.videoHeight;
  canvas.getContext('2d').drawImage(video,0,0);
  imageBase64 = canvas.toDataURL('image/jpeg',0.8);
  video.srcObject.getTracks().forEach(t=>t.stop()); video.style.display='none';
}
```

## Loading Animation
```css
@keyframes scan { 0%{transform:translateY(-100%)} 100%{transform:translateY(100%)} }
.scanning { position:relative; overflow:hidden; }
.scanning::after { content:''; position:absolute; left:0; right:0; height:2px;
  background:linear-gradient(90deg,transparent,#22c55e,transparent); animation:scan 1.5s infinite; }
@keyframes dots { 0%{content:''} 33%{content:'.'} 66%{content:'..'} 100%{content:'...'} }
.dots::after { content:''; animation:dots 1.5s infinite steps(4); }
```

## Testing Checklist
1. Static HTML: `python3 -m http.server 8888 &` then `curl -s http://localhost:8888/`
2. Express: `npm install && node server.js &` then `curl -s http://localhost:3000/`
3. Check JS: `node -e "...parse script tags..."`
4. Fix any errors, re-test until passing.

## Common Bugs
- Start screen not hiding: use `onclick='getElementById("s1").style.display="none"'`
- SVG arcs: use `Math.cos(angle*Math.PI/180)*radius` — never approximate
- Fetch to /api without server: static HTML can't call /api. Use local JS logic.
- Always add `transition` on hover/active states.
