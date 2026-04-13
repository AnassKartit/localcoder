"""Auto-setup — zero config, detects everything, starts everything."""

import json, os, shutil, sys, time, urllib.request
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from localcoder.backends import (
    BACKENDS,
    MODELS,
    CONFIG_DIR,
    discover_all,
    get_system_ram_gb,
    check_backend_installed,
    install_backend,
    download_model_hf,
    download_model_ollama,
    find_model_file,
    start_llama_server,
    start_ollama_serve,
    check_backend_running,
    get_running_models,
    recommend_model,
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


def _check_server(url, timeout=2):
    """Check if a server is responding."""
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def _wait_for_server(port, timeout=60):
    """Wait for server to become ready."""
    for _ in range(timeout // 2):
        if _check_server(f"http://127.0.0.1:{port}/v1/models"):
            return True
        time.sleep(2)
    return False


def auto_setup():
    """Zero-config auto-setup. Detects everything, starts everything, no questions.

    Priority order:
      1. Already running server → connect
      2. localfit installed → use localfit run
      3. llama-server installed + model downloaded → auto-start
      4. Ollama installed → auto-start with best model
      5. Nothing installed → install llama.cpp + download best model
    """
    ram = get_system_ram_gb()
    best_model_id, best_reason = recommend_model(ram)

    # ── Step 1: Check if any server is already running ──
    for bid in ("llamacpp", "ollama"):
        if check_backend_running(bid):
            models = get_running_models(bid)
            model_name = models[0] if models else "local"
            port = BACKENDS[bid]["default_port"]
            api_url = f"http://127.0.0.1:{port}/v1"
            console.print(f"  [green]✓[/] {BACKENDS[bid]['name']} running on :{port}")
            if models:
                console.print(f"  [green]✓[/] Model: {model_name}")
            cfg = {
                "model": model_name,
                "api_base": api_url,
                "backend": bid,
                "model_id": best_model_id,
                "setup_complete": True,
            }
            save_config(cfg)
            return cfg

    # ── Step 2: Try localfit (if installed) ──
    localfit_bin = shutil.which("localfit")
    if localfit_bin:
        console.print(f"  [cyan]localfit[/] detected — auto-starting model...")
        console.print(f"  [dim]Best for {ram}GB: {best_model_id} — {best_reason}[/]")
        import subprocess

        try:
            subprocess.Popen(
                [localfit_bin, "--serve", best_model_id, "--background"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            console.print(f"  [dim]Waiting for server...[/]")
            if _wait_for_server(8089, timeout=120):
                models = get_running_models("llamacpp")
                model_name = models[0] if models else best_model_id
                cfg = {
                    "model": model_name,
                    "api_base": "http://127.0.0.1:8089/v1",
                    "backend": "llamacpp",
                    "model_id": best_model_id,
                    "setup_complete": True,
                }
                save_config(cfg)
                console.print(f"  [green]✓[/] Server ready — {model_name}")
                return cfg
            else:
                console.print(
                    f"  [yellow]localfit server didn't start in time, trying direct...[/]"
                )
        except Exception:
            pass

    # ── Step 3: Try llama-server with existing model ──
    if check_backend_installed("llamacpp"):
        # Check if any model is already downloaded
        for mid, minfo in MODELS.items():
            if minfo.get("backend") == "llamacpp":
                mfile = find_model_file(mid)
                if mfile:
                    console.print(
                        f"  [cyan]llama.cpp[/] found with model: {minfo['name']}"
                    )
                    console.print(f"  [dim]Starting llama-server...[/]")
                    proc = start_llama_server(mid)
                    if proc and _wait_for_server(8089, timeout=60):
                        models = get_running_models("llamacpp")
                        model_name = models[0] if models else mid
                        cfg = {
                            "model": model_name,
                            "api_base": "http://127.0.0.1:8089/v1",
                            "backend": "llamacpp",
                            "model_id": mid,
                            "setup_complete": True,
                        }
                        save_config(cfg)
                        console.print(f"  [green]✓[/] Server ready — {model_name}")
                        return cfg

    # ── Step 4: Try Ollama with existing or auto-pull model ──
    if check_backend_installed("ollama"):
        console.print(f"  [cyan]Ollama[/] detected — starting...")
        if not check_backend_running("ollama"):
            start_ollama_serve()
            time.sleep(3)

        # Check if best model's ollama tag exists
        model_info = MODELS.get(best_model_id, {})
        ollama_tag = model_info.get("ollama_tag")
        if not ollama_tag:
            # Find any model with an ollama tag that fits
            for mid, minfo in MODELS.items():
                if minfo.get("ollama_tag") and ram >= minfo.get("ram_required", 999):
                    ollama_tag = minfo["ollama_tag"]
                    best_model_id = mid
                    model_info = minfo
                    break

        if ollama_tag:
            console.print(f"  [dim]Pulling {ollama_tag}...[/]")
            download_model_ollama(best_model_id)
            if _wait_for_server(11434, timeout=120):
                cfg = {
                    "model": ollama_tag,
                    "api_base": "http://127.0.0.1:11434/v1",
                    "backend": "ollama",
                    "model_id": best_model_id,
                    "setup_complete": True,
                }
                save_config(cfg)
                console.print(f"  [green]✓[/] Ollama ready — {ollama_tag}")
                return cfg

    # ── Step 5: Nothing installed — auto-install llama.cpp + download best model ──
    console.print(f"\n  [yellow]No AI backend found. Auto-installing...[/]")
    console.print(
        f"  [dim]RAM: {ram}GB → Best model: {best_model_id} — {best_reason}[/]\n"
    )

    # Prefer llama.cpp (faster, better for coding)
    console.print(f"  [bold]Installing llama.cpp...[/]")
    if install_backend("llamacpp"):
        console.print(f"  [green]✓[/] llama.cpp installed")
        model_info = MODELS.get(best_model_id, {})
        if model_info.get("backend") == "llamacpp" and model_info.get("hf_repo"):
            console.print(f"  [bold]Downloading {model_info['name']}...[/]")
            download_model_hf(best_model_id)
            proc = start_llama_server(best_model_id)
            if proc and _wait_for_server(8089, timeout=120):
                models = get_running_models("llamacpp")
                model_name = models[0] if models else best_model_id
                cfg = {
                    "model": model_name,
                    "api_base": "http://127.0.0.1:8089/v1",
                    "backend": "llamacpp",
                    "model_id": best_model_id,
                    "setup_complete": True,
                }
                save_config(cfg)
                console.print(f"  [green]✓[/] Ready — {model_name}")
                return cfg

    # Last resort: try Ollama
    console.print(f"  [bold]Trying Ollama...[/]")
    if install_backend("ollama"):
        start_ollama_serve()
        time.sleep(3)
        # Find an Ollama-compatible model
        for mid, minfo in MODELS.items():
            if minfo.get("ollama_tag") and ram >= minfo.get("ram_required", 999):
                download_model_ollama(mid)
                cfg = {
                    "model": minfo["ollama_tag"],
                    "api_base": "http://127.0.0.1:11434/v1",
                    "backend": "ollama",
                    "model_id": mid,
                    "setup_complete": True,
                }
                save_config(cfg)
                console.print(f"  [green]✓[/] Ready — {minfo['ollama_tag']}")
                return cfg

    console.print(f"  [red]Could not auto-setup. Install manually:[/]")
    console.print(f"    [dim]pip install localfit && localfit run[/]")
    return None


def detect_and_connect():
    """Smart model detection — scan everything, connect or launch.

    Priority:
      1. Scan all running servers → collect all available models
      2. If exactly one model running → auto-connect
      3. If multiple models running → show picker (like Open WebUI)
      4. If nothing running → use localfit to start (GPU-aware)
      5. If no localfit → auto_setup()

    Returns (api_base, model_name, backend_id) or None.
    """
    from localcoder.backends import (
        discover_all,
        get_running_models,
        get_system_ram_gb,
        get_gpu_memory_info,
        recommend_model,
    )

    console.print(f"\n  [dim]Scanning for running models...[/]")

    # ── Step 1: Collect all running models across all backends ──
    available = []  # list of (model_name, api_base, backend_id, port)

    discovery = discover_all()
    for d in discovery:
        if d["running"] and d["models"]:
            for m in d["models"]:
                api = f"http://127.0.0.1:{d['port']}/v1"
                available.append((m, api, d["id"], d["port"]))

    # Also check common custom ports (vLLM, TGI, etc.)
    for extra_port in [8000, 8080, 5000]:
        try:
            url = f"http://127.0.0.1:{extra_port}/v1/models"
            req = urllib.request.Request(
                url, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=1) as resp:
                data = json.loads(resp.read())
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        available.append(
                            (
                                mid,
                                f"http://127.0.0.1:{extra_port}/v1",
                                "custom",
                                extra_port,
                            )
                        )
        except Exception:
            pass

    # ── Step 2: Decision based on what's available ──
    if len(available) == 1:
        # Exactly one model — auto-connect
        model, api, bid, port = available[0]
        console.print(f"  [green]✓[/] Found: [bold]{model}[/] on :{port}")
        cfg = {
            "model": model,
            "api_base": api,
            "backend": bid,
            "model_id": model,
            "setup_complete": True,
        }
        save_config(cfg)
        return cfg

    elif len(available) > 1:
        # Multiple models — show picker
        console.print(f"\n  [bold]Multiple models available:[/]\n")
        for i, (model, api, bid, port) in enumerate(available):
            backend_name = BACKENDS.get(bid, {}).get("name", bid)
            console.print(
                f"    [bold]{i + 1}.[/] [cyan]{model}[/]  [dim]{backend_name} :{port}[/]"
            )
        console.print()

        try:
            choice = input(f"  Choose (1-{len(available)}) [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "1"

        idx = int(choice) - 1 if choice.isdigit() else 0
        if idx < 0 or idx >= len(available):
            idx = 0

        model, api, bid, port = available[idx]
        console.print(f"  [green]✓[/] Selected: [bold]{model}[/] on :{port}")
        cfg = {
            "model": model,
            "api_base": api,
            "backend": bid,
            "model_id": model,
            "setup_complete": True,
        }
        save_config(cfg)
        return cfg

    # ── Step 3: Nothing running — try localfit ──
    console.print(f"  [dim]No models running.[/]")

    lf_bin = shutil.which("localfit")
    if lf_bin:
        ram = get_system_ram_gb()
        gpu = get_gpu_memory_info()
        gpu_total = gpu.get("total_mb", 0) // 1024
        gpu_used = gpu.get("used_mb", 0) // 1024
        gpu_free = gpu_total - gpu_used

        best_id, best_reason = recommend_model(ram)
        best_info = MODELS.get(best_id, {})
        best_size = best_info.get("size_gb", 5)

        console.print(
            f"\n  [dim]RAM: {ram}GB · GPU: {gpu_free}GB free / {gpu_total}GB[/]"
        )

        if gpu_free < best_size and gpu_used > 2:
            # GPU is busy — warn and offer to free it
            console.print(
                f"  [yellow]GPU memory in use ({gpu_used}GB) — model needs ~{best_size}GB[/]"
            )
            console.print(
                f"  [dim]localfit will unload conflicting models automatically.[/]"
            )

        console.print(
            f"  [bold]Best model for your hardware:[/] {best_id} — {best_reason}"
        )
        console.print(f"\n  [dim]Starting via localfit...[/]")

        import subprocess

        try:
            subprocess.Popen(
                [lf_bin, "--serve", best_id, "--background"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait for server
            best_port = best_info.get("backend", "llamacpp")
            wait_port = BACKENDS.get(best_port, {}).get("default_port", 8089)
            console.print(f"  [dim]Waiting for server on :{wait_port}...[/]")
            for w in range(60):
                time.sleep(2)
                try:
                    models = get_running_models(best_port)
                    if models:
                        model = models[0]
                        api = f"http://127.0.0.1:{wait_port}/v1"
                        console.print(
                            f"  [green]✓[/] Ready: [bold]{model}[/] on :{wait_port}"
                        )
                        cfg = {
                            "model": model,
                            "api_base": api,
                            "backend": best_port,
                            "model_id": best_id,
                            "setup_complete": True,
                        }
                        save_config(cfg)
                        return cfg
                except Exception:
                    pass
                if w > 0 and w % 10 == 0:
                    console.print(f"  [dim]Still starting... ({w * 2}s)[/]")
        except Exception as e:
            console.print(f"  [red]Failed to start: {e}[/]")

    # ── Step 4: Fallback to auto_setup ──
    return auto_setup()


def ensure_setup():
    """Smart setup — always detect what's ACTUALLY running, update config.

    Never trust stale config. Always scan real servers.
    """
    from localcoder.backends import discover_all, get_running_models

    cfg = load_config()

    # ── Always scan what's really running right now ──
    available = []
    discovery = discover_all()
    for d in discovery:
        if d["running"] and d["models"]:
            for m in d["models"]:
                api = f"http://127.0.0.1:{d['port']}/v1"
                available.append((m, api, d["id"], d["port"]))

    # Also check common custom ports
    for extra_port in [8000, 8080, 5000]:
        try:
            url = f"http://127.0.0.1:{extra_port}/v1/models"
            req = urllib.request.Request(
                url, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=1) as resp:
                data = json.loads(resp.read())
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        available.append(
                            (
                                mid,
                                f"http://127.0.0.1:{extra_port}/v1",
                                "custom",
                                extra_port,
                            )
                        )
        except Exception:
            pass

    if available:
        # Use the first running model (or match config if possible)
        best = available[0]
        # Try to match what config says we should use
        if cfg.get("model"):
            for a in available:
                if cfg["model"] in a[0] or a[0] in cfg.get("model", ""):
                    best = a
                    break

        model, api, bid, port = best
        # Update config with what's ACTUALLY running
        cfg["model"] = model
        cfg["api_base"] = api
        cfg["backend"] = bid
        cfg["model_id"] = model
        cfg["setup_complete"] = True
        save_config(cfg)
        return cfg

    # Nothing running — detect and connect (start servers)
    if cfg.get("setup_complete"):
        console.print(f"  [yellow]No server running — starting...[/]")
    return detect_and_connect()
