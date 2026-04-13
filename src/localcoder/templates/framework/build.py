"""Build an AI app from framework templates.

Usage:
  python build.py ingredients-scanner ./output-dir
  python build.py voice-memo ./my-voice-app
  python build.py --list
"""
import json, os, shutil, sys

FRAMEWORK_DIR = os.path.dirname(os.path.abspath(__file__))
APPS_DIR = os.path.join(FRAMEWORK_DIR, 'apps')
SHELL_DIR = os.path.join(FRAMEWORK_DIR, '_shell')
ADAPTERS_DIR = os.path.join(FRAMEWORK_DIR, '_adapters')
SERVER_DIR = os.path.join(FRAMEWORK_DIR, '_server')


def list_apps():
    """List all available app templates."""
    apps = []
    for name in sorted(os.listdir(APPS_DIR)):
        config_path = os.path.join(APPS_DIR, name, 'config.json')
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
            apps.append(cfg)
    return apps


def build_app(app_id, output_dir):
    """Build a complete app from template components."""
    config_path = os.path.join(APPS_DIR, app_id, 'config.json')
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"App template '{app_id}' not found")

    with open(config_path) as f:
        cfg = json.load(f)
    app_dir = os.path.join(APPS_DIR, app_id)

    os.makedirs(output_dir, exist_ok=True)

    # ── Build server.js ──
    with open(os.path.join(SERVER_DIR, 'server.js')) as f:
        server = f.read()
    server = server.replace('{{SYSTEM_PROMPT}}', cfg['system_prompt'].replace('`', '\\`').replace('$', '\\$'))
    with open(os.path.join(output_dir, 'server.js'), 'w') as f:
        f.write(server)

    # ── Build package.json ──
    with open(os.path.join(SERVER_DIR, 'package.json')) as f:
        pkg = f.read()
    pkg = pkg.replace('{{APP_ID}}', cfg['id'])
    with open(os.path.join(output_dir, 'package.json'), 'w') as f:
        f.write(pkg)

    # ── Build index.html ──
    # Load shell CSS + JS
    with open(os.path.join(SHELL_DIR, 'base.css')) as f:
        css = f.read()
    with open(os.path.join(SHELL_DIR, 'base.js')) as f:
        base_js = f.read()

    custom_css = ''
    custom_html = ''
    custom_js = ''
    custom_css_path = os.path.join(app_dir, 'app.css')
    custom_html_path = os.path.join(app_dir, 'app.html')
    custom_js_path = os.path.join(app_dir, 'app.js')
    if os.path.exists(custom_css_path):
        with open(custom_css_path) as f:
            custom_css = f.read()
    if os.path.exists(custom_html_path):
        with open(custom_html_path) as f:
            custom_html = f.read()
    if os.path.exists(custom_js_path):
        with open(custom_js_path) as f:
            custom_js = f.read()

    # Load adapters based on config
    adapter_js = ''
    adapter_html = ''
    inputs = cfg.get('inputs', ['text'])

    if 'text' in inputs:
        with open(os.path.join(ADAPTERS_DIR, 'text.js')) as f:
            adapter_js += f.read() + '\n'

    if 'image' in inputs:
        with open(os.path.join(ADAPTERS_DIR, 'image.js')) as f:
            adapter_js += f.read() + '\n'
        adapter_html += '''
        <div class="btn-row">
          <button class="btn-secondary" onclick="uploadImage()"><span class="btn-icon">📁</span> Upload</button>
          <button class="btn-secondary" onclick="openCamera()"><span class="btn-icon">📸</span> Camera</button>
          <button id="captureBtn" class="btn-secondary" onclick="capturePhoto()" style="display:none"><span class="btn-icon">📷</span> Capture</button>
        </div>
        <video id="camVideo" class="camera-feed" playsinline></video>
        <img id="preview" class="preview-image">'''

    if 'voice' in inputs:
        with open(os.path.join(ADAPTERS_DIR, 'voice.js')) as f:
            adapter_js += f.read() + '\n'
        adapter_html += '''
        <div class="btn-row">
          <button id="recordBtn" class="btn-secondary" onclick="toggleRecording(this)"><span class="btn-icon">🎙️</span> Record</button>
          <span id="recordTimer" style="color:var(--text-dim);font-family:var(--font-mono);font-size:0.85rem"></span>
        </div>
        <audio id="audioPreview" controls style="display:none;width:100%;margin-top:8px;border-radius:8px"></audio>'''

    # Compose analyze function
    analyze_fn = '''
    async function analyze() {
      const msg = getTextInput('input');
      const img = typeof getImageBase64 === 'function' ? getImageBase64() : null;
      const audio = typeof getAudioBase64 === 'function' ? getAudioBase64() : null;
      if (!msg && !img && !audio) return;

      const resultEl = document.getElementById('result');
      const btn = document.getElementById('analyzeBtn');
      btn.disabled = true;
      showLoading(resultEl, 'Analyzing');

      try {
        const body = { message: msg || 'Analyze this' };
        if (img) body.image = img;
        if (audio) body.audio = audio;

        const res = await fetch('/api/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        showResult(resultEl, data.analysis);
      } catch (err) {
        showError(resultEl, err.message);
      } finally {
        btn.disabled = false;
        if (typeof clearImage === 'function') clearImage();
        if (typeof clearAudio === 'function') clearAudio();
      }
    }'''

    # Build full HTML
    # Per-app theme overrides
    theme = cfg.get("theme", {})
    accent = theme.get("accent", "#34d399")
    accent2 = theme.get("accent2", "#2dd4bf")
    bg = theme.get("bg", "#08080c")
    theme_css = f"""
    :root {{
      --bg: {bg};
      --accent: {accent};
      --accent-dim: {accent}1f;
      --accent-glow: {accent}0f;
    }}
    .app-title {{
      background: linear-gradient(135deg, {accent}, {accent2});
      -webkit-background-clip: text;
      background-clip: text;
    }}
    .btn-primary {{
      background: linear-gradient(135deg, {accent}, {accent2});
      box-shadow: 0 2px 16px {accent}1f;
    }}
    .btn-primary:hover {{ box-shadow: 0 4px 24px {accent}33; }}
    .result-area {{ border-left-color: {accent}; }}
    .loading {{ color: {accent}; }}
    .loading .spinner {{ border-top-color: {accent}; border-color: {accent}1f; border-top-color: {accent}; }}
    .scanning::after {{ background: linear-gradient(90deg, transparent, {accent}, transparent); }}
    body {{ background-color: {bg}; }}
    """

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{cfg["title"]} {cfg["icon"]}</title>
      <style>{css}{theme_css}{custom_css}</style>
</head>
<body>
  <div class="app-container">
    <div class="app-card">
      <div class="app-title">{cfg["icon"]} {cfg["title"]}</div>
      <div class="app-subtitle">{cfg["subtitle"]}</div>
    </div>

      <div class="app-card">
        <div class="input-area">
          <textarea id="input" placeholder="{cfg.get("placeholder", "Type here...")}" oninput="autoResize(this)"></textarea>
          {custom_html}
          {adapter_html}
          <button id="analyzeBtn" class="btn-primary" onclick="analyze()">
            <span class="btn-icon">⚡</span> Analyze
        </button>
      </div>
    </div>

    <div class="app-card">
      <div id="result" class="result-area">
        <span class="empty">Results will appear here...</span>
      </div>
    </div>

    <div class="app-footer">
      Powered by local AI · Any OpenAI-compatible endpoint
    </div>
  </div>

  <script>
{base_js}
{adapter_js}
{analyze_fn}
{custom_js}

    // Enter to analyze
    document.getElementById('input').addEventListener('keydown', e => {{
      if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); analyze(); }}
    }});
  </script>
</body>
</html>'''

    with open(os.path.join(output_dir, 'index.html'), 'w') as f:
        f.write(html)

    return cfg


def main():
    if len(sys.argv) < 2 or sys.argv[1] == '--list':
        apps = list_apps()
        print('\nAvailable apps:\n')
        for a in apps:
            inputs = ', '.join(a.get('inputs', []))
            model = a.get('model', 'any')
            print(f"  {a['icon']}  {a['id']:<22} {a['title']:<20} inputs: {inputs:<16} model: {model}")
        print(f'\nUsage: python build.py <app-id> <output-dir>\n')
        return

    app_id = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else app_id

    cfg = build_app(app_id, output_dir)
    print(f"\n  {cfg['icon']}  {cfg['title']} built → {output_dir}/")
    print(f"\n  cd {output_dir} && npm install && npm start")
    print(f"  Open http://localhost:3000\n")
    print(f"  Switch AI: LLM_API_BASE=https://api.openai.com/v1 LLM_API_KEY=sk-... npm start\n")


if __name__ == '__main__':
    main()
