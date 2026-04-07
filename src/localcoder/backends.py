"""Backend discovery, installation, and model management."""
import json, os, shutil, subprocess, sys, time, urllib.request, urllib.parse
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

console = Console()

# ── Platform detection ──
HOME = Path.home()
CONFIG_DIR = HOME / ".localcoder"
MODELS_DIR = HOME / "models"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform == "linux"
IS_WSL = IS_LINUX and "microsoft" in (Path("/proc/version").read_text().lower() if Path("/proc/version").exists() else "")

def _find_binary(name, extra_paths=None):
    """Find a binary in PATH or known locations."""
    found = shutil.which(name)
    if found:
        return Path(found)
    for p in (extra_paths or []):
        if Path(p).exists():
            return Path(p)
    return Path(name)  # fallback — will fail on check

# ── Known backends ──
BACKENDS = {
    "llamacpp": {
        "name": "llama.cpp",
        "default_port": 8089,
        "binary": _find_binary("llama-server", [
            HOME / ".unsloth/llama.cpp/llama-server",
            Path("/usr/local/bin/llama-server"),
        ]),
        "install_cmd": "curl -fsSL https://unsloth.ai/install.sh | sh",
    },
    "ollama": {
        "name": "Ollama",
        "default_port": 11434,
        "binary": _find_binary("ollama", [
            Path("/opt/homebrew/bin/ollama"),
            Path("/usr/local/bin/ollama"),
            HOME / ".local/bin/ollama",
        ]),
        "install_cmd": "curl -fsSL https://ollama.com/install.sh | sh" if IS_LINUX else "brew install ollama",
    },
}

# ── Known models ──
MODELS = {
    "gemma4-26b": {
        "name": "Gemma 4 26B Q3_K_XL",
        "hf_repo": "unsloth/gemma-4-26B-A4B-it-GGUF",
        "hf_pattern": "*UD-Q3_K_XL*",
        "size_gb": 12,
        "ram_required": 16,
        "description": "Best quality on 24GB Mac. MoE, 49 tok/s, perfect tool calling.",
        "ollama_tag": "gemma4:26b",
        "backend": "llamacpp",
        "server_flags": "-ngl 99 -c 131072 -np 1 -fa on -ctk q4_0 -ctv q4_0 --no-warmup --jinja",
    },
    "qwen35b-a3b": {
        "name": "Qwen 3.5 35B-A3B Q2_K_XL",
        "hf_repo": "unsloth/Qwen3.5-35B-A3B-GGUF",
        "hf_pattern": "*UD-Q2_K_XL*",
        "size_gb": 11.3,
        "ram_required": 16,
        "description": "MoE coding beast. 49 tok/s, 256 experts, tool calling, vision.",
        "ollama_tag": None,
        "backend": "llamacpp",
        "server_flags": "-ngl 99 -c 32768 -np 1 -fa on -ctk q4_0 -ctv q4_0 --no-warmup --jinja --reasoning-budget 0",
    },
    "qwen35-4b": {
        "name": "Qwen 3.5 4B",
        "hf_repo": "unsloth/Qwen3.5-4B-GGUF",
        "hf_pattern": "*UD-Q4_K_XL*",
        "size_gb": 2.7,
        "ram_required": 8,
        "description": "Ultrafast at 50 tok/s. Great for quick tasks, only 2.7GB GPU.",
        "ollama_tag": None,
        "backend": "llamacpp",
        "server_flags": "-ngl 99 -c 32768 --jinja --reasoning-budget 0",
    },
    "gemma4-e4b": {
        "name": "Gemma 4 E4B",
        "hf_repo": None,
        "size_gb": 5.5,
        "ram_required": 8,
        "description": "Sweet spot for 16GB. Audio + image + code, 57 tok/s.",
        "ollama_tag": "gemma4:e4b",
        "backend": "ollama",
    },
    "gemma4-e2b": {
        "name": "Gemma 4 E2B",
        "hf_repo": None,
        "size_gb": 4,
        "ram_required": 8,
        "description": "Speed demon. 95 tok/s, basic tasks.",
        "ollama_tag": "gemma4:e2b",
        "backend": "ollama",
    },
    "qwen3.5-27b": {
        "name": "Qwen 3.5 27B",
        "hf_repo": None,
        "size_gb": 17,
        "ram_required": 24,
        "description": "Strong alternative. Dense 27B, good tool calling.",
        "ollama_tag": "qwen3.5:27b",
        "backend": "ollama",
    },
}


def _parse_footprint_mb(pid):
    """Get process memory footprint in MB using macOS footprint command."""
    if not IS_MAC:
        return 0
    try:
        fp = subprocess.run(
            ["/usr/bin/footprint", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        for line in fp.stdout.splitlines():
            if "Footprint:" in line:
                parts = line.split("Footprint:")[1].strip().split()
                val = float(parts[0])
                unit = parts[1] if len(parts) > 1 else "KB"
                if "GB" in unit:
                    return int(val * 1024)
                elif "MB" in unit:
                    return int(val)
                elif "KB" in unit:
                    return max(1, int(val / 1024))
                return int(val)
    except Exception:
        pass
    return 0


def get_system_ram_gb():
    """Get total system RAM in GB (macOS, Linux, WSL)."""
    try:
        if IS_MAC:
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3)
            return int(out.stdout.strip()) // (1024**3)
        else:
            # Linux / WSL
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) // (1024 * 1024)
    except:
        pass
    return 0


def get_machine_specs():
    """Get full machine specs: chip, cores, RAM, GPU memory breakdown."""
    specs = {
        "chip": "Unknown",
        "cpu_cores": 0,
        "gpu_cores": 0,
        "ram_gb": get_system_ram_gb(),
        "gpu_total_mb": 0,
        "gpu_used_mb": 0,
        "gpu_free_mb": 0,
        "gpu_processes": [],   # list of {name, pid, rss_mb}
        "mem_pressure": "unknown",
    }

    if IS_MAC:
        # Chip name
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=3,
            )
            specs["chip"] = out.stdout.strip()
            if not specs["chip"] or "Apple" not in specs["chip"]:
                # Fallback for Apple Silicon
                out2 = subprocess.run(
                    ["system_profiler", "SPHardwareDataType"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in out2.stdout.splitlines():
                    if "Chip" in line and ":" in line:
                        specs["chip"] = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

        # CPU / GPU core counts
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.ncpu"], capture_output=True, text=True, timeout=3,
            )
            specs["cpu_cores"] = int(out.stdout.strip())
        except Exception:
            pass
        try:
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=5,
            )
            for line in out.stdout.splitlines():
                if "Total Number of Cores" in line:
                    specs["gpu_cores"] = int(line.split(":")[-1].strip())
                    break
        except Exception:
            pass

        # Metal GPU budget — use real ioreg value, then check sysctl override
        # 1. Try ioreg for real Metal VRAM,totalMB
        try:
            import re as _re_ioreg
            _ioreg_out = subprocess.run(["ioreg", "-l"], capture_output=True, text=True, timeout=10)
            for _line in _ioreg_out.stdout.splitlines():
                if "VRAM,totalMB" in _line:
                    _m = _re_ioreg.search(r'"VRAM,totalMB"=(\d+)', _line)
                    if _m:
                        specs["gpu_total_mb"] = int(_m.group(1))
                    break
        except Exception:
            pass

        # 2. Check if user overrode with iogpu.wired_limit_mb
        if specs["gpu_total_mb"] == 0:
            try:
                out = subprocess.run(
                    ["sysctl", "-n", "iogpu.wired_limit_mb"],
                    capture_output=True, text=True, timeout=3,
                )
                custom_limit = int(out.stdout.strip())
                if custom_limit > 0:
                    specs["gpu_total_mb"] = custom_limit
            except Exception:
                pass

        # 3. Fallback to estimate
        if specs["gpu_total_mb"] == 0:
            specs["gpu_total_mb"] = int(specs["ram_gb"] * 1024 * 0.67)

        # Find GPU-heavy processes (llama-server, ollama, any ML inference)
        gpu_proc_names = ["llama-server", "ollama", "ollama_llama_server",
                          "mlx_lm", "whisper"]
        try:
            out = subprocess.run(
                ["ps", "axo", "pid,comm"],
                capture_output=True, text=True, timeout=3,
            )
            for line in out.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 2:
                    continue
                pid, comm = parts[0], parts[1]
                name = os.path.basename(comm)
                if not any(gp in name for gp in gpu_proc_names):
                    continue
                mem_mb = _parse_footprint_mb(pid)
                if mem_mb < 10:
                    # Fallback to RSS
                    try:
                        rss = subprocess.run(
                            ["ps", "-o", "rss=", "-p", pid],
                            capture_output=True, text=True,
                        )
                        if rss.stdout.strip():
                            mem_mb = int(rss.stdout.strip()) // 1024
                    except Exception:
                        pass

                if mem_mb > 100:
                    specs["gpu_processes"].append({
                        "name": name, "pid": int(pid), "rss_mb": mem_mb,
                    })
        except Exception:
            pass

        specs["gpu_used_mb"] = sum(p["rss_mb"] for p in specs["gpu_processes"])
        specs["gpu_free_mb"] = max(0, specs["gpu_total_mb"] - specs["gpu_used_mb"])

        # Memory pressure
        try:
            out = subprocess.run(
                ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                capture_output=True, text=True, timeout=3,
            )
            level = int(out.stdout.strip())
            specs["mem_pressure"] = {0: "normal", 1: "warn", 2: "critical", 4: "critical"}.get(level, "unknown")
        except Exception:
            pass

    elif IS_LINUX:
        # Linux / WSL
        try:
            with open("/proc/cpuinfo") as f:
                specs["cpu_cores"] = sum(1 for line in f if line.startswith("processor"))
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                        specs["gpu_free_mb"] = avail_kb // 1024
        except Exception:
            pass

        # Check for NVIDIA GPU
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                parts = out.stdout.strip().split(",")
                specs["chip"] = parts[0].strip()
                specs["gpu_total_mb"] = int(parts[1].strip())
                specs["gpu_used_mb"] = int(parts[2].strip())
                specs["gpu_free_mb"] = int(parts[3].strip())
        except FileNotFoundError:
            specs["gpu_total_mb"] = specs["ram_gb"] * 1024
            specs["gpu_free_mb"] = specs["gpu_total_mb"]

    return specs


def cleanup_gpu_memory(force=False):
    """Free GPU memory by unloading idle models and killing stale processes.

    Returns dict with what was cleaned up.
    """
    cleaned = {"ollama_unloaded": [], "processes_killed": [], "freed_mb": 0}

    # 1. Unload Ollama models (set keep_alive=0)
    if check_backend_running("ollama"):
        try:
            models = get_running_models("ollama")
            for m in models:
                urllib.request.urlopen(
                    urllib.request.Request(
                        "http://127.0.0.1:11434/api/generate",
                        data=json.dumps({"model": m, "keep_alive": 0}).encode(),
                        headers={"Content-Type": "application/json"},
                    ), timeout=5,
                )
                cleaned["ollama_unloaded"].append(m)
        except Exception:
            pass

    # 2. Kill stale llama-server processes (if force or not our session)
    if force:
        try:
            out = subprocess.run(
                ["pgrep", "-f", "llama-server"], capture_output=True, text=True,
            )
            for pid in out.stdout.strip().splitlines():
                pid = pid.strip()
                if pid:
                    rss = subprocess.run(
                        ["ps", "-o", "rss=", "-p", pid],
                        capture_output=True, text=True,
                    )
                    mb = int(rss.stdout.strip()) // 1024 if rss.stdout.strip() else 0
                    subprocess.run(["kill", pid], timeout=3)
                    cleaned["processes_killed"].append({"pid": int(pid), "freed_mb": mb})
                    cleaned["freed_mb"] += mb
        except Exception:
            pass

    # Give time for memory to be released
    if cleaned["ollama_unloaded"] or cleaned["processes_killed"]:
        time.sleep(2)

    return cleaned


def get_top_memory_processes(min_mb=80, limit=12):
    """Get top memory-consuming processes with accurate footprint.

    Categorizes processes as:
    - 'ml': ML inference servers (llama-server, ollama)
    - 'app': User apps (Chrome, Slack, etc.)
    - 'system': System processes (WindowServer, kernel_task)
    """
    SYSTEM_PROCS = {
        "WindowServer", "WindowManager", "kernel_task", "launchd",
        "mds", "mds_stores", "opendirectoryd", "fseventsd",
        "corebrightnessd", "bluetoothd", "nearbyd", "systemstats",
        "loginwindow", "Dock", "Finder", "SystemUIServer",
        "ControlCenter", "NotificationCenter", "Terminal", "iTerm2",
        "zsh", "bash", "sh",
    }
    # System procs safe to kill (macOS auto-restarts them lean, freeing bloated memory)
    # Maps name → description for the debloat wizard
    SYSTEM_RESTARTABLE = {
        "CoreLocationAgent":    "Location services cache — often leaks to 8GB+",
        "CacheDeleteExtension": "Storage cleanup daemon — bloats during disk scans",
        "remindd":              "Reminders sync daemon — known memory leak on macOS 15",
        "suggestd":             "Siri suggestions indexer — heavy background ML",
        "photoanalysisd":       "Photos face/scene ML analysis — runs after imports",
        "mediaanalysisd":       "Media ML classifier — visual lookup, Live Text",
        "nsurlsessiond":        "Background network downloads — iCloud sync cache",
        "cloudd":               "iCloud Drive sync daemon — bloats with many files",
        "bird":                 "CloudKit/iCloud container daemon",
        "callservicesd":        "FaceTime/phone call routing daemon",
        "SafariLaunchAgent":    "Safari preload — keeps old pages in memory",
        "SoftwareUpdateNotificationManager": "macOS update checker — safe to kill",
        "com.apple.WebKit.Networking": "WebKit network process — cache bloat",
    }
    ML_PROCS = {
        "llama-server", "ollama", "ollama_llama_server",
        "mlx_lm", "whisper", "vllm", "tgi",
    }

    procs = []
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,rss=,comm="], capture_output=True, text=True, timeout=5,
        )
        # Pre-filter by RSS to avoid calling footprint on hundreds of tiny processes
        candidates = []
        for line in out.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            pid, rss_kb, comm = parts[0], parts[1], " ".join(parts[2:])
            try:
                rss_mb = int(rss_kb) // 1024
            except ValueError:
                continue
            if rss_mb < min_mb // 4:  # loose pre-filter
                continue
            candidates.append((pid, rss_mb, comm))

        # Sort by RSS descending, only footprint top N candidates (fast)
        candidates.sort(key=lambda x: x[1], reverse=True)
        candidates = candidates[:limit * 3]  # check 3x limit, take top N

        for pid, rss_mb, comm in candidates:
            name = os.path.basename(comm.split()[0]) if comm else "?"

            # Use RSS directly (fast) — footprint is 0.3s per process
            fp_mb = rss_mb

            if fp_mb < min_mb:
                continue

            # Categorize
            if name in ML_PROCS or any(ml in name for ml in ML_PROCS):
                category = "ml"
            elif name in SYSTEM_PROCS:
                category = "system"
            elif name in SYSTEM_RESTARTABLE or any(sr in name for sr in SYSTEM_RESTARTABLE):
                category = "bloat"
            else:
                category = "app"

            procs.append({
                "pid": int(pid),
                "name": name,
                "mb": fp_mb,
                "category": category,
                "killable": category not in ("system",),
            })
    except Exception:
        pass

    # Normalize names for grouping
    def _group_name(name):
        # Group all Chrome helpers under "Chrome"
        if "Google" in name or "Chrome" in name:
            return "Chrome"
        return name

    grouped = {}
    for p in procs:
        key = _group_name(p["name"])
        if key in grouped:
            grouped[key]["mb"] += p["mb"]
            grouped[key]["count"] += 1
            grouped[key]["pids"].append(p["pid"])
        else:
            grouped[key] = {**p, "name": key, "count": 1, "pids": [p["pid"]]}

    result = sorted(grouped.values(), key=lambda x: x["mb"], reverse=True)
    return result[:limit]


def print_machine_specs(specs=None):
    """Print a compact machine specs panel using Rich."""
    if specs is None:
        specs = get_machine_specs()

    ram = specs["ram_gb"]
    gpu_total = specs["gpu_total_mb"]
    gpu_used = specs["gpu_used_mb"]
    gpu_free = specs["gpu_free_mb"]

    # Color code free GPU memory
    if gpu_free > 14000:
        free_color = "green"
    elif gpu_free > 8000:
        free_color = "yellow"
    else:
        free_color = "red"

    pressure_color = {"normal": "green", "warn": "yellow", "critical": "red"}.get(
        specs["mem_pressure"], "dim"
    )

    lines = [
        f"  [bold]{specs['chip']}[/]  ·  {specs['cpu_cores']} CPU"
        + (f" · {specs['gpu_cores']} GPU cores" if specs['gpu_cores'] else ""),
        f"  RAM: [bold]{ram}GB[/] total  ·  Metal GPU budget: [bold]{gpu_total // 1024}GB[/]"
        + (f"  ·  pressure: [{pressure_color}]{specs['mem_pressure']}[/{pressure_color}]"
           if specs["mem_pressure"] != "unknown" else ""),
        f"  GPU VRAM: [{free_color}]{gpu_free // 1024}GB free[/{free_color}]"
        + f"  ·  {gpu_used // 1024}GB used  ·  {gpu_total // 1024}GB total",
    ]

    if specs["gpu_processes"]:
        procs = "  GPU processes: " + ", ".join(
            f"[cyan]{p['name']}[/] ({p['rss_mb']//1024}GB)" for p in specs["gpu_processes"]
        )
        lines.append(procs)

    console.print(Panel(
        "\n".join(lines),
        title="[bold]Machine Specs[/]",
        border_style="dim",
        padding=(0, 1),
    ))


def _detect_model_info(server_config, model_id=None):
    """Detect model name, quant level, and file size from model path or model_id."""
    info = {"name": None, "quant": None, "size_gb": None}

    # Try model_id first
    if model_id and model_id in MODELS:
        m = MODELS[model_id]
        info["name"] = m["name"].split(" Q")[0] if " Q" in m["name"] else m["name"]
        info["size_gb"] = m["size_gb"]
        # Extract quant from name
        for part in m["name"].split():
            if part.startswith("Q") and "_" in part:
                info["quant"] = part
                break

    # Try to parse from model path
    model_path = server_config.get("model_path", "") or ""
    if model_path:
        import re
        basename = os.path.basename(model_path)

        # Detect quant from filename (e.g., Q3_K_XL, Q4_K_M, Q8_0)
        quant_match = re.search(r'(Q\d+_K(?:_[A-Z]+)?|Q\d+_\d+|IQ\d+_[A-Z]+)', basename, re.IGNORECASE)
        if quant_match:
            info["quant"] = quant_match.group(1).upper()

        # Detect model name from path
        name_patterns = [
            (r'gemma[-_]?4[-_]?(E?\d+[bB])', 'Gemma 4'),
            (r'qwen[-_]?3\.?5[-_]?(\d+[bB])', 'Qwen 3.5'),
            (r'llama[-_]?3[-_.]?(\d+[bB])', 'Llama 3'),
            (r'mistral[-_]?(\d+[bB])', 'Mistral'),
            (r'phi[-_]?(\d+)', 'Phi'),
        ]
        for pattern, prefix in name_patterns:
            m = re.search(pattern, basename, re.IGNORECASE)
            if m:
                info["name"] = f"{prefix} {m.group(1).upper()}"
                break

        # Detect file size
        if os.path.exists(model_path):
            try:
                size_bytes = os.path.getsize(model_path)
                info["size_gb"] = round(size_bytes / (1024**3), 1)
            except OSError:
                pass

    return info


def _build_dashboard_layout(model_id=None):
    """Build the full dashboard as a single Rich renderable (for clear-screen rendering)."""
    from rich.columns import Columns
    from rich.text import Text
    from rich.rule import Rule

    specs = get_machine_specs()
    diag = diagnose_gpu_health(model_id)
    top_procs = get_top_memory_processes(min_mb=80, limit=8)
    swap_mb = get_swap_usage_mb()

    # ── Status Bar (full-width colored line) ──
    status_map = {
        "healthy": ("green", "HEALTHY"),
        "degraded": ("yellow", "DEGRADED"),
        "critical": ("red", "CRITICAL"),
        "unknown": ("dim", "UNKNOWN"),
    }
    sc, sl = status_map.get(diag["status"], ("dim", "?"))
    status_bar = Rule(title=f"[bold {sc}] {sl} [/bold {sc}]", style=sc)

    # ── Header ──
    header = Text()
    header.append(f"  {specs['chip']}  ·  {specs['ram_gb']}GB RAM  ·  ", style="bold")
    header.append(f"{specs.get('gpu_cores', '?')} GPU cores", style="bold")

    # ── Model Info Line ──
    model_info_obj = _detect_model_info(diag["server_config"], model_id)
    model_line = None
    if model_info_obj["name"]:
        parts = []
        parts.append(f"[bold cyan]{model_info_obj['name']}[/bold cyan]")
        if model_info_obj["quant"]:
            parts.append(f"[yellow]{model_info_obj['quant']}[/yellow]")
        if model_info_obj["size_gb"]:
            parts.append(f"[dim]{model_info_obj['size_gb']}GB[/dim]")
        model_line = Text.from_markup("  " + " · ".join(parts))

    # ── Status Cards (equal height, horizontal row) ──
    CARD_HEIGHT = 6  # content lines per card (excluding border)

    gpu_on = diag["on_gpu"]
    compute_lines = []
    if diag["server_config"].get("running"):
        icon = "[green]●[/]" if gpu_on else "[red]●[/]"
        compute_lines.append(f"{icon} {'GPU (Metal)' if gpu_on else 'CPU — SLOW!'}")
        compute_lines.append(f"  Layers: {diag['gpu_layers']}/99")
        compute_lines.append(f"  Util: {diag['gpu_util_pct']}%")
        compute_lines.append(f"  Model: {diag['server_config'].get('footprint_mb', 0)} MB")
        if not gpu_on:
            compute_lines.append("[dim]GPU = 20x faster[/]")
            compute_lines.append("[dim]Use -ngl 99[/]")
    else:
        compute_lines.append("[dim]Server not running[/]")

    kv_lines = []
    kv_ok = diag["kv_quantized"]
    kv_icon = "[green]●[/]" if kv_ok else "[red]●[/]"
    kv_lines.append(f"{kv_icon} {'Quantized' if kv_ok else 'Full (2x mem!)'}")
    if diag["kv_type"]:
        kv_lines.append(f"  Type: {diag['kv_type']}")
    kv_lines.append(f"  Size: ~{diag['kv_cache_est_mb']} MB")
    kv_lines.append(f"  Ctx: {diag['context_size'] // 1024}K")
    fa_icon = "[green]●[/]" if diag["flash_attn"] else "[yellow]●[/]"
    kv_lines.append(f"{fa_icon} FlashAttn: {'on' if diag['flash_attn'] else 'off'}")

    pressure_color = {"normal": "green", "warn": "yellow", "critical": "red"}.get(diag["mem_pressure"], "dim")
    swap_color = "red" if diag["swap_thrashing"] else "green"
    gpu_headroom = diag["gpu_total_mb"] - diag["gpu_alloc_mb"]
    hr_color = "green" if gpu_headroom > 2048 else "yellow" if gpu_headroom > 0 else "red"
    mem_lines = [
        f"  Pressure: [{pressure_color}]{diag['mem_pressure']}[/{pressure_color}]",
        f"  Swap: [{swap_color}]{swap_mb // 1024}GB[/{swap_color}]",
        f"  GPU: {diag['gpu_alloc_mb'] // 1024}/{diag['gpu_total_mb'] // 1024}GB",
        f"  Free: [{hr_color}]{gpu_headroom // 1024}GB[/{hr_color}]",
    ]
    if diag["swap_thrashing"]:
        mem_lines.append("[dim]Swap = 100x slower[/]")

    # Pad all cards to the same height
    for card_lines in (compute_lines, kv_lines, mem_lines):
        while len(card_lines) < CARD_HEIGHT:
            card_lines.append("")

    cards = Columns([
        Panel("\n".join(compute_lines), title="[bold]Compute[/]", border_style="cyan", width=26, padding=(0, 1)),
        Panel("\n".join(kv_lines), title="[bold]KV Cache[/]", border_style="cyan", width=26, padding=(0, 1)),
        Panel("\n".join(mem_lines), title="[bold]Memory[/]", border_style="cyan", width=26, padding=(0, 1)),
    ], padding=1)

    # ── VRAM Usage Bar ──
    gpu_budget_mb = diag["gpu_total_mb"] if diag["gpu_total_mb"] > 0 else (specs["ram_gb"] * 1024 * 75 // 100)
    model_mb = diag.get("model_size_mb", 0) or (diag["server_config"].get("footprint_mb", 0))
    kv_mb = diag["kv_cache_est_mb"]
    apps_mb = max(0, diag["gpu_alloc_mb"] - model_mb - kv_mb)
    free_mb = max(0, gpu_budget_mb - model_mb - kv_mb - apps_mb)

    BAR_WIDTH = 50
    total_for_bar = max(1, gpu_budget_mb)
    seg_model = max(0, int(BAR_WIDTH * model_mb / total_for_bar))
    seg_kv = max(0, int(BAR_WIDTH * kv_mb / total_for_bar))
    seg_apps = max(0, int(BAR_WIDTH * apps_mb / total_for_bar))
    seg_free = max(0, BAR_WIDTH - seg_model - seg_kv - seg_apps)

    vram_bar = Text()
    vram_bar.append("  VRAM ", style="bold")
    vram_bar.append("\u2588" * seg_model, style="cyan")
    vram_bar.append("\u2588" * seg_kv, style="magenta")
    vram_bar.append("\u2588" * seg_apps, style="yellow")
    vram_bar.append("\u2591" * seg_free, style="dim")
    vram_bar.append(f"  {gpu_budget_mb // 1024}GB", style="dim")

    vram_legend = Text.from_markup(
        "       [cyan]\u2588[/] Model"
        f" ({model_mb // 1024}G)"
        "  [magenta]\u2588[/] KV Cache"
        f" ({kv_mb // 1024}G)"
        "  [yellow]\u2588[/] Apps"
        f" ({apps_mb // 1024}G)"
        "  [dim]\u2591[/] Free"
        f" ({free_mb // 1024}G)"
    )

    # ── Process Table ──
    table = Table(
        show_header=True, header_style="bold",
        border_style="dim", padding=(0, 1), expand=False, width=82,
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Process", min_width=18)
    table.add_column("Memory", justify="right", width=8)
    table.add_column("Type", width=6)
    table.add_column("", min_width=14)

    total_reclaimable = 0
    for i, p in enumerate(top_procs, 1):
        mb = p["mb"]
        name = p["name"]
        count = p.get("count", 1)
        label = f"{name}" + (f" \u00d7{count}" if count > 1 else "")

        cat_style = {"ml": "[cyan]ML[/]", "app": "[yellow]app[/]", "system": "[dim]sys[/]", "bloat": "[red]bloat[/]"}
        cat = cat_style.get(p["category"], "[dim]?[/]")

        bar_width = min(14, max(1, mb // 300))
        bar_color = "red" if mb > 2000 else "yellow" if mb > 500 else "green"
        bar = f"[{bar_color}]{'\u2588' * bar_width}[/{bar_color}]"

        size_str = f"{mb / 1024:.1f}G" if mb >= 1024 else f"{mb}M"
        table.add_row(str(i), label, size_str, cat, bar)

        if p["category"] in ("app", "bloat") and p["killable"]:
            total_reclaimable += mb

    # ── Fixes ──
    fix_lines = []
    if diag["issues"]:
        for issue in diag["issues"]:
            fix_lines.append(f"  [red]\u25cf[/] {issue}")
        fix_lines.append("")

    # Bloat fixes
    for p in top_procs:
        if p["category"] == "bloat" and p["mb"] > 500:
            freed = p["mb"] // 1024
            fix_lines.append(f"  [green]\u2192[/] Kill {p['name']} [dim](~{freed}GB — auto-restarts lean)[/]")
    # App fixes
    for p in top_procs:
        if p["category"] == "app" and p["mb"] > 500:
            count = p.get("count", 1)
            freed = p["mb"] // 1024
            name = p["name"]
            if name == "Chrome":
                fix_lines.append(f"  [green]\u2192[/] Close Chrome tabs [dim]({count} procs = ~{freed}GB)[/]")
            elif "claude" in name.lower():
                fix_lines.append(f"  [green]\u2192[/] Close Claude windows [dim]({count} = ~{freed}GB)[/]")
            elif freed >= 1:
                fix_lines.append(f"  [green]\u2192[/] Quit {name} [dim](~{freed}GB)[/]")

    if total_reclaimable > 2000:
        fix_lines.append("")
        fix_lines.append(f"  [bold]Reclaimable: ~{total_reclaimable // 1024}GB[/]  \u00b7  [dim]localcoder --cleanup[/]")

    fixes_panel = None
    if fix_lines:
        border = "red" if diag["status"] == "critical" else "yellow" if diag["status"] == "degraded" else "dim"
        fixes_panel = Panel("\n".join(fix_lines), title="[bold]Fixes[/]", border_style=border, padding=(0, 1))

    # ── Glossary (noob-friendly, using a borderless Rich Table for alignment) ──
    glossary_table = Table(show_header=False, show_edge=False, show_lines=False,
                           box=None, padding=(0, 1), expand=False)
    glossary_table.add_column("Term", style="dim", width=14, no_wrap=True)
    glossary_table.add_column("Description", style="dim")

    glossary_entries = [
        ("KV Cache",      "Stores conversation history in GPU. Grows with context length.\n"
                          "128K ctx = 630MB (q4_0) or 1.2GB (f16). Use -ctk q4_0 to halve it."),
        ("Quantization",  "Compresses model weights: Q3=small  Q4=sweet spot  Q8=best quality.\n"
                          "Rule: ~0.7GB per 1B params at Q4. 26B Q3 = 12GB, Q4 = 18GB."),
        ("GPU Layers",    "-ngl 99 = all on GPU (fast). Partial offload = 5-10x slower."),
        ("Flash Attn",    "-fa on = memory-efficient attention. Always enable it."),
        ("Swap",          "RAM overflow to disk. 100x slower. Keep under 2GB."),
        ("MoE",           "Mixture of Experts -- only 4B of 26B active per token."),
        ("Metal Limit",   "macOS reserves ~25% RAM. Override: sudo sysctl iogpu.wired_limit_mb=N"),
    ]
    for term, desc in glossary_entries:
        glossary_table.add_row(term, desc)

    glossary = Panel(glossary_table, title="[bold dim]What do these mean?[/]", border_style="dim", padding=(0, 1))

    return status_bar, header, model_line, cards, vram_bar, vram_legend, table, fixes_panel, glossary, diag


def _build_status_bar(diag, specs):
    """Build a pinned bottom status bar like Claude Code / btop."""
    from rich.text import Text

    swap_mb = get_swap_usage_mb()
    gpu_alloc = diag.get("gpu_alloc_mb", 0)
    gpu_total = diag.get("gpu_total_mb", 0)
    pressure = diag.get("mem_pressure", "?")

    # Color-code values
    pc = {"normal": "green", "warn": "yellow", "critical": "red"}.get(pressure, "dim")
    sc = "red" if swap_mb > 4000 else "yellow" if swap_mb > 1000 else "green"
    gc = "red" if gpu_alloc > gpu_total else "yellow" if gpu_alloc > gpu_total * 0.8 else "green"

    bar = Text()
    bar.append(" GPU ", style="bold white on blue")
    bar.append(" ")
    bar.append(f"{gpu_alloc // 1024}/{gpu_total // 1024}GB", style=gc)
    bar.append("  ")
    bar.append(" SWAP ", style="bold white on blue")
    bar.append(" ")
    bar.append(f"{swap_mb // 1024}GB", style=sc)
    bar.append("  ")
    bar.append(" MEM ", style="bold white on blue")
    bar.append(" ")
    bar.append(f"{pressure}", style=pc)
    bar.append("    ")

    # Shortcuts
    bar.append(" h ", style="bold black on white")
    bar.append(" health ", style="dim")
    bar.append(" c ", style="bold black on white")
    bar.append(" cleanup ", style="dim")
    bar.append(" d ", style="bold black on white")
    bar.append(" debloat ", style="dim")
    bar.append(" s ", style="bold black on white")
    bar.append(" simulate ", style="dim")

    return bar


def print_health_dashboard(model_id=None):
    """Render GPU health dashboard — clear screen, fixed width, status bar at bottom."""
    import shutil

    term_w, term_h = shutil.get_terminal_size()

    # Use a fixed-width console to prevent stretching
    from rich.console import Console as _Console
    out = _Console(width=min(90, term_w), highlight=False)

    # Phase 1: Loading spinner
    out.clear()
    loading = out.status("[bold cyan]  Scanning GPU, processes, server...[/]", spinner="dots")
    loading.start()

    specs = get_machine_specs()
    diag = diagnose_gpu_health(model_id)

    loading.stop()

    # Phase 2: Build layout
    result = _build_dashboard_layout(model_id)
    status_bar_top, header, model_line, cards, vram_bar, vram_legend, table, fixes_panel, glossary, _diag = result

    # Phase 3: Clear and render all at once
    out.clear()

    out.print(status_bar_top)
    out.print(header)
    if model_line:
        out.print(model_line)
    out.print()
    out.print(cards)
    out.print(vram_bar)
    out.print(vram_legend)
    out.print()
    out.print(table)
    if fixes_panel:
        out.print(fixes_panel)

    # Glossary only if space
    if term_h > 42:
        out.print(glossary)

    # Status bar at bottom (no ANSI cursor tricks — just print it)
    status_bar_widget = _build_status_bar(diag, specs)
    out.print()
    out.print(status_bar_widget)
    out.print()

    return diag


def check_backend_installed(backend_id):
    """Check if a backend binary exists."""
    b = BACKENDS[backend_id]
    # Also check in PATH
    binary = b["binary"]
    if binary.exists():
        return True
    if shutil.which(binary.name):
        return True
    return False


def check_backend_running(backend_id):
    """Check if backend server is responding."""
    b = BACKENDS[backend_id]
    port = b["default_port"]
    try:
        url = f"http://127.0.0.1:{port}/v1/models"
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return True
    except:
        return False


def get_running_models(backend_id):
    """Get list of models from a running backend."""
    b = BACKENDS[backend_id]
    port = b["default_port"]
    try:
        url = f"http://127.0.0.1:{port}/v1/models"
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        return [m.get("id", "") for m in data.get("data", [])]
    except:
        return []


def discover_all():
    """Discover all backends and their models."""
    results = []
    for bid, b in BACKENDS.items():
        installed = check_backend_installed(bid)
        running = check_backend_running(bid) if installed else False
        models = get_running_models(bid) if running else []
        results.append({
            "id": bid,
            "name": b["name"],
            "installed": installed,
            "running": running,
            "models": models,
            "port": b["default_port"],
        })
    return results


def install_backend(backend_id):
    """Install a backend (macOS, Linux, WSL)."""
    b = BACKENDS[backend_id]
    console.print(f"\n  [bold]Installing {b['name']}...[/]")
    console.print(f"  [dim]{b['install_cmd']}[/]\n")

    r = subprocess.run(["bash", "-c", b["install_cmd"]], timeout=600)
    if r.returncode == 0:
        # Re-discover binary path after install
        BACKENDS[backend_id]["binary"] = _find_binary(
            "llama-server" if backend_id == "llamacpp" else "ollama",
            [BACKENDS[backend_id]["binary"]]
        )
    return r.returncode == 0


def download_model_hf(model_id):
    """Download a model from HuggingFace."""
    m = MODELS[model_id]
    if not m.get("hf_repo"):
        console.print(f"  [red]No HuggingFace repo for {model_id}[/]")
        return None

    local_dir = MODELS_DIR / model_id
    console.print(f"\n  [bold]Downloading {m['name']}...[/]")
    console.print(f"  [dim]From: {m['hf_repo']}[/]")
    console.print(f"  [dim]To:   {local_dir}[/]")
    console.print(f"  [dim]Size: ~{m['size_gb']} GB[/]\n")

    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=m["hf_repo"],
            local_dir=str(local_dir),
            allow_patterns=m.get("hf_pattern", "*").split(",") if m.get("hf_pattern") else None,
        )
        return str(local_dir)
    except ImportError:
        # Fallback to CLI
        cmd = ["huggingface-cli", "download", m["hf_repo"], "--local-dir", str(local_dir)]
        if m.get("hf_pattern"):
            cmd += ["--include", m["hf_pattern"]]
        r = subprocess.run(cmd, timeout=1800)
        return str(local_dir) if r.returncode == 0 else None


def download_model_ollama(model_id):
    """Pull a model via Ollama."""
    m = MODELS[model_id]
    tag = m.get("ollama_tag")
    if not tag:
        return False
    console.print(f"\n  [bold]Pulling {tag} via Ollama...[/]")
    r = subprocess.run(["ollama", "pull", tag], timeout=1800)
    return r.returncode == 0


def find_model_file(model_id):
    """Find the GGUF file for a model."""
    local_dir = MODELS_DIR / model_id
    if not local_dir.exists():
        # Check HF cache
        cache_dir = HOME / ".cache/huggingface/hub"
        m = MODELS.get(model_id, {})
        if m.get("hf_repo"):
            repo_dir = cache_dir / f"models--{m['hf_repo'].replace('/', '--')}"
            if repo_dir.exists():
                for f in repo_dir.rglob("*.gguf"):
                    if "mmproj" not in f.name:
                        return str(f)
        return None

    # Find the GGUF file in local dir
    for f in local_dir.rglob("*.gguf"):
        if "mmproj" not in f.name:
            return str(f)
    return None


def find_mmproj_file(model_id):
    """Find the vision projector file for a model."""
    local_dir = MODELS_DIR / model_id
    search_dirs = [local_dir]

    # Also check HF cache
    m = MODELS.get(model_id, {})
    if m.get("hf_repo"):
        cache_dir = HOME / ".cache/huggingface/hub" / f"models--{m['hf_repo'].replace('/', '--')}"
        search_dirs.append(cache_dir)

    for d in search_dirs:
        if d.exists():
            for f in d.rglob("*mmproj*"):
                return str(f)
    return None


def start_llama_server(model_id, port=8089):
    """Start llama-server with a model."""
    m = MODELS.get(model_id, {})
    model_file = find_model_file(model_id)
    if not model_file:
        console.print(f"  [red]Model file not found for {model_id}[/]")
        return None

    binary = str(BACKENDS["llamacpp"]["binary"])
    if not os.path.exists(binary):
        binary = shutil.which("llama-server")
    if not binary:
        console.print(f"  [red]llama-server not found[/]")
        return None

    flags = m.get("server_flags", "-ngl 99 -c 32768 --jinja").split()
    cmd = [binary, "-m", model_file, "--port", str(port)] + flags

    # Add mmproj if available
    mmproj = find_mmproj_file(model_id)
    if mmproj:
        cmd += ["--mmproj", mmproj]
    else:
        cmd += ["--no-mmproj"]

    console.print(f"  [dim]Starting: {' '.join(os.path.basename(c) if '/' in c else c for c in cmd[:6])}...[/]")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for server
    for i in range(60):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=1):
                console.print(f"  [green]✓ Server ready on port {port}[/]")
                return proc
        except:
            time.sleep(1)

    console.print(f"  [red]Server failed to start[/]")
    proc.kill()
    return None


def get_gpu_memory_info():
    """Get GPU memory total and available (macOS Metal)."""
    info = {"total_mb": 0, "free_mb": 0, "used_by_llama_mb": 0}
    ram = get_system_ram_gb()
    if IS_MAC:
        # Metal GPU limit is ~67% of unified memory
        info["total_mb"] = int(ram * 1024 * 0.67)
        info["free_mb"] = info["total_mb"]

        # Check if llama-server is using GPU
        try:
            out = subprocess.run(["pgrep", "-f", "llama-server"], capture_output=True, text=True)
            if out.stdout.strip():
                pid = out.stdout.strip().split()[0]
                rss = subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True)
                if rss.stdout.strip():
                    info["used_by_llama_mb"] = int(rss.stdout.strip()) // 1024
                    info["free_mb"] = max(0, info["total_mb"] - info["used_by_llama_mb"])
        except:
            pass
    else:
        info["total_mb"] = ram * 1024
        info["free_mb"] = info["total_mb"]
    return info


def get_llama_server_config():
    """Parse running llama-server process flags and API state."""
    config = {
        "running": False,
        "pid": None,
        "model_path": None,
        "ngl": 0,           # GPU layers (-ngl)
        "n_ctx": 0,         # Context size (-c)
        "kv_quant": None,   # KV cache quantization type (-ctk/-ctv)
        "flash_attn": False, # Flash attention (-fa)
        "footprint_mb": 0,  # Process memory footprint
        "flags": [],
    }

    try:
        out = subprocess.run(
            ["pgrep", "-f", "llama-server"], capture_output=True, text=True,
        )
        pids = out.stdout.strip().splitlines()
        if not pids:
            return config
        config["running"] = True
        config["pid"] = int(pids[0].strip())

        # Get full command line
        cmd_out = subprocess.run(
            ["ps", "-o", "args=", "-p", str(config["pid"])],
            capture_output=True, text=True,
        )
        args = cmd_out.stdout.strip().split()
        config["flags"] = args

        # Parse flags
        for i, arg in enumerate(args):
            if arg == "-ngl" and i + 1 < len(args):
                config["ngl"] = int(args[i + 1])
            elif arg == "-c" and i + 1 < len(args):
                config["n_ctx"] = int(args[i + 1])
            elif arg == "-ctk" and i + 1 < len(args):
                config["kv_quant"] = args[i + 1]
            elif arg == "-fa":
                config["flash_attn"] = True
            elif arg == "-m" and i + 1 < len(args):
                config["model_path"] = args[i + 1]

        # Get process memory footprint
        if IS_MAC:
            config["footprint_mb"] = _parse_footprint_mb(config["pid"])
        else:
            try:
                rss = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(config["pid"])],
                    capture_output=True, text=True,
                )
                if rss.stdout.strip():
                    config["footprint_mb"] = int(rss.stdout.strip()) // 1024
            except Exception:
                pass

    except Exception:
        pass

    return config


def get_metal_gpu_stats():
    """Get real GPU stats — Metal on macOS, nvidia-smi on Linux."""
    stats = {
        "total_mb": 0,
        "alloc_mb": 0,
        "in_use_mb": 0,
        "free_vram_bytes": 0,
        "utilization_pct": 0,
        "temperature_c": None,
        "fan_pct": None,
        "power_w": None,
        "gpu_name": None,
    }

    if IS_MAC:
        try:
            import re
            out = subprocess.run(
                ["ioreg", "-l"], capture_output=True, text=True, timeout=10,
            )
            for line in out.stdout.splitlines():
                if "VRAM,totalMB" in line:
                    m = re.search(r'"VRAM,totalMB"=(\d+)', line)
                    if m:
                        stats["total_mb"] = int(m.group(1))
                if "PerformanceStatistics" in line and "Alloc system memory" in line:
                    m = re.search(r'"Alloc system memory"=(\d+)', line)
                    if m:
                        stats["alloc_mb"] = int(m.group(1)) // (1024 * 1024)
                    m2 = re.search(r'"In use system memory"=(\d+)', line)
                    if m2:
                        stats["in_use_mb"] = int(m2.group(1)) // (1024 * 1024)
                    m3 = re.search(r'"Device Utilization %"=(\d+)', line)
                    if m3:
                        stats["utilization_pct"] = int(m3.group(1))

            # Thermal state on macOS (approximate — no direct GPU temp on Apple Silicon)
            try:
                therm = subprocess.run(
                    ["pmset", "-g", "therm"], capture_output=True, text=True, timeout=3,
                )
                if "CPU_Scheduler_Limit" in therm.stdout:
                    # Thermal throttling active
                    stats["temperature_c"] = 95  # approximate
            except Exception:
                pass

        except Exception:
            pass

    elif IS_LINUX:
        # nvidia-smi for NVIDIA GPUs
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,fan.speed,power.draw",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                parts = [p.strip() for p in out.stdout.strip().split(",")]
                if len(parts) >= 8:
                    stats["gpu_name"] = parts[0]
                    stats["total_mb"] = int(float(parts[1]))
                    stats["alloc_mb"] = int(float(parts[2]))
                    stats["in_use_mb"] = int(float(parts[2]))
                    stats["utilization_pct"] = int(float(parts[5]))
                    try:
                        stats["temperature_c"] = int(float(parts[6]))
                    except (ValueError, IndexError):
                        pass
                    try:
                        stats["fan_pct"] = int(float(parts[7].replace("%", "")))
                    except (ValueError, IndexError):
                        pass
                    try:
                        stats["power_w"] = float(parts[8])
                    except (ValueError, IndexError):
                        pass
        except FileNotFoundError:
            # No NVIDIA GPU — check for AMD via rocm-smi
            try:
                out = subprocess.run(
                    ["rocm-smi", "--showmeminfo", "vram", "--csv"],
                    capture_output=True, text=True, timeout=5,
                )
                if out.returncode == 0:
                    for line in out.stdout.splitlines()[1:]:
                        parts = line.split(",")
                        if len(parts) >= 3:
                            stats["total_mb"] = int(parts[0]) // (1024 * 1024)
                            stats["alloc_mb"] = int(parts[1]) // (1024 * 1024)
            except FileNotFoundError:
                pass

    return stats


def get_disk_info():
    """Get disk space and model storage info."""
    info = {
        "disk_total_gb": 0,
        "disk_free_gb": 0,
        "hf_cache_gb": 0,
        "models": [],  # list of {name, size_gb, path}
        "docker_gb": 0,
    }
    try:
        # Disk space
        st = os.statvfs(HOME)
        info["disk_total_gb"] = round((st.f_blocks * st.f_frsize) / (1024**3))
        info["disk_free_gb"] = round((st.f_bavail * st.f_frsize) / (1024**3))

        # HuggingFace cache total
        hf_cache = HOME / ".cache/huggingface/hub"
        if hf_cache.exists():
            total = 0
            # Sum blob sizes (the real files, not symlinks)
            blobs_dir = hf_cache
            for blob in blobs_dir.rglob("*"):
                if blob.is_file() and not blob.is_symlink():
                    total += blob.stat().st_size
            info["hf_cache_gb"] = round(total / (1024**3))

        # Individual GGUF models
        for gguf in hf_cache.rglob("*.gguf") if hf_cache.exists() else []:
            name = gguf.name
            if "mmproj" in name.lower():
                continue
            real = gguf.resolve()
            try:
                sz = real.stat().st_size / (1024**3)
                info["models"].append({"name": name, "size_gb": round(sz, 1), "path": str(real)})
            except OSError:
                pass
        info["models"].sort(key=lambda x: x["size_gb"], reverse=True)

        # Docker (if running)
        try:
            out = subprocess.run(["docker", "system", "df", "--format", "{{.Size}}"],
                                 capture_output=True, text=True, timeout=3)
            if out.returncode == 0:
                for line in out.stdout.strip().splitlines():
                    line = line.strip().upper()
                    if "GB" in line:
                        info["docker_gb"] += float(line.replace("GB", ""))
                    elif "MB" in line:
                        info["docker_gb"] += float(line.replace("MB", "")) / 1024
                info["docker_gb"] = round(info["docker_gb"])
        except (FileNotFoundError, Exception):
            pass
    except Exception:
        pass
    return info


def get_swap_usage_mb():
    """Get swap usage in MB."""
    try:
        if IS_MAC:
            out = subprocess.run(
                ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=3,
            )
            # "total = 10240.00M  used = 8538.06M  free = 1701.94M"
            for part in out.stdout.split():
                if part.endswith("M") and "used" not in out.stdout.split()[out.stdout.split().index(part) - 1]:
                    continue
            import re
            m = re.search(r'used\s*=\s*([\d.]+)M', out.stdout)
            if m:
                return int(float(m.group(1)))
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("SwapTotal:"):
                        total = int(line.split()[1]) // 1024
                    if line.startswith("SwapFree:"):
                        free = int(line.split()[1]) // 1024
                        return total - free
    except Exception:
        pass
    return 0


def diagnose_gpu_health(model_id=None):
    """Full GPU health diagnostic. Returns dict with status and recommendations.

    Checks:
    1. Is model running on GPU or CPU?
    2. Is KV cache optimized?
    3. Is context size appropriate?
    4. Is swap thrashing happening?
    5. Are flags optimal?
    """
    diag = {
        "status": "unknown",     # "healthy", "degraded", "critical"
        "on_gpu": False,
        "gpu_layers": 0,
        "total_layers": 99,
        "kv_quantized": False,
        "kv_type": None,
        "flash_attn": False,
        "context_size": 0,
        "kv_cache_est_mb": 0,
        "model_size_mb": 0,
        "gpu_total_mb": 0,
        "gpu_alloc_mb": 0,
        "gpu_util_pct": 0,
        "swap_used_mb": 0,
        "swap_thrashing": False,
        "mem_pressure": "unknown",
        "issues": [],
        "fixes": [],
        "server_config": {},
    }

    # Get server config
    srv = get_llama_server_config()
    diag["server_config"] = srv

    if not srv["running"]:
        diag["status"] = "unknown"
        diag["issues"].append("llama-server not running")
        return diag

    # GPU layer offload
    diag["gpu_layers"] = srv["ngl"]
    diag["on_gpu"] = srv["ngl"] >= 90  # -ngl 99 means all on GPU
    diag["context_size"] = srv["n_ctx"]
    diag["flash_attn"] = srv["flash_attn"]

    if not diag["on_gpu"]:
        diag["issues"].append(f"Only {srv['ngl']} layers on GPU — model partially on CPU")
        diag["fixes"].append("Restart with -ngl 99 to offload all layers to GPU")

    # KV cache
    diag["kv_type"] = srv["kv_quant"]
    diag["kv_quantized"] = srv["kv_quant"] in ("q4_0", "q8_0", "q4_1", "f16")
    if not diag["kv_quantized"]:
        diag["issues"].append("KV cache not quantized — using full precision (2x memory)")
        diag["fixes"].append("Add -ctk q4_0 -ctv q4_0 to quantize KV cache (saves ~50% KV memory)")

    if not diag["flash_attn"]:
        diag["issues"].append("Flash attention disabled — slower and more memory")
        diag["fixes"].append("Add -fa on to enable flash attention")

    # Estimate KV cache memory
    # For Gemma 4 26B: 5 global layers × 128K context × 2 (K+V) × hidden_dim
    # With q4_0: ~630MB. Without quantization: ~1.2GB
    if diag["context_size"] > 0:
        # Rough estimate: 128K ctx with q4_0 KV ≈ 630MB, without ≈ 1200MB
        ctx_ratio = diag["context_size"] / 131072
        if diag["kv_quantized"]:
            diag["kv_cache_est_mb"] = int(630 * ctx_ratio)
        else:
            diag["kv_cache_est_mb"] = int(1200 * ctx_ratio)

    # Model size
    if model_id and model_id in MODELS:
        diag["model_size_mb"] = int(MODELS[model_id]["size_gb"] * 1024)

    # Metal GPU stats
    metal = get_metal_gpu_stats()
    diag["gpu_total_mb"] = metal["total_mb"]
    diag["gpu_alloc_mb"] = metal["alloc_mb"]
    diag["gpu_util_pct"] = metal["utilization_pct"]

    # Swap check
    diag["swap_used_mb"] = get_swap_usage_mb()
    diag["swap_thrashing"] = diag["swap_used_mb"] > 4000  # >4GB swap = bad

    if diag["swap_thrashing"]:
        diag["issues"].append(f"Swap thrashing: {diag['swap_used_mb'] // 1024}GB in swap — major slowdown")
        diag["fixes"].append("Reduce context size (-c 32768) or use smaller quant to free GPU memory")

    # Memory pressure
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=3,
        )
        level = int(out.stdout.strip())
        diag["mem_pressure"] = {0: "normal", 1: "warn", 2: "critical", 4: "critical"}.get(level, "unknown")
    except Exception:
        pass

    if diag["mem_pressure"] == "critical":
        diag["issues"].append("Critical memory pressure — system may kill processes")
        diag["fixes"].append("Run: localcoder --cleanup")

    # Context size warnings
    if diag["context_size"] > 65536 and not diag["kv_quantized"]:
        diag["issues"].append(f"Large context ({diag['context_size']//1024}K) without KV quantization")
        diag["fixes"].append("Either reduce context or add -ctk q4_0 -ctv q4_0")

    # Check if Metal limit could be raised
    if IS_MAC and diag["gpu_total_mb"] > 0:
        ram_mb = get_system_ram_gb() * 1024
        current_limit = diag["gpu_total_mb"]
        max_safe = int(ram_mb * 0.90)  # leave 10% for system
        if current_limit < max_safe and diag["swap_thrashing"]:
            new_limit = max_safe
            diag["fixes"].append(
                f"Raise Metal GPU limit: sudo sysctl iogpu.wired_limit_mb={new_limit}"
                f" (current: {current_limit}MB, max safe: {new_limit}MB)"
            )

    # Overall status
    if not diag["issues"]:
        diag["status"] = "healthy"
    elif diag["swap_thrashing"] or not diag["on_gpu"] or diag["mem_pressure"] == "critical":
        diag["status"] = "critical"
    else:
        diag["status"] = "degraded"

    return diag


def print_gpu_health(diag=None, model_id=None):
    """Print GPU health diagnostic panel."""
    if diag is None:
        diag = diagnose_gpu_health(model_id)

    status_style = {
        "healthy": ("green", "✓ Healthy"),
        "degraded": ("yellow", "⚠ Degraded"),
        "critical": ("red", "✗ Critical"),
        "unknown": ("dim", "? Unknown"),
    }
    color, label = status_style.get(diag["status"], ("dim", "?"))

    lines = []

    # GPU offload status
    if diag["server_config"].get("running"):
        gpu_icon = "[green]●[/] GPU" if diag["on_gpu"] else "[red]●[/] CPU (SLOW!)"
        lines.append(f"  Compute: {gpu_icon}  ·  {diag['gpu_layers']} layers offloaded  ·  GPU util: {diag['gpu_util_pct']}%")

        # KV cache
        kv_icon = "[green]●[/]" if diag["kv_quantized"] else "[red]●[/]"
        kv_info = f"quantized ({diag['kv_type']})" if diag["kv_quantized"] else "full precision (2x memory!)"
        lines.append(
            f"  KV cache: {kv_icon} {kv_info}  ·  ~{diag['kv_cache_est_mb']}MB"
            f"  ·  context: {diag['context_size'] // 1024}K tokens"
        )

        # Flash attention
        fa_icon = "[green]●[/]" if diag["flash_attn"] else "[yellow]●[/]"
        lines.append(f"  Flash attn: {fa_icon} {'on' if diag['flash_attn'] else 'off'}"
                     f"  ·  footprint: {diag['server_config'].get('footprint_mb', 0)}MB")

    # Memory
    swap_color = "red" if diag["swap_thrashing"] else "green"
    pressure_color = {"normal": "green", "warn": "yellow", "critical": "red"}.get(
        diag["mem_pressure"], "dim"
    )
    lines.append(
        f"  Memory: [{pressure_color}]{diag['mem_pressure']}[/{pressure_color}]"
        f"  ·  swap: [{swap_color}]{diag['swap_used_mb'] // 1024}GB[/{swap_color}]"
        f"  ·  GPU alloc: {diag['gpu_alloc_mb'] // 1024}GB / {diag['gpu_total_mb'] // 1024}GB"
    )

    # Issues
    if diag["issues"]:
        lines.append("")
        for issue in diag["issues"]:
            lines.append(f"  [red]✗[/] {issue}")
    if diag["fixes"]:
        lines.append("")
        for fix in diag["fixes"]:
            lines.append(f"  [green]→[/] {fix}")

    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]GPU Health [{color}]{label}[/{color}][/]",
        border_style=color,
        padding=(0, 1),
    ))

    return diag


def auto_optimize_server(model_id=None):
    """Check if server needs optimization and apply fixes.

    Returns True if server was restarted with better flags.
    """
    diag = diagnose_gpu_health(model_id)

    if diag["status"] == "healthy":
        return False

    needs_restart = False
    srv = diag["server_config"]
    model_info = MODELS.get(model_id, {}) if model_id else {}
    optimal_flags = model_info.get("server_flags", "").split() if model_info else []

    # Check if current flags are suboptimal
    if not diag["on_gpu"] and "-ngl" not in " ".join(srv.get("flags", [])):
        needs_restart = True
    if not diag["kv_quantized"] and "-ctk" not in " ".join(srv.get("flags", [])):
        needs_restart = True
    if not diag["flash_attn"] and "-fa" not in " ".join(srv.get("flags", [])):
        needs_restart = True

    if needs_restart and model_id:
        console.print("\n  [yellow]Server running with suboptimal flags — restarting with optimizations...[/]")
        # Kill current server
        if srv.get("pid"):
            try:
                subprocess.run(["kill", str(srv["pid"])], timeout=5)
                time.sleep(2)
            except Exception:
                pass
        # Start with optimal flags
        proc = start_llama_server(model_id)
        if proc:
            console.print("  [green]✓ Server restarted with optimal GPU flags[/]")
            return True
        else:
            console.print("  [red]Failed to restart server[/]")

    # If swap thrashing, try to free memory without restart
    if diag["swap_thrashing"] and not needs_restart:
        console.print("\n  [yellow]Swap thrashing detected — cleaning up GPU memory...[/]")
        cleanup_gpu_memory(force=False)

    return False


# ── macOS Debloat categories for ML workloads ──
DEBLOAT_CATEGORIES = {
    "ml_hogs": {
        "name": "ML & Analysis Daemons",
        "desc": "Apple's background ML that competes with your model for GPU",
        "safe": True,
        "services": {
            "com.apple.photoanalysisd":     "Photos face/scene ML — uses GPU + 2-8GB RAM",
            "com.apple.mediaanalysisd":     "Visual Lookup, Live Text ML — GPU heavy",
            "com.apple.suggestd":           "Siri suggestions indexer — background ML",
            "com.apple.intelligenced":      "Apple Intelligence (Sequoia) — GPU heavy",
            "com.apple.mlruntime":          "Core ML runtime — shared GPU compute",
        },
    },
    "location_bloat": {
        "name": "Location & Sync Bloat",
        "desc": "Known memory leakers on macOS 14/15",
        "safe": True,
        "services": {
            "com.apple.CoreLocationAgent":  "Location cache — leaks to 8GB+ (notorious)",
            "com.apple.remindd":            "Reminders sync — memory leak on macOS 15",
            "com.apple.cloudd":             "iCloud Drive sync — bloats with many files",
            "com.apple.bird":               "CloudKit container daemon",
        },
    },
    "telemetry": {
        "name": "Telemetry & Analytics",
        "desc": "Crash reports, analytics, diagnostics — zero impact to disable",
        "safe": True,
        "services": {
            "com.apple.analyticsd":         "Analytics collection",
            "com.apple.ReportCrash":        "Crash report generation",
            "com.apple.spindump":           "CPU sampling diagnostics",
            "com.apple.DiagnosticReportCleanup": "Diagnostic cleanup",
            "com.apple.ap.adprivacyd":      "Ad privacy daemon",
            "com.apple.ap.adservicesd":     "Ad services",
            "com.apple.triald":             "A/B testing framework",
        },
    },
    "siri_ai": {
        "name": "Siri & Apple AI",
        "desc": "Siri, assistant, Apple Intelligence",
        "safe": True,
        "services": {
            "com.apple.Siri.agent":         "Siri main service",
            "com.apple.assistantd":         "Assistant daemon",
            "com.apple.parsec.fbf":         "Siri search suggestions",
            "com.apple.tipsd":              "Tips and suggestions",
            "com.apple.ScreenTimeAgent":    "Screen time tracking",
        },
    },
}


def debloat_wizard():
    """Interactive debloat wizard for ML workloads.

    Shows categories of services that can be disabled to free GPU/memory.
    User picks categories, we disable via launchctl.
    Creates restore script.
    """
    import shutil

    console.clear()
    console.print()
    console.print("  [bold]localcoder debloat wizard[/]")
    console.print("  [dim]Disable macOS services that compete with your model for GPU & memory[/]")
    console.print("  [dim]All changes are reversible — a restore script is saved automatically[/]\n")

    # Show current bloated processes
    top_procs = get_top_memory_processes(min_mb=200, limit=5)
    bloat_procs = [p for p in top_procs if p["category"] == "bloat"]
    if bloat_procs:
        console.print("  [yellow]Currently bloated system processes:[/]")
        for p in bloat_procs:
            mb = p["mb"]
            size = f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb}MB"
            desc = SYSTEM_RESTARTABLE.get(p["name"], "")
            console.print(f"    [red]●[/] {p['name']}  [bold]{size}[/]  [dim]{desc}[/]")
        console.print()

    # Show categories
    cats = list(DEBLOAT_CATEGORIES.items())
    for i, (key, cat) in enumerate(cats, 1):
        n_services = len(cat["services"])
        console.print(f"  [bold]{i}.[/] {cat['name']}  [dim]({n_services} services)[/]")
        console.print(f"     [dim]{cat['desc']}[/]")
        for svc, desc in list(cat["services"].items())[:3]:
            console.print(f"     [dim]  · {svc.split('.')[-1]}: {desc}[/]")
        if n_services > 3:
            console.print(f"     [dim]  + {n_services - 3} more[/]")
        console.print()

    console.print(f"  [bold]k.[/] Kill bloated processes now  [dim](one-time, they may restart)[/]")
    console.print(f"  [bold]a.[/] All categories  [dim](maximum GPU headroom)[/]")
    console.print(f"  [bold]r.[/] Restore all  [dim](re-enable everything)[/]")
    console.print(f"  [bold]q.[/] Quit\n")

    try:
        ans = input("  Choose (e.g. 1,2 or a): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if ans == "q" or not ans:
        return

    if ans == "r":
        _debloat_restore()
        return

    if ans == "k":
        _kill_bloated_processes()
        return

    # Parse selection
    selected_cats = []
    if ans == "a":
        selected_cats = list(DEBLOAT_CATEGORIES.keys())
    else:
        for part in ans.replace(" ", "").split(","):
            try:
                idx = int(part) - 1
                if 0 <= idx < len(cats):
                    selected_cats.append(cats[idx][0])
            except ValueError:
                pass

    if not selected_cats:
        console.print("  [dim]No categories selected.[/]")
        return

    # Confirm
    total_services = sum(len(DEBLOAT_CATEGORIES[c]["services"]) for c in selected_cats)
    cat_names = ", ".join(DEBLOAT_CATEGORIES[c]["name"] for c in selected_cats)
    console.print(f"\n  [yellow]Will disable {total_services} services: {cat_names}[/]")
    try:
        confirm = input("  Proceed? (y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if confirm != "y":
        return

    # Disable services
    disabled = []
    restore_cmds = []
    for cat_key in selected_cats:
        cat = DEBLOAT_CATEGORIES[cat_key]
        for svc, desc in cat["services"].items():
            # Try both user and system domains
            for domain in [f"gui/{os.getuid()}", "system"]:
                cmd = ["launchctl", "disable", f"{domain}/{svc}"]
                r = subprocess.run(cmd, capture_output=True, text=True)
                # Also bootout if currently loaded
                subprocess.run(
                    ["launchctl", "bootout", f"{domain}/{svc}"],
                    capture_output=True, text=True,
                )
                restore_cmds.append(f"launchctl enable {domain}/{svc}")
            disabled.append(svc)
            console.print(f"  [green]✓[/] {svc.split('.')[-1]}  [dim]{desc}[/]")

    # Also kill currently bloated processes
    for p in bloat_procs:
        for pid in p.get("pids", [p["pid"]]):
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        console.print(f"  [green]✓[/] Killed {p['name']} (was {p['mb'] // 1024}GB)")

    # Save restore script
    restore_path = CONFIG_DIR / "restore_debloat.sh"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(restore_path, "w") as f:
        f.write("#!/bin/bash\n# localcoder debloat restore script\n")
        f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M')}\n\n")
        for cmd in restore_cmds:
            f.write(f"{cmd}\n")
        f.write('\necho "All services restored. Reboot recommended."\n')
    os.chmod(restore_path, 0o755)

    console.print(f"\n  [green]Disabled {len(disabled)} services.[/]")
    console.print(f"  [dim]Restore script: {restore_path}[/]")
    console.print(f"  [dim]Run: localcoder --debloat  then choose 'r' to restore[/]\n")


def _kill_bloated_processes():
    """Kill all currently bloated system processes (one-time)."""
    import signal
    procs = get_top_memory_processes(min_mb=300)
    bloat = [p for p in procs if p["category"] == "bloat"]
    if not bloat:
        console.print("  [dim]No bloated processes found.[/]")
        return

    freed = 0
    for p in bloat:
        for pid in p.get("pids", [p["pid"]]):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        mb = p["mb"]
        freed += mb
        console.print(f"  [green]✓[/] Killed {p['name']} ({mb // 1024}GB)")

    console.print(f"\n  [green]Freed ~{freed // 1024}GB[/]  [dim](processes may restart smaller)[/]")


def _debloat_restore():
    """Restore all debloated services."""
    restore_path = CONFIG_DIR / "restore_debloat.sh"
    if not restore_path.exists():
        console.print("  [dim]No restore script found — nothing to restore.[/]")
        return

    console.print("  [yellow]Restoring all disabled services...[/]")
    r = subprocess.run(["bash", str(restore_path)], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        console.print("  [green]All services restored. Reboot recommended.[/]")
        restore_path.unlink()
    else:
        console.print(f"  [red]Some services failed to restore: {r.stderr[:200]}[/]")


# LocalLLaMA community favorites for coding — from Best LLMs 2025 megathread
# Updated from r/LocalLLaMA actual user recommendations, not benchmarks
COMMUNITY_CODING_MODELS = {
    # <=8GB VRAM
    "lfm2-8b-a1b": {"name": "LFM2 8B-A1B", "hf": "liquid/LFM2-8B-A1B-GGUF", "vram": "8GB", "note": "Crazy fast MoE, great general + tool calling"},
    "qwen3-4b": {"name": "Qwen 3 4B", "hf": "unsloth/Qwen3-4B-GGUF", "vram": "4GB", "note": "Best tool calling at 4B size"},
    # 12-24GB VRAM (most LocalLLaMA users)
    "qwen3-coder-30b": {"name": "Qwen 3 Coder 30B-A3B", "hf": "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF", "vram": "12-24GB", "note": "Top agentic coder, MoE"},
    "nemotron-30b-a3b": {"name": "Nemotron 30B-A3B", "hf": "unsloth/Nemotron-3-Nano-30B-A3B-GGUF", "vram": "12-24GB", "note": "NVIDIA MoE, fastest generation"},
    "gemma4-26b": {"name": "Gemma 4 26B-A4B", "hf": "unsloth/gemma-4-26B-A4B-it-GGUF", "vram": "12-16GB", "note": "Best tool calling + vision, 49 tok/s"},
    "devstral-24b": {"name": "Devstral Small 24B", "hf": "lmstudio-community/Devstral-Small-2-24B-Instruct-2512-GGUF", "vram": "12-24GB", "note": "Reliable daily driver for coding"},
    "glm-4.6v-flash": {"name": "GLM 4.6V Flash", "hf": "THUDM/glm-4.6v-flash-9b-gguf", "vram": "8-12GB", "note": "Best small model of the year (r/LocalLLaMA)"},
    # 24-48GB VRAM
    "gpt-oss-20b": {"name": "GPT-OSS 20B", "hf": "unsloth/gpt-oss-20b-GGUF", "vram": "24GB", "note": "Best accuracy under 48GB"},
    "qwen3.5-35b-a3b": {"name": "Qwen 3.5 35B-A3B", "hf": "unsloth/Qwen3.5-35B-A3B-GGUF", "vram": "12-24GB", "note": "1.5M downloads, MoE coding beast"},
    # 48-96GB VRAM
    "glm-4.5-air": {"name": "GLM 4.5 Air", "hf": "THUDM/glm-4.5-9b-air-gguf", "vram": "48-96GB", "note": "Flat-out amazing for codegen (r/LocalLLaMA)"},
    # 96GB+
    "gpt-oss-120b": {"name": "GPT-OSS 120B", "hf": "unsloth/gpt-oss-120b-GGUF", "vram": "96GB+", "note": "Most recommended for agentic coding"},
    "devstral-123b": {"name": "Devstral 123B", "hf": "mistralai/Devstral-2-123B-GGUF", "vram": "96GB+", "note": "Compact 123B, fits 2x RTX Pro"},
    "minimax-m2": {"name": "MiniMax M2.1", "hf": "unsloth/MiniMax-M2.1-GGUF", "vram": "96GB+", "note": "Frontier performance, fantastic agentic coding"},
}


_hf_model_cache = {"data": None, "ts": 0}


def _fetch_all_hf_models():
    """Fetch GGUF models from all top providers in parallel. Cached for 10 minutes.

    One call, returns everything — trending, liked, latest. No duplicate fetches.
    """
    import concurrent.futures

    # Return cache if fresh
    if _hf_model_cache["data"] and time.time() - _hf_model_cache["ts"] < 600:
        return _hf_model_cache["data"]

    providers = ["unsloth", "bartowski", "lmstudio-community"]
    all_raw = []

    def _fetch_one(author):
        """Fetch from one provider — downloads sort gets us everything we need."""
        try:
            url = f"https://huggingface.co/api/models?author={author}&sort=downloads&direction=-1&limit=20"
            req = urllib.request.Request(url, headers={"User-Agent": "localcoder/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read())
        except Exception:
            return []

    # Parallel fetch — all 3 providers at once (~1 API call time instead of 3)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_fetch_one, p): p for p in providers}
            for future in concurrent.futures.as_completed(futures, timeout=10):
                author = futures[future]
                try:
                    for m in future.result():
                        m["_author"] = author
                        all_raw.append(m)
                except Exception:
                    pass
    except Exception:
        return []

    # Deduplicate by base model name, prefer unsloth > bartowski > lmstudio
    provider_rank = {"unsloth": 0, "bartowski": 1, "lmstudio-community": 2}
    seen = {}
    for m in all_raw:
        tags = m.get("tags", [])
        if "gguf" not in tags:
            continue
        dl = m.get("downloads", 0)
        if dl < 1000:
            continue

        rid = m["id"]
        base = rid.split("/")[-1].replace("-GGUF", "").replace("-Instruct", "").replace("-it", "").lower()
        author = m.get("_author", "")
        rank = provider_rank.get(author, 9)

        if base not in seen or rank < seen[base]["_rank"]:
            name = rid.split("/")[-1].replace("-GGUF", "").replace("-Instruct", "").replace("-it", "")
            tags = m.get("tags", [])

            # Detect modalities from tags
            caps = []
            if "image-text-to-text" in tags:
                caps.append("vision")
            if any("audio" in t for t in tags):
                caps.append("audio")
            if any("code" in t.lower() or "coder" in t.lower() for t in tags) or "coder" in name.lower():
                caps.append("code")
            if any("moe" in t.lower() for t in tags) or "A3B" in name or "A4B" in name or "A10B" in name:
                caps.append("MoE")

            # Estimate smallest quant size from model name
            # Rule: ~0.5GB per 1B params at Q2, MoE active params only
            import re as _re_est
            param_match = _re_est.search(r'(\d+)[bB]', name)
            active_match = _re_est.search(r'A(\d+)[bB]', name)
            est_smallest_gb = None
            if param_match:
                total_b = int(param_match.group(1))
                active_b = int(active_match.group(1)) if active_match else total_b
                # For MoE: estimate from total params, not active
                # Q2 quant ≈ 0.35 GB per 1B total params
                est_smallest_gb = round(total_b * 0.35, 1)

            seen[base] = {
                "repo_id": rid,
                "label": name,
                "downloads": dl,
                "likes": m.get("likes", 0),
                "author": author,
                "caps": caps,
                "est_smallest_gb": est_smallest_gb,
                "_rank": rank,
                "_base": base,
            }

    result = list(seen.values())
    _hf_model_cache["data"] = result
    _hf_model_cache["ts"] = time.time()
    return result


def fetch_unsloth_top_models(limit=12):
    """Top GGUF models sorted by downloads. Cached, parallel fetch."""
    models = _fetch_all_hf_models()
    models_sorted = sorted(models, key=lambda x: x["downloads"], reverse=True)
    return models_sorted[:limit]


def fetch_hf_trending_models(limit=5, sort="downloads"):
    """GGUF models sorted by downloads or likes. Cached, parallel fetch."""
    models = _fetch_all_hf_models()
    if sort == "likes":
        models_sorted = sorted(models, key=lambda x: x.get("likes", 0), reverse=True)
    else:
        models_sorted = sorted(models, key=lambda x: x["downloads"], reverse=True)
    return models_sorted[:limit]


# Legacy compat — old code referenced this directly
def _fetch_unsloth_top_compat(limit=12):
    return fetch_unsloth_top_models(limit)




def fetch_hf_model(query):
    """Fetch GGUF model info from HuggingFace.

    Accepts:
    - Full URL: https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF
    - Repo ID: unsloth/gemma-4-26B-A4B-it-GGUF
    - Search term: gemma 4 26b gguf

    Returns dict with model name, GGUF files with sizes, or None.
    """
    import re as _re

    repo_id = None

    # Parse URL
    if "huggingface.co" in query:
        # https://huggingface.co/org/model or /org/model/...
        m = _re.search(r'huggingface\.co/([^/]+/[^/\s?#]+)', query)
        if m:
            repo_id = m.group(1)
    elif "/" in query and " " not in query:
        # Direct repo ID: unsloth/gemma-4-26B-A4B-it-GGUF
        repo_id = query
    elif "ollama.com" in query:
        # Ollama URL — extract model name for search
        m = _re.search(r'ollama\.com/library/([^/\s?#]+)', query)
        if m:
            query = m.group(1) + " gguf"

    # If no repo_id, search HuggingFace
    if not repo_id:
        try:
            search_url = f"https://huggingface.co/api/models?search={urllib.parse.quote(query + ' gguf')}&sort=downloads&direction=-1&limit=5"
            req = urllib.request.Request(search_url, headers={"User-Agent": "localcoder/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read())
            # Pick first GGUF repo
            for r in results:
                if any("gguf" in t.lower() for t in r.get("tags", [])):
                    repo_id = r["id"]
                    break
            if not repo_id and results:
                repo_id = results[0]["id"]
        except Exception:
            return None

    if not repo_id:
        return None

    # Fetch model metadata + file sizes (with fallback to search)
    data = None
    try:
        api_url = f"https://huggingface.co/api/models/{repo_id}?blobs=true"
        req = urllib.request.Request(api_url, headers={"User-Agent": "localcoder/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        # Direct lookup failed — try searching with the repo name as query
        try:
            import re as _re2
            search_term = repo_id.split("/")[-1].replace("-", " ").replace("_", " ")
            # Strip version numbers for better search
            search_term = _re2.sub(r'\b\d{4}\b', '', search_term).strip()
            search_url = f"https://huggingface.co/api/models?search={urllib.parse.quote(search_term)}&sort=downloads&direction=-1&limit=3"
            req = urllib.request.Request(search_url, headers={"User-Agent": "localcoder/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read())
            if results:
                repo_id = results[0]["id"]
                api_url = f"https://huggingface.co/api/models/{repo_id}?blobs=true"
                req = urllib.request.Request(api_url, headers={"User-Agent": "localcoder/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
        except Exception:
            pass

    if not data:
        return None

    # Extract GGUF files with sizes
    gguf_files = []
    for s in data.get("siblings", []):
        name = s.get("rfilename", "")
        size = s.get("size", 0)
        if not name.endswith(".gguf") or size < 500_000_000:  # skip tiny/split files
            continue
        if "mmproj" in name.lower():
            continue  # skip vision projectors
        if "-0000" in name:
            continue  # skip split file parts (except first)

        # Parse quant from filename
        quant = "unknown"
        qm = _re.search(r'(BF16|F16|Q\d+_K(?:_[A-Z]+)?|Q\d+_\d+|IQ\d+_[A-Z]+|MXFP\d+)', name, _re.IGNORECASE)
        if qm:
            quant = qm.group(1).upper()

        gguf_files.append({
            "filename": name,
            "size_bytes": size,
            "size_gb": round(size / (1024**3), 1),
            "quant": quant,
        })

    # Sort by size ascending
    gguf_files.sort(key=lambda x: x["size_bytes"])

    return {
        "repo_id": repo_id,
        "name": data.get("id", repo_id).split("/")[-1],
        "tags": data.get("tags", []),
        "downloads": data.get("downloads", 0),
        "gguf_files": gguf_files,
    }


def simulate_hf_model(query):
    """Fetch a model from HuggingFace and show which quants fit.

    The "holy shit" feature: paste a URL, see instant fit analysis for every quant.
    """
    specs = get_machine_specs()
    metal = get_metal_gpu_stats()
    gpu_total = metal.get("total_mb") or specs["gpu_total_mb"]
    gpu_used = metal.get("alloc_mb", 0)

    console.clear()
    loading = console.status("[bold cyan]  Fetching from HuggingFace...[/]", spinner="dots")
    loading.start()

    model = fetch_hf_model(query)
    loading.stop()

    if not model:
        console.print(f"\n  [red]Model not found: {query}[/]")
        console.print(f"  [dim]Try a HuggingFace URL or search term like 'llama 3.1 70b gguf'[/]\n")
        return

    console.clear()
    console.print()
    console.print(f"  [bold]{model['repo_id']}[/]")
    console.print(f"  [dim]{specs['chip']}  ·  {specs['ram_gb']}GB RAM  ·  GPU budget: {gpu_total // 1024}GB  ·  In use: {gpu_used // 1024}GB[/]\n")

    if not model["gguf_files"]:
        console.print(f"  [yellow]No GGUF files found in this repo.[/]\n")
        return

    # Show all quants with fit status
    table = Table(
        title=f"Available Quants ({len(model['gguf_files'])})",
        show_header=True, header_style="bold", border_style="dim", padding=(0, 1),
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Quant", width=14)
    table.add_column("Size", justify="right", width=8)
    table.add_column("Fits GPU?", width=18)
    table.add_column("Est. Speed", width=12)
    table.add_column("", width=18)

    best_fit_idx = None
    for i, f in enumerate(model["gguf_files"], 1):
        size_gb = f["size_gb"]
        size_mb = int(size_gb * 1024)
        fits = size_mb < gpu_total
        fits_free = size_mb < (gpu_total - gpu_used)

        if fits_free:
            status = "[green]✓ fits[/]"
            if best_fit_idx is None or f["size_gb"] > model["gguf_files"][best_fit_idx - 1]["size_gb"]:
                best_fit_idx = i
        elif fits:
            status = "[yellow]⚠ tight[/]"
            if best_fit_idx is None:
                best_fit_idx = i
        else:
            status = "[red]✗ too big[/]"

        # Speed estimate
        tps = min(120, max(1, int(49 * 12 / max(1, size_gb)))) if fits else max(1, int(5 * 16 / max(1, size_gb)))
        speed = f"~{tps} tok/s" if fits else f"[red]~{tps} tok/s[/]"

        # Visual bar
        bar_pct = min(1.0, size_mb / gpu_total) if gpu_total else 0
        bar_w = int(bar_pct * 16)
        bar_color = "green" if fits_free else "yellow" if fits else "red"
        bar = f"[{bar_color}]{'█' * bar_w}[/{bar_color}][dim]{'░' * (16 - bar_w)}[/]"

        table.add_row(str(i), f["quant"], f"{size_gb}GB", status, speed, bar)

    console.print(table)

    # Recommendation
    is_unsloth = "unsloth" in model["repo_id"].lower()
    if best_fit_idx:
        bf = model["gguf_files"][best_fit_idx - 1]
        console.print(f"\n  [green bold]→ Best fit: #{best_fit_idx} {bf['quant']} ({bf['size_gb']}GB)[/]")
        console.print(f"    [dim]Highest quality that fits your {gpu_total // 1024}GB GPU[/]")
        if is_unsloth:
            console.print(f"    [dim]Unsloth quants use imatrix calibration — better quality than standard GGUF[/]")
    else:
        console.print(f"\n  [red]No quant fits your {gpu_total // 1024}GB GPU budget.[/]")
        smallest = model["gguf_files"][0]
        console.print(f"  [dim]Smallest: {smallest['quant']} at {smallest['size_gb']}GB (need {gpu_total // 1024}GB GPU)[/]")
        if not is_unsloth:
            console.print(f"  [dim]Tip: check unsloth/ on HuggingFace — they often have smaller K_XL quants[/]")

    # Interactive: pick one to simulate in detail or download
    console.print(f"\n  [dim]Enter # for detailed analysis, 'd #' to download, or 'q' to quit[/]\n")
    try:
        ans = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if ans == "q" or not ans:
        return

    download = False
    if ans.startswith("d ") or ans.startswith("d"):
        download = True
        ans = ans.lstrip("d ").strip()

    try:
        idx = int(ans) - 1
        if 0 <= idx < len(model["gguf_files"]):
            chosen = model["gguf_files"][idx]
            # Run detailed simulation with real size
            _simulate_with_real_size(chosen, model["repo_id"], specs, gpu_total, gpu_used)

            if download:
                console.print(f"\n  [yellow]Downloading {chosen['filename']}...[/]")
                _download_gguf(model["repo_id"], chosen["filename"])
    except ValueError:
        pass


def _simulate_with_real_size(gguf, repo_id, specs, gpu_total, gpu_used):
    """Show detailed fit analysis for a specific GGUF file with real size."""
    size_gb = gguf["size_gb"]
    size_mb = int(size_gb * 1024)
    fits = size_mb < gpu_total
    fits_free = size_mb < (gpu_total - gpu_used)

    kv_per_1k = max(2, int(size_gb * 0.4))
    tps = min(120, max(1, int(49 * 12 / max(1, size_gb)))) if fits else max(1, int(5))

    console.print(f"\n  [bold]{repo_id}[/]  ·  [cyan]{gguf['quant']}[/]  ·  [bold]{size_gb}GB[/]")

    # Memory bar
    bw = 50
    mb = int(min(1.0, size_mb / gpu_total) * bw) if gpu_total else 0
    ub = int(min(1.0, gpu_used / gpu_total) * bw) if gpu_total else 0
    fb = max(0, bw - mb - ub)
    console.print(f"\n  [cyan]{'█' * ub}[/][{'green' if fits else 'red'}]{'█' * mb}[/][dim]{'░' * fb}[/]  {gpu_total // 1024}GB")
    console.print(f"  [cyan]■[/] used:{gpu_used // 1024}G  [{'green' if fits else 'red'}]■[/] model:{size_gb}G  [dim]░[/] free:{max(0, gpu_total - gpu_used - size_mb) // 1024}G")

    # Context table
    console.print()
    for ctx in [8192, 32768, 65536, 131072]:
        kv = kv_per_1k * (ctx // 1024)
        tot = size_mb + kv
        h = gpu_total - tot
        icon = "[green]✓[/]" if h > 2000 else "[yellow]⚠[/]" if h > 0 else "[red]✗[/]"
        kv_s = f"{kv}M" if kv < 1024 else f"{kv / 1024:.1f}G"
        console.print(f"  {icon} {ctx // 1024}K ctx  →  model {size_gb}G + KV {kv_s} = {tot / 1024:.1f}G")

    console.print(f"\n  Est. speed: [bold]~{tps} tok/s[/]" + ("" if fits else "  [red](CPU swap)[/]"))

    if not fits:
        console.print(f"  [yellow]→ Try a smaller quant or: sudo sysctl iogpu.wired_limit_mb={int(specs['ram_gb'] * 1024 * 0.9)}[/]")


def _download_gguf(repo_id, filename):
    """Download a GGUF file from HuggingFace.

    Uses huggingface_hub if available (supports gated models with token).
    Falls back to curl. No token needed for public repos (Unsloth, bartowski, etc).
    Gated models (Meta Llama) need: huggingface-cli login
    """
    local_dir = MODELS_DIR / repo_id.replace("/", "--")
    local_dir.mkdir(parents=True, exist_ok=True)
    dest = local_dir / os.path.basename(filename)

    if dest.exists():
        console.print(f"  [green]Already downloaded: {dest}[/]")
        return str(dest)

    try:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(repo_id=repo_id, filename=filename, local_dir=str(local_dir))
        console.print(f"  [green]✓ Downloaded: {path}[/]")
        return path
    except ImportError:
        url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
        console.print(f"  [dim]Downloading {os.path.basename(filename)}...[/]")
        cmd = ["curl", "-L", "-o", str(dest), "--progress-bar", url]
        r = subprocess.run(cmd)
        if r.returncode == 0 and dest.exists():
            console.print(f"  [green]✓ Downloaded: {dest}[/]")
        else:
            console.print(f"  [red]Download failed. If gated model, run: huggingface-cli login[/]")
        return str(dest)
    except Exception as e:
        if "401" in str(e) or "403" in str(e) or "gated" in str(e).lower():
            console.print(f"  [red]Gated model — run: huggingface-cli login[/]")
        else:
            console.print(f"  [red]Download failed: {e}[/]")
        return None


def simulate_model_fit(model_query):
    """Predict if a model will fit BEFORE downloading."""
    import re as _re

    specs = get_machine_specs()
    metal = get_metal_gpu_stats()

    gpu_total = metal.get("total_mb") or specs["gpu_total_mb"]
    gpu_used = metal.get("alloc_mb", 0)
    gpu_free = max(0, gpu_total - gpu_used)

    # Find in known models
    model_id = None
    model_info = None
    query = model_query.lower().replace("-", "").replace("_", "").replace(" ", "")
    for mid, m in MODELS.items():
        mid_clean = mid.lower().replace("-", "").replace("_", "")
        name_clean = m["name"].lower().replace("-", "").replace("_", "").replace(" ", "")
        if query in mid_clean or query in name_clean:
            model_id = mid
            model_info = m
            break

    if not model_info:
        param_match = _re.search(r'(\d+)b', query)
        quant_match = _re.search(r'q(\d)', query)
        if param_match:
            params_b = int(param_match.group(1))
            quant = int(quant_match.group(1)) if quant_match else 4
            bpw = {2: 2.5, 3: 3.5, 4: 4.5, 5: 5.5, 6: 6.5, 8: 8.5}.get(quant, 4.5)
            size_gb = round(params_b * bpw / 8, 1)
            model_info = {"name": f"{params_b}B Q{quant}", "size_gb": size_gb}
        else:
            console.print(f"\n  [red]Unknown model: {model_query}[/]")
            console.print(f"  [dim]Known: {', '.join(MODELS.keys())}  or  '70b q4'[/]\n")
            return

    name = model_info["name"]
    size_gb = model_info["size_gb"]
    size_mb = int(size_gb * 1024)
    kv_per_1k = max(2, int(size_gb * 0.4))  # MB per 1K ctx

    fits_gpu = size_mb < gpu_total
    fits_free = size_mb < gpu_free
    base_tps = min(120, max(1, int(49 * 12 / max(1, size_gb)))) if fits_gpu else max(1, int(10 * 16 / max(1, size_gb)))

    # Render
    console.clear()
    console.print()

    if fits_free:
        console.print(f"  [green bold]✓ {name} WILL FIT[/]  ·  {size_gb}GB model  ·  {gpu_free // 1024}GB free")
    elif fits_gpu:
        console.print(f"  [yellow bold]⚠ {name} TIGHT FIT[/]  ·  {size_gb}GB  ·  close apps first")
    else:
        console.print(f"  [red bold]✗ {name} WON'T FIT[/]  ·  {size_gb}GB model  ·  {gpu_total // 1024}GB limit")

    console.print(f"  [dim]{specs['chip']}  ·  {specs['ram_gb']}GB RAM  ·  GPU budget: {gpu_total // 1024}GB[/]\n")

    # Memory bar
    bw = 60
    mb = int(min(1.0, size_mb / gpu_total) * bw) if gpu_total else 0
    ub = int(min(1.0, gpu_used / gpu_total) * bw) if gpu_total else 0
    fb = max(0, bw - mb - ub)
    console.print(f"  GPU Memory:  [cyan]{'█' * ub}[/][{'green' if fits_gpu else 'red'}]{'█' * mb}[/][dim]{'░' * fb}[/]")
    console.print(f"  [cyan]■[/] used:{gpu_used // 1024}G  [{'green' if fits_gpu else 'red'}]■[/] model:{size_gb}G  [dim]░[/] free:{max(0, gpu_total - gpu_used - size_mb) // 1024}G\n")

    # Performance
    perf = Table(show_header=True, header_style="bold", border_style="dim", padding=(0, 1))
    perf.add_column("", width=18)
    perf.add_column("Value", width=16)
    perf.add_column("", width=38)
    perf.add_row("Model", f"{size_gb} GB", "Fits GPU" if fits_gpu else "[red]Exceeds GPU → swap[/]")
    perf.add_row("Compute", "GPU" if fits_gpu else "[red]CPU[/]", "All layers on GPU" if fits_gpu else "[red]5-10x slower[/]")
    perf.add_row("Speed", f"~{base_tps} tok/s", "" if fits_gpu else "[red]swap thrashing[/]")
    perf.add_row("Download", f"~{max(1, int(size_gb * 12))}s", f"at 100MB/s ({size_gb}GB)")
    console.print(perf)
    console.print()

    # Context table
    ct = Table(title="Context Length vs Memory", show_header=True, header_style="bold", border_style="dim", padding=(0, 1))
    ct.add_column("Context", width=8)
    ct.add_column("KV Cache", width=8, justify="right")
    ct.add_column("Total", width=8, justify="right")
    ct.add_column("Verdict", width=25)
    for ctx in [4096, 8192, 32768, 65536, 131072]:
        kv = kv_per_1k * (ctx // 1024)
        tot = size_mb + kv
        h = gpu_total - tot
        s = "[green]✓ fits[/]" if h > 2000 else f"[yellow]⚠ tight[/]" if h > 0 else f"[red]✗ OOM ({-h // 1024}GB over)[/]"
        ct.add_row(f"{ctx // 1024}K", f"{kv}M" if kv < 1024 else f"{kv / 1024:.1f}G", f"{tot / 1024:.1f}G", s)
    console.print(ct)

    console.print()
    if not fits_gpu:
        for mid, m in sorted(MODELS.items(), key=lambda x: x[1]["size_gb"], reverse=True):
            if m["size_gb"] * 1024 < gpu_total:
                console.print(f"  [green]→ Try:[/] {m['name']} ({m['size_gb']}GB) — {m.get('description', '')}")
                break
        console.print(f"  [green]→ Or:[/] sudo sysctl iogpu.wired_limit_mb={int(specs['ram_gb'] * 1024 * 0.9)}")
    elif not fits_free:
        console.print(f"  [yellow]→[/] localcoder --cleanup  [dim](free {gpu_used // 1024}GB)[/]")
    else:
        console.print(f"  [green]→[/] localcoder{' -m ' + model_id if model_id else ''}  [dim](ready to run)[/]")
    console.print()


def recommend_model(ram_gb):
    """Recommend the best model for given RAM."""
    if ram_gb >= 48:
        return "gemma4-26b", "26B Q4_K_M (best quality) + vision + 128K context. Plenty of headroom."
    elif ram_gb >= 36:
        return "qwen35b-a3b", "Qwen 3.5 35B-A3B Q3_K_XL — best coding quality at 36GB+."
    elif ram_gb >= 24:
        return "gemma4-26b", "Gemma 4 26B Q3_K_XL — 49 tok/s, best overall for 24GB. Also try Qwen 35B Q2."
    elif ram_gb >= 16:
        return "gemma4-e4b", "E4B is the sweet spot for 16GB. Audio + image + code, 57 tok/s."
    elif ram_gb >= 8:
        return "qwen35-4b", "Qwen 3.5 4B — ultrafast at 50 tok/s, only 2.7GB GPU."
    else:
        return "gemma4-e2b", "E2B is the only option under 8GB."


def can_run_simultaneously(ram_gb, model1_gb, model2_gb):
    """Check if two models can run at the same time."""
    gpu_limit = ram_gb * 0.67  # Metal limit ~67% of unified memory
    return (model1_gb + model2_gb) < gpu_limit


def stop_conflicting_backends(target_backend):
    """Stop other backends to free GPU memory."""
    if target_backend == "ollama":
        # Kill llama-server if running (frees GPU for Ollama)
        if check_backend_running("llamacpp"):
            console.print(f"  [yellow]Stopping llama-server to free GPU for Ollama...[/]")
            try:
                subprocess.run(["pkill", "-f", "llama-server"], timeout=5)
                time.sleep(2)
            except:
                pass
    elif target_backend == "llamacpp":
        # Unload Ollama models to free GPU
        if check_backend_running("ollama"):
            console.print(f"  [yellow]Unloading Ollama models to free GPU...[/]")
            try:
                models = get_running_models("ollama")
                for m in models:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            "http://127.0.0.1:11434/api/generate",
                            data=json.dumps({"model": m, "keep_alive": 0}).encode(),
                            headers={"Content-Type": "application/json"}
                        ), timeout=5
                    )
                time.sleep(2)
            except:
                pass


def start_ollama_serve():
    """Ensure Ollama is serving."""
    if check_backend_running("ollama"):
        return True
    try:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        return check_backend_running("ollama")
    except:
        return False
