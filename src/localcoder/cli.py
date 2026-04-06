"""localcoder CLI — main entry point."""
import argparse, json, os, sys, time

from rich.console import Console

console = Console()


def main():
    parser = argparse.ArgumentParser(
        description="localcoder — local AI coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  localcoder                           interactive mode (auto-setup on first run)
  localcoder --setup                  run setup wizard
  localcoder -p "build a react app"   one-shot mode
  localcoder -c                       continue last session
  localcoder --yolo                   auto-approve everything
  localcoder -m gemma4:e4b            use specific model
  localcoder --models                 list/switch models
""")
    parser.add_argument("-p", "--prompt", type=str, help="Run a single task and exit")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true", help="Continue last session")
    parser.add_argument("-m", "--model", type=str, default=None, help="Model name")
    parser.add_argument("--yolo", action="store_true", help="Auto-approve everything")
    parser.add_argument("--bypass", action="store_true", help="Same as --yolo")
    parser.add_argument("--ask", action="store_true", help="Ask before every tool")
    parser.add_argument("--api", type=str, default=None, help="API base URL")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--models", action="store_true", help="List and select models")
    parser.add_argument("--status", action="store_true", help="Show backend status")
    parser.add_argument("--specs", action="store_true", help="Show machine specs and GPU memory")
    parser.add_argument("--cleanup", action="store_true", help="Free GPU memory (unload models, kill stale servers)")
    parser.add_argument("--health", action="store_true", help="Diagnose GPU health: offload, KV cache, swap, context")
    parser.add_argument("--debloat", action="store_true", help="Disable macOS services that steal GPU/memory from your model")
    parser.add_argument("--simulate", nargs="?", const="__interactive__", metavar="MODEL", help="Will this model fit? Interactive picker or --simulate '70b q4'")
    parser.add_argument("--bench", action="store_true", help="Benchmark all installed models (local LM Arena)")
    parser.add_argument("--arena", action="store_true", help="Show model leaderboard")
    parser.add_argument("--force", action="store_true", help="Re-run benchmarks even if cached")
    parser.add_argument("--fetch", type=str, metavar="URL_OR_NAME", help="Fetch model from HuggingFace/Ollama URL and check fit")
    args = parser.parse_args()

    # ── Setup wizard ──
    if args.setup:
        from localcoder.setup import wizard
        wizard()
        return

    # ── Machine specs ──
    if args.specs:
        from localcoder.backends import get_machine_specs, print_machine_specs
        print_machine_specs()
        return

    # ── GPU cleanup wizard ──
    if args.cleanup:
        from localcoder.backends import (
            cleanup_gpu_memory, get_machine_specs, print_machine_specs,
            get_top_memory_processes, print_health_dashboard,
        )
        import signal

        print_health_dashboard()

        procs = get_top_memory_processes(min_mb=200)
        killable = [p for p in procs if p["killable"] and p["category"] in ("app", "bloat") and p["mb"] > 300]

        if not killable:
            console.print("\n  [dim]No heavy apps to clean up.[/]")
        else:
            console.print(f"\n  [bold]Quick cleanup — select apps to quit:[/]\n")
            for i, p in enumerate(killable, 1):
                mb = p["mb"]
                size = f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb}MB"
                console.print(f"  [bold]{i}.[/] {p['name']}"
                              + (f" ×{p['count']}" if p.get("count", 1) > 1 else "")
                              + f"  [dim]({size})[/]")
            console.print(f"  [bold]a.[/] All of the above")
            console.print(f"  [bold]0.[/] Skip\n")

            try:
                ans = input("  Choose (e.g. 1,3 or a): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "0"

            if ans and ans != "0":
                targets = killable if ans == "a" else []
                if not targets:
                    for part in ans.replace(" ", "").split(","):
                        try:
                            idx = int(part) - 1
                            if 0 <= idx < len(killable):
                                targets.append(killable[idx])
                        except ValueError:
                            pass

                for t in targets:
                    for pid in t.get("pids", [t["pid"]]):
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            pass
                    console.print(f"  [green]✓ Quit {t['name']}[/]")

                import time as _time
                _time.sleep(2)

        # Also clean ML processes
        console.print("\n  [bold yellow]Cleaning ML backends...[/]")
        result = cleanup_gpu_memory(force=True)
        if result["ollama_unloaded"]:
            console.print(f"  [green]Unloaded Ollama: {', '.join(result['ollama_unloaded'])}[/]")
        if result["processes_killed"]:
            for p in result["processes_killed"]:
                console.print(f"  [green]Killed llama-server PID {p['pid']}[/]")
        if not result["ollama_unloaded"] and not result["processes_killed"]:
            console.print("  [dim]No ML backends to clean.[/]")

        console.print()
        print_machine_specs()
        return

    # ── Benchmark / Arena ──
    if args.bench:
        from localcoder.bench import run_full_bench
        run_full_bench(force=args.force)
        return
    if args.arena:
        from localcoder.bench import show_leaderboard
        show_leaderboard()
        return

    # ── Fetch model from HuggingFace ──
    if args.fetch:
        from localcoder.backends import simulate_hf_model
        simulate_hf_model(args.fetch)
        return

    # ── Simulate model fit ──
    if args.simulate:
        from localcoder.backends import simulate_model_fit, MODELS, get_machine_specs
        if args.simulate == "__interactive__":
            # Interactive picker
            specs = get_machine_specs()
            os.system("clear" if os.name != "nt" else "cls")
            console.print(f"\n  [bold]Will it fit?[/]  ·  {specs['chip']}  ·  {specs['ram_gb']}GB RAM\n")

            # Known models
            models_list = list(MODELS.items())
            for i, (mid, m) in enumerate(models_list, 1):
                size = m["size_gb"]
                gpu_mb = specs["gpu_total_mb"]
                fits = size * 1024 < gpu_mb
                icon = "[green]✓[/]" if fits else "[red]✗[/]"
                console.print(f"  {icon} [bold]{i:>2}.[/] {m['name']:<30} {size:>5}GB  [dim]{m.get('description', '')[:45]}[/]")

            # Show community coding models by VRAM tier
            from localcoder.backends import COMMUNITY_CODING_MODELS
            gpu_mb = specs["gpu_total_mb"]

            console.print(f"\n  [dim]── r/LocalLLaMA top coding models ──[/]")
            fav_list = list(COMMUNITY_CODING_MODELS.items())
            fav_start = len(models_list) + 1
            for j, (mid, m) in enumerate(fav_list, fav_start):
                # Quick fit check based on smallest likely quant
                fits = "✓" if any(
                    v * 1024 < gpu_mb
                    for v in [3, 6, 10, 12, 15, 20]
                    if m["vram"].startswith(str(v)[:2]) or m["vram"].startswith("<")
                ) else "?"
                console.print(
                    f"     [bold]{j:>2}.[/] {m['name']:<28}"
                    f" [dim]{m['vram']:>8}[/]"
                    f"  [dim]{m['note'][:40]}[/]"
                )

            console.print(f"\n  [bold] s.[/] Search HuggingFace  [dim](paste URL or search term)[/]")
            console.print(f"  [bold] c.[/] Custom  [dim](type size like '13b q4')[/]")
            console.print(f"  [bold] q.[/] Quit\n")

            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return

            if choice == "q" or not choice:
                return
            elif choice == "s":
                try:
                    q = input("  Search or paste URL: ").strip()
                except (EOFError, KeyboardInterrupt):
                    return
                if q:
                    from localcoder.backends import simulate_hf_model
                    simulate_hf_model(q)
            elif choice == "c":
                try:
                    custom = input("  Model (e.g. '70b q4'): ").strip()
                except (EOFError, KeyboardInterrupt):
                    return
                if custom:
                    simulate_model_fit(custom)
            else:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(models_list):
                        simulate_model_fit(models_list[idx][0])
                    elif idx < len(models_list) + len(fav_list):
                        _, fav = fav_list[idx - len(models_list)]
                        from localcoder.backends import simulate_hf_model
                        simulate_hf_model(fav["hf"])
                except ValueError:
                    # Maybe they typed a model spec or URL
                    if "/" in choice or "huggingface" in choice:
                        from localcoder.backends import simulate_hf_model
                        simulate_hf_model(choice)
                    else:
                        simulate_model_fit(choice)
        else:
            simulate_model_fit(args.simulate)
        return

    # ── Debloat wizard ──
    if args.debloat:
        from localcoder.backends import debloat_wizard
        debloat_wizard()
        return

    # ── GPU health diagnostic (interactive) ──
    if args.health:
        # Try Textual TUI first (fixed layout, keyboard shortcuts)
        try:
            from localcoder.tui import run_tui_dashboard
            result = run_tui_dashboard()
            # Handle exit codes from TUI actions
            if result == 10:  # cleanup
                args.cleanup = True
                # fall through
            elif result == 11:  # debloat
                from localcoder.backends import debloat_wizard
                debloat_wizard()
                return
            elif result == 12:  # simulate
                args.simulate = "__interactive__"
                # fall through to simulate handler below
            else:
                return
        except (ImportError, Exception):
            pass  # Fall back to Rich dashboard

        from localcoder.backends import (
            print_health_dashboard, get_top_memory_processes,
            cleanup_gpu_memory,
        )
        import signal

        diag = print_health_dashboard()

        # Interactive kill prompt
        procs = get_top_memory_processes(min_mb=200)
        killable = [p for p in procs if p["killable"] and p["category"] in ("app", "bloat") and p["mb"] > 300]
        if killable and diag["status"] in ("critical", "degraded"):
            console.print(f"  [bold]Kill processes to free memory?[/]")
            for i, p in enumerate(killable, 1):
                mb = p["mb"]
                size = f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb}MB"
                tag = "[red]bloat[/]" if p["category"] == "bloat" else "[yellow]app[/]"
                n = p["name"] + (f" ×{p.get('count',1)}" if p.get("count",1) > 1 else "")
                console.print(f"    [bold]{i}[/]. {n}  {tag}  [dim]{size}[/]")
            console.print(f"    [bold]a[/]. All    [bold]m[/]. ML backends only    [bold]q[/]. Quit\n")

            try:
                ans = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "q"

            if ans == "q" or not ans:
                pass
            elif ans == "m":
                result = cleanup_gpu_memory(force=True)
                if result["ollama_unloaded"]:
                    console.print(f"  [green]Unloaded: {', '.join(result['ollama_unloaded'])}[/]")
                if result["processes_killed"]:
                    for pk in result["processes_killed"]:
                        console.print(f"  [green]Killed llama-server PID {pk['pid']}[/]")
            else:
                targets = killable if ans == "a" else []
                if not targets:
                    for part in ans.replace(" ", "").split(","):
                        try:
                            idx = int(part) - 1
                            if 0 <= idx < len(killable):
                                targets.append(killable[idx])
                        except ValueError:
                            pass
                for t in targets:
                    for pid in t.get("pids", [t["pid"]]):
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except (ProcessLookupError, PermissionError):
                            pass
                    console.print(f"  [green]✓[/] Killed {t['name']}")

            if ans and ans != "q":
                import time as _t
                _t.sleep(2)
                console.print()
                print_health_dashboard()
        return

    # ── Status ──
    if args.status:
        from localcoder.backends import discover_all, BACKENDS
        from rich.table import Table
        discovery = discover_all()
        table = Table(title="Backend Status", show_header=True, header_style="bold cyan")
        table.add_column("Backend")
        table.add_column("Installed")
        table.add_column("Running")
        table.add_column("Models")
        for d in discovery:
            installed = "[green]✓[/]" if d["installed"] else "[red]✗[/]"
            running = f"[green]:{d['port']}[/]" if d["running"] else "[dim]—[/]"
            models = ", ".join(d["models"][:5]) or "[dim]none[/]"
            table.add_row(d["name"], installed, running, models)
        console.print(table)
        return

    # ── Model selector ──
    if args.models:
        from localcoder.backends import discover_all
        discovery = discover_all()
        for d in discovery:
            if d["models"]:
                console.print(f"\n  [bold]{d['name']}[/] [dim](:{d['port']})[/]")
                for m in d["models"]:
                    console.print(f"    [cyan]{m}[/]")
        if not any(d["models"] for d in discovery):
            console.print("  [dim]No models found. Run: localcoder --setup[/]")
        return

    # ── Ensure setup ──
    from localcoder.setup import ensure_setup, load_config
    cfg = ensure_setup()
    if not cfg:
        console.print("  [dim]Setup cancelled.[/]")
        return

    # ── Resolve config ──
    api_base = args.api or cfg.get("api_base", "http://127.0.0.1:8089/v1")
    model = args.model or cfg.get("model", "gemma4-26b")

    # ── Detect backend from model/api override ──
    from localcoder.backends import (
        check_backend_installed, check_backend_running,
        start_llama_server, start_ollama_serve,
        stop_conflicting_backends, get_system_ram_gb, get_gpu_memory_info,
        can_run_simultaneously, MODELS, BACKENDS,
    )
    backend_id = cfg.get("backend", "llamacpp")

    # If user specified --api with Ollama port, switch backend
    if args.api and "11434" in args.api:
        backend_id = "ollama"
    elif args.api and "8089" in args.api:
        backend_id = "llamacpp"
    # If user specified an Ollama-style model name, switch backend
    if args.model and ":" in args.model:
        backend_id = "ollama"
        api_base = "http://127.0.0.1:11434/v1"

    # ── Check backend is running, auto-start if needed ──
    if not check_backend_running(backend_id):
        ram = get_system_ram_gb()
        gpu = get_gpu_memory_info()
        model_info = MODELS.get(cfg.get("model_id", ""), {})
        model_size = model_info.get("size_gb", 12)

        # Check if another backend is hogging GPU
        other = "ollama" if backend_id == "llamacpp" else "llamacpp"
        other_running = check_backend_running(other)

        if other_running and not can_run_simultaneously(ram, model_size, 0):
            other_name = BACKENDS[other]["name"]
            console.print(f"\n  [yellow]GPU memory conflict detected[/]")
            console.print(f"  [dim]RAM: {ram}GB · GPU limit: ~{gpu['total_mb']//1024}GB · {other_name} is using GPU[/]")
            console.print(f"  [dim]Need ~{model_size}GB for model — not enough with {other_name} loaded.[/]")
            console.print()
            console.print(f"  [bold]1.[/] Stop {other_name} and use {BACKENDS[backend_id]['name']} [dim](recommended)[/]")
            console.print(f"  [bold]2.[/] Try anyway [dim](will be very slow — swap thrashing)[/]")
            console.print(f"  [bold]3.[/] Cancel")
            try:
                ans = input("\n  Choose (1/2/3): ").strip()
            except (EOFError, KeyboardInterrupt):
                ans = "1"
            if ans == "1":
                stop_conflicting_backends(backend_id)
            elif ans == "3":
                return
            # ans == "2" continues without stopping

        console.print(f"  [yellow]Starting {BACKENDS[backend_id]['name']}...[/]")
        if backend_id == "llamacpp":
            model_id = cfg.get("model_id", "gemma4-26b")
            proc = start_llama_server(model_id)
            if not proc:
                console.print("  [red]Failed to start llama-server.[/]")
                if check_backend_installed("ollama"):
                    console.print("  [yellow]Falling back to Ollama...[/]")
                    backend_id = "ollama"
                    api_base = "http://127.0.0.1:11434/v1"
                    if not check_backend_running("ollama"):
                        start_ollama_serve()
                    ollama_model = MODELS.get(model_id, {}).get("ollama_tag")
                    if ollama_model:
                        model = ollama_model
                else:
                    console.print("  [red]No backend available. Run: localcoder --setup[/]")
                    return
        else:
            if not check_backend_running("ollama"):
                if not start_ollama_serve():
                    console.print("  [red]Failed to start Ollama.[/]")
                    if check_backend_running("llamacpp"):
                        console.print("  [yellow]Falling back to llama.cpp...[/]")
                        backend_id = "llamacpp"
                        api_base = "http://127.0.0.1:8089/v1"
                    else:
                        console.print("  [red]No backend available. Run: localcoder --setup[/]")
                        return

    # ── Boot sequence (like an OS POST screen) ──
    from localcoder.backends import (
        get_machine_specs, diagnose_gpu_health, get_swap_usage_mb,
        get_metal_gpu_stats, get_llama_server_config, _detect_model_info,
        get_top_memory_processes, cleanup_gpu_memory,
    )
    import time as _t

    skip_boot = cfg.get("skip_boot_health", False)

    if skip_boot:
        # Fast mode — one line
        specs = get_machine_specs()
        diag = diagnose_gpu_health(cfg.get("model_id"))
        swap_mb = get_swap_usage_mb()
        ga, gt = diag.get("gpu_alloc_mb", 0), diag.get("gpu_total_mb", 0)
        gc = "green" if ga < gt * 0.8 else "yellow" if ga < gt else "red"
        sc = "red" if swap_mb > 4000 else "green"
        sc2 = {"healthy": "green", "degraded": "yellow", "critical": "red"}.get(diag["status"], "dim")
        gi = "[green]●[/]" if diag["on_gpu"] else "[red]●[/]"
        console.print(f"   {gi} [{gc}]GPU {ga // 1024}/{gt // 1024}GB[/{gc}]  [{sc}]swap {swap_mb // 1024}GB[/{sc}]  [{sc2}]{diag['status']}[/{sc2}]")
    else:
        # Full boot sequence — gather everything first, render once
        with console.status("[bold]  Starting localcoder...", spinner="dots"):
            specs = get_machine_specs()
            metal = get_metal_gpu_stats()
            srv = get_llama_server_config()
            swap_mb = get_swap_usage_mb()
            diag = diagnose_gpu_health(cfg.get("model_id"))
            procs = get_top_memory_processes(min_mb=500, limit=3)

        gpu_total = metal.get("total_mb") or specs["gpu_total_mb"]
        gpu_alloc = metal.get("alloc_mb", 0)
        gpu_free = max(0, gpu_total - gpu_alloc)
        status = diag["status"]
        sc2 = {"healthy": "green", "degraded": "yellow", "critical": "red"}.get(status, "dim")

        # ── Render boot screen with logo animation ──
        from rich.table import Table as _Table
        from rich.panel import Panel
        from rich.text import Text as _RText
        from rich.console import Group as _Group
        from rich.live import Live as _Live

        # Logo animation (Copilot-style: border draws, text reveals)
        B = "#e07a5f"
        G = "#81b29a"
        LOGO = [
            (f"[bold #e07a5f]██╗      ██████╗  ██████╗ █████╗ ██╗     [/]",),
            (f"[bold #d4725a]██║     ██╔═══██╗██╔════╝██╔══██╗██║     [/]",),
            (f"[bold #c96a55]██║     ██║   ██║██║     ███████║██║     [/]",),
            (f"[bold #be6250]██║     ██║   ██║██║     ██╔══██║██║     [/]",),
            (f"[bold #b35a4b]███████╗╚██████╔╝╚██████╗██║  ██║███████╗[/]",),
            (f"[bold #a85246]╚══════╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝[/]",),
            (f"[bold #81b29a] ██████╗ ██████╗ ██████╗ ███████╗██████╗ [/]",),
            (f"[bold #76a890]██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗[/]",),
            (f"[bold #6b9e86]██║     ██║   ██║██║  ██║█████╗  ██████╔╝[/]",),
            (f"[bold #60947c]██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗[/]",),
            (f"[bold #558a72]╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║[/]",),
            (f"[bold #4a8068] ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝[/]",),
        ]

        def _mk_frame(*lines):
            return _Group(*(_RText.from_markup(l) for l in lines))

        def _logo_frm(cols=99, scan=False, sub="", extra=None):
            out = [f"  [{B}]┌──────────────────────────────────────────────────┐[/]"]
            for lt in LOGO:
                raw = lt[0]
                c = raw.split(']')[0] + ']'
                p = raw.replace('[/]', '').split(']')[-1] if ']' in raw else raw
                s = p[:cols]
                cur = "[white bold]▌[/]" if scan and cols < len(p) else ""
                pad = " " * max(0, 48 - len(s) - (1 if cur else 0))
                out.append(f"  [{B}]│[/]{c}{s}[/]{cur}{pad}[{B}]│[/]")
            out.append(f"  [{B}]└──────────────────────────────────────────────────┘[/]")
            if sub: out.append(sub)
            if extra: out.extend(extra)
            return _mk_frame(*out)

        try:
            import time as _ta
            os.system("clear" if os.name != "nt" else "cls")
            with _Live(console=console, refresh_per_second=20, transient=True) as live:
                # Border draws
                for w in [2, 16, 32, 48]:
                    ln = [f"  [{B}]┌{'─' * w}{'─' * (48 - w)}┐[/]"]
                    for _ in range(12): ln.append(f"  [{B}]│[/]{' ' * 48}[{B}]│[/]")
                    ln.append(f"  [{B}]└{'─' * w}{'─' * (48 - w)}┘[/]")
                    live.update(_mk_frame(*ln)); _ta.sleep(0.04)

                # Logo reveals with cursor
                for col in range(0, 48, 3):
                    live.update(_logo_frm(cols=col, scan=True)); _ta.sleep(0.04)

                # Full logo + subtitle
                live.update(_logo_frm(sub=f"  [{B}]✦[/] [dim]Command-line interface[/]  [bold {G}]✓ offline[/]"))
                _ta.sleep(0.3)
        except Exception:
            pass

        # Print static logo (stays visible — animation was transient)
        os.system("clear" if os.name != "nt" else "cls")
        console.print(f"  [{B}]┌──────────────────────────────────────────────────┐[/]")
        for lt in LOGO:
            raw = lt[0]
            c = raw.split(']')[0] + ']'
            p = raw.replace('[/]', '').split(']')[-1] if ']' in raw else raw
            pad = " " * max(0, 48 - len(p))
            console.print(f"  [{B}]│[/]{lt[0]}{pad}[{B}]│[/]")
        console.print(f"  [{B}]└──────────────────────────────────────────────────┘[/]")
        console.print(f"  [{B}]✦[/] [dim]Command-line interface[/]  [bold {G}]✓ offline[/]")
        console.print()

        # Calculate model GPU usage
        model_mb = 0
        if srv["running"]:
            mi = _detect_model_info(srv, cfg.get("model_id"))
            model_mb = int((mi.get("size_gb") or 0) * 1024)
        if model_mb == 0:
            mi_fallback = MODELS.get(cfg.get("model_id", ""), {})
            model_mb = int(mi_fallback.get("size_gb", 0) * 1024)
        kv_mb = diag.get("kv_cache_est_mb", 0)
        used_mb = model_mb + kv_mb
        free_mb = max(0, gpu_total - used_mb)
        model_fits = used_mb < gpu_total

        if model_fits and swap_mb < 2000:
            border_color, verdict = "green", "READY"
        elif model_fits:
            border_color, verdict = "yellow", "READY"
        else:
            border_color, verdict = "red", "SLOW"

        # Dashboard table inside a panel (logo stays above)
        t = _Table(show_header=False, show_edge=False, box=None, padding=0, expand=False)
        t.add_column(width=9, style="bold dim")
        t.add_column(width=34)
        t.add_column(width=30)

        # GPU bar
        gpu_pct = min(1.0, used_mb / max(1, gpu_total))
        gw = 30
        gf = int(gpu_pct * gw)
        gc = "green" if gpu_pct < 0.75 else "yellow" if gpu_pct < 0.9 else "red"
        t.add_row(
            "GPU",
            f"[{gc}]{'━' * gf}[/{gc}][dim]{'─' * (gw - gf)}[/]",
            f"[{gc}]{used_mb // 1024}/{gpu_total // 1024}GB[/{gc}]  {free_mb // 1024}GB free",
        )

        # Swap bar
        sp = min(1.0, swap_mb / 8192)
        sf = int(sp * gw)
        sc = "green" if swap_mb < 1000 else "yellow" if swap_mb < 4000 else "red"
        t.add_row(
            "Swap",
            f"[{sc}]{'━' * sf}[/{sc}][dim]{'─' * (gw - sf)}[/]",
            f"[{sc}]{swap_mb // 1024}GB[/{sc}]" + (" [dim]close apps to fix[/]" if swap_mb > 2000 else ""),
        )
        # Disk bar
        try:
            from localcoder.backends import get_disk_info
            di = get_disk_info()
            dtot = max(1, di["disk_total_gb"])
            dfree = di["disk_free_gb"]
            dused = dtot - dfree
            dpct = min(1.0, dused / dtot)
            dfl = int(dpct * gw)
            dc = "green" if dfree > 50 else "yellow" if dfree > 20 else "red"
            cache_info = f"  [dim]cache {di['hf_cache_gb']}GB[/]" if di["hf_cache_gb"] > 0 else ""
            t.add_row(
                "Disk",
                f"[{dc}]{'━' * dfl}[/{dc}][dim]{'─' * (gw - dfl)}[/]",
                f"[{dc}]{dfree}GB free[/{dc}]{cache_info}",
            )
        except Exception:
            pass
        t.add_row("", "", "")

        # Model
        if srv["running"]:
            mi2 = _detect_model_info(srv, cfg.get("model_id"))
            mn = mi2["name"] or "?"
            mq = f" {mi2['quant']}" if mi2.get("quant") else ""
            ms = f" {mi2['size_gb']}GB" if mi2.get("size_gb") else ""
            gb = "[green]● GPU[/]" if srv["ngl"] >= 90 else "[red]● CPU[/]"
            t.add_row("Model", f"[cyan]{mn}{mq}{ms}[/]", f"{gb}  ctx {srv['n_ctx'] // 1024}K")
        else:
            t.add_row("Model", "[dim]not running[/]", "[dim]will auto-start[/]")

        # Machine
        t.add_row("Machine", f"[dim]{specs['chip']}[/]", f"[dim]{specs['ram_gb']}GB  {specs.get('gpu_cores', '?')} GPU cores[/]")

        console.print(Panel(
            t,
            title=f"[bold #e07a5f] localcoder [/]",
            subtitle=f"[bold {border_color}] {verdict} [/]",
            border_style=border_color,
            padding=(1, 2),
            width=80,
        ))

        # One verdict line
        if model_fits and swap_mb < 2000:
            console.print(f"  [green]All good. Full speed.[/]")
        elif model_fits:
            console.print(f"  [yellow]AI runs fine. Mac slow from other apps using RAM.[/]")
        else:
            console.print(f"  [red]Model too big — ~5 tok/s instead of ~49. Try --simulate for alternatives.[/]")

        # ── Trending models (fetched live) ──
        try:
            from localcoder.backends import fetch_unsloth_top_models, COMMUNITY_CODING_MODELS
            from rich.markup import escape as _esc
            from localcoder.backends import get_disk_info

            # Detect what's already installed
            di = get_disk_info()
            installed_names = {m["name"].lower().replace(".gguf", "").replace("-", "").replace("_", "") for m in di.get("models", [])}

            all_downloadable = []

            # Show installed models first
            if di.get("models"):
                console.print(f"\n  [dim]── Installed ({len(di['models'])} models, {sum(m['size_gb'] for m in di['models']):.0f}GB) ──[/]")
                for m in di["models"][:5]:
                    name = _esc(m["name"].replace(".gguf", ""))
                    console.print(f"  [green]✓[/]  {name:<28} [dim]{m['size_gb']}GB[/]")

            # Trending
            console.print(f"\n  [dim]── Trending (live from HuggingFace) ──[/]")
            trending = fetch_unsloth_top_models(limit=5)
            num = 1
            for m in trending:
                dl = m["downloads"]
                dl_str = f"{dl // 1000}K" if dl < 1_000_000 else f"{dl / 1_000_000:.1f}M"
                label = _esc(m["label"])
                # Capability icons
                caps = m.get("caps", [])
                cap_str = ""
                if "vision" in caps: cap_str += " [magenta]img[/]"
                if "code" in caps: cap_str += " [cyan]code[/]"
                if "MoE" in caps: cap_str += " [green]MoE[/]"
                if "audio" in caps: cap_str += " [yellow]audio[/]"

                # Fit check from estimated size
                est = m.get("est_smallest_gb")
                fit_tag = ""
                if est:
                    if est * 1024 > gpu_total:
                        fit_tag = f" [red]~{est}GB min · won't fit[/]"
                    else:
                        fit_tag = f" [green]~{est}GB min · fits[/]"

                # Check if already installed
                base = m["label"].lower().replace("-", "").replace("_", "")
                if any(base in inst for inst in installed_names):
                    console.print(f"  [green]✓[/]  {label:<22}{cap_str}  [dim]{dl_str} dl  installed[/]")
                else:
                    console.print(f"  [bold cyan]{num}[/]  {label:<22}{cap_str}  [dim]{dl_str} dl[/]{fit_tag}")
                all_downloadable.append({"label": m["label"], "repo": m["repo_id"]})
                num += 1

            # Community favorites (most liked, deduplicated)
            try:
                from localcoder.backends import fetch_hf_trending_models as _fetch_trend
                liked = _fetch_trend(limit=8, sort="likes")
                trending_repos = {t["repo_id"] for t in trending}
                liked = [l for l in liked if l["repo_id"] not in trending_repos][:4]
                if liked:
                    console.print(f"\n  [dim]── Most liked ──[/]")
                    for lm in liked:
                        label = _esc(lm["label"])
                        dl = lm["downloads"]
                        dl_str = f"{dl // 1000}K" if dl < 1_000_000 else f"{dl / 1_000_000:.1f}M"
                        caps = lm.get("caps", [])
                        cap_str = ""
                        if "vision" in caps: cap_str += " [magenta]img[/]"
                        if "code" in caps: cap_str += " [cyan]code[/]"
                        if "MoE" in caps: cap_str += " [green]MoE[/]"
                        est = lm.get("est_smallest_gb")
                        fit_tag = ""
                        if est:
                            if est * 1024 > gpu_total:
                                fit_tag = f" [red]~{est}GB min · won't fit[/]"
                            else:
                                fit_tag = f" [green]~{est}GB min · fits[/]"
                        base = lm["label"].lower().replace("-", "").replace("_", "")
                        if any(base in inst for inst in installed_names):
                            console.print(f"  [green]✓[/]  {label:<22}{cap_str}  [dim]{dl_str} dl  installed[/]")
                        else:
                            console.print(f"  [bold cyan]{num}[/]  {label:<22}{cap_str}  [dim]{dl_str} dl[/]{fit_tag}")
                        all_downloadable.append({"label": lm["label"], "repo": lm["repo_id"]})
                        num += 1
            except Exception:
                pass

        except Exception:
            all_downloadable = []

        console.print(f"\n  [bold]enter[/] start coding")
        console.print(f"  [bold]1-{max(1, len(all_downloadable))}[/]   try a model [dim](shows quants, downloads if needed)[/]")
        console.print(f"  [bold]c[/]     cleanup GPU  [bold]s[/] skip boot  [bold]q[/] quit")

        try:
            ans = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return

        if ans == "q":
            return
        elif ans.isdigit() and all_downloadable:
            idx = int(ans) - 1
            if 0 <= idx < len(all_downloadable):
                pick = all_downloadable[idx]
                console.print(f"\n  [bold]Checking {pick['label']}...[/]")

                # Fetch real quants from HuggingFace
                try:
                    from localcoder.backends import simulate_hf_model
                    simulate_hf_model(pick["repo"])
                except Exception as e:
                    console.print(f"  [red]Error: {e}[/]")

                console.print(f"\n  [dim]Press Enter to continue to coding...[/]")
                try:
                    input("  ")
                except (EOFError, KeyboardInterrupt):
                    pass
            else:
                console.print(f"  [dim]Invalid number. Press Enter to start.[/]")
                try:
                    input("  ")
                except (EOFError, KeyboardInterrupt):
                    pass
        elif ans == "c":
            console.print("\n  [bold]Freeing memory...[/]  [dim](only unloading unused AI models — your apps are safe)[/]")
            result = cleanup_gpu_memory(force=False)
            if result["ollama_unloaded"]:
                console.print(f"  [green]✓[/] Unloaded: {', '.join(result['ollama_unloaded'])}")
            else:
                console.print(f"  [dim]No unused models to unload.[/]")

            # Show what user could close
            big_hogs = [p for p in procs if p["category"] in ("app", "bloat") and p["mb"] >= 500]
            if big_hogs:
                console.print(f"\n  [bold]Want more speed?[/] Close these apps when you don't need them:")
                for p in big_hogs:
                    n = p["name"] + (f" ×{p['count']}" if p.get("count", 1) > 1 else "")
                    console.print(f"    {n}  [dim]({p['mb'] // 1024}GB)[/]")

            console.print(f"\n  [dim]Press Enter to start coding...[/]")
            try:
                input("  ")
            except (EOFError, KeyboardInterrupt):
                pass
        elif ans == "s":
            config_path = os.path.expanduser("~/.localcoder/config.json")
            try:
                with open(config_path) as f:
                    c = json.load(f)
                c["skip_boot_health"] = True
                with open(config_path, "w") as f:
                    json.dump(c, f, indent=2)
                console.print(f"  [dim]Got it. Run localcoder --health anytime to see this again.[/]")
                import time as _t2
                _t2.sleep(1)
            except Exception:
                pass

        # Clear boot screen, start fresh
        os.system("clear" if os.name != "nt" else "cls")

    # ── Set env and run the agent ──
    os.environ["GEMMA_API_BASE"] = api_base
    os.environ["GEMMA_MODEL"] = model

    # Import and run the original localcoder agent
    # For now, exec the original script if it exists nearby
    agent_script = os.path.join(os.path.dirname(__file__), "agent.py")
    if os.path.exists(agent_script):
        # Use the modular agent
        from localcoder.agent import run_agent
        run_agent(api_base, model, args)
    else:
        # Fallback: find the agent script
        original = os.path.expanduser("~/Projects/gemma4-research/gemma4coder")
        if os.path.exists(original):
            os.execv(sys.executable, [sys.executable, original] + sys.argv[1:])
        else:
            console.print("  [red]Agent not found. Ensure localcoder agent.py is installed.[/]")


if __name__ == "__main__":
    main()
