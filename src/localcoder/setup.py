"""Interactive setup wizard — runs on first launch or `localcoder --setup`."""
import json, os, sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from localcoder.backends import (
    BACKENDS, MODELS, CONFIG_DIR, discover_all, get_system_ram_gb,
    check_backend_installed, install_backend, download_model_hf,
    download_model_ollama, find_model_file, start_llama_server,
    start_ollama_serve, check_backend_running,
)

console = Console()
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def wizard():
    """Interactive first-run setup wizard."""
    console.print()
    title = Text()
    title.append("◆ ", style="bold magenta")
    title.append("localcoder setup", style="bold white")
    console.print(Panel(
        "[bold]Welcome! Let's get your local AI coding agent running.[/]\n"
        "[dim]This wizard will install a backend, download a model, and start serving.[/]",
        title=title, title_align="left",
        border_style="magenta", padding=(1, 2),
    ))

    ram = get_system_ram_gb()
    console.print(f"\n  [dim]System RAM:[/] [bold]{ram} GB[/]")

    # ── Step 1: Detect backends ──
    console.print(f"\n  [bold magenta]Step 1:[/] Checking backends...\n")
    discovery = discover_all()

    table = Table(show_header=True, header_style="bold cyan", padding=(0, 2))
    table.add_column("Backend")
    table.add_column("Installed")
    table.add_column("Running")
    table.add_column("Models")

    for d in discovery:
        installed = "[green]✓[/]" if d["installed"] else "[red]✗[/]"
        running = f"[green]:{d['port']}[/]" if d["running"] else "[dim]—[/]"
        models = ", ".join(d["models"][:3]) if d["models"] else "[dim]none[/]"
        table.add_row(d["name"], installed, running, models)

    console.print(table)

    # ── Step 2: Install backend if needed ──
    any_installed = any(d["installed"] for d in discovery)
    if not any_installed:
        console.print(f"\n  [bold magenta]Step 2:[/] No backend found. Install one:\n")
        console.print(f"    [bold]1.[/] llama.cpp via Unsloth [dim](recommended for 26B, best speed)[/]")
        console.print(f"    [bold]2.[/] Ollama [dim](easiest, good for E4B/E2B)[/]")
        console.print(f"    [bold]3.[/] Both")
        console.print()

        try:
            choice = input("  Choose (1/2/3): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if choice in ("1", "3"):
            install_backend("llamacpp")
        if choice in ("2", "3"):
            install_backend("ollama")

        # Re-discover
        discovery = discover_all()

    # ── Step 3: Choose model ──
    console.print(f"\n  [bold magenta]Step 3:[/] Choose a model:\n")

    recommended = []
    for mid, m in MODELS.items():
        fits = ram >= m["ram_required"]
        rec = " [green](recommended)[/]" if fits and mid == "gemma4-26b" and ram >= 24 else ""
        if not fits:
            rec = " [red](needs {m['ram_required']}GB+)[/]"
        recommended.append((mid, m, fits, rec))

    for i, (mid, m, fits, rec) in enumerate(recommended):
        style = "bold" if fits else "dim"
        console.print(f"    [{style}]{i+1}. {m['name']}[/{style}] [dim]({m['size_gb']}GB, {m['description']})[/]{rec}")

    console.print()
    try:
        choice = input("  Choose model (1-4): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    idx = int(choice) - 1 if choice.isdigit() else 0
    if idx < 0 or idx >= len(recommended):
        idx = 0

    model_id, model_info, _, _ = recommended[idx]
    console.print(f"\n  [green]Selected: {model_info['name']}[/]")

    # ── Step 4: Download model ──
    console.print(f"\n  [bold magenta]Step 4:[/] Downloading model...\n")

    model_file = find_model_file(model_id)
    if model_file:
        console.print(f"  [green]✓ Model already downloaded: {os.path.basename(model_file)}[/]")
    else:
        if model_info.get("backend") == "llamacpp" and model_info.get("hf_repo"):
            download_model_hf(model_id)
        elif model_info.get("ollama_tag"):
            # Ensure Ollama is running
            if not check_backend_running("ollama"):
                start_ollama_serve()
            download_model_ollama(model_id)

    # ── Step 5: Determine backend + API URL ──
    backend_id = model_info.get("backend", "ollama")
    port = BACKENDS[backend_id]["default_port"]
    api_url = f"http://127.0.0.1:{port}/v1"

    # For llama.cpp models, start the server
    if backend_id == "llamacpp":
        if not check_backend_running("llamacpp"):
            console.print(f"\n  [bold magenta]Step 5:[/] Starting llama-server...\n")
            proc = start_llama_server(model_id, port)
            if not proc:
                console.print(f"  [yellow]Falling back to Ollama...[/]")
                backend_id = "ollama"
                port = 11434
                api_url = f"http://127.0.0.1:{port}/v1"
                if model_info.get("ollama_tag"):
                    start_ollama_serve()
                    download_model_ollama(model_id)
        else:
            console.print(f"\n  [green]✓ llama-server already running on :{port}[/]")
    else:
        if not check_backend_running("ollama"):
            console.print(f"\n  [bold magenta]Step 5:[/] Starting Ollama...\n")
            start_ollama_serve()

    # ── Step 6: Save config ──
    model_name = model_info.get("ollama_tag", model_id)
    if backend_id == "llamacpp":
        # Get actual model name from server
        from localcoder.backends import get_running_models
        running = get_running_models("llamacpp")
        if running:
            model_name = running[0]

    cfg = {
        "model": model_name,
        "api_base": api_url,
        "backend": backend_id,
        "model_id": model_id,
        "setup_complete": True,
    }
    save_config(cfg)

    console.print(f"\n  [bold magenta]Step 6:[/] Configuration saved.\n")

    # ── Step 7: Configure OpenCode / OpenClaw (optional) ──
    console.print(f"  [bold magenta]Step 7:[/] Configure other tools?\n")

    import shutil
    has_opencode = shutil.which("opencode")
    has_openclaw = shutil.which("openclaw")

    if has_opencode or has_openclaw:
        tools_found = []
        if has_opencode:
            tools_found.append("OpenCode")
        if has_openclaw:
            tools_found.append("OpenClaw")
        console.print(f"  [green]Found:[/] {', '.join(tools_found)}")
        console.print(f"  [dim]Auto-configure them to use your local model?[/]")
        console.print(f"    [bold]1.[/] Yes — configure all [dim](recommended)[/]")
        console.print(f"    [bold]2.[/] No — skip")

        try:
            ans = input("\n  Choose (1/2): ").strip()
        except (EOFError, KeyboardInterrupt):
            ans = "2"

        if ans == "1":
            _configure_opencode(api_url, model_name, model_id, model_info)
            _configure_openclaw(api_url, model_name, model_id, model_info)
    else:
        console.print(f"  [dim]No OpenCode or OpenClaw found. Install with:[/]")
        console.print(f"    [dim]curl -fsSL https://opencode.ai/install | bash[/]")
        console.print(f"    [dim]brew install openclaw[/]")

    # ── Done ──
    console.print()
    console.print(Panel(
        Text.assemble(
            ("Setup complete! ", "bold green"),
            ("Run ", "dim"), ("localcoder", "bold cyan"), (" to start.\n\n", "dim"),
            ("Model:   ", "dim"), (f"{model_info['name']}\n", "bold cyan"),
            ("Backend: ", "dim"), (f"{BACKENDS[backend_id]['name']} (:{port})\n", "green"),
            ("API:     ", "dim"), (f"{api_url}\n", "dim"),
        ),
        border_style="green", padding=(1, 2),
    ))

    return cfg


def _configure_opencode(api_url, model_name, model_id, model_info):
    """Auto-configure OpenCode to use the local model."""
    import shutil
    if not shutil.which("opencode"):
        return

    config_path = Path.home() / ".config/opencode/opencode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing or create new
    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except:
            pass

    # Add/update llamacpp provider
    if "provider" not in existing:
        existing["provider"] = {}

    existing["provider"]["llamacpp"] = {
        "name": "llama.cpp (local)",
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": api_url},
        "models": {
            model_id: {
                "name": model_info.get("name", model_name),
                "tool_call": True,
                "reasoning": False,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 131072, "output": 8192},
            }
        },
    }
    existing["$schema"] = "https://opencode.ai/config.json"
    existing["model"] = f"llamacpp/{model_id}"

    config_path.write_text(json.dumps(existing, indent=2))
    console.print(f"  [green]✓ OpenCode configured[/] [dim]({config_path})[/]")
    console.print(f"    [dim]Model: llamacpp/{model_id} → {api_url}[/]")


def _configure_openclaw(api_url, model_name, model_id, model_info):
    """Auto-configure OpenClaw to use the local model."""
    import shutil
    if not shutil.which("openclaw"):
        return

    config_path = Path.home() / ".openclaw/openclaw.json"
    if not config_path.exists():
        console.print(f"  [dim]OpenClaw config not found — run 'openclaw' first to initialize[/]")
        return

    try:
        cfg = json.loads(config_path.read_text())
    except:
        console.print(f"  [yellow]Could not parse OpenClaw config[/]")
        return

    # Add/update llamacpp provider
    if "models" not in cfg:
        cfg["models"] = {"mode": "merge", "providers": {}}
    if "providers" not in cfg["models"]:
        cfg["models"]["providers"] = {}

    cfg["models"]["providers"]["llamacpp"] = {
        "api": "openai-completions",
        "baseUrl": api_url,
        "apiKey": "dummy",
        "models": [{
            "id": model_name,
            "name": model_info.get("name", model_name),
            "reasoning": False,
            "contextWindow": 131072,
            "maxTokens": 8192,
        }],
    }

    # Set as default model
    if "agents" not in cfg:
        cfg["agents"] = {"defaults": {}}
    if "defaults" not in cfg["agents"]:
        cfg["agents"]["defaults"] = {}
    if "model" not in cfg["agents"]["defaults"]:
        cfg["agents"]["defaults"]["model"] = {}
    cfg["agents"]["defaults"]["model"]["primary"] = f"llamacpp/{model_name}"

    config_path.write_text(json.dumps(cfg, indent=2))
    console.print(f"  [green]✓ OpenClaw configured[/] [dim]({config_path})[/]")
    console.print(f"    [dim]Model: llamacpp/{model_name} → {api_url}[/]")


def ensure_setup():
    """Check if setup is done, run wizard if not."""
    cfg = load_config()
    if cfg.get("setup_complete"):
        return cfg
    return wizard()
