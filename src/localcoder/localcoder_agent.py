#!/usr/bin/env /opt/homebrew/bin/python3
"""localcoder — Claude Code-style CLI agent powered by local models."""
import os, subprocess, sys, json, urllib.request, urllib.parse, time, re, argparse, logging, signal

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings

from localcoder.localcoder_display import (
    ThinkingSpinner,
    show_startup_animation,
    show_tool_animation,
    tool_running_indicator,
    generating_indicator,
    context_usage_bar,
    context_usage_bar_compact,
)

console = Console()

# ── Config ──
API_BASE = os.environ.get("GEMMA_API_BASE", "http://127.0.0.1:8089/v1")
MODEL = os.environ.get("GEMMA_MODEL", "gemma4-26b")
CWD = os.getcwd()
REASONING_EFFORT = "medium"  # none, low, medium, high — toggle with /think

# ── Backend detection ──
def detect_backend():
    """Auto-detect backend type and model info from the API server."""
    info = {"backend": "unknown", "model_name": MODEL, "quant": "", "size": "", "ctx": ""}
    try:
        # Check if it's Ollama (has /api/tags)
        if "11434" in API_BASE:
            info["backend"] = "Ollama"
        else:
            info["backend"] = "llama.cpp"

        # Get model list from /models endpoint
        req = urllib.request.Request(f"{API_BASE}/models", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        models = data.get("data", [])
        if models:
            m = models[0]
            mid = m.get("id", MODEL)
            info["model_name"] = mid

            # Parse quant from model ID (e.g. "gemma-4-26B-A4B-it-UD-Q3_K_XL")
            for q in ["Q2_K", "Q3_K_S", "Q3_K_M", "Q3_K_L", "Q3_K_XL", "Q4_K_S", "Q4_K_M", "Q4_K_XL", "Q5_K_M", "Q6_K", "Q8_0", "BF16", "F16", "IQ3_S", "IQ4_XS"]:
                if q.lower().replace("_", "") in mid.lower().replace("_", "").replace("-", ""):
                    info["quant"] = q
                    break

            # Parse model size
            for s in ["e2b", "e4b", "26b", "27b", "31b", "12b", "8b", "4b", "2b", "1b", "70b"]:
                if s in mid.lower().replace("-", ""):
                    info["size"] = s.upper()
                    break

        # Try to get context size from /props or /health
        try:
            req2 = urllib.request.Request(f"{API_BASE.replace('/v1','')}/props", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req2, timeout=2) as resp2:
                props = json.loads(resp2.read())
            ctx = props.get("default_generation_settings", {}).get("n_ctx", 0)
            if ctx:
                if ctx >= 131072: info["ctx"] = "128K"
                elif ctx >= 65536: info["ctx"] = "64K"
                elif ctx >= 32768: info["ctx"] = "32K"
                elif ctx >= 16384: info["ctx"] = "16K"
                else: info["ctx"] = f"{ctx//1024}K"
        except:
            pass

    except:
        pass
    return info

BACKEND_INFO = {"backend": "unknown", "model_name": MODEL, "quant": "", "size": "", "ctx": ""}

CONFIG_FILE = os.path.expanduser("~/.localcoder/config.json")

def _save_config(**kwargs):
    """Save config values to ~/.localcoder/config.json."""
    try:
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        cfg.update(kwargs)
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except:
        pass

def _load_config():
    """Load config from ~/.localcoder/config.json."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}

def _save_last_model(model, api_base):
    _save_config(model=model, api_base=api_base, backend="ollama" if "11434" in api_base else "llamacpp")

def _check_permissions():
    """Check and guide user through macOS permissions on first run."""
    console.print()
    console.print(Panel(
        "[bold]macOS Permissions[/]  [dim]checking what Local Coder can access...[/]",
        border_style="#81b29a", padding=(0, 1),
    ))

    PERMS = [
        {
            "name": "Microphone",
            "why": "Voice input — speak prompts instead of typing (Ctrl+R)",
            "test": lambda: _test_mic(),
            "fix": "System Settings → Privacy & Security → Microphone → enable your terminal",
        },
        {
            "name": "Screen Recording",
            "why": "Computer use — let the AI see your screen and automate GUI tasks",
            "test": lambda: _test_screen(),
            "fix": "System Settings → Privacy & Security → Screen Recording → enable your terminal",
        },
        {
            "name": "Accessibility",
            "why": "System control — set wallpaper, control apps, click UI elements",
            "test": lambda: _test_accessibility(),
            "fix": "System Settings → Privacy & Security → Accessibility → enable your terminal",
        },
        {
            "name": "Automation",
            "why": "App control — automate Finder, Safari, System Events",
            "test": lambda: _test_automation(),
            "fix": "System Settings → Privacy & Security → Automation → enable your terminal",
        },
    ]

    all_granted = True
    denied = []

    for perm in PERMS:
        try:
            granted = perm["test"]()
        except:
            granted = False

        if granted:
            console.print(f"  [green]✓[/] [bold]{perm['name']}[/]  [dim]{perm['why']}[/]")
        else:
            all_granted = False
            denied.append(perm)
            console.print(f"  [yellow]○[/] [bold]{perm['name']}[/]  [dim]{perm['why']}[/]")

    if denied:
        console.print(f"\n  [yellow]Some permissions not granted yet.[/]")
        console.print(f"  [dim]Local Coder works without them, but these features will be limited:[/]\n")
        for perm in denied:
            console.print(f"    [yellow]•[/] [bold]{perm['name']}[/]: {perm['why']}")
            console.print(f"      [dim]Fix: {perm['fix']}[/]")

        console.print(f"\n  [dim]Open System Settings now? (y/n)[/]")
        try:
            ans = input("  ▸ ").strip().lower()
            if ans in ("y", "yes", ""):
                subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy"], capture_output=True)
                console.print(f"  [green]Opened System Settings.[/] Grant permissions, then restart Local Coder.")
        except:
            pass
    else:
        console.print(f"\n  [green]✓ All permissions granted![/]")

    _save_config(permissions_checked=True)
    console.print()


def _test_mic():
    import tempfile
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        r = subprocess.run(["rec", "-q", "-r", "16000", "-c", "1", "-b", "16", tmp, "trim", "0", "0.3"],
            capture_output=True, timeout=5)
        return os.path.exists(tmp) and os.path.getsize(tmp) > 100
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _test_screen():
    import tempfile
    tmp = tempfile.mktemp(suffix=".png")
    try:
        subprocess.run(["screencapture", "-x", tmp], capture_output=True, timeout=5)
        return os.path.exists(tmp) and os.path.getsize(tmp) > 1000
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _test_accessibility():
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first process'],
        capture_output=True, text=True, timeout=5)
    return r.returncode == 0


def _test_automation():
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to get picture of desktop 1'],
        capture_output=True, text=True, timeout=5)
    return r.returncode == 0


def _switch_model(new_model, new_url):
    """Switch to a new model — handles running, downloaded, and cross-backend."""
    global MODEL, API_BASE, BACKEND_INFO
    import shutil as _shutil

    # Check if this model is already running on its backend
    is_running = False
    try:
        req = urllib.request.Request(f"{new_url}/models", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        running_ids = [m.get("id", "").lower() for m in data.get("data", [])]
        is_running = new_model.lower() in " ".join(running_ids).lower()
    except:
        pass

    if is_running:
        # Model already running — just switch
        MODEL = new_model
        API_BASE = new_url
        BACKEND_INFO.update(detect_backend())
        _save_last_model(MODEL, API_BASE)
        console.print(f"  [green]✓ Switched to [bold]{MODEL}[/] on {BACKEND_INFO['backend']}[/]")
        return

    # Model is downloaded but not running — need to load it
    is_ollama = "11434" in new_url

    if is_ollama:
        # Ollama model — just switch, Ollama auto-loads on first request
        MODEL = new_model
        API_BASE = new_url
        BACKEND_INFO.update(detect_backend())
        _save_last_model(MODEL, API_BASE)
        console.print(f"  [green]✓ Switched to [bold]{MODEL}[/] on Ollama[/]")
        console.print(f"  [dim]Ollama will load the model on first request[/]")
        return

    # llama.cpp downloaded model — needs server restart
    # Find the GGUF file
    all_m = discover_all_models()
    gguf_path = None
    for m in all_m:
        if m["id"] == new_model and m.get("path"):
            gguf_path = m["path"]
            break

    if not gguf_path:
        console.print(f"  [red]Cannot find GGUF file for {new_model}[/]")
        return

    console.print(f"  [yellow]Restarting llama-server with {new_model}...[/]")

    # Kill current server
    subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
    time.sleep(3)

    # Find mmproj in same directory
    model_dir = os.path.dirname(gguf_path)
    mmproj = None
    for f in os.listdir(model_dir):
        if "mmproj" in f.lower() and f.endswith(".gguf"):
            mmproj = os.path.join(model_dir, f)
            break

    # Start new server
    binary = os.path.expanduser("~/.unsloth/llama.cpp/llama-server")
    if not os.path.exists(binary):
        binary = _shutil.which("llama-server") or binary

    cmd = [binary, "-m", gguf_path, "--port", "8089",
           "-ngl", "99", "-c", "131072", "-np", "1",
           "-fa", "on", "-ctk", "q4_0", "-ctv", "q4_0",
           "--no-warmup", "--cache-ram", "0", "--jinja",
           "--reasoning-budget", "0"]
    if mmproj:
        cmd += ["--mmproj", mmproj]
    else:
        cmd += ["--no-mmproj"]

    console.print(f"  [dim]Starting server...[/]")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for ready
    ready = False
    for i in range(60):
        try:
            req = urllib.request.Request("http://127.0.0.1:8089/health")
            with urllib.request.urlopen(req, timeout=1):
                ready = True
                break
        except:
            time.sleep(1)
        if not proc.poll() is None:
            console.print(f"  [red]Server crashed — model may not fit in GPU[/]")
            return

    if ready:
        MODEL = new_model
        API_BASE = "http://127.0.0.1:8089/v1"
        BACKEND_INFO.update(detect_backend())
        _save_last_model(MODEL, API_BASE)
        console.print(f"  [green]✓ Switched to [bold]{MODEL}[/] on llama.cpp ({BACKEND_INFO.get('ctx', '?')})[/]")
    else:
        console.print(f"  [red]Server failed to start in 60s[/]")


def _load_last_model():
    """Load last used model from config. Also auto-detect what's actually running."""
    global MODEL, API_BASE
    # 1. Load saved config
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            if cfg.get("model"):
                MODEL = cfg["model"]
            if cfg.get("api_base"):
                API_BASE = cfg["api_base"]
    except:
        pass

    # 2. Auto-detect: if llama-server is running, use whatever model it has loaded
    try:
        req = urllib.request.Request("http://127.0.0.1:8089/v1/models",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        running = [m.get("id", "") for m in data.get("data", [])]
        if running:
            MODEL = running[0]
            API_BASE = "http://127.0.0.1:8089/v1"
            return
    except:
        pass

    # 3. Fallback: check Ollama
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        loaded = [m.get("name", "") for m in data.get("models", [])]
        if loaded:
            MODEL = loaded[0]
            API_BASE = "http://127.0.0.1:11434/v1"
    except:
        pass

# ── Multi-backend discovery ──
BACKENDS = [
    {"name": "llama.cpp", "url": "http://127.0.0.1:8089/v1", "type": "llamacpp"},
    {"name": "Ollama", "url": "http://127.0.0.1:11434/v1", "type": "ollama"},
]

def discover_all_models():
    """Discover models from running backends + downloaded GGUFs."""
    all_models = []
    seen = set()

    # 1. Running models from backends
    for backend in BACKENDS:
        try:
            req = urllib.request.Request(f"{backend['url']}/models", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
            for m in data.get("data", []):
                mid = m.get("id", "")
                if mid:
                    all_models.append({"id": mid, "backend": backend["name"], "url": backend["url"], "status": "running"})
                    seen.add(mid.lower())
        except:
            pass

    # 2. Downloaded GGUFs in HuggingFace cache (available for llama.cpp)
    import glob
    hf_cache = os.path.expanduser("~/.cache/huggingface/hub")
    for gguf in glob.glob(f"{hf_cache}/models--*/snapshots/*/*.gguf"):
        name = os.path.basename(gguf)
        if "mmproj" in name.lower():
            continue
        if name.lower() not in seen:
            size_gb = os.path.getsize(gguf) / (1024**3)
            all_models.append({
                "id": name,
                "backend": "llama.cpp",
                "url": "http://127.0.0.1:8089/v1",
                "status": "downloaded",
                "path": gguf,
                "size_gb": round(size_gb, 1),
            })
            seen.add(name.lower())

    return all_models

def select_model_interactive():
    """Interactive model selector with fuzzy search autocomplete."""
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML as PT_HTML

    models = discover_all_models()
    if not models:
        console.print(f"\n  [red]No backends found. Start llama-server or Ollama.[/]")
        return None, None

    # Display with styled backend grouping
    console.print()
    console.print(Panel(
        "[bold]Select Model[/]  [dim]type to search · enter to select · esc to cancel[/]",
        border_style="#81b29a", padding=(0, 1),
    ))

    by_backend = {}
    for m in models:
        by_backend.setdefault(m["backend"], []).append(m)

    for backend, mlist in by_backend.items():
        console.print(f"\n  [bold #81b29a]{backend}[/]")
        for m in mlist:
            is_current = m["id"] == MODEL and m["url"] == API_BASE
            status = m.get("status", "running")
            if is_current:
                dot = "[bold green]●[/]"
                name_style = "bold white"
                tag = " [bold green]← active[/]"
            elif status == "running":
                dot = "[green]○[/]"
                name_style = "cyan"
                tag = ""
            else:
                dot = "[dim]◌[/]"
                name_style = "dim cyan"
                size = f" ({m.get('size_gb', '?')}GB)" if m.get("size_gb") else ""
                tag = f" [dim yellow]downloaded{size}[/]"
            console.print(f"    {dot} [{name_style}]{m['id']}[/]{tag}")
    console.print()

    # Build fuzzy completer
    class ModelCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.lower()
            for m in models:
                label = m["id"]
                if text in label.lower() or not text:
                    status = m.get("status", "running")
                    tag = '<style fg="ansigreen">running</style>' if status == "running" else '<style fg="ansiyellow">downloaded</style>'
                    yield Completion(
                        label,
                        start_position=-len(document.text_before_cursor),
                        display=PT_HTML(f'<b>{label}</b> <style fg="ansigray">{m["backend"]}</style> {tag}'),
                    )

    try:
        from prompt_toolkit.shortcuts import radiolist_dialog
        from prompt_toolkit.styles import Style as PTStyle

        # Get system RAM for recommendations
        try:
            if sys.platform == "darwin":
                _ram = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3).stdout.strip()) // (1024**3)
            else:
                with open("/proc/meminfo") as _f:
                    for _l in _f:
                        if _l.startswith("MemTotal:"):
                            _ram = int(_l.split()[1]) // (1024 * 1024); break
        except:
            _ram = 24
        metal_gb = int(_ram * 0.67)

        # Benchmark data from our tests (tok/s on M4 Pro 24GB)
        BENCHMARKS = {
            "gemma-4-26b-a4b-it-ud-q3_k_xl": {"tok_s": 49, "gpu_gb": 13.6, "quality": "★★★★★", "note": "Best overall on 24GB"},
            "qwen3.5-35b-a3b-ud-q2_k_xl": {"tok_s": 49, "gpu_gb": 12.0, "quality": "★★★★☆", "note": "More code detail"},
            "qwen3.5-4b-ud-q4_k_xl": {"tok_s": 50, "gpu_gb": 2.7, "quality": "★★★☆☆", "note": "Ultrafast, basic tasks"},
            "gemma-4-e4b-it-q4_k_m": {"tok_s": 38, "gpu_gb": 9.6, "quality": "★★★★☆", "note": "Audio + image"},
            "gemma4:e4b": {"tok_s": 38, "gpu_gb": 9.6, "quality": "★★★★☆", "note": "Audio + image"},
            "gemma4:e2b": {"tok_s": 57, "gpu_gb": 7.2, "quality": "★★☆☆☆", "note": "Speed demon"},
            "gemma4:26b": {"tok_s": 9, "gpu_gb": 16.8, "quality": "★★★★★", "note": "Slow on 24GB (swap)"},
            "qwen3.5:27b": {"tok_s": 5, "gpu_gb": 16.2, "quality": "★★★★☆", "note": "Dense — swap thrashing"},
        }

        # Build choices with recommendations
        choices = []
        for m in models:
            mid = m["id"]
            status = m.get("status", "running")
            is_current = m["id"] == MODEL and m["url"] == API_BASE

            # Look up benchmark
            bench_key = mid.lower().replace(".gguf", "")
            bench = BENCHMARKS.get(bench_key, {})
            if not bench:
                # Fuzzy match
                for bk, bv in BENCHMARKS.items():
                    if bk in bench_key or bench_key in bk:
                        bench = bv; break

            gpu = bench.get("gpu_gb", m.get("size_gb", 0))
            fits = gpu and gpu < metal_gb
            tok_s = bench.get("tok_s", 0)
            quality = bench.get("quality", "")
            note = bench.get("note", "")

            # Build label
            parts = []
            if is_current:
                parts.append("→ ")
            else:
                parts.append("  ")

            parts.append(mid)

            # Speed + fit indicator
            if tok_s:
                parts.append(f"  {tok_s} tok/s")
            if gpu:
                parts.append(f"  {gpu}GB")
            if quality:
                parts.append(f"  {quality}")

            # Status
            if status == "running":
                parts.append("  ✓ running")
            elif m.get("size_gb"):
                parts.append(f"  ↓ downloaded")

            # Fit warning
            if gpu and not fits:
                parts.append("  ⚠ won't fit")

            # Recommendation
            if note:
                parts.append(f"  ({note})")

            label = "".join(parts)
            choices.append((m, label))

        # Sort: running first, then by tok/s descending
        def _sort_key(item):
            m = item[0]
            bench_key = m["id"].lower().replace(".gguf", "")
            bench = BENCHMARKS.get(bench_key, {})
            if not bench:
                for bk, bv in BENCHMARKS.items():
                    if bk in bench_key or bench_key in bk:
                        bench = bv; break
            is_current = 0 if (m["id"] == MODEL and m["url"] == API_BASE) else 1
            is_running = 0 if m.get("status") == "running" else 1
            speed = -(bench.get("tok_s", 0))
            return (is_current, is_running, speed)

        choices.sort(key=_sort_key)

        dialog_style = PTStyle.from_dict({
            "dialog": "bg:#1a1a2e",
            "dialog.body": "bg:#1a1a2e #e0e0e0",
            "dialog frame.label": "bg:#e07a5f #ffffff bold",
            "dialog shadow": "bg:#000000",
            "radiolist": "bg:#1a1a2e",
            "button": "bg:#81b29a #000000 bold",
            "button.focused": "bg:#e07a5f #ffffff bold",
        })

        # Add disk space info
        disk_free = "?"
        hf_cache = "?"
        try:
            from localcoder.backends import get_disk_info
            di = get_disk_info()
            disk_free = f"{di['disk_free_gb']}GB"
            hf_cache = f"{di['hf_cache_gb']}GB"
        except Exception:
            pass

        # Add separator + trending models (live from HuggingFace)
        try:
            from localcoder.backends import fetch_unsloth_top_models, fetch_hf_trending_models
            # Separator
            sep_entry = {"id": "__sep_trending__", "url": ""}
            choices.append((sep_entry, "  ─── Trending (live from HuggingFace) ───────────────────"))

            trending = fetch_unsloth_top_models(limit=6)
            for t in trending:
                if any(t["label"].lower().replace("-","") in c[0]["id"].lower().replace("-","") for c in choices):
                    continue
                dl = t["downloads"]
                dl_str = f"{dl // 1000}K" if dl < 1_000_000 else f"{dl / 1_000_000:.1f}M"
                entry = {"id": t["repo_id"], "url": "hf_download", "hf_repo": t["repo_id"]}
                label = f"  ★ {t['label']:<30}  {dl_str} dl  → download + install"
                choices.append((entry, label))

            # Most liked (different ranking)
            liked = fetch_hf_trending_models(limit=8, sort="likes")
            trending_repos = {t["repo_id"] for t in trending}
            liked = [l for l in liked if l["repo_id"] not in trending_repos][:4]
            if liked:
                sep2 = {"id": "__sep_liked__", "url": ""}
                choices.append((sep2, "  ─── Most liked ────────────────────────────────────────"))
                for lm in liked:
                    if any(lm["label"].lower().replace("-","") in c[0]["id"].lower().replace("-","") for c in choices):
                        continue
                    dl = lm["downloads"]
                    dl_str = f"{dl // 1000}K" if dl < 1_000_000 else f"{dl / 1_000_000:.1f}M"
                    entry = {"id": lm["repo_id"], "url": "hf_download", "hf_repo": lm["repo_id"]}
                    label = f"  ♥ {lm['label']:<30}  {dl_str} dl  → download + install"
                    choices.append((entry, label))
        except Exception:
            pass

        result = radiolist_dialog(
            title="Select Model",
            text=f"RAM: {_ram}GB · GPU: ~{metal_gb}GB · Disk: {disk_free} free · Cache: {hf_cache} · ↑↓ arrows",
            values=choices,
            style=dialog_style,
        ).run()

        if result and result.get("id", "").startswith("__sep"):
            return None, None  # separator selected, ignore

        if result:
            # Handle HuggingFace download selection
            if result.get("url") == "hf_download":
                repo = result.get("hf_repo", result["id"])
                console.print(f"\n  [bold]Fetching quants for {repo}...[/]")
                try:
                    from localcoder.backends import simulate_hf_model
                    simulate_hf_model(repo)
                except Exception as e:
                    console.print(f"  [red]{e}[/]")
                return None, None  # don't switch yet — user needs to download first
            return result["id"], result["url"]
        return None, None

    except Exception:
        # Fallback to text prompt if dialog fails
        try:
            choice = pt_prompt(
                PT_HTML('<style fg="#81b29a" bold="true">  model▸ </style>'),
                completer=ModelCompleter(),
                complete_while_typing=True,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None, None

    if not choice or choice.lower() in ('q', 'quit', 'esc'):
        return None, None

    # Exact match
    for m in models:
        if choice == m["id"]:
            return m["id"], m["url"]

    # Fuzzy match
    for m in models:
        if choice.lower() in m["id"].lower():
            return m["id"], m["url"]

    console.print(f"  [red]Not found: {choice}[/]")
    return None, None

# ── Clipboard paste ──
def get_clipboard_image():
    """Get image from macOS clipboard, save to temp file, return path."""
    try:
        # Check if clipboard has image data
        r = subprocess.run(
            ["osascript", "-e", 'the clipboard as «class PNGf»'],
            capture_output=True, timeout=3
        )
        if r.returncode != 0:
            return None

        # Save clipboard image via Python
        tmp = os.path.join(CWD, ".localcoder-clipboard.png")
        subprocess.run(
            ["osascript", "-e", f'set f to open for access POSIX file "{tmp}" with write permission',
             "-e", 'set eof f to 0',
             "-e", 'write (the clipboard as «class PNGf») to f',
             "-e", 'close access f'],
            capture_output=True, timeout=5
        )
        if os.path.isfile(tmp) and os.path.getsize(tmp) > 100:
            return tmp
    except:
        pass
    return None

# ── Tools ──
TOOLS = [
    {"type":"function","function":{"name":"bash","description":"Run any shell command.","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}},
    {"type":"function","function":{"name":"write_file","description":"Create or overwrite a file.","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
    {"type":"function","function":{"name":"read_file","description":"Read a file.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
    {"type":"function","function":{"name":"edit_file","description":"Find and replace text in a file.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_text":{"type":"string"},"new_text":{"type":"string"}},"required":["path","old_text","new_text"]}}},
    {"type":"function","function":{"name":"web_search","description":"Search the web via DuckDuckGo.","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}},
    {"type":"function","function":{"name":"fetch_url","description":"Fetch a URL and return status + body.","parameters":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}}},
    {"type":"function","function":{"name":"read_pdf","description":"Read a PDF file. Extracts text and renders pages as images for visual understanding of charts, diagrams, layouts. Use for any PDF.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Path to the PDF file"},"pages":{"type":"string","description":"Page range: 'all', '1', '1-3', '2,5,8'. Default: first 5 pages."}},"required":["path"]}}},
    {"type":"function","function":{"name":"computer_use","description":"Control the Mac GUI. Every action automatically takes a screenshot and reads the screen content. Actions: scroll (SCROLL PAGE DOWN to see more content), click:x,y (click at coordinates 0-1000), type:text, key:name, hotkey:cmd+key, open:URL_or_AppName, wait:seconds. IMPORTANT: Use 'scroll' to see more content on a page. Do NOT call 'screenshot' repeatedly.","parameters":{"type":"object","properties":{"action":{"type":"string","description":"Action: 'scroll' (page down), 'scroll up', 'click:500,300', 'type:hello world', 'key:return', 'hotkey:cmd+a', 'open:https://x.com/search?q=AI', 'open:WhatsApp', 'wait:2'"}},"required":["action"]}}},
]

SNAPSHOT_DIR = os.path.join(CWD, ".localcoder-snapshots")

_last_snapshot = {}
def snapshot_file(path):
    """Save a backup before modifying an existing file. Dedupes within 30s."""
    full = os.path.join(CWD, path) if not os.path.isabs(path) else path
    if not os.path.isfile(full):
        return None
    # Don't snapshot the same file within 30 seconds
    now = time.time()
    if path in _last_snapshot and now - _last_snapshot[path] < 30:
        return None
    _last_snapshot[path] = now
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = path.replace("/", "__").replace("\\", "__")
    snap_path = os.path.join(SNAPSHOT_DIR, f"{ts}__{safe_name}")
    try:
        import shutil
        shutil.copy2(full, snap_path)
        logging.getLogger("localcoder").info(f"Snapshot: {path} → {snap_path}")
        # Clean old snapshots — keep max 20 per file
        all_snaps = sorted([s for s in os.listdir(SNAPSHOT_DIR) if safe_name in s])
        for old in all_snaps[:-20]:
            os.remove(os.path.join(SNAPSHOT_DIR, old))
        return snap_path
    except:
        return None

def list_snapshots(path=None):
    """List all snapshots, optionally filtered by filename."""
    if not os.path.isdir(SNAPSHOT_DIR):
        return "No snapshots yet."
    snaps = sorted(os.listdir(SNAPSHOT_DIR), reverse=True)
    if path:
        safe = path.replace("/", "__").replace("\\", "__")
        snaps = [s for s in snaps if safe in s]
    if not snaps:
        return "No snapshots found."
    lines = []
    for i, s in enumerate(snaps[:15]):
        parts = s.split("__", 2)
        ts = parts[0] if parts else "?"
        fname = parts[-1] if len(parts) > 1 else s
        fp = os.path.join(SNAPSHOT_DIR, s)
        size = os.path.getsize(fp)
        lines.append(f"  [{i}] {ts} — {fname} ({size} bytes)")
    return "Snapshots:\n" + "\n".join(lines)

def restore_snapshot(index=0, path=None):
    """Restore a file from a snapshot."""
    if not os.path.isdir(SNAPSHOT_DIR):
        return "No snapshots."
    snaps = sorted(os.listdir(SNAPSHOT_DIR), reverse=True)
    if path:
        safe = path.replace("/", "__").replace("\\", "__")
        snaps = [s for s in snaps if safe in s]
    if not snaps or index >= len(snaps):
        return "Snapshot not found."
    snap = snaps[index]
    parts = snap.split("__", 2)
    orig_name = parts[-1] if len(parts) > 1 else snap
    orig_path = orig_name.replace("__", "/")
    full = os.path.join(CWD, orig_path)
    snap_path = os.path.join(SNAPSHOT_DIR, snap)
    try:
        import shutil
        shutil.copy2(snap_path, full)
        return f"Restored {orig_path} from snapshot {parts[0]}"
    except Exception as e:
        return f"Restore failed: {e}"

def exec_tool(name, args):
    if name == "bash":
        cmd = args.get("command", "")
        # If downloading an image with curl, add browser user-agent to avoid blocks
        if "curl" in cmd and any(ext in cmd for ext in ('.png', '.jpg', '.jpeg', '.webp', '.gif')):
            if '-A' not in cmd and '--user-agent' not in cmd and '-H' not in cmd:
                cmd = cmd.replace("curl ", 'curl -L -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" ', 1)
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=CWD, timeout=60)
            return (r.stdout + r.stderr).strip()[:4000] or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command started (timeout normal for servers)."
    elif name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        snapshot_file(path)  # backup before overwrite
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        lines = content.count('\n') + 1
        # If writing an image/binary, note the path for display
        if any(path.lower().endswith(e) for e in ('.png', '.jpg', '.jpeg', '.webp', '.gif')):
            return f"IMAGE:{full}|Written: {path} ({len(content)} bytes)"
        return f"Written: {path} ({lines} lines, {len(content)} chars)"
    elif name == "read_file":
        path = args.get("path", "")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        with open(full) as f:
            content = f.read()
        # Strip HTML for .html files to save context
        if path.endswith('.html') and '<html' in content[:200].lower():
            text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:3000]
        return content[:5000]
    elif name == "edit_file":
        path = args.get("path", "")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        with open(full) as f:
            content = f.read()
        old = args.get("old_text", "")
        if old not in content:
            return "Error: old_text not found"
        snapshot_file(path)  # backup before edit
        new_content = content.replace(old, args.get("new_text", ""), 1)
        with open(full, "w") as f:
            f.write(new_content)
        # Show diff summary
        old_lines = content.count('\n')
        new_lines = new_content.count('\n')
        diff = new_lines - old_lines
        diff_str = f" ({'+' if diff > 0 else ''}{diff} lines)" if diff != 0 else ""
        return f"Edited: {path}{diff_str}"
    elif name == "fetch_url":
        url = args.get("url", "")
        try:
            # Auto-detect image URLs — download and display directly
            if any(url.lower().endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.webp', '.gif')):
                img_name = os.path.basename(url.split("?")[0])[:50] or "image.jpg"
                img_path = os.path.join(CWD, img_name)
                try:
                    dl = subprocess.run(
                        ["curl", "-fsSL", "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36", "-o", img_path, url],
                        capture_output=True, timeout=15
                    )
                    if os.path.isfile(img_path) and os.path.getsize(img_path) > 500:
                        # Validate magic bytes
                        with open(img_path, 'rb') as _f:
                            hdr = _f.read(8)
                        if hdr[:2] == b'\xff\xd8' or hdr[:4] == b'\x89PNG' or hdr[:4] == b'GIF8' or hdr[:4] == b'RIFF':
                            show_image_inline(img_path)
                            sz = os.path.getsize(img_path) // 1024
                            return f"Image downloaded and displayed: {img_name} ({sz} KB)\nSaved to: {img_path}"
                        else:
                            os.unlink(img_path)
                            return f"Downloaded file is not a valid image (server returned HTML). Try a different URL."
                except:
                    pass

            # Use Jina Reader — reads full page, renders JS, returns markdown
            jina_url = f"https://r.jina.ai/{url}"
            req = urllib.request.Request(jina_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/plain"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    full = resp.read().decode("utf-8", errors="replace")
                    if len(full) > 100:
                        # Extract images from full content
                        imgs = re.findall(r'!\[.*?\]\((https?://[^\)]+)\)', full)
                        imgs += re.findall(r'(https?://[^\s\)\"]+\.(?:png|jpg|jpeg|webp|gif))', full)
                        good = list(dict.fromkeys(i for i in imgs if 'nav__' not in i and 'icon' not in i.lower()))
                        parts = [f"Status: 200 (via Jina Reader) · {len(full)} chars total"]
                        # Put images FIRST so they don't get truncated
                        if good:
                            parts.append(f"\n--- {len(good)} images found on this page ---")
                            parts.extend(good[:8])
                            parts.append("--- end images ---\n")
                        parts.append(full[:1500])
                        return "\n".join(parts)
            except:
                pass

            # Direct fallback
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ct = resp.headers.get('Content-Type', '')
                raw = resp.read(10000).decode("utf-8", errors="replace")
                if 'image' in ct:
                    return f"Status: {resp.status} · This is an image file ({ct}, {len(raw)} bytes). Use bash with 'curl -o filename.png {url}' to download it."
                if 'html' in ct:
                    og = re.findall(r'(?:property|name)="(?:og|twitter):image"[^>]*content="([^"]+)"', raw)
                    imgs = re.findall(r'https?://[^\s"\'<>]+\.(?:png|jpg|jpeg|webp|gif)', raw)
                    all_imgs = list(dict.fromkeys(og + imgs))
                    text = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL)
                    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                    text = re.sub(r'<[^>]+>', ' ', text)
                    text = re.sub(r'\s+', ' ', text).strip()
                    img_str = "\n\nImages:\n" + "\n".join(all_imgs[:5]) if all_imgs else ""
                    return f"Status: {resp.status}\n{text[:1500]}{img_str}"
                return f"Status: {resp.status}\nType: {ct}\n{raw[:1500]}"
        except Exception as e:
            return f"Error: {e}"
    elif name == "web_search":
        query = args.get("query", "")
        is_image_query = any(w in query.lower() for w in ['image', 'logo', 'photo', 'screenshot', 'picture', 'png', 'jpg', 'icon', 'wallpaper', 'unsplash'])

        # Image search via ddgs package
        if is_image_query:
            try:
                from ddgs import DDGS
                results = DDGS().images(query.replace('image', '').replace('unsplash', '').strip(), max_results=5)
                imgs = []
                for r in results:
                    imgs.append(f"- {r.get('title','')[:60]}\n  URL: {r.get('image','')}\n  Thumbnail: {r.get('thumbnail','')}\n  Source: {r.get('url','')}")
                if imgs:
                    first_url = results[0].get('image', '') if results else ''
                    if first_url:
                        try:
                            show_image_url(first_url, max_width=50, max_height=12)
                        except Exception:
                            pass
                    return f"Image search results for '{query}':\n\n" + "\n\n".join(imgs)
            except ImportError:
                pass  # ddgs not installed, try unsplash fallback
            except Exception:
                pass

            # Unsplash fallback (no API key, direct source URLs)
            try:
                kw = urllib.parse.quote(query.replace('image', '').replace('unsplash', '').strip())
                imgs = []
                for i in range(5):
                    url = f"https://source.unsplash.com/random/800x600/?{kw}&sig={i}"
                    imgs.append(f"- Unsplash image {i+1}\n  URL: {url}")
                return f"Unsplash images for '{query}':\n\n" + "\n\n".join(imgs) + "\n\nUse these URLs directly in <img src='URL'> tags."
            except Exception:
                pass

        # Regular web search via ddgs package
        try:
            from ddgs import DDGS
            results = DDGS().text(query, max_results=5)
            formatted = []
            for r in results:
                formatted.append(f"[{r.get('title','')}]({r.get('href','')})\n{r.get('body','')[:150]}")
            return f"Search results for '{query}':\n\n" + "\n\n".join(formatted) if formatted else f"No results for '{query}'"
        except ImportError:
            return f"Search requires 'ddgs' package. Install: pip install ddgs"
        except Exception as e:
            return f"Search error: {e}"
    elif name == "computer_use":
        action = args.get("action", "screenshot")
        import base64 as _b64

        try:
            # Execute action FIRST (before screenshot)
            action_result = ""
            # Track last opened app for screenshot focus
            if not hasattr(exec_tool, '_last_app'):
                exec_tool._last_app = "Google Chrome"

            if action == "screenshot":
                # Auto-convert repeated screenshots to scroll
                if not hasattr(exec_tool, '_screenshot_count'):
                    exec_tool._screenshot_count = 0
                exec_tool._screenshot_count += 1
                if exec_tool._screenshot_count > 1:
                    action = "scroll"  # force scroll instead of redundant screenshot
                    console.print(f"  [dim yellow]Auto-scrolling instead of repeated screenshot[/]")

            if action == "screenshot" or action.startswith("scroll"):
                # Bring last app to front
                subprocess.run(["osascript", "-e", f'tell application "{exec_tool._last_app}" to activate'],
                    capture_output=True, timeout=3)
                time.sleep(0.3)
                if action.startswith("scroll"):
                    direction = "down"
                    if "up" in action:
                        direction = "up"
                    # Click content area to ensure page has focus
                    subprocess.run(["cliclick", "c:400,500"], capture_output=True, timeout=3)
                    time.sleep(0.2)
                    # Use space bar for scroll down (works in all browsers)
                    # Use shift+space for scroll up
                    # 3 space presses for a full page scroll
                    for _ in range(3):
                        if direction == "down":
                            subprocess.run(["osascript", "-e",
                                'tell application "System Events" to keystroke space'],
                                capture_output=True, timeout=3)
                        else:
                            subprocess.run(["osascript", "-e",
                                'tell application "System Events" to keystroke space using shift down'],
                                capture_output=True, timeout=3)
                        time.sleep(0.3)
                    time.sleep(0.5)
                    action_result = f"Scrolled {direction}"
                else:
                    action_result = "Screenshot taken (see below)"
            elif action.startswith("click:"):
                exec_tool._screenshot_count = 0  # reset on non-screenshot action
                coords = action.split(":", 1)[1]
                x, y = int(coords.split(",")[0].strip()), int(coords.split(",")[1].strip())
                # Convert from 1000x1000 normalized to actual screen coordinates
                # Get screen size
                try:
                    _scr = subprocess.run(["osascript", "-e",
                        'tell application "Finder" to get bounds of window of desktop'],
                        capture_output=True, text=True, timeout=3)
                    _parts = _scr.stdout.strip().split(", ")
                    scr_w, scr_h = int(_parts[2]), int(_parts[3])
                except:
                    scr_w, scr_h = 1512, 982  # fallback M4 Pro
                sx = int(x * scr_w / 1000)
                sy = int(y * scr_h / 1000)
                # Bring last app to front before clicking
                subprocess.run(["osascript", "-e", f'tell application "{exec_tool._last_app}" to activate'],
                    capture_output=True, timeout=3)
                time.sleep(0.2)
                subprocess.run(["cliclick", f"c:{sx},{sy}"], capture_output=True, timeout=3)
                action_result = f"Clicked at screen ({sx},{sy}) from bbox ({x},{y})"
                time.sleep(0.3)
            elif action.startswith("type:"):
                text = action.split(":", 1)[1]
                subprocess.run(["osascript", "-e",
                    f'tell application "System Events" to keystroke "{text}"'],
                    capture_output=True, timeout=3)
                action_result = f"Typed: {text}"
                time.sleep(0.3)
            elif action.startswith("key:"):
                key = action.split(":", 1)[1].strip()
                # Normalize key names: underscore→hyphen, common aliases
                key = key.replace("_", "-").replace("escape", "esc").replace("enter", "return")
                # Map key names to AppleScript key codes
                _key_map = {"return": 36, "esc": 53, "tab": 48, "delete": 51, "space": 49,
                            "arrow-up": 126, "arrow-down": 125, "arrow-left": 123, "arrow-right": 124,
                            "page-down": 121, "page-up": 116, "home": 115, "end": 119, "enter": 36}
                kc = _key_map.get(key)
                if kc:
                    subprocess.run(["osascript", "-e",
                        f'tell application "System Events" to key code {kc}'],
                        capture_output=True, timeout=3)
                else:
                    # Single character key
                    subprocess.run(["osascript", "-e",
                        f'tell application "System Events" to keystroke "{key}"'],
                        capture_output=True, timeout=3)
                action_result = f"Pressed key: {key}"
                time.sleep(0.3)
            elif action.startswith("hotkey:"):
                keys = action.split(":", 1)[1].strip()
                # Convert cmd+a to osascript
                parts = keys.split("+")
                key_char = parts[-1]
                modifiers = [p for p in parts[:-1]]
                mod_str = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
                subprocess.run(["osascript", "-e",
                    f'tell application "System Events" to keystroke "{key_char}"{mod_str}'],
                    capture_output=True, timeout=5)
                action_result = f"Hotkey: {keys}"
                time.sleep(0.3)
            elif action.startswith("open:"):
                target = action.split(":", 1)[1].strip()
                if target.startswith("http") or "." in target and "/" in target:
                    # URL — open in Chrome specifically
                    url = target if target.startswith("http") else f"https://{target}"
                    subprocess.run(["open", "-a", "Google Chrome", url], capture_output=True, timeout=5)
                    action_result = f"Opened {url} in Chrome"
                else:
                    # App name
                    subprocess.run(["open", "-a", target], capture_output=True, timeout=5)
                    action_result = f"Opened {target}"
                time.sleep(2)
                # Track and bring to front
                app_name = "Google Chrome" if ("http" in target or "." in target) else target
                exec_tool._last_app = app_name
                subprocess.run(["osascript", "-e", f'tell application "{app_name}" to activate'],
                    capture_output=True, timeout=3)
                time.sleep(0.5)
            elif action.startswith("wait:"):
                secs = float(action.split(":", 1)[1])
                time.sleep(min(secs, 5))
                action_result = f"Waited {secs}s"
            else:
                action_result = f"Unknown action: {action}"

            # ADK pattern: EVERY action captures current_state (screenshot)
            # Bring target app to front before screenshotting
            subprocess.run(["osascript", "-e", f'tell application "{exec_tool._last_app}" to activate'],
                capture_output=True, timeout=3)
            time.sleep(0.5)

            ss_path = "/tmp/localcoder-screen.png"
            subprocess.run(["screencapture", "-x", ss_path], capture_output=True, timeout=5)
            subprocess.run(["sips", "-Z", "1000", ss_path, "--out", ss_path],
                capture_output=True, timeout=5)

            # Display inline
            show_image_inline(ss_path)

            # Auto-read the screenshot content (like ADK's current_state)
            screen_desc = ""
            try:
                img_data = _b64.b64encode(open(ss_path, "rb").read()).decode()
                vision_payload = json.dumps({
                    "model": MODEL,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "Extract the MAIN CONTENT from this screenshot. Ignore browser chrome/menus. Focus on: posts, tweets, articles, chat messages, search results, form data. Report as structured data."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_data}"}}
                    ]}],
                    "max_tokens": 1500
                }).encode()
                vision_req = urllib.request.Request(f"{API_BASE}/chat/completions",
                    data=vision_payload, headers={"Content-Type": "application/json"})
                vision_resp = urllib.request.urlopen(vision_req, timeout=90)
                vision_r = json.loads(vision_resp.read())
                vision_msg = vision_r["choices"][0]["message"]
                screen_desc = vision_msg.get("content", "") or vision_msg.get("reasoning_content", "")
                # Strip reasoning preamble — find where actual data starts
                for marker in ["```", "{", "**", "1.", "- ", "•", "Post", "Tweet", "@", "Author"]:
                    idx = screen_desc.find(marker)
                    if idx > 0 and idx < 300:
                        screen_desc = screen_desc[idx:]
                        break
                # Remove common preamble sentences
                for phrase in ["I need to extract", "The user wants", "Let me analyze",
                               "The screenshot shows", "I will now", "Ignore browser"]:
                    if screen_desc.lstrip().startswith(phrase):
                        nl = screen_desc.find("\n\n")
                        if nl > 0:
                            screen_desc = screen_desc[nl+2:]
                screen_desc = screen_desc[:2000]
            except:
                screen_desc = "(could not read screen)"

            return f"{action_result}\n\n[SCREEN CONTENT]:\n{screen_desc}\n\n[NEXT ACTION]: To see MORE content, call computer_use with action:scroll. To click something, use action:click:x,y (0-1000 coords). To finish, describe what you found. Do NOT call action:screenshot again — every action already captures the screen."

        except Exception as e:
            return f"Computer use error: {e}"

    elif name == "read_pdf":
        path = args.get("path", "")
        full = os.path.join(CWD, path) if not os.path.isabs(path) else path
        if not os.path.isfile(full):
            return f"Error: PDF not found: {full}"

        pages_arg = args.get("pages", "1-5")
        tmp_dir = os.path.join(CWD, ".localcoder-pdf-tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        import shutil as _shutil

        try:
            # Get page count
            info = subprocess.run(["pdfinfo", full], capture_output=True, text=True, timeout=5)
            total_pages = 0
            for line in info.stdout.split("\n"):
                if line.startswith("Pages:"):
                    total_pages = int(line.split(":")[1].strip())
                    break

            # Parse page range
            if pages_arg == "all":
                page_list = list(range(1, min(total_pages + 1, 21)))  # cap at 20
            elif "-" in pages_arg:
                start, end = pages_arg.split("-")
                page_list = list(range(int(start), min(int(end) + 1, total_pages + 1)))
            elif "," in pages_arg:
                page_list = [int(p) for p in pages_arg.split(",")]
            else:
                page_list = [int(pages_arg)]

            # Extract text
            text_result = subprocess.run(
                ["pdftotext", "-f", str(page_list[0]), "-l", str(page_list[-1]), full, "-"],
                capture_output=True, text=True, timeout=15
            )
            text_content = text_result.stdout[:3000]

            # Convert pages to images for vision
            page_images = []
            for pg in page_list[:5]:  # max 5 page images
                img_prefix = os.path.join(tmp_dir, f"page_{pg}")
                subprocess.run(
                    ["pdftoppm", "-f", str(pg), "-l", str(pg), "-r", "150", "-png", full, img_prefix],
                    capture_output=True, timeout=10
                )
                # pdftoppm outputs page_N-01.png
                for f in os.listdir(tmp_dir):
                    if f.startswith(f"page_{pg}") and f.endswith(".png"):
                        img_path = os.path.join(tmp_dir, f)
                        page_images.append(img_path)
                        # Display inline
                        show_image_inline(img_path)
                        break

            # Build result with image references
            result_parts = [
                f"PDF: {os.path.basename(full)} ({total_pages} pages)",
                f"Showing pages: {','.join(str(p) for p in page_list)}",
                f"\n--- TEXT CONTENT ---\n{text_content}",
            ]
            if page_images:
                result_parts.append(f"\n--- {len(page_images)} page images rendered (displayed inline in terminal) ---")
                # Include base64 so vision models can see the pages
                import base64 as _b64
                for img in page_images:
                    try:
                        img_b64 = _b64.b64encode(open(img, "rb").read()).decode()
                        sz_kb = os.path.getsize(img) // 1024
                        result_parts.append(f"[Image: {os.path.basename(img)} ({sz_kb}KB) — base64 attached for vision]")
                    except:
                        result_parts.append(f"Page image: {img}")

            return "\n".join(result_parts)

        except Exception as e:
            return f"Error reading PDF: {e}"
        finally:
            # Cleanup old tmp files (keep last 5 mins)
            try:
                import time as _time
                for f in os.listdir(tmp_dir):
                    fp = os.path.join(tmp_dir, f)
                    if _time.time() - os.path.getmtime(fp) > 300:
                        os.unlink(fp)
            except:
                pass

    return f"Unknown tool: {name}"

def estimate_tokens(text):
    return len(str(text)) // 4

def summarize_tool_result(content, fname):
    """Smart truncation based on tool type"""
    if not content or len(content) < 300:
        return content
    # Bash: keep first/last lines (errors are usually at the end)
    if fname == "bash":
        lines = content.split('\n')
        if len(lines) > 8:
            return '\n'.join(lines[:4]) + f'\n... ({len(lines)-8} lines omitted) ...\n' + '\n'.join(lines[-4:])
        return content[:600]
    # Search: keep first 3 results only
    if fname == "web_search":
        results = content.split('\n\n')
        return '\n\n'.join(results[:4])[:600]
    # File reads: keep first chunk
    if fname == "read_file":
        return content[:500] + "...(truncated)" if len(content) > 500 else content
    # Everything else
    return content[:400] + "...(truncated)" if len(content) > 400 else content

def compress_messages(messages, max_tokens=12000):
    """Claude-style context management:
    1. Summarize tool results (keep structure, drop bulk)
    2. Collapse old tool call pairs into summaries
    3. Always keep: system, last user msg, recent context
    """
    if not messages:
        return messages

    system = messages[0]
    rest = messages[1:]

    # Pass 1: Truncate all tool results
    for i, msg in enumerate(rest):
        if isinstance(msg, dict) and msg.get("role") == "tool":
            # Find which tool generated this
            fname = "unknown"
            for j in range(i-1, -1, -1):
                prev = rest[j]
                if hasattr(prev, 'tool_calls') or (isinstance(prev, dict) and prev.get("tool_calls")):
                    tc = prev.get("tool_calls", []) if isinstance(prev, dict) else prev.tool_calls
                    if tc:
                        fname = tc[0].get("function", {}).get("name", "") if isinstance(tc[0], dict) else tc[0].function.name
                    break
            msg["content"] = summarize_tool_result(msg.get("content", ""), fname)

    total = estimate_tokens(json.dumps([system] + rest))
    if total <= max_tokens:
        return [system] + rest

    # Pass 2: Collapse old assistant+tool pairs into summaries
    # Keep last 4 messages intact, summarize the rest
    if len(rest) > 6:
        keep = rest[-6:]
        old = rest[:-6]

        # Summarize old turns into a single context message
        summary_parts = []
        for msg in old:
            if isinstance(msg, dict):
                role = msg.get("role", "")
                if role == "user":
                    summary_parts.append(f"User asked: {msg.get('content', '')[:80]}")
                elif role == "assistant" and msg.get("content"):
                    summary_parts.append(f"Agent responded: {msg['content'][:80]}")
                elif role == "tool":
                    summary_parts.append(f"Tool returned: {msg.get('content', '')[:40]}")

        if summary_parts:
            summary = {"role": "user", "content": f"[Earlier in this conversation: {'; '.join(summary_parts[:5])}]"}
            rest = [summary] + keep
        else:
            rest = keep

    total = estimate_tokens(json.dumps([system] + rest))
    if total <= max_tokens:
        return [system] + rest

    # Pass 3: Emergency — keep only system + last 3
    return [system] + rest[-3:]

def chat_api(messages, spinner=None):
    """Call the LLM API with streaming.

    Streams text content live to console, accumulates tool calls.
    Returns a compatible response dict for agent_loop.
    """
    before = len(messages)
    messages = compress_messages(messages)
    tokens_est = estimate_tokens(json.dumps(messages))
    if before != len(messages):
        logging.getLogger("localcoder").info(f"Compressed {before} → {len(messages)} msgs (~{tokens_est} tokens)")
    else:
        logging.getLogger("localcoder").debug(f"API call: {len(messages)} msgs, ~{tokens_est} tokens")

    body = {
        "model": MODEL, "messages": messages, "tools": TOOLS,
        "temperature": 1.0, "top_p": 0.95, "stream": True,
    }
    if REASONING_EFFORT != "medium":
        body["reasoning_effort"] = REASONING_EFFORT
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )

    content_parts = []
    reasoning_parts = []
    tool_calls = {}  # index → {id, function: {name, arguments}}
    finish_reason = None
    usage = {}
    timings = {}
    model_name = MODEL
    streaming_started = False
    reasoning_started = False
    token_count = 0

    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if line == "data: [DONE]":
                break
            if not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})
            finish_reason = chunk.get("choices", [{}])[0].get("finish_reason") or finish_reason
            model_name = chunk.get("model", model_name)

            # Usage info (llama.cpp sends it in the last chunk)
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("timings"):
                timings = chunk["timings"]

            def _kill_spinner():
                """Fully stop the spinner and clear its terminal line."""
                nonlocal spinner
                if spinner:
                    try:
                        if spinner._live is not None:
                            spinner._live.stop()
                            spinner._live = None
                    except Exception:
                        pass
                    spinner = None
                    # Clear the spinner line and move cursor
                    sys.stdout.write("\r\033[K")
                    sys.stdout.flush()

            # Stream reasoning content (dimmed, hidden when effort=none)
            reasoning_chunk = delta.get("reasoning_content", "")
            if reasoning_chunk:
                reasoning_parts.append(reasoning_chunk)
                token_count += 1
                if REASONING_EFFORT != "none":
                    if not reasoning_started:
                        reasoning_started = True
                        _kill_spinner()
                        sys.stdout.write("  \033[2;35m┌─ thinking ─────────────────────\033[0m\n  \033[2;35m│ \033[0m\033[2m")
                        sys.stdout.flush()
                    sys.stdout.write(f"\033[2m{reasoning_chunk}\033[0m")
                    sys.stdout.flush()

            # Stream text content live (normal style)
            text_chunk = delta.get("content", "")
            if text_chunk:
                if not streaming_started:
                    streaming_started = True
                    if reasoning_started:
                        # End reasoning block, start content
                        sys.stdout.write("\033[0m\n  \033[2;35m└────────────────────────────────\033[0m\n\n  ")
                    else:
                        _kill_spinner()
                        sys.stdout.write("  ")
                    sys.stdout.flush()
                sys.stdout.write(text_chunk)
                sys.stdout.flush()
                content_parts.append(text_chunk)
                token_count += 1

            # Accumulate tool calls
            for tc_delta in delta.get("tool_calls", []):
                idx = tc_delta.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        "id": tc_delta.get("id", f"call_{idx}"),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc_delta.get("function", {}).get("name"):
                    tool_calls[idx]["function"]["name"] = tc_delta["function"]["name"]
                if tc_delta.get("function", {}).get("arguments"):
                    tool_calls[idx]["function"]["arguments"] += tc_delta["function"]["arguments"]

    if streaming_started:
        sys.stdout.write("\n")
        sys.stdout.flush()

    # Build compatible response dict
    content = "".join(content_parts)
    reasoning = "".join(reasoning_parts)
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls.keys())]

    if not usage:
        usage = {"completion_tokens": token_count, "prompt_tokens": 0, "total_tokens": token_count}

    tps = timings.get("predicted_per_second", 0)
    logging.getLogger("localcoder").info(
        f"Response: {usage.get('completion_tokens',0)} tokens, {tps:.0f} tok/s, prompt={usage.get('prompt_tokens',0)}"
    )

    return {
        "choices": [{"message": msg, "finish_reason": finish_reason}],
        "usage": usage,
        "timings": timings,
        "model": model_name,
    }

# ── Rich display ──
def show_tool_call(fname, args):
    show_tool_animation(console, fname, args)

def show_image_inline(path):
    """Display image inline in terminal — auto-detects best method"""
    if not os.path.isfile(path):
        return
    timg = "/opt/homebrew/bin/timg"
    if os.path.exists(timg):
        try:
            # Use iTerm2 protocol for best quality, fall back to half-blocks
            proto = "i" if os.environ.get("TERM_PROGRAM", "").startswith("iTerm") else "h"
            subprocess.run([timg, "-g", "60x20", "-p", proto, path], timeout=5, cwd=CWD)
            console.print(f"  [dim green]📸 {os.path.basename(path)}[/]")
            return
        except:
            pass
    # Fallback: open in Preview
    subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    console.print(f"  [dim green]📸 Opened {os.path.basename(path)} in Preview[/]")

def show_result(result, tool_name=None):
    if not result or result == "(no output)":
        return

    # ── Search results: rich formatted cards ──
    if tool_name == "web_search" and "earch results for" in result:
        _show_search_results(result)
        return

    # ── Fetch URL: show status + preview ──
    if tool_name == "fetch_url" and result.startswith("Status:"):
        _show_fetch_result(result)
        return

    # ── Bash: styled output panel ──
    if tool_name == "bash":
        lines = result.split('\n')
        output = "\n".join(lines[:12])
        if len(lines) > 12:
            output += f"\n\033[2m… {len(lines)-12} more lines\033[0m"
        is_error = result.startswith("Error") or "error" in result[:100].lower() or "Traceback" in result[:100]
        border = "red" if is_error else "yellow"
        console.print(Panel(
            output, border_style=border, padding=(0, 1),
            title="[dim]output[/]" if not is_error else "[red]error[/]",
            title_align="left",
        ))
        _auto_preview_images(result)
        return

    # ── Read file: compact preview ──
    if tool_name == "read_file":
        lines = result.split('\n')
        output = "\n".join(lines[:10])
        if len(lines) > 10:
            output += f"\n… {len(lines)-10} more lines"
        console.print(Panel(output, border_style="blue", padding=(0, 1),
                            title="[dim]content[/]", title_align="left"))
        return

    # ── Default: truncated panel ──
    lines = result.split('\n')
    output = "\n".join(lines[:10])
    if len(lines) > 10:
        output += f"\n… ({len(lines)-10} more lines)"
    console.print(Panel(output, border_style="dim", padding=(0, 1)))

    # Auto-preview any image files mentioned in tool output
    _auto_preview_images(result)


def _show_search_results(result):
    """Render web search results as styled cards with clickable links."""
    # Parse header
    header_match = re.match(r"(?:Image s|S)earch results for '([^']*)':", result)
    query = header_match.group(1) if header_match else "search"

    # Split into individual results
    parts = result.split("\n\n")
    entries = [e for e in (parts[1:] if len(parts) > 1 else parts) if e.strip()]

    table = Table(
        show_header=False, show_edge=False, pad_edge=False,
        padding=(0, 1), expand=True, box=None,
    )
    table.add_column(ratio=1)

    for i, entry in enumerate(entries[:5]):
        entry = entry.strip()
        if not entry:
            continue

        # Parse markdown-style [title](url)\nsnippet
        md_match = re.match(r'\[([^\]]+)\]\(([^)]+)\)\n?(.*)', entry, re.DOTALL)
        if md_match:
            title, url, snippet = md_match.group(1), md_match.group(2), md_match.group(3).strip()
        else:
            # Image result format: "- Title\n  URL: ...\n  Source: ..."
            lines = entry.split('\n')
            title = lines[0].lstrip('- ').strip()
            url = ""
            snippet = ""
            for line in lines[1:]:
                if line.strip().startswith("URL:"):
                    url = line.split("URL:", 1)[1].strip()
                elif line.strip().startswith("Source:"):
                    snippet = line.split("Source:", 1)[1].strip()

        # Build styled entry
        row = Text()
        row.append(f"  {i+1}. ", style="bold cyan")
        row.append(title, style="bold white")
        row.append("\n")
        if url:
            # Shorten display URL
            display_url = url.replace("https://", "").replace("http://", "")
            if len(display_url) > 70:
                display_url = display_url[:67] + "..."
            row.append(f"     {display_url}", style="dim green")
            row.append("\n")
        if snippet:
            row.append(f"     {snippet[:120]}", style="dim")

        table.add_row(row)

    title_text = Text()
    title_text.append(" search ", style="bold magenta")
    title_text.append(f'"{query}"', style="bold white")
    title_text.append(f"  ({len(entries)} results)", style="dim")

    console.print(Panel(
        table,
        title=title_text, title_align="left",
        border_style="magenta",
        padding=(0, 0),
    ))


def _show_fetch_result(result):
    """Render fetch_url results with status and clean preview."""
    lines = result.split('\n')
    status_line = lines[0] if lines else ""

    # Extract status code
    status_match = re.search(r'Status:\s*(\d+)', status_line)
    status = status_match.group(1) if status_match else "?"
    status_style = "green" if status == "200" else "yellow"

    # Content preview
    content_lines = lines[1:]
    preview = "\n".join(content_lines[:8])
    if len(content_lines) > 8:
        preview += f"\n... ({len(content_lines)-8} more lines)"

    title_text = Text()
    title_text.append(" fetch ", style="bold blue")
    title_text.append(f"[{status}]", style=f"bold {status_style}")

    console.print(Panel(
        preview,
        title=title_text, title_align="left",
        border_style="blue",
        padding=(0, 1),
    ))

def show_response(text):
    """Render model response as markdown with proper formatting.
    Auto-detects image URLs and downloads+displays them inline."""
    if not text:
        return
    console.print()
    try:
        md = Markdown(text, code_theme="monokai")
        console.print(md, width=min(console.width - 4, 100))
    except:
        console.print(text)
    console.print()

    # Auto-detect and preview images from response
    _auto_preview_images(text)


def _auto_preview_images(text):
    """Detect image URLs and local file paths in text, preview them inline."""
    IMG_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.bmp')

    # 1. Image URLs — download and display
    img_urls = re.findall(r'(https?://[^\s\)\]\"\']+\.(?:png|jpg|jpeg|webp|gif))', text)
    for url in img_urls[:3]:
        try:
            img_name = os.path.basename(url.split("?")[0])[:40] or "image.jpg"
            img_path = os.path.join(CWD, img_name)
            if not os.path.exists(img_path):
                subprocess.run(
                    ["curl", "-fsSL", "-A", "Mozilla/5.0", "-o", img_path, url],
                    capture_output=True, timeout=10,
                )
            if os.path.isfile(img_path) and os.path.getsize(img_path) > 500:
                if _is_image_file(img_path):
                    show_image_inline(img_path)
        except Exception:
            pass

    # 2. Local file paths — detect and preview
    local_paths = re.findall(r'(?:^|\s)([/~][\w/.\-]+\.(?:png|jpg|jpeg|webp|gif|svg|bmp))', text)
    local_paths += re.findall(r'(?:^|\s)(\.\/[\w/.\-]+\.(?:png|jpg|jpeg|webp|gif|svg|bmp))', text)
    for path in local_paths[:3]:
        path = os.path.expanduser(path.strip())
        if not os.path.isabs(path):
            path = os.path.join(CWD, path)
        if os.path.isfile(path) and _is_image_file(path):
            show_image_inline(path)

    # 3. Files just created by write_file — check recent tool output
    # (handled by show_result already)


def _is_image_file(path):
    """Check file header to verify it's a real image."""
    try:
        with open(path, 'rb') as f:
            hdr = f.read(8)
        return (hdr[:2] == b'\xff\xd8' or hdr[:4] == b'\x89PNG' or
                hdr[:4] == b'GIF8' or hdr[:4] == b'RIFF' or
                b'<svg' in open(path, 'rb').read(200))
    except Exception:
        return False


def show_image_url(url, max_width=50, max_height=15):
    """Download and display an image URL inline in terminal."""
    try:
        img_name = os.path.basename(url.split("?")[0])[:30] or "preview.jpg"
        img_path = os.path.join("/tmp", f"localcoder-{img_name}")
        subprocess.run(
            ["curl", "-fsSL", "-A", "Mozilla/5.0", "-o", img_path, url],
            capture_output=True, timeout=10,
        )
        if os.path.isfile(img_path) and os.path.getsize(img_path) > 500:
            timg = "/opt/homebrew/bin/timg"
            if os.path.exists(timg):
                proto = "i" if os.environ.get("TERM_PROGRAM", "").startswith("iTerm") else "h"
                subprocess.run([timg, "-g", f"{max_width}x{max_height}", "-p", proto, img_path], timeout=5)
                return True
    except Exception:
        pass
    return False

# print_thinking is now handled by ThinkingSpinner from localcoder.localcoder_display

# ── Permissions ──
# ── Sandbox ──
class Sandbox:
    """Command-level sandbox. Default ON. Blocks destructive operations."""

    # Bash commands that are ALWAYS blocked in sandbox
    BLOCKED_CMDS = [
        "rm -rf", "rm -r", "rmdir", "mkfs", "dd if=",
        "sudo", "> /dev/", "chmod 777",
        "| sh", "| bash", "| zsh",  # pipe to shell
        "| python", "| perl", "| ruby",  # pipe to interpreter
        "eval ", "exec ",
        "ssh ", "scp ", "rsync ",
        "kill -9", "killall", "pkill",
        "launchctl", "defaults write",
        "networksetup", "osascript.*delete",
    ]

    # Paths that are NEVER writable in sandbox
    BLOCKED_PATHS = [
        "~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gcloud",
        "~/.env", "~/.bashrc", "~/.zshrc", "~/.profile",
        "~/.bash_profile", "~/.netrc", "~/.npmrc",
        "~/.pypirc", "~/.docker", "~/.kube",
        "/etc/", "/usr/", "/System/", "/Library/",
        "~/.localcoder/config.json",  # protect own config
    ]

    # Bash commands allowed in sandbox (read-only operations)
    SAFE_PREFIXES = [
        "ls", "cat", "head", "tail", "less", "more",
        "find", "grep", "rg", "ag", "fd",
        "wc", "sort", "uniq", "diff", "file", "stat",
        "git status", "git diff", "git log", "git show", "git blame",
        "git branch", "git remote", "git stash list",
        "echo", "printf", "which", "type", "man",
        "python3 -c", "node -e",  # allow one-liner execution
        "npm list", "pip list", "pip show",
        "curl -fsSL", "curl -sL", "curl -s",  # GET requests only
        "open ",  # open files/URLs
        "timg",  # image display
    ]

    @staticmethod
    def is_bash_allowed(cmd):
        """Check if a bash command is safe in sandbox mode."""
        cmd_lower = cmd.strip().lower()

        # Block dangerous commands
        for blocked in Sandbox.BLOCKED_CMDS:
            if blocked.lower() in cmd_lower:
                return False, f"Blocked: '{blocked}' not allowed in sandbox mode"

        # Block writing to protected paths
        for path in Sandbox.BLOCKED_PATHS:
            expanded = os.path.expanduser(path)
            if expanded in cmd or path in cmd:
                return False, f"Blocked: writing to '{path}' not allowed in sandbox mode"

        return True, ""

    @staticmethod
    def is_path_writable(path):
        """Check if a file path is writable in sandbox mode."""
        full = os.path.expanduser(path)
        # Resolve relative paths to CWD
        if not os.path.isabs(full):
            full = os.path.join(CWD, full)
        full = os.path.abspath(full)

        # Block protected paths
        for blocked in Sandbox.BLOCKED_PATHS:
            expanded = os.path.abspath(os.path.expanduser(blocked))
            if full.startswith(expanded):
                return False, f"Blocked: '{blocked}' is protected"

        # Must be within CWD or /tmp
        if not (full.startswith(CWD) or full.startswith("/tmp")):
            return False, f"Blocked: writes only allowed in project directory or /tmp"

        return True, ""


class Permissions:
    def __init__(self, mode="auto", sandbox=True):
        self.mode = mode
        self.sandbox = sandbox
        self.approved = set()
        self._load_approved()

    SAFE = {"read_file", "read_pdf", "web_search", "fetch_url"}

    def _config_path(self):
        return os.path.expanduser("~/.localcoder/approved_tools.json")

    def _load_approved(self):
        """Load previously approved tools from disk."""
        try:
            with open(self._config_path()) as f:
                saved = json.load(f)
            self.approved = set(saved.get("tools", []))
        except Exception:
            pass

    def _save_approved(self):
        """Save approved tools to disk for next session."""
        try:
            os.makedirs(os.path.dirname(self._config_path()), exist_ok=True)
            with open(self._config_path(), "w") as f:
                json.dump({"tools": list(self.approved)}, f)
        except Exception:
            pass

    def check(self, fname, args=None):
        """Check if a tool call is allowed. Returns True/False."""
        # Sandbox checks (before permission check)
        if self.sandbox:
            if fname == "bash" and args:
                cmd = args.get("command", "")
                allowed, reason = Sandbox.is_bash_allowed(cmd)
                if not allowed:
                    console.print(f"  [red]🛡 {reason}[/]")
                    console.print(f"  [dim]Run with --unrestricted to disable sandbox[/]")
                    return False

            if fname in ("write_file", "edit_file") and args:
                path = args.get("path", "")
                full = os.path.join(CWD, path) if not os.path.isabs(path) else path
                allowed, reason = Sandbox.is_path_writable(full)
                if not allowed:
                    console.print(f"  [red]🛡 {reason}[/]")
                    return False

            if fname == "computer_use":
                console.print(f"  [red]🛡 computer_use disabled in sandbox mode[/]")
                console.print(f"  [dim]Run with --unrestricted to enable[/]")
                return False

        # Permission modes
        if self.mode == "bypass" or fname in self.approved:
            return True
        if self.mode == "auto" and fname in self.SAFE:
            return True

        console.print(Panel(
            f"[bold yellow]Allow [white]{fname}[/white]?[/]  [dim]y[/]es · [dim]n[/]o · [dim]a[/]lways",
            border_style="yellow", padding=(0, 1),
        ))
        # Flush any leftover input from streaming
        import termios
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass
        try:
            ans = input("  ▸ ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans in ("a", "always"):
            self.approved.add(fname)
            self._save_approved()
            console.print(f"  [green]✓ {fname} — always approved (remembered)[/]")
            return True
        if ans in ("y", "yes"):
            console.print(f"  [green]✓ approved[/]")
            return True
        if ans == "":
            # Empty input — re-prompt, don't auto-approve
            console.print(f"  [dim]Type y, n, or a[/]")
            try:
                ans = input("  ▸ ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            if ans in ("y", "yes"):
                console.print(f"  [green]✓ approved[/]")
                return True
            if ans in ("a", "always"):
                self.approved.add(fname)
                self._save_approved()
                console.print(f"  [green]✓ {fname} — always approved (remembered)[/]")
                return True
        console.print(f"  [red]✗ denied[/]")
        return False

# ── Agent loop ──
def agent_loop(messages, perms):
    total_tokens = 0
    loop_start = time.time()
    spinner = ThinkingSpinner(console)
    recent_tools = []
    self_corrected = False
    for turn in range(25):
        spinner.start()
        spinner.update(tokens=total_tokens)
        try:
            resp = chat_api(messages, spinner=spinner)
        except urllib.error.URLError:
            spinner.stop()
            console.print("[bold red]  ✗ API timeout — context full. Auto-clearing old messages.[/]")
            if len(messages) > 3:
                messages[:] = [messages[0]] + messages[-2:]
                continue
            break
        except Exception as e:
            spinner.stop()
            console.print(f"[bold red]  ✗ {e}[/]")
            break
        finally:
            # Ensure spinner is always cleaned up before printing
            try:
                if spinner._live is not None:
                    spinner._live.stop()
                    spinner._live = None
            except Exception:
                pass

        choice = resp["choices"][0]
        msg = choice["message"]
        usage = resp.get("usage", {})
        timings = resp.get("timings", {})
        tps = timings.get("predicted_per_second", 0)
        total_tokens += usage.get("completion_tokens", 0)

        # Use reasoning_content as content when content is empty
        # (Gemma 4 thinking mode puts answers in reasoning)
        content_text = msg.get("content", "").strip()
        reasoning_text = msg.get("reasoning_content", "").strip()
        if not content_text and reasoning_text:
            content_text = reasoning_text
        content_text = re.sub(r'<\|?channel\|?>', '', content_text).strip()
        # Text was already streamed live by chat_api — only show via
        # markdown if it came from reasoning_content fallback
        if content_text and not msg.get("content", "").strip():
            show_response(content_text)

        if not msg.get("tool_calls"):
            elapsed = time.time() - loop_start
            if elapsed < 60:
                t = f"{elapsed:.0f}s"
            else:
                m, s = divmod(elapsed, 60)
                t = f"{m:.0f}m {s:.0f}s"
            console.print(f"\n  [dim]✦ {t} · {total_tokens} tokens · {tps:.0f} tok/s[/]")
            # Show context usage after completion
            ctx_str = BACKEND_INFO.get("ctx", "")
            if ctx_str:
                ctx_max = int(ctx_str.replace("K", "")) * 1024
                ctx_used = estimate_tokens(json.dumps(messages))
                context_usage_bar(console, ctx_used, ctx_max)
            break

        messages.append(msg)
        for tc in msg["tool_calls"]:
            fname = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except:
                args = {}

            # Loop detection — catch repeating patterns, then self-correct
            # Normalize: bash(cat file) counts as read_file(file)
            if fname == "bash" and args.get("command", "").startswith("cat "):
                tool_sig = f"read:{args['command'].split()[-1]}"
            elif fname == "read_file":
                tool_sig = f"read:{args.get('path','')}"
            elif fname == "fetch_url":
                tool_sig = f"fetch:{args.get('url','')[:50]}"
            else:
                tool_sig = f"{fname}:{json.dumps(args)[:60]}"
            recent_tools.append(tool_sig)
            # Track consecutive errors
            if not hasattr(agent_loop, '_error_count'):
                agent_loop._error_count = 0

            if len(recent_tools) >= 3:
                last3 = recent_tools[-3:]
                is_loop = False
                if last3[0] == last3[1] == last3[2]:
                    is_loop = True
                # Catch alternating reads of same file (cat/read_file flip)
                elif len(set(last3)) <= 2 and all(s.startswith("read:") for s in last3):
                    is_loop = True

                if is_loop:
                    if not self_corrected:
                        # First loop — force model to act
                        self_corrected = True
                        console.print("  [yellow]⚠ Loop detected — redirecting...[/]")
                        messages.append({"role": "tool", "tool_call_id": tc["id"],
                            "content": "LOOP DETECTED: You already have this data. STOP reading the same file. "
                                       "You have all the information you need. NOW ACT:\n"
                                       "1. If the user asked to build an app — START writing code with write_file\n"
                                       "2. If you need more data from a URL — use web_search or fetch_url\n"
                                       "3. If you need to run something — use bash\n"
                                       "DO NOT read the same file again. Use write_file to create the output NOW."})
                        recent_tools.clear()
                        continue
                    else:
                        # Second loop — auto-continue, don't block on user input
                        console.print("  [yellow]⚠ Still looping — forcing action...[/]")
                        messages.append({"role": "user",
                            "content": "You are stuck in a loop. STOP reading files. "
                                       "You already have all the content. "
                                       "START BUILDING NOW. Use write_file to create the output immediately."})
                        self_corrected = False
                        recent_tools.clear()
                        continue

            show_tool_call(fname, args)
            logging.getLogger("localcoder").info(f"Tool: {fname}({json.dumps(args)[:200]})")

            if not perms.check(fname, args):
                logging.getLogger("localcoder").info(f"Tool denied: {fname}")
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "Denied by user."})
                continue

            try:
                t0 = time.time()
                result = exec_tool(fname, args)
                logging.getLogger("localcoder").info(f"Tool result: {fname} → {len(result)} chars in {time.time()-t0:.1f}s")
            except Exception as e:
                result = f"Error: {e}"
                logging.getLogger("localcoder").error(f"Tool error: {fname} → {e}")
                console.print(f"  [bold red]✗ {e}[/]")

            show_result(result, fname)

            # Computer use results already include screen content from vision extraction
            # (built into the tool itself, ADK-style)

            # Detect repeated errors — if 3+ consecutive bash errors, tell model to web_search
            if fname == "bash" and result and ("error" in result.lower() or "Error" in result):
                agent_loop._error_count += 1
                if agent_loop._error_count >= 3:
                    console.print("  [yellow]⚠ 3 consecutive errors — suggesting web search...[/]")
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result[:1500]})
                    messages.append({"role": "user",
                        "content": "You've had 3 consecutive errors with this approach. "
                                   "STOP trying variations of the same command. "
                                   "Use web_search to find the correct way to do this on macOS. "
                                   "Search for the specific error message or task."})
                    agent_loop._error_count = 0
                    recent_tools.clear()
                    continue
            else:
                agent_loop._error_count = 0

            # Auto-show: if bash just created/downloaded an image, verify and display it
            if fname == "bash":
                now = time.time()
                candidates = []
                for fn in os.listdir(CWD):
                    if any(fn.lower().endswith(e) for e in ('.png', '.jpg', '.jpeg', '.webp', '.gif')):
                        fp = os.path.join(CWD, fn)
                        if os.path.getmtime(fp) > now - 5:
                            candidates.append((os.path.getmtime(fp), fp, fn))
                if candidates:
                    candidates.sort(reverse=True)
                    _, fp, fn = candidates[0]
                    try:
                        with open(fp, 'rb') as img_f:
                            header = img_f.read(16)
                        is_image = (header[:8] == b'\x89PNG\r\n\x1a\n' or
                                    header[:2] == b'\xff\xd8' or
                                    header[:4] == b'GIF8' or
                                    header[:4] == b'RIFF')
                        if is_image:
                            show_image_inline(fp)
                            result += "\n[IMAGE DISPLAYED INLINE IN TERMINAL]"
                        else:
                            console.print(f"  [red]⚠ {fn} is not a valid image (server returned HTML). Try a different URL.[/]")
                            result += f"\n[ERROR: Downloaded file {fn} is NOT an image. The server returned HTML. Try a completely different image source — avoid wikimedia SVG thumbnails.]"
                    except Exception as img_err:
                        logging.getLogger("localcoder").error(f"Image check error: {img_err}")

            # Keep more for fetch_url (has images), less for others
            max_len = 5000 if fname == "read_pdf" else 2500 if fname in ("fetch_url", "web_search") else 1500
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)[:max_len]})

        tokens = usage.get("completion_tokens", 0) if usage else 0
        stats = Text()
        stats.append("  ")
        if tokens:
            stats.append(f"{tokens} tokens", style="dim")
            stats.append(" · ", style="dim")
        if tps > 0:
            stats.append(f"{tps:.0f} tok/s", style="dim cyan")
        console.print(stats)

    return total_tokens

# ── Banner ──
def _model_label():
    bi = BACKEND_INFO
    name = f"Gemma 4 {bi['size']}" if bi["size"] else bi["model_name"]
    quant = f" {bi['quant']}" if bi["quant"] else ""
    return f"{name}{quant}"

LOGO_TEXT = [
    ("[bold #e07a5f]██╗      ██████╗  ██████╗ █████╗ ██╗     [/]",),
    ("[bold #d4725a]██║     ██╔═══██╗██╔════╝██╔══██╗██║     [/]",),
    ("[bold #c96a55]██║     ██║   ██║██║     ███████║██║     [/]",),
    ("[bold #be6250]██║     ██║   ██║██║     ██╔══██║██║     [/]",),
    ("[bold #b35a4b]███████╗╚██████╔╝╚██████╗██║  ██║███████╗[/]",),
    ("[bold #a85246]╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝[/]",),
    ("[bold #81b29a] ██████╗ ██████╗ ██████╗ ███████╗██████╗ [/]",),
    ("[bold #76a890]██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗[/]",),
    ("[bold #6b9e86]██║     ██║   ██║██║  ██║█████╗  ██████╔╝[/]",),
    ("[bold #60947c]██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗[/]",),
    ("[bold #558a72]╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║[/]",),
    ("[bold #4a8068] ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝[/]",),
]


def show_banner():
    global BACKEND_INFO
    BACKEND_INFO = detect_backend()
    bi = BACKEND_INFO
    ml = _model_label()

    # GPU stats
    gpu_str = ""
    try:
        from localcoder.backends import get_metal_gpu_stats, get_swap_usage_mb, MODELS
        metal = get_metal_gpu_stats()
        swap = get_swap_usage_mb()
        gt = metal.get("total_mb", 0)
        model_size_gb = bi.get("size_gb", 0)
        if not model_size_gb:
            for mid, m in MODELS.items():
                if m.get("name", "") in ml or mid in ml.lower().replace(" ", ""):
                    model_size_gb = m["size_gb"]
                    break
        model_mb = int(model_size_gb * 1024) if model_size_gb else 0
        if gt > 0 and model_mb > 0:
            pct = min(1.0, model_mb / max(1, gt))
            bc = "green" if pct < 0.75 else "yellow" if pct < 0.9 else "red"
            gpu_str = f"[{bc}]{model_mb // 1024}/{gt // 1024}GB GPU[/{bc}]"
    except ImportError:
        pass

    from rich.text import Text as RText
    from rich.console import Group
    from rich.live import Live

    console.print()

    gpu_icon = "[green]●[/]" if bi.get('gpu') else "[yellow]●[/]"
    status_line = f"  {gpu_icon} [bold cyan]{ml}[/]  ·  {bi['backend']}  ·  {bi['ctx'] or '?'} context  ·  {gpu_str}  ·  [green]$0.00[/]"

    # ── Pre-designed frames (Copilot-style: each frame is a complete screen) ──
    def _frame(*lines):
        return Group(*(RText.from_markup(l) for l in lines))

    B = "#e07a5f"  # border/accent
    G = "#81b29a"  # green accent

    # Helper: build bordered logo frame with optional extras below
    def _logo_frame(reveal_cols=99, scan=False, subtitle="", extras=None):
        lines = [f"  [{B}]┌──────────────────────────────────────────────────┐[/]"]
        for r, lt in enumerate(LOGO_TEXT):
            raw = lt[0]
            color = raw.split(']')[0] + ']'
            plain = raw.replace('[/]', '').split(']')[-1] if ']' in raw else raw
            shown = plain[:reveal_cols]
            cursor = f"[white bold]▌[/]" if scan and reveal_cols < len(plain) else ""
            rest = " " * max(0, 48 - len(shown) - (1 if cursor else 0))
            lines.append(f"  [{B}]│[/]{color}{shown}[/]{cursor}{rest}[{B}]│[/]")
        lines.append(f"  [{B}]└──────────────────────────────────────────────────┘[/]")
        if subtitle:
            lines.append(subtitle)
        if extras:
            lines.extend(extras)
        return _frame(*lines)

    try:
        with Live(console=console, refresh_per_second=20, transient=True) as live:

            # Act 1: Border materializes (corners → edges → full)
            corners = [
                f"  [{B}]┌┐[/]",
                *["" for _ in range(12)],
                f"  [{B}]└┘[/]",
            ]
            live.update(_frame(*corners))
            time.sleep(0.07)

            for w in [12, 24, 36, 48]:
                lines = [f"  [{B}]┌{'─' * w}{'─' * (48 - w)}┐[/]"]
                for _ in range(12):
                    lines.append(f"  [{B}]│[/]{' ' * 48}[{B}]│[/]")
                lines.append(f"  [{B}]└{'─' * w}{'─' * (48 - w)}┘[/]")
                live.update(_frame(*lines))
                time.sleep(0.04)

            # Act 2: Logo reveals left-to-right with typing cursor
            for col in range(0, 48, 3):
                live.update(_logo_frame(reveal_cols=col, scan=True))
                time.sleep(0.045)

            # Act 3: Full logo holds, subtitle types in
            live.update(_logo_frame(reveal_cols=99))
            time.sleep(0.12)

            subs = [
                f"  [{B}]✦[/] [dim]Command-line[/]",
                f"  [{B}]✦[/] [dim]Command-line interface[/]",
                f"  [{B}]✦[/] [dim]Command-line interface[/]  [bold {G}]✓ offline[/]",
            ]
            for s in subs:
                live.update(_logo_frame(reveal_cols=99, subtitle=s))
                time.sleep(0.1)

            # Act 4: Description + status appear
            desc = [
                "",
                f"  Write, test, and debug code right from your terminal.",
                f"  Runs [bold]100% on your GPU[/]. No API keys. No cloud. Enter [bold]?[/] for help.",
            ]
            live.update(_logo_frame(reveal_cols=99, subtitle=subs[-1], extras=desc))
            time.sleep(0.2)

            full_extras = desc + ["", status_line, f"  [dim]{os.path.basename(CWD)}/[/]", ""]
            live.update(_logo_frame(reveal_cols=99, subtitle=subs[-1], extras=full_extras))
            time.sleep(0.5)

    except Exception:
        pass

    # ── Static final render ──
    console.print(f"  [{B}]┌──────────────────────────────────────────────────┐[/]")
    for lt in LOGO_TEXT:
        raw = lt[0]
        color = raw.split(']')[0] + ']'
        plain = raw.replace('[/]', '').split(']')[-1] if ']' in raw else raw
        pad = " " * max(0, 48 - len(plain))
        console.print(f"  [{B}]│[/]{lt[0]}{pad}[{B}]│[/]")
    console.print(f"  [{B}]└──────────────────────────────────────────────────┘[/]")
    console.print(f"  [{B}]✦[/] [dim]Command-line interface[/]  [bold {G}]✓ offline[/]")
    console.print()
    console.print(f"  Write, test, and debug code right from your terminal.")
    console.print(f"  Runs [bold]100% on your GPU[/]. No API keys. No cloud. Enter [bold]?[/] for help.")
    console.print()
    console.print(status_line)
    console.print(f"  [dim]{os.path.basename(CWD)}/[/]")
    console.print()

_toolbar_gpu_cache = {"text": "", "ts": 0}

def get_toolbar():
    """Bottom toolbar — model + GPU + offline. GPU stats cached (no ioreg per keystroke)."""
    bi = BACKEND_INFO
    ml = _model_label()
    ctx = bi['ctx'] or '?'

    # Cache GPU part — compute once, reuse for 60s
    gpu_part = _toolbar_gpu_cache["text"]
    if time.time() - _toolbar_gpu_cache["ts"] > 60:
        try:
            from localcoder.backends import MODELS
            # Just use model size from registry — no ioreg call
            model_gb = 0
            gt_gb = 16  # default Metal budget
            for mid, m in MODELS.items():
                if m.get("name", "") in ml or mid in ml.lower().replace(" ", ""):
                    model_gb = m["size_gb"]
                    break
            if model_gb > 0:
                gc = "ansigreen" if model_gb < gt_gb else "ansired"
                gpu_part = f' <style bg="{gc}" fg="ansiblack"> GPU {int(model_gb)}/{gt_gb}GB </style>'
                _toolbar_gpu_cache["text"] = gpu_part
                _toolbar_gpu_cache["ts"] = time.time()
        except ImportError:
            pass

    return HTML(
        f' <b>{ml}</b>'
        f' <style bg="ansigreen" fg="ansiblack"> {bi["backend"]} </style>'
        f' <style bg="ansiblue" fg="ansiwhite"> {ctx} </style>'
        f'{gpu_part}'
        f' <style bg="ansidarkgray" fg="ansiwhite"> ✓ offline </style>'
    )

# ── Main ──
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="localcoder — local AI coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  localcoder                            interactive mode
  localcoder -p "build a react app"     one-shot mode
  localcoder -c                         continue last session
  localcoder --yolo                     auto-approve everything
  localcoder -m gemma4-e4b              use E4B model
  localcoder -m gemma4-26b --yolo -p "fix the bug"
""")
    parser.add_argument("-p", "--prompt", type=str, help="Run a single task and exit")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true", help="Continue last session")
    parser.add_argument("-m", "--model", type=str, default=None, help="Model name (default: gemma4-26b)")
    parser.add_argument("--yolo", action="store_true", help="Auto-approve all tools (sandbox still active)")
    parser.add_argument("--bypass", action="store_true", help="Same as --yolo")
    parser.add_argument("--unrestricted", action="store_true", help="Disable sandbox — full system access (dangerous)")
    parser.add_argument("--ask", action="store_true", help="Ask before every tool")
    parser.add_argument("--api", type=str, default=None, help="API base URL (default: http://127.0.0.1:8089/v1)")
    args = parser.parse_args(argv)

    # Override globals
    global MODEL, API_BASE
    if args.model:
        MODEL = args.model
    if args.api:
        API_BASE = args.api

    mode = "bypass" if (args.yolo or args.bypass) else ("ask" if args.ask else "auto")
    sandbox = not args.unrestricted

    if args.unrestricted:
        console.print(f"  [red bold]⚠ UNRESTRICTED MODE — sandbox disabled. Full system access.[/]")
    elif args.yolo:
        console.print(f"  [yellow]Auto-approve mode. Sandbox still active (no rm -rf, no sudo, no writes outside project).[/]")

    # ── First-run permission check ──
    cfg = _load_config()
    if sys.platform == "darwin" and not cfg.get("permissions_checked"):
        _check_permissions()

    # Logging — always on, auto-rotate
    log_file = os.path.join(CWD, ".localcoder.log")
    try:
        if os.path.exists(log_file) and os.path.getsize(log_file) > 1_000_000:
            with open(log_file, 'r') as f:
                lines = f.readlines()
            with open(log_file, 'w') as f:
                f.writelines(lines[-500:])
    except: pass
    # Only log our stuff, not library noise
    logger = logging.getLogger("localcoder")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(fh)
    # Silence noisy libraries
    logging.getLogger("markdown_it").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logger.info(f"=== Session started · model={MODEL} · cwd={CWD} · perms={mode} ===")
    perms = Permissions(mode, sandbox=sandbox)

    system = {
        "role": "system",
        "content": f"You are Local Coder, an autonomous AI agent with coding AND computer use abilities. Working directory: {CWD}\n"
                   f"Platform: macOS. Today is {time.strftime('%B %d, %Y')}.\n"
                   f"ACT with tools. Never say 'I can\\'t'. Never give up.\n\n"
                   f"TOOL SELECTION:\n"
                   f"- CODING tasks (build apps, write code, edit files): use bash, write_file, read_file, edit_file\n"
                   f"- WEB SEARCH (find info, images, docs, APIs): use web_search tool. This is the DEFAULT for 'search', 'find', 'look up'.\n"
                   f"- BROWSE A SPECIFIC WEBSITE: use computer_use ONLY when user says 'on [website]', 'open [url]', 'browse [site]'.\n"
                   f"  NOT a trigger: 'search for', 'find me', 'look up' — these use web_search.\n"
                   f"  HOW: computer_use action:open:https://THE_URL_HERE (opens Chrome directly)\n"
                   f"  After opening: take screenshot, read visible content, report to user.\n"
                   f"  You CAN browse any website. Never say you cannot access a site.\n"
                   f"- PDFs: use read_pdf tool\n"
                   f"- Images: download with bash curl (auto-displays in terminal)\n\n"
                   f"WHEN TO USE WHAT:\n"
                   f"- Static page (fan page, portfolio, gallery, landing): just write ONE index.html file. No server needed.\n"
                   f"- AI-powered app (analyzer, chatbot, scanner): use the 3-file pattern below (index.html + server.js + package.json).\n"
                   f"- NEVER build an Express server for a simple static page.\n\n"
                   f"APP SKILLS (pre-built templates you MUST use as baseline):\n"
                   f"Skills directory: {os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.agents', 'skills'))}\n"
                   f"When user asks to build an app, FIRST list ALL available skills and pick the closest:\n"
                   f"  1. bash: ls {os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.agents', 'skills'))}\n"
                   f"     (lists: quiz-game, flashcards, gallery, food-scanner, etc.)\n"
                   f"  2. Pick the CLOSEST matching skill. 'math quiz' → quiz-game. 'vocabulary' → flashcards. 'fan page' → gallery.\n"
                   f"  3. read_file the SKILL.md in that skill folder\n"
                   f"  3. read_file the assets/index.html template\n"
                   f"  4. write_file a customized copy to the user's project directory\n"
                   f"  5. Test the result\n"
                   f"NEVER generate complex HTML/SVG/CSS from scratch if a skill template exists.\n"
                   f"Use web_search to find images for galleries/fan pages.\n\n"
                   f"WEB APP ARCHITECTURE (ONLY for AI-powered apps that need a backend):\n\n"
                   f"APP STRUCTURE: Always 3 files in the SAME directory:\n"
                   f"  package.json, server.js, index.html (all inline CSS+JS)\n"
                   f"  server.js must serve index.html with: app.get('/', (req,res) => res.sendFile(__dirname+'/index.html'));\n\n"
                   f"SERVER.JS TEMPLATE (copy this pattern exactly):\n"
                   f"  const express = require('express');\n"
                   f"  const app = express();\n"
                   f"  app.use(express.json({{limit:'50mb'}}));\n"
                   f"  const API_BASE = process.env.LLM_API_BASE || 'http://127.0.0.1:8089/v1';\n"
                   f"  const MODEL = process.env.LLM_MODEL || 'local';\n"
                   f"  app.get('/', (req,res) => res.sendFile(__dirname+'/index.html'));\n"
                   f"  app.post('/api/analyze', async (req,res) => {{\n"
                   f"    const {{message, image}} = req.body;\n"
                   f"    const userContent = image\n"
                   f"      ? [{{type:'text',text:message}}, {{type:'image_url',image_url:{{url:image}}}}]\n"
                   f"      : message;\n"
                   f"    const r = await fetch(API_BASE+'/chat/completions', {{\n"
                   f"      method:'POST', headers:{{'Content-Type':'application/json'}},\n"
                   f"      body:JSON.stringify({{model:MODEL, stream:false, max_tokens:2048,\n"
                   f"        messages:[{{role:'system',content:SYSTEM_PROMPT}}, {{role:'user',content:userContent}}]}}) }});\n"
                   f"    const data = await r.json();\n"
                   f"    res.json({{analysis: data.choices[0].message.content}}); }});\n"
                   f"  app.listen(3000);\n\n"
                   f"FRONTEND INDEX.HTML PATTERNS:\n"
                   f"- DESIGN: Dark theme bg:#0a0a14. Card: background:rgba(255,255,255,0.04); backdrop-filter:blur(20px); border:1px solid rgba(255,255,255,0.08); border-radius:24px.\n"
                   f"  Buttons: border-radius:14px; background:linear-gradient(135deg,#6366f1,#8b5cf6); color:white; font-weight:600; padding:14px 28px.\n"
                   f"  Title: font-size:2rem; font-weight:700; background:linear-gradient(to right,#f97316,#22c55e); -webkit-background-clip:text; color:transparent.\n"
                   f"  Result area: background:rgba(255,255,255,0.03); border-left:3px solid #22c55e; border-radius:16px; padding:24px; white-space:pre-wrap.\n"
                   f"  Use system-ui font. Add transition:all 0.2s on buttons. Loading: spinner animation.\n\n"
                   f"- IMAGE UPLOAD (must use FileReader, never fake it):\n"
                   f"  let imageBase64 = null;\n"
                   f"  function uploadImage() {{\n"
                   f"    const input = document.createElement('input');\n"
                   f"    input.type='file'; input.accept='image/*';\n"
                   f"    input.onchange = e => {{\n"
                   f"      const file = e.target.files[0]; if(!file) return;\n"
                   f"      const reader = new FileReader();\n"
                   f"      reader.onload = ev => {{ imageBase64 = ev.target.result;\n"
                   f"        document.getElementById('preview').src = imageBase64;\n"
                   f"        document.getElementById('preview').style.display = 'block'; }};\n"
                   f"      reader.readAsDataURL(file); }};\n"
                   f"    input.click(); }}\n\n"
                   f"- CAMERA CAPTURE (must use getUserMedia, never fake it):\n"
                   f"  async function openCamera() {{\n"
                   f"    const stream = await navigator.mediaDevices.getUserMedia({{video:{{facingMode:'environment'}}}});\n"
                   f"    const video = document.getElementById('camVideo');\n"
                   f"    video.srcObject = stream; video.style.display='block'; video.play();\n"
                   f"    document.getElementById('captureBtn').style.display='inline-block'; }}\n"
                   f"  function capturePhoto() {{\n"
                   f"    const video = document.getElementById('camVideo');\n"
                   f"    const canvas = document.createElement('canvas');\n"
                   f"    canvas.width=video.videoWidth; canvas.height=video.videoHeight;\n"
                   f"    canvas.getContext('2d').drawImage(video,0,0);\n"
                   f"    imageBase64 = canvas.toDataURL('image/jpeg',0.8);\n"
                   f"    document.getElementById('preview').src = imageBase64;\n"
                   f"    document.getElementById('preview').style.display='block';\n"
                   f"    video.srcObject.getTracks().forEach(t=>t.stop()); video.style.display='none'; }}\n\n"
                   f"- SEND TO API (always this pattern):\n"
                   f"  async function analyze() {{\n"
                   f"    const msg = document.getElementById('input').value;\n"
                   f"    if(!msg && !imageBase64) return;\n"
                   f"    document.getElementById('result').innerHTML = '<div class=\"loading\">Analyzing...</div>';\n"
                   f"    const res = await fetch('/api/analyze', {{\n"
                   f"      method:'POST', headers:{{'Content-Type':'application/json'}},\n"
                   f"      body:JSON.stringify({{message:msg||'Analyze this', image:imageBase64}}) }});\n"
                   f"    const data = await res.json();\n"
                   f"    document.getElementById('result').innerHTML = formatMarkdown(data.analysis);\n"
                   f"    imageBase64 = null; }}\n\n"
                   f"- MARKDOWN RENDERER:\n"
                   f"  function formatMarkdown(text) {{\n"
                   f"    return text.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>')\n"
                   f"      .replace(/^### (.+)$/gm,'<h3>$1</h3>').replace(/^## (.+)$/gm,'<h2>$1</h2>')\n"
                   f"      .replace(/^\\* (.+)$/gm,'<li>$1</li>').replace(/\\n/g,'<br>'); }}\n\n"
                   f"- LOADING/SCANNING ANIMATION (always show while waiting for API):\n"
                   f"  CSS: @keyframes scan {{ 0%{{transform:translateY(-100%)}} 100%{{transform:translateY(100%)}} }}\n"
                   f"  .scanning {{ position:relative; overflow:hidden; }}\n"
                   f"  .scanning::after {{ content:''; position:absolute; left:0; right:0; height:2px;\n"
                   f"    background:linear-gradient(90deg,transparent,#22c55e,transparent); animation:scan 1.5s infinite; }}\n"
                   f"  Also add a pulsing text: <div class='loading'>🔬 Scanning ingredients<span class='dots'></span></div>\n"
                   f"  CSS: @keyframes dots {{ 0%{{content:''}} 33%{{content:'.'}} 66%{{content:'..'}} 100%{{content:'...'}} }}\n"
                   f"  .dots::after {{ content:''; animation:dots 1.5s infinite steps(4); }}\n"
                   f"  Show loading BEFORE fetch, hide AFTER response. Disable button during loading.\n\n"
                   f"- NEVER fake FileReader/camera/API calls. ALWAYS use real implementations above.\n"
                   f"- NEVER use SSE/streaming. Use stream:false and return full JSON.\n"
                   f"- ALWAYS serve index.html from __dirname, NOT from a public/ subdirectory.\n"
                   f"- ALWAYS test after building: npm install, node server.js &, curl POST to verify.\n\n"
                   f"SELF-TESTING (MANDATORY for HTML/web apps):\n"
                   f"After writing ANY index.html or web app, you MUST test it:\n"
                   f"1. If static HTML: run 'bash: python3 -m http.server 8888 &' then 'bash: sleep 1 && curl -s http://localhost:8888/ | head -5' to verify it loads\n"
                   f"2. If Express app: run 'bash: node server.js &' then 'bash: sleep 2 && curl -s http://localhost:3000/ | head -5'\n"
                   f"3. Check for JS errors: run 'bash: node -e \"const fs=require(\\\"fs\\\"); const html=fs.readFileSync(\\\"index.html\\\",\\\"utf8\\\"); const scripts=html.match(/<script[^>]*>([\\\\s\\\\S]*?)<\\\\/script>/g); scripts?.forEach(s => {{ try {{ new Function(s.replace(/<\\\\/?script[^>]*>/g,\\\"\\\")); }} catch(e) {{ console.error(\\\"JS ERROR:\\\", e.message); }} }})\"'\n"
                   f"4. If ANY test fails: read the error, fix the code, test again. DO NOT STOP until tests pass.\n"
                   f"5. For interactive elements (buttons, forms): verify onclick/event handlers exist in the HTML.\n"
                   f"6. For SVG: verify paths are valid (use M, L, A, Z commands with real coordinates, not placeholders).\n\n"
                   f"BROWSER TESTING (use computer_use to test interactively):\n"
                   f"- After building an HTML app, open it: computer_use action:open:file:///path/to/index.html\n"
                   f"- Take screenshot to see the result\n"
                   f"- Click buttons: computer_use action:click:x,y\n"
                   f"- If something looks wrong, fix the code and re-test\n\n"
                   f"COMMON BUGS TO AVOID:\n"
                   f"- Start screen not hiding on button click: always add onclick='document.getElementById(\"screen1\").style.display=\"none\"; document.getElementById(\"screen2\").style.display=\"block\"'\n"
                   f"- SVG pie/chart slices: use Math.cos(angle*Math.PI/180)*radius and Math.sin() for arc endpoints. Never approximate.\n"
                   f"- Fetch to /api without server: static HTML files can't call /api. Use local JS logic or start Express.\n"
                   f"- Missing CSS transitions: always add transition on hover/active states.\n\n"
                   f"RULES:\n"
                   f"- After reading a file ONCE, do NOT re-read it\n"
                   f"- Write complete code with write_file, not code blocks in chat\n"
                   f"- NEVER stop after writing files. Always test. Fix bugs. Test again.\n"
                   f"- Be concise. Do NOT narrate your plan before acting. Just call the tool directly.\n"
                   f"- Never say 'I will now...' or 'Let me...' — just do it."
    }

    # Auto-detect running model + load saved preference
    _load_last_model()

    show_banner()

    # One-shot mode
    if args.prompt:
        console.print(f"\n  [magenta]❯[/] [bold]{args.prompt}[/]")
        messages = [system, {"role": "user", "content": args.prompt}]
        agent_loop(messages, perms)
        return

    # Interactive mode
    bi = BACKEND_INFO
    cwd_short = os.path.basename(CWD)
    console.print()
    shortcuts = Text()
    shortcuts.append("  ")
    shortcuts.append(" ctrl+r ", style="bold white on #3d5a80")
    shortcuts.append(" voice ", style="dim")
    shortcuts.append(" ctrl+v ", style="bold white on #3d5a80")
    shortcuts.append(" image ", style="dim")
    shortcuts.append(" /gpu ", style="bold white on #555555")
    shortcuts.append(" stats ", style="dim")
    shortcuts.append(" /clean ", style="bold white on #555555")
    shortcuts.append(" free ", style="dim")
    shortcuts.append(" /think ", style="bold white on #555555")
    shortcuts.append(" reason ", style="dim")
    shortcuts.append(" /models ", style="bold white on #555555")
    shortcuts.append(" switch ", style="dim")
    console.print(shortcuts)
    console.print()

    total_tokens = 0
    history_file = os.path.join(CWD, ".localcoder-history.json")

    # --continue: restore last session
    if args.cont:
        try:
            with open(history_file) as f:
                messages = json.load(f)
            n = len([m for m in messages if isinstance(m, dict) and m.get("role") == "user"])
            console.print(f"  [green]✦ Resumed session ({n} messages)[/]")
        except:
            console.print(f"  [dim]No saved session — starting fresh[/]")
            messages = [system]
    else:
        messages = [system]

    # Clipboard image state + voice state
    _clipboard_image_path = [None]
    _voice_proc = [None]  # active recording process
    _voice_wav = [None]   # wav file path

    # Voice input setup
    _voice_available = False
    _voice_lang = "auto"
    try:
        import shutil as _shutil
        _whisper_bin = _shutil.which("whisper-cli")
        _whisper_model = os.path.expanduser("~/.local/share/whisper/ggml-small.bin")
        if not os.path.exists(_whisper_model):
            _whisper_model = os.path.expanduser("~/.local/share/whisper/ggml-base.bin")
        _sox_rec = _shutil.which("rec")
        _voice_available = bool(_whisper_bin and os.path.exists(_whisper_model) and _sox_rec)

        # Load language preference
        _cfg = _load_config()
        _voice_lang = _cfg.get("voice_language", "auto")

        # First-time voice setup — ask language
        if _voice_available and _voice_lang == "auto" and not _cfg.get("voice_setup_done"):
            console.print()
            console.print(Panel(
                "[bold]Voice Input Setup[/]  [dim]one-time configuration[/]",
                border_style="#81b29a", padding=(0, 1),
            ))
            console.print(f"  [dim]Select your primary speaking language for voice input:[/]\n")
            LANGS = [
                ("en", "English"),
                ("fr", "French"),
                ("ar", "Arabic"),
                ("es", "Spanish"),
                ("de", "German"),
                ("ja", "Japanese"),
                ("zh", "Chinese"),
                ("auto", "Auto-detect (less accurate on short phrases)"),
            ]
            for i, (code, name) in enumerate(LANGS):
                console.print(f"    [bold]{i+1}.[/] {name} [dim]({code})[/]")
            console.print()
            try:
                ans = input("  ▸ ").strip()
                idx = int(ans) - 1 if ans.isdigit() else 0
                if 0 <= idx < len(LANGS):
                    _voice_lang = LANGS[idx][0]
                else:
                    _voice_lang = "en"
            except:
                _voice_lang = "en"
            _save_config(voice_language=_voice_lang, voice_setup_done=True)
            console.print(f"  [green]✓ Voice language: {_voice_lang}[/]")
            console.print(f"  [dim]Change anytime with /voice-lang[/]\n")

        if _voice_available:
            logger.info(f"Voice input available (whisper-cli + rec, lang={_voice_lang})")
    except:
        pass

    # Key bindings
    kb = KeyBindings()

    @kb.add('c-r')
    def _voice_toggle(event):
        """Ctrl+R: toggle voice — start recording or stop+transcribe."""
        if not _voice_available:
            try:
                fd = os.open("/dev/tty", os.O_WRONLY)
                os.write(fd, b"\n  \033[33mVoice not available. Run: localcoder --setup\033[0m\n")
                os.close(fd)
            except:
                pass
            return

        if _voice_proc[0] is not None:
            # STOP + TRANSCRIBE (Ctrl+R again)
            _do_voice_transcribe(event)
            return

        # START RECORDING
        try:
            fd = os.open("/dev/tty", os.O_WRONLY)
            _voice_wav[0] = os.path.join(CWD, ".localcoder-voice.wav")
            _voice_proc[0] = subprocess.Popen(
                [_sox_rec, "-q", "-r", "16000", "-c", "1", "-b", "16", _voice_wav[0], "trim", "0", "30"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            os.write(fd, b"\n  \033[1;35m\xe2\x97\x8f\033[0m \033[35mRecording...\033[0m \033[2mpress Ctrl+R to stop\033[0m\n")
            os.close(fd)
        except Exception as e:
            _voice_proc[0] = None
            try:
                fd = os.open("/dev/tty", os.O_WRONLY)
                os.write(fd, f"\n  \033[31mRecord error: {e}\033[0m\n".encode())
                os.close(fd)
            except:
                pass

    def _do_voice_transcribe(event):
        """Stop recording and transcribe."""
        if _voice_proc[0] is None:
            return

        try:
            fd = os.open("/dev/tty", os.O_WRONLY)

            # Stop recording
            try:
                _voice_proc[0].send_signal(signal.SIGINT)
                _voice_proc[0].wait(timeout=3)
            except:
                _voice_proc[0].kill()
            _voice_proc[0] = None

            os.write(fd, b"  \033[2mTranscribing...\033[0m\n")

            # Transcribe with whisper (Metal GPU — only ~200MB, fits in headroom)
            result = subprocess.run(
                [_whisper_bin, "--model", _whisper_model,
                 "--language", _voice_lang, "--no-timestamps", "--threads", "8",
                 "--file", _voice_wav[0]],
                capture_output=True, text=True, timeout=30
            )

            # Parse detected language
            lang = ""
            for line in result.stderr.split("\n"):
                if "auto-detected language:" in line:
                    lang = line.split("auto-detected language:")[-1].strip().split()[0]

            # Parse transcription
            lines = []
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line and not line.startswith("[") and not line.startswith("whisper_"):
                    lines.append(line)
            text = " ".join(lines).strip()
            text = text.replace("(silence)", "").replace("[BLANK_AUDIO]", "").strip()

            if text:
                lang_tag = f" [{lang}]" if lang else ""
                os.write(fd, f"  \033[32m✓\033[0m \033[2m{text[:80]}{lang_tag}\033[0m\n".encode())
                event.app.current_buffer.insert_text(text)
            else:
                os.write(fd, b"  \033[2mNo speech detected\033[0m\n")

            os.close(fd)

            # Cleanup
            if _voice_wav[0] and os.path.exists(_voice_wav[0]):
                os.unlink(_voice_wav[0])

        except Exception as e:
            _voice_proc[0] = None
            try:
                os.write(fd, f"\n  \033[31mTranscribe error: {e}\033[0m\n".encode())
                os.close(fd)
            except:
                pass

    @kb.add('c-v')
    def _paste_image(event):
        """Ctrl+V: check clipboard for image, show preview immediately."""
        img = get_clipboard_image()
        if img:
            _clipboard_image_path[0] = img
            buf = event.app.current_buffer
            buf.insert_text("[📎 image] ")
            # Show preview immediately by writing to /dev/tty (bypasses prompt_toolkit)
            try:
                timg = "/opt/homebrew/bin/timg"
                if os.path.exists(timg):
                    tty_fd = os.open("/dev/tty", os.O_WRONLY)
                    os.write(tty_fd, b"\n")
                    spawnSync = subprocess.Popen(
                        [timg, "-g", "40x12", "-p", "i", img],
                        stdout=tty_fd, stderr=tty_fd
                    )
                    spawnSync.wait(timeout=5)
                    sz = os.path.getsize(img) // 1024
                    os.write(tty_fd, f"  📎 clipboard ({sz} KB)\n".encode())
                    os.close(tty_fd)
            except:
                pass
        else:
            # Normal paste — insert text from clipboard
            try:
                txt = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2).stdout
                if txt:
                    event.app.current_buffer.insert_text(txt)
            except:
                pass

    # Slash command autocomplete
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML as PT_HTML_CMD
    SLASH_COMMANDS = {
        "/models": "Switch model (fuzzy search)",
        "/model": "Set model by name",
        "/clear": "Clear conversation",
        "/gpu": "Show GPU memory, swap, model status",
        "/clean": "Free GPU memory (unload idle models)",
        "/health": "Full GPU health dashboard",
        "/resume": "Restore last session",
        "/context": "Show token usage",
        "/paste": "Paste clipboard image",
        "/undo": "Revert last file change",
        "/snapshots": "List file backups",
        "/diff": "Show file changes",
        "/cost": "Show token cost ($0.00)",
        "/ask": "Ask before every tool",
        "/auto": "Auto-approve safe tools",
        "/bypass": "Approve everything",
        "/yolo": "Same as /bypass",
        "/log": "View debug log",
        "/think": "Toggle reasoning: none → low → medium → high",
        "/deploy": "Generate & deploy an AI-powered React app",
        "/exit": "Exit",
    }

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/"):
                for cmd, desc in SLASH_COMMANDS.items():
                    if text.lower() in cmd.lower() or cmd.startswith(text):
                        # Escape XML-invalid chars in description
                        safe_desc = desc.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        try:
                            display = PT_HTML_CMD(f'<b>{cmd}</b> <style fg="ansigray">{safe_desc}</style>')
                        except Exception:
                            display = f"{cmd} {desc}"
                        yield Completion(
                            cmd,
                            start_position=-len(text),
                            display=display,
                        )

    session = PromptSession(
        history=FileHistory(os.path.join(CWD, ".localcoder-input-history")),
        bottom_toolbar=get_toolbar,
        key_bindings=kb,
        completer=SlashCompleter(),
        complete_while_typing=True,
    )

    while True:
        _clipboard_image_path[0] = None
        try:
            console.print(Rule(style="dim"))
            task = session.prompt(HTML('<style fg="ansimagenta" bg="" bold="true">❯ </style>')).strip()
        except KeyboardInterrupt:
            console.print(f"\n  [dim]bye[/]")
            break
        except EOFError:
            break

        if not task:
            continue

        if task == "/clear":
            messages = [system]; total_tokens = 0
            console.clear()
            show_banner()
            bi = BACKEND_INFO
            ml = f"Gemma 4 {bi['size']}" if bi["size"] else MODEL
            qt = f" {bi['quant']}" if bi["quant"] else ""
            console.print(f"\n  [dim]model[/] [bold cyan]{ml}{qt}[/]  [dim]backend[/] [bold green]{bi['backend']}[/]  [dim]ctx[/] [bold green]{bi['ctx'] or '?'}[/]  [dim]perms[/] [bold yellow]{perms.mode}[/]")
            console.print(f"  [green]Conversation cleared.[/]\n"); continue
        if task == "/cost":
            console.print(f"  [green]$0.00 — {total_tokens} tokens[/]"); continue
        if task.startswith("/think"):
            global REASONING_EFFORT
            import tty, termios
            levels = ["none", "low", "medium", "high"]
            icons  = ["⚡", "💭", "🧠", "🔬"]
            tags   = ["off", "light", "think", "deep"]
            descs  = ["No thinking", "Quick reasoning", "Balanced", "Deep reasoning"]
            idx = levels.index(REASONING_EFFORT) if REASONING_EFFORT in levels else 2

            # ANSI colors
            DIM = "\033[2m"
            BOLD = "\033[1m"
            REV = "\033[7m"  # reverse video (highlight)
            RST = "\033[0m"

            def _draw(i):
                bar = f"  {icons[i]} "
                for j in range(len(levels)):
                    if j == i:
                        bar += f" {REV}{BOLD} {tags[j]} {RST} "
                    else:
                        bar += f" {DIM} {tags[j]} {RST} "
                bar += f" {DIM}{descs[i]}  ← → enter{RST}"
                sys.stdout.write(f"\r\033[K{bar}")
                sys.stdout.flush()

            _draw(idx)
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch == '\r' or ch == '\n':
                        break
                    if ch == '\x1b':
                        seq = sys.stdin.read(2)
                        if seq == '[D':  # left
                            idx = max(0, idx - 1)
                        elif seq == '[C':  # right
                            idx = min(len(levels) - 1, idx + 1)
                    elif ch == 'q' or ch == '\x03':
                        break
                    _draw(idx)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

            REASONING_EFFORT = levels[idx]
            sys.stdout.write(f"\r\033[K")
            console.print(f"  {icons[idx]} Reasoning: [bold]{REASONING_EFFORT}[/] — {descs[idx]}")
            continue
        if task == "/context":
            ctx_used = estimate_tokens(json.dumps(messages))
            ctx_str = BACKEND_INFO.get("ctx", "")
            if ctx_str:
                ctx_max = int(ctx_str.replace("K", "")) * 1024
                context_usage_bar(console, ctx_used, ctx_max)
            else:
                console.print(f"  [cyan]~{ctx_used} tokens / ? ({len(messages)} msgs)[/]")
            continue
        if task == "/gpu":
            try:
                from localcoder.backends import (
                    get_machine_specs, get_metal_gpu_stats, get_swap_usage_mb,
                    get_llama_server_config, _detect_model_info, get_top_memory_processes,
                )
                specs = get_machine_specs()
                metal = get_metal_gpu_stats()
                swap = get_swap_usage_mb()
                srv = get_llama_server_config()
                procs = get_top_memory_processes(min_mb=300, limit=5)

                gt = metal.get("total_mb") or specs["gpu_total_mb"]
                sc = "red" if swap > 4000 else "yellow" if swap > 1000 else "green"

                # Use model size, not ioreg alloc
                model_mb = 0
                if srv.get("running"):
                    mi_gpu = _detect_model_info(srv, None)
                    model_mb = int((mi_gpu.get("size_gb") or 0) * 1024)
                if model_mb == 0:
                    model_mb = 12 * 1024  # fallback

                gf = max(0, gt - model_mb)
                gc = "green" if model_mb < gt else "red"

                bar_w = 30
                pct = min(1.0, model_mb / max(1, gt))
                filled = int(pct * bar_w)
                bc = "green" if pct < 0.75 else "yellow" if pct < 0.9 else "red"
                bar = f"[{bc}]{'━' * filled}[/{bc}][dim]{'─' * (bar_w - filled)}[/]"

                console.print(f"\n  [bold]GPU[/]  {bar}  [{gc}]{model_mb // 1024}/{gt // 1024}GB[/{gc}]  free: {gf // 1024}GB")
                console.print(f"  [bold]Swap[/] [{sc}]{swap // 1024}GB[/{sc}]  [bold]Pressure[/] {specs.get('mem_pressure', '?')}")

                if srv.get("running"):
                    mi = _detect_model_info(srv, None)
                    ms = mi["name"] or "?"
                    if mi["quant"]: ms += f" {mi['quant']}"
                    gi = "[green]GPU[/]" if srv["ngl"] >= 90 else "[red]CPU[/]"
                    console.print(f"  [bold]Model[/] [cyan]{ms}[/]  {gi}  ctx {srv['n_ctx'] // 1024}K  footprint {srv.get('footprint_mb', 0)}MB")

                app_procs = [p for p in procs if p["category"] == "app"]
                if app_procs:
                    hogs = "  ".join(f"{p['name']}{'×'+str(p['count']) if p.get('count',1)>1 else ''} {p['mb']//1024}G" for p in app_procs[:4])
                    console.print(f"  [bold]Apps[/]  {hogs}")
                console.print()
            except ImportError:
                console.print("  [dim]Install localcoder package for GPU stats[/]")
            continue
        if task == "/clean":
            try:
                from localcoder.backends import (
                    cleanup_gpu_memory, get_metal_gpu_stats, get_swap_usage_mb,
                    get_top_memory_processes,
                )
                # Before
                metal_before = get_metal_gpu_stats()
                swap_before = get_swap_usage_mb()
                ga_before = metal_before.get("alloc_mb", 0)

                console.print(f"\n  [yellow]Freeing GPU memory...[/]  [dim](safe — won't close your apps)[/]")
                result = cleanup_gpu_memory(force=False)

                if result["ollama_unloaded"]:
                    console.print(f"  [green]✓[/] Unloaded: {', '.join(result['ollama_unloaded'])}")
                else:
                    console.print(f"  [dim]No idle models to unload.[/]")

                # After
                import time as _tc; _tc.sleep(1)
                metal_after = get_metal_gpu_stats()
                swap_after = get_swap_usage_mb()
                ga_after = metal_after.get("alloc_mb", 0)
                gt = metal_after.get("total_mb") or 16384
                freed = max(0, ga_before - ga_after)

                gc = "green" if ga_after < gt else "red"
                console.print(f"  [bold]Before[/] {ga_before // 1024}GB  [bold]After[/] [{gc}]{ga_after // 1024}GB[/{gc}]  [bold]Freed[/] {freed // 1024}GB  [bold]Swap[/] {swap_after // 1024}GB")

                app_procs = get_top_memory_processes(min_mb=500, limit=3)
                apps = [p for p in app_procs if p["category"] == "app"]
                if apps and ga_after > gt:
                    console.print(f"  [dim]Still overloaded. Close these for more: {', '.join(p['name'] for p in apps[:3])}[/]")
                console.print()
            except ImportError:
                console.print("  [dim]Install localcoder package for cleanup[/]")
            continue
        if task == "/health":
            try:
                from localcoder.backends import print_health_dashboard
                print_health_dashboard()
            except ImportError:
                console.print("  [dim]Install localcoder package for health dashboard[/]")
            continue
        if task == "/resume":
            try:
                with open(history_file) as f: messages = json.load(f)
                console.print(f"  [green]Resumed {len(messages)} messages[/]")
            except: console.print("  [dim]No saved session[/]")
            continue
        if task in ("/ask", "/auto", "/bypass", "/yolo"):
            perms.mode = "bypass" if task == "/yolo" else task[1:]
            console.print(f"  [yellow]Permissions: {perms.mode}[/]"); continue
        if task == "/undo" or task.startswith("/undo "):
            parts = task.split(None, 1)
            path = parts[1] if len(parts) > 1 else None
            msg = restore_snapshot(0, path)
            console.print(f"  [green]{msg}[/]"); continue
        if task == "/snapshots" or task.startswith("/snapshots "):
            parts = task.split(None, 1)
            path = parts[1] if len(parts) > 1 else None
            console.print(list_snapshots(path)); continue
        if task.startswith("/diff "):
            path = task.split(None, 1)[1]
            full = os.path.join(CWD, path)
            if os.path.isfile(full):
                # Diff current vs latest snapshot
                snaps = sorted([s for s in os.listdir(SNAPSHOT_DIR) if path.replace("/","__") in s], reverse=True) if os.path.isdir(SNAPSHOT_DIR) else []
                if snaps:
                    snap_path = os.path.join(SNAPSHOT_DIR, snaps[0])
                    try:
                        r = subprocess.run(["diff", "--color=always", "-u", snap_path, full], capture_output=True, text=True, timeout=5)
                        if r.stdout:
                            console.print(Panel(r.stdout[:3000], title=f"[bold]diff {path}[/]", border_style="yellow"))
                        else:
                            console.print(f"  [dim]No changes since last snapshot[/]")
                    except:
                        console.print(f"  [red]diff failed[/]")
                else:
                    console.print(f"  [dim]No snapshots for {path}[/]")
            else:
                console.print(f"  [red]File not found: {path}[/]")
            continue
        if task in ("/models", "/model"):
            new_model, new_url = select_model_interactive()
            if new_model:
                _switch_model(new_model, new_url)
            continue
        if task.startswith("/model "):
            name = task.split(None, 1)[1]
            # Find matching model
            all_m = discover_all_models()
            matched = None
            for m in all_m:
                if name.lower() in m["id"].lower():
                    matched = m; break
            if matched:
                _switch_model(matched["id"], matched["url"])
            else:
                console.print(f"  [red]Model not found: {name}[/]")
            continue
        if task == "/paste":
            img = get_clipboard_image()
            if img:
                show_image_inline(img)
                console.print(f"  [green]Clipboard image saved. Ask a question about it.[/]")
                # Add as next user message with image reference
                task = input("  [dim]Question about image:[/] ").strip() or "What is in this image?"
                import base64 as b64mod
                img_b64 = b64mod.b64encode(open(img, "rb").read()).decode()
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": task},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
                ]})
                console.print(f"\n  [magenta]❯[/] [bold]{task}[/] [dim](+ clipboard image)[/]")
                agent_loop(messages, perms)
                try:
                    with open(history_file, "w") as f: json.dump(messages[-20:], f)
                except: pass
                continue
            else:
                console.print(f"  [dim]No image in clipboard[/]")
                continue
        if task.startswith("/api"):
            parts = task.split(None, 1)
            if len(parts) == 2:
                API_BASE = parts[1]
                console.print(f"  [cyan]API: {API_BASE}[/]")
            else:
                console.print(f"  [cyan]Current: {API_BASE}[/]")
                console.print(f"  [dim]/api http://localhost:11434/v1  (Ollama)[/]")
                console.print(f"  [dim]/api http://localhost:8089/v1   (llama.cpp)[/]")
            continue
        if task == "/log":
            try:
                with open(log_file) as f:
                    lines = f.readlines()
                console.print(f"  [dim]{log_file} ({len(lines)} lines)[/]")
                for line in lines[-15:]:
                    console.print(f"  [dim]{line.rstrip()}[/]")
            except:
                console.print("  [dim]No log file[/]")
            continue
        if task.startswith("/voice-lang"):
            parts = task.split(None, 1)
            if len(parts) == 2:
                _voice_lang = parts[1].strip()
                _save_config(voice_language=_voice_lang)
                console.print(f"  [green]✓ Voice language: {_voice_lang}[/]")
            else:
                console.print(f"  [cyan]Current: {_voice_lang}[/]")
                console.print(f"  [dim]Usage: /voice-lang en|fr|ar|es|de|ja|zh|auto[/]")
            continue
        if task in ("/exit", "/quit"):
            break
        if task == "/deploy" or task.startswith("/deploy "):
            _handle_deploy(task, messages, perms, system, console)
            continue

        console.print(Rule(style="dim"))

        # Handle clipboard image if pasted
        clip_img = _clipboard_image_path[0]
        if clip_img and os.path.isfile(clip_img):
            import base64 as b64mod, shutil
            # Save to a permanent file with timestamp
            ts = time.strftime("%Y%m%d_%H%M%S")
            saved_name = f".localcoder-image-{ts}.png"
            saved_path = os.path.join(CWD, saved_name)
            shutil.copy2(clip_img, saved_path)
            img_b64 = b64mod.b64encode(open(saved_path, "rb").read()).decode()
            sz_kb = os.path.getsize(saved_path) // 1024
            # Clean up the "[📎 image]" prefix from prompt
            task = task.replace("[📎 image]", "").strip() or "What is in this image?"
            console.print(f"  [magenta]❯[/] [bold]{task}[/]")
            # Show image inline AFTER prompt (now we're in normal terminal mode)
            show_image_inline(saved_path)
            console.print(f"  [green]📎[/] [dim]{saved_name}[/] [dim green]({sz_kb} KB)[/]\n")
            messages.append({"role": "user", "content": [
                {"type": "text", "text": f"{task}\n[Attached image: {saved_path}]"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
            ]})
        else:
            console.print(f"  [magenta]❯[/] [bold]{task}[/]\n")
            messages.append({"role": "user", "content": task})

        try:
            tokens = agent_loop(messages, perms)
            total_tokens += tokens
        except KeyboardInterrupt:
            console.print(f"\n  [bold yellow]⚡ Interrupted[/]")

        try:
            safe = [m for m in messages if isinstance(m, dict)]
            with open(history_file, "w") as f: json.dump(safe[-20:], f)
        except: pass

    # ── Exit: offer memory cleanup ──
    _cleanup_on_exit()


def _handle_deploy(task, messages, perms, system, console):
    """Build an AI app from the framework templates."""
    from rich.rule import Rule

    # Load framework apps
    framework_dir = os.path.join(os.path.dirname(__file__), "templates", "framework")
    build_module = os.path.join(framework_dir, "build.py")

    if not os.path.exists(build_module):
        console.print(f"  [red]Framework not found[/]")
        return

    # Import builder
    import importlib.util
    spec = importlib.util.spec_from_file_location("build", build_module)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    apps = builder.list_apps()
    if not apps:
        console.print(f"  [red]No app templates found[/]")
        return

    # Parse: /deploy or /deploy app-id or /deploy "description"
    parts = task.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else None

    # Direct app ID match
    if arg and any(a['id'] == arg for a in apps):
        selected = next(a for a in apps if a['id'] == arg)
    elif arg:
        # Fuzzy match or treat as custom description
        matched = [a for a in apps if arg.lower() in a['id'] or arg.lower() in a.get('title', '').lower()]
        if matched:
            selected = matched[0]
        else:
            # Custom: use chatbot template with custom prompt
            selected = next((a for a in apps if a['id'] == 'chatbot'), apps[0]).copy()
            selected['title'] = arg[:40]
            selected['subtitle'] = arg
            selected['system_prompt'] = f"You are an AI expert for: {arg}. Help the user with detailed, accurate responses. Use emoji and structured formatting."
    else:
        # Interactive picker
        console.print(f"\n  [bold #34d399]⚡ Deploy — AI App Framework[/]\n")
        for i, a in enumerate(apps, 1):
            inputs = ', '.join(a.get('inputs', []))
            model = a.get('model', 'any')
            console.print(f"  [bold cyan]{i}[/]  {a['icon']}  {a['title']:<20} [dim]{inputs:<18} {model}[/]")
        console.print(f"  [bold cyan]{len(apps)+1}[/]  🛠️  Custom App")
        console.print()

        try:
            choice = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        try:
            idx = int(choice) - 1
            if idx == len(apps):
                # Custom
                try:
                    desc = input("  Describe your app: ").strip()
                    if not desc:
                        return
                except (EOFError, KeyboardInterrupt):
                    return
                selected = next((a for a in apps if a['id'] == 'chatbot'), apps[0]).copy()
                selected['title'] = desc[:40]
                selected['subtitle'] = desc
                selected['system_prompt'] = f"You are an AI expert for: {desc}. Help the user. Use emoji and structured markdown."
            elif 0 <= idx < len(apps):
                selected = apps[idx]
            else:
                return
        except ValueError:
            # Typed an app name
            matched = [a for a in apps if choice.lower() in a['id'] or choice.lower() in a.get('title', '').lower()]
            selected = matched[0] if matched else apps[0]

    # App output directory
    default_name = selected['id']
    try:
        app_name = input(f"  App name [{default_name}]: ").strip() or default_name
    except (EOFError, KeyboardInterrupt):
        return
    app_name = re.sub(r'[^a-z0-9-]', '-', app_name.lower())
    app_dir = os.path.join(CWD, app_name)

    console.print(f"\n  {selected['icon']}  [bold]{selected['title']}[/]")
    console.print(f"  [dim]{selected.get('subtitle', '')}[/]")
    console.print(f"  [dim]Inputs: {', '.join(selected.get('inputs', []))}  Model: {selected.get('model', 'any')}[/]")
    console.print(Rule(style="dim"))

    # Build
    if os.path.exists(app_dir):
        try:
            ans = input(f"  {app_name}/ exists. Overwrite? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if ans not in ("y", "yes"):
            return
        import shutil
        shutil.rmtree(app_dir)

    console.print(f"  [dim]Building {app_name}...[/]")

    # Write custom config if modified
    app_config_dir = os.path.join(framework_dir, "apps", selected['id'])
    if selected.get('title') != next((a['title'] for a in apps if a['id'] == selected['id']), None):
        # Custom app — write temp config
        import json as _json, tempfile
        tmp_app_dir = os.path.join(framework_dir, "apps", "_custom")
        os.makedirs(tmp_app_dir, exist_ok=True)
        with open(os.path.join(tmp_app_dir, "config.json"), "w") as f:
            _json.dump(selected, f, indent=2)
        try:
            builder.build_app("_custom", app_dir)
        finally:
            import shutil
            shutil.rmtree(tmp_app_dir, ignore_errors=True)
    else:
        builder.build_app(selected['id'], app_dir)

    file_count = sum(len(files) for _, _, files in os.walk(app_dir))
    console.print(f"  [green]✓[/] Created {file_count} files in {app_name}/")

    # npm install
    console.print(f"  [dim]Installing dependencies...[/]")
    try:
        r = subprocess.run("npm install", shell=True, cwd=app_dir,
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            console.print(f"  [green]✓[/] Dependencies installed")
        else:
            console.print(f"  [yellow]npm install warnings (may still work)[/]")
    except Exception:
        console.print(f"  [yellow]npm install issue — run manually[/]")

    # Summary
    console.print(f"\n  [green bold]✓ {selected['icon']} {selected['title']} is ready![/]\n")
    console.print(f"  [bold]Run:[/]     cd {app_name} && npm start")
    console.print(f"  [bold]Open:[/]    http://localhost:3000")
    console.print(f"\n  [bold]Switch AI provider:[/]")
    console.print(f"  [dim]Local:[/]    LLM_API_BASE=http://localhost:8089/v1 npm start")
    console.print(f"  [dim]OpenAI:[/]   LLM_API_BASE=https://api.openai.com/v1 LLM_API_KEY=sk-... npm start")
    console.print(f"  [dim]Gemini:[/]   LLM_API_BASE=https://generativelanguage.googleapis.com/v1beta/openai LLM_API_KEY=... npm start")
    console.print(f"  [dim]Groq:[/]     LLM_API_BASE=https://api.groq.com/openai/v1 LLM_API_KEY=... npm start")

    # Start?
    console.print()
    try:
        ans = input("  Start now? (y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "n"

    if ans in ("y", "yes"):
        console.print(f"\n  [green]Starting on http://localhost:3000...[/]")
        console.print(f"  [dim]Ctrl+C to stop[/]\n")
        try:
            subprocess.run("node server.js", shell=True, cwd=app_dir)
        except KeyboardInterrupt:
            console.print(f"\n  [dim]Server stopped[/]")


def _cleanup_on_exit():
    """Ask user if they want to free GPU memory on exit."""
    console.print()

    # Check what's running
    llama_running = False
    ollama_models = []
    try:
        req = urllib.request.Request("http://127.0.0.1:8089/health")
        urllib.request.urlopen(req, timeout=1)
        llama_running = True
    except: pass

    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            ollama_models = [m.get("name", "") for m in data.get("models", [])]
    except: pass

    if not llama_running and not ollama_models:
        console.print(f"  [dim]No models loaded in GPU. bye![/]\n")
        return

    # Show what's using GPU
    console.print(f"\n  [bold]GPU cleanup[/]", end="")
    if llama_running:
        console.print(f"  [dim]·  llama-server on :8089[/]", end="")
    if ollama_models:
        console.print(f"  [dim]·  Ollama: {', '.join(ollama_models)}[/]", end="")
    console.print()
    console.print(f"  [bold]1[/] keep running  [bold]2[/] unload models  [bold]3[/] stop all  [bold]enter[/] keep")
    console.print()

    try:
        ans = input("    ▸ ").strip()
    except (EOFError, KeyboardInterrupt):
        ans = "1"

    if ans == "2":
        # Unload Ollama models
        for m in ollama_models:
            try:
                data = json.dumps({"model": m, "keep_alive": 0}).encode()
                req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                    data=data, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5)
                console.print(f"    [green]✓[/] [dim]Unloaded {m}[/]")
            except: pass
        console.print(f"    [green]✓[/] [dim]Ollama models unloaded[/]")

    elif ans == "3":
        # Kill llama-server
        if llama_running:
            subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
            console.print(f"    [green]✓[/] [dim]llama-server stopped[/]")
        # Unload Ollama models
        for m in ollama_models:
            try:
                data = json.dumps({"model": m, "keep_alive": 0}).encode()
                req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                    data=data, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5)
            except: pass
        console.print(f"    [green]✓[/] [dim]All models unloaded, GPU memory freed[/]")
    else:
        console.print(f"    [dim]Keeping models loaded. bye![/]")

    console.print()


if __name__ == "__main__":
    main()
