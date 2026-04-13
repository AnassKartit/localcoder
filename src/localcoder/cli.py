"""localcoder CLI — main entry point."""

import argparse, json, os, sys, time, shutil, subprocess, uuid
from pathlib import Path

from rich.console import Console
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

DEFAULT_LAUNCH_LOGO = Path.home() / ".localcoder" / "launch-logo.png"
LOCALCODER_HOME = Path.home() / ".localcoder"
DOWNLOADS_DIR = Path.home() / "Downloads"
PACKAGE_LAUNCHERS_DIR = Path(__file__).resolve().parent / "assets" / "launchers"
ITERM2_DYNAMIC_PROFILES_DIR = (
    Path.home() / "Library" / "Application Support" / "iTerm2" / "DynamicProfiles"
)
LOCALCODER_ITERM2_AR_PROFILE = "LocalCoder Arabic"
LANG_LOGO_NAMES = {
    "en": [
        "launch-logo-en.png",
        "LocalCoderEN.png",
        "localCoderEN.png",
        "launch-logo.png",
    ],
    "fr": ["launch-logo-fr.png", "LocalCoderFR.png", "localCoderFR.png"],
    "ar": ["launch-logo-ar.png", "LocalCoderAR.png", "localCoderAR.png"],
}

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as _bidi_get_display
except Exception:
    arabic_reshaper = None
    _bidi_get_display = None


def _shape_arabic(text):
    if not text:
        return text
    try:
        native_pref = os.environ.get("LOCALCODER_NATIVE_ARABIC")
        tp = os.environ.get("TERM_PROGRAM", "")
        term_name = os.environ.get("TERM", "")
        terminal_supports_native = (
            tp in ("Apple_Terminal", "iTerm.app", "WezTerm")
            or "kitty" in term_name
            or "ghostty" in term_name
        )
        if native_pref == "1" or (native_pref != "0" and terminal_supports_native):
            return f"\u2067{text}\u2069"
        if not arabic_reshaper:
            return text
        reshaped = arabic_reshaper.reshape(text)
        if _bidi_get_display:
            return _bidi_get_display(reshaped)
        return reshaped
    except Exception:
        return text


def _ui_text(lang, en, ar):
    return _shape_arabic(ar) if lang == "ar" else en


def _resolve_launch_logo(cfg, lang="en"):
    override = os.environ.get("LOCALCODER_LAUNCH_LOGO") or cfg.get("launch_logo_path")
    candidates = []
    if override:
        candidates.append(Path(os.path.expanduser(override)))
    else:
        lang = (lang or "en").lower()
        for root in (PACKAGE_LAUNCHERS_DIR, LOCALCODER_HOME, DOWNLOADS_DIR):
            for name in LANG_LOGO_NAMES.get(lang, []):
                candidates.append(root / name)
        if lang != "en":
            for root in (PACKAGE_LAUNCHERS_DIR, LOCALCODER_HOME, DOWNLOADS_DIR):
                for name in LANG_LOGO_NAMES["en"]:
                    candidates.append(root / name)
        candidates.extend(
            [
                DEFAULT_LAUNCH_LOGO,
                DEFAULT_LAUNCH_LOGO.with_suffix(".webp"),
                DEFAULT_LAUNCH_LOGO.with_suffix(".jpg"),
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _find_pixterm():
    candidates = [
        shutil.which("pixterm"),
        str(Path.home() / "go" / "bin" / "pixterm"),
        str(Path.home() / ".local" / "bin" / "pixterm"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _find_timg():
    candidates = [
        shutil.which("timg"),
        "/opt/homebrew/bin/timg",
        str(Path.home() / ".local" / "bin" / "timg"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _terminal_program():
    if os.environ.get("TERM_PROGRAM"):
        return os.environ["TERM_PROGRAM"]
    if os.environ.get("WEZTERM_PANE"):
        return "WezTerm"
    term = os.environ.get("TERM", "")
    if "kitty" in term:
        return "kitty"
    if "ghostty" in term:
        return "ghostty"
    return term or "unknown"


def _terminal_supports_inline_images():
    tp = _terminal_program().lower()
    return (
        tp.startswith("iterm")
        or tp.startswith("wezterm")
        or tp.startswith("kitty")
        or tp.startswith("ghostty")
    )


def _launch_inline_images_enabled(cfg):
    raw = os.environ.get("LOCALCODER_LAUNCH_INLINE_IMAGE")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(cfg.get("launch_inline_image", False))


def _resolve_remote_api_key(cli_key=None):
    return (
        cli_key
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("AZURE_OPENAI_API_KEY")
        or os.environ.get("AZURE_AI_API_KEY")
        or os.environ.get("FOUNDRY_API_KEY")
        or os.environ.get("LOCALCODER_API_KEY")
        or ""
    )


def _terminal_prefers_arabic_rtl():
    tp = _terminal_program().lower()
    return (
        tp.startswith("iterm")
        or tp.startswith("wezterm")
        or tp.startswith("kitty")
        or tp.startswith("ghostty")
    )


def _ensure_macos_arabic_fonts():
    if sys.platform != "darwin":
        return False
    noto_sans = Path.home() / "Library" / "Fonts" / "NotoSansArabic[wdth,wght].ttf"
    noto_naskh = Path.home() / "Library" / "Fonts" / "NotoNaskhArabic[wght].ttf"
    if noto_sans.exists() and noto_naskh.exists():
        return True
    brew = shutil.which("brew")
    if not brew:
        return False
    try:
        subprocess.run(
            [
                brew,
                "install",
                "--cask",
                "font-noto-sans-arabic",
                "font-noto-naskh-arabic",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return noto_sans.exists() and noto_naskh.exists()


def _run_osascript(script):
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _capture_terminal_app_font():
    name = _run_osascript(
        'tell application "Terminal" to tell selected tab of front window to get font name'
    )
    size = _run_osascript(
        'tell application "Terminal" to tell selected tab of front window to get font size'
    )
    try:
        return name, int(size)
    except Exception:
        return None


def _set_terminal_app_font(font_name, font_size):
    _run_osascript(
        f'tell application "Terminal" to set font name of selected tab of front window to "{font_name}"'
    )
    _run_osascript(
        f'tell application "Terminal" to set font size of selected tab of front window to {int(font_size)}'
    )


def _capture_iterm2_profile_name():
    return _run_osascript(
        'tell application "iTerm2" to tell current session of current window to get profile name'
    )


def _emit_iterm2_set_profile(profile_name):
    if not profile_name or not sys.stdout.isatty():
        return
    sys.stdout.write(f"\033]1337;SetProfile={profile_name}\a")
    sys.stdout.flush()


def _ensure_iterm2_arabic_profile(parent_profile="Default"):
    try:
        ITERM2_DYNAMIC_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        profile = {
            "Name": LOCALCODER_ITERM2_AR_PROFILE,
            "Guid": str(uuid.uuid5(uuid.NAMESPACE_DNS, "localcoder-arabic-profile")),
            "Dynamic Profile Parent Name": parent_profile or "Default",
            "Use Non-ASCII Font": True,
            "Non Ascii Font": "NotoSansArabic-Regular 16",
        }
        profile_path = ITERM2_DYNAMIC_PROFILES_DIR / "localcoder-fonts.json"
        profile_path.write_text(
            json.dumps({"Profiles": [profile]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


def _apply_session_font_overrides(ui_lang):
    """Best-effort session-local font switch for Arabic on macOS terminals."""
    if (
        sys.platform != "darwin"
        or ui_lang != "ar"
        or not sys.stdout.isatty()
        or os.environ.get("LOCALCODER_FORCE_TERMINAL_FONT", "1") == "0"
    ):
        return None

    tp = _terminal_program().lower()

    if tp == "apple_terminal":
        previous = _capture_terminal_app_font()
        _set_terminal_app_font("NotoSansArabic-Regular", 16)

        def _restore_terminal():
            if previous:
                _set_terminal_app_font(previous[0], previous[1])

        return _restore_terminal

    if tp.startswith("iterm"):
        previous_profile = _capture_iterm2_profile_name() or "Default"
        if _ensure_iterm2_arabic_profile(parent_profile=previous_profile):
            _emit_iterm2_set_profile(LOCALCODER_ITERM2_AR_PROFILE)

            def _restore_iterm():
                if (
                    previous_profile
                    and previous_profile != LOCALCODER_ITERM2_AR_PROFILE
                ):
                    _emit_iterm2_set_profile(previous_profile)

            return _restore_iterm

    return None


def _render_timg_launch_logo(cfg, lang="en"):
    logo_path = _resolve_launch_logo(cfg, lang=lang)
    timg = _find_timg()
    if not logo_path or not timg or not sys.stdout.isatty():
        return False

    term = shutil.get_terminal_size((100, 32))
    cols = max(50, min(term.columns - 2, 140))
    rows = max(12, min(term.lines - 10, 34))
    proto = "i" if os.environ.get("TERM_PROGRAM", "").startswith("iTerm") else "h"

    try:
        os.system("clear" if os.name != "nt" else "cls")
        subprocess.run(
            [timg, "-g", f"{cols}x{rows}", "-C", "-p", proto, str(logo_path)],
            check=True,
            stdout=sys.stdout,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _render_pixterm_launch_logo(cfg, lang="en"):
    logo_path = _resolve_launch_logo(cfg, lang=lang)
    pixterm = _find_pixterm()
    if not logo_path or not pixterm or not sys.stdout.isatty():
        return False

    term = shutil.get_terminal_size((100, 32))
    cols = max(46, min(term.columns - 4, 110))
    rows = max(12, min(term.lines // 2, 18))

    try:
        os.system("clear" if os.name != "nt" else "cls")
        subprocess.run(
            [
                pixterm,
                "-d",
                "0",
                "-s",
                "2",
                "-tc",
                str(cols),
                "-tr",
                str(rows),
                "-m",
                "0b1118",
                str(logo_path),
            ],
            check=True,
            stdout=sys.stdout,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _render_terminal_launch_logo(cfg, lang="en"):
    if not _terminal_supports_inline_images():
        return False
    if os.environ.get("TERM_PROGRAM", "").startswith("iTerm"):
        return _render_timg_launch_logo(cfg, lang=lang) or _render_pixterm_launch_logo(
            cfg, lang=lang
        )
    return _render_pixterm_launch_logo(cfg, lang=lang) or _render_timg_launch_logo(
        cfg, lang=lang
    )


def _launch_style(cfg):
    return (
        (
            os.environ.get("LOCALCODER_LAUNCH_STYLE")
            or cfg.get("launch_style")
            or "retro"
        )
        .strip()
        .lower()
    )


def _retro_bar(used, total, width=26):
    total = max(1, total)
    ratio = max(0.0, min(1.0, used / total))
    filled = int(round(ratio * width))
    return f"[#6CFF6C]{'█' * filled}[/][#17301b]{'░' * (width - filled)}[/]"


def _render_retro_launcher(
    specs,
    srv,
    verdict,
    border_color,
    used_mb,
    gpu_total,
    free_mb,
    swap_mb,
    all_good,
    ui_lang="en",
):
    title = Text(
        _ui_text(ui_lang, "LOCALCODER.EXE", "المبرمج-المحلي.EXE"), style="bold #d7b46a"
    )
    subtitle = Text(
        _ui_text(
            ui_lang,
            "retro boot profile :: local offline coding system",
            "نمط إقلاع كلاسيكي :: نظام برمجة محلي دون اتصال",
        ),
        style="#53d6ff",
    )

    logo_lines = [
        " _     ___   ____   _    _      ____ ___  ____  _____ ____ ",
        "| |   / _ \\ / ___| / \\  | |    / ___/ _ \\|  _ \\| ____|  _ \\",
        "| |  | | | | |    / _ \\ | |   | |  | | | | | | |  _| | |_) |",
        "| |__| |_| | |___/ ___ \\| |___| |__| |_| | |_| | |___|  _ < ",
        "|_____\\___/ \\____/_/   \\_\\_____|\\____\\___/|____/|_____|_| \\_\\",
    ]
    logo = Group(*[Text(line, style="bold #d7b46a") for line in logo_lines])

    status = Table.grid(expand=True)
    status.add_column(style="bold #53d6ff", ratio=18)
    status.add_column(ratio=42)
    status.add_column(style="#f5deb3", ratio=26)

    renderer = (
        _ui_text(ui_lang, "GPU", "معالج رسوم")
        if srv.get("ngl", 0) >= 90
        else _ui_text(ui_lang, "CPU", "معالج")
    )
    status.add_row(
        _ui_text(ui_lang, "MODEL SLOT", "خانة النموذج"),
        f"[#d7b46a]{srv.get('model_path') or _ui_text(ui_lang, 'autodetect', 'اكتشاف تلقائي')}[/]",
        f"[bold {border_color}]{verdict}[/]",
    )
    status.add_row(
        _ui_text(ui_lang, "RENDERER", "المعالجة"),
        f"[#d7b46a]{renderer}[/]  ctx {max(1, srv.get('n_ctx', 0)) // 1024}K",
        f"[#53d6ff]{specs['chip']}[/]",
    )
    status.add_row(
        _ui_text(ui_lang, "VRAM LOAD", "تحميل الذاكرة"),
        _retro_bar(used_mb, gpu_total),
        f"[#f5deb3]{used_mb // 1024}/{gpu_total // 1024} GB[/]  "
        + _ui_text(ui_lang, f"free {free_mb // 1024} GB", f"متاح {free_mb // 1024} GB"),
    )
    status.add_row(
        _ui_text(ui_lang, "SWAP FILE", "ملف المبادلة"),
        _retro_bar(min(swap_mb, 8192), 8192),
        f"[#f5deb3]{swap_mb // 1024} GB[/]  "
        + _ui_text(
            ui_lang,
            "stable" if all_good else "watch apps",
            "مستقر" if all_good else "أغلق بعض التطبيقات",
        ),
    )
    status.add_row(
        _ui_text(ui_lang, "SYSTEM", "النظام"),
        f"[#f5deb3]{specs['ram_gb']} GB RAM[/]  {specs.get('gpu_cores', '?')} GPU cores",
        f"[#53d6ff]{_ui_text(ui_lang, 'offline / agent ready', 'دون اتصال / الوكيل جاهز')}[/]",
    )

    controls = Text()
    controls.append(" [ENTER] ", style="bold black on #d7b46a")
    controls.append(
        _ui_text(ui_lang, "start session  ", "ابدأ الجلسة  "), style="#f5deb3"
    )
    controls.append(" [1-9] ", style="bold black on #53d6ff")
    controls.append(_ui_text(ui_lang, "try models  ", "جرّب النماذج  "), style="#f5deb3")
    controls.append(" [C] ", style="bold black on #c97b36")
    controls.append(_ui_text(ui_lang, "cleanup  ", "تنظيف  "), style="#f5deb3")
    controls.append(" [Q] ", style="bold black on #ff7b72")
    controls.append(_ui_text(ui_lang, "quit", "خروج"), style="#f5deb3")

    return Panel(
        Group(title, subtitle, Text(""), logo, Text(""), status, Text(""), controls),
        title=f"[bold #53d6ff] {_ui_text(ui_lang, 'boot menu', 'قائمة الإقلاع')} [/]",
        subtitle=f"[bold {border_color}] {verdict} [/]",
        border_style=border_color,
        box=box.DOUBLE,
        padding=(1, 2),
        width=min(100, console.width - 2 if console.width > 4 else 100),
    )


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
""",
    )
    parser.add_argument("-p", "--prompt", type=str, help="Run a single task and exit")
    parser.add_argument(
        "-c",
        "--continue",
        dest="cont",
        action="store_true",
        help="Continue last session",
    )
    parser.add_argument("-m", "--model", type=str, default=None, help="Model name")
    parser.add_argument("-ar", "--arabic", action="store_true", help="Arabic UI")
    parser.add_argument("-fr", "--french", action="store_true", help="French launcher")
    parser.add_argument("--yolo", action="store_true", help="Auto-approve everything")
    parser.add_argument("--bypass", action="store_true", help="Same as --yolo")
    parser.add_argument("--ask", action="store_true", help="Ask before every tool")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Compact system prompt (best for small models)",
    )
    parser.add_argument(
        "--system",
        type=str,
        default=None,
        help="Custom system prompt (string or path to file)",
    )
    parser.add_argument("--api", type=str, default=None, help="API base URL")
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API key for OpenAI-compatible remote endpoints",
    )
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--models", action="store_true", help="List and select models")
    parser.add_argument("--status", action="store_true", help="Show backend status")
    parser.add_argument(
        "--specs", action="store_true", help="Show machine specs and GPU memory"
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Free GPU memory (unload models, kill stale servers)",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Diagnose GPU health: offload, KV cache, swap, context",
    )
    parser.add_argument(
        "--debloat",
        action="store_true",
        help="Disable macOS services that steal GPU/memory from your model",
    )
    parser.add_argument(
        "--simulate",
        nargs="?",
        const="__interactive__",
        metavar="MODEL",
        help="Will this model fit? Interactive picker or --simulate '70b q4'",
    )
    parser.add_argument(
        "--bench",
        action="store_true",
        help="Benchmark all installed models (local LM Arena)",
    )
    parser.add_argument("--arena", action="store_true", help="Show model leaderboard")
    parser.add_argument(
        "--force", action="store_true", help="Re-run benchmarks even if cached"
    )
    parser.add_argument(
        "--fetch",
        type=str,
        metavar="URL_OR_NAME",
        help="Fetch model from HuggingFace/Ollama URL and check fit",
    )
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
            cleanup_gpu_memory,
            get_machine_specs,
            print_machine_specs,
            get_top_memory_processes,
            print_health_dashboard,
        )
        import signal

        print_health_dashboard()

        procs = get_top_memory_processes(min_mb=200)
        killable = [
            p
            for p in procs
            if p["killable"] and p["category"] in ("app", "bloat") and p["mb"] > 300
        ]

        if not killable:
            console.print("\n  [dim]No heavy apps to clean up.[/]")
        else:
            console.print(f"\n  [bold]Quick cleanup — select apps to quit:[/]\n")
            for i, p in enumerate(killable, 1):
                mb = p["mb"]
                size = f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb}MB"
                console.print(
                    f"  [bold]{i}.[/] {p['name']}"
                    + (f" ×{p['count']}" if p.get("count", 1) > 1 else "")
                    + f"  [dim]({size})[/]"
                )
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
            console.print(
                f"  [green]Unloaded Ollama: {', '.join(result['ollama_unloaded'])}[/]"
            )
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
            console.print(
                f"\n  [bold]Will it fit?[/]  ·  {specs['chip']}  ·  {specs['ram_gb']}GB RAM\n"
            )

            # Known models
            models_list = list(MODELS.items())
            for i, (mid, m) in enumerate(models_list, 1):
                size = m["size_gb"]
                gpu_mb = specs["gpu_total_mb"]
                fits = size * 1024 < gpu_mb
                icon = "[green]✓[/]" if fits else "[red]✗[/]"
                console.print(
                    f"  {icon} [bold]{i:>2}.[/] {m['name']:<30} {size:>5}GB  [dim]{m.get('description', '')[:45]}[/]"
                )

            # Show community coding models by VRAM tier
            from localcoder.backends import COMMUNITY_CODING_MODELS

            gpu_mb = specs["gpu_total_mb"]

            console.print(f"\n  [dim]── r/LocalLLaMA top coding models ──[/]")
            fav_list = list(COMMUNITY_CODING_MODELS.items())
            fav_start = len(models_list) + 1
            for j, (mid, m) in enumerate(fav_list, fav_start):
                # Quick fit check based on smallest likely quant
                fits = (
                    "✓"
                    if any(
                        v * 1024 < gpu_mb
                        for v in [3, 6, 10, 12, 15, 20]
                        if m["vram"].startswith(str(v)[:2]) or m["vram"].startswith("<")
                    )
                    else "?"
                )
                console.print(
                    f"     [bold]{j:>2}.[/] {m['name']:<28}"
                    f" [dim]{m['vram']:>8}[/]"
                    f"  [dim]{m['note'][:40]}[/]"
                )

            console.print(
                f"\n  [bold] s.[/] Search HuggingFace  [dim](paste URL or search term)[/]"
            )
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
            print_health_dashboard,
            get_top_memory_processes,
            cleanup_gpu_memory,
        )
        import signal

        diag = print_health_dashboard()

        # Interactive kill prompt
        procs = get_top_memory_processes(min_mb=200)
        killable = [
            p
            for p in procs
            if p["killable"] and p["category"] in ("app", "bloat") and p["mb"] > 300
        ]
        if killable and diag["status"] in ("critical", "degraded"):
            console.print(f"  [bold]Kill processes to free memory?[/]")
            for i, p in enumerate(killable, 1):
                mb = p["mb"]
                size = f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb}MB"
                tag = "[red]bloat[/]" if p["category"] == "bloat" else "[yellow]app[/]"
                n = p["name"] + (
                    f" ×{p.get('count', 1)}" if p.get("count", 1) > 1 else ""
                )
                console.print(f"    [bold]{i}[/]. {n}  {tag}  [dim]{size}[/]")
            console.print(
                f"    [bold]a[/]. All    [bold]m[/]. ML backends only    [bold]q[/]. Quit\n"
            )

            try:
                ans = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "q"

            if ans == "q" or not ans:
                pass
            elif ans == "m":
                result = cleanup_gpu_memory(force=True)
                if result["ollama_unloaded"]:
                    console.print(
                        f"  [green]Unloaded: {', '.join(result['ollama_unloaded'])}[/]"
                    )
                if result["processes_killed"]:
                    for pk in result["processes_killed"]:
                        console.print(
                            f"  [green]Killed llama-server PID {pk['pid']}[/]"
                        )
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
        table = Table(
            title="Backend Status", show_header=True, header_style="bold cyan"
        )
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
            console.print("  [dim]No models found — launching setup wizard...[/]")
            from localcoder.setup import wizard

            wizard()
        return

    # ── Ensure setup (skip when --api points to a running server) ──
    from localcoder.setup import ensure_setup, load_config, save_config

    if args.api:
        # External server provided (e.g. from localfit --launch localcoder)
        # Skip setup wizard — just verify server is reachable
        import urllib.request

        api_base = args.api.rstrip("/")
        api_key = _resolve_remote_api_key(args.api_key)
        # Try /models with auth header (works for Modal, OpenAI-compatible servers)
        reachable = False
        for health_url in [
            f"{api_base}/models",
            f"{api_base}/health",
            api_base.replace("/v1", ""),
        ]:
            try:
                req = urllib.request.Request(health_url)
                if api_key:
                    req.add_header("Authorization", f"Bearer {api_key}")
                urllib.request.urlopen(req, timeout=5)
                reachable = True
                break
            except Exception:
                continue
        if not reachable:
            # Don't block -- remote APIs may have cold starts
            console.print(f"  [yellow]Server may be starting (cold start)...[/]")
            console.print(f"  [dim]API: {api_base}[/]")
        # Build minimal config from CLI args
        cfg = load_config() or {}
        cfg["setup_complete"] = True
        cfg["backend"] = "llamacpp"
        cfg["api_base"] = api_base
        if args.model:
            cfg["model"] = args.model
            cfg["model_id"] = args.model
    else:
        cfg = ensure_setup()
        if not cfg:
            console.print("  [dim]Setup cancelled.[/]")
            return

    # ── Resolve config ──
    api_base = (args.api or cfg.get("api_base", "http://127.0.0.1:8089/v1")).rstrip("/")
    model = args.model or cfg.get("model", "gemma4-26b")
    if args.api_key:
        os.environ["LOCALCODER_API_KEY"] = args.api_key

    # Update globals IMMEDIATELY so boot screen shows correct info
    from localcoder import localcoder_agent

    localcoder_agent.MODEL = model
    localcoder_agent.API_BASE = api_base
    ui_lang = (
        "ar"
        if args.arabic
        else "fr"
        if args.french
        else (os.environ.get("LOCALCODER_UI_LANG") or cfg.get("ui_lang") or "en")
    )

    if (
        ui_lang == "ar"
        and sys.platform == "darwin"
        and not cfg.get("arabic_font_bootstrap_done")
    ):
        if _ensure_macos_arabic_fonts():
            cfg["arabic_font_bootstrap_done"] = True
            save_config(cfg)

    restore_terminal_font = _apply_session_font_overrides(ui_lang)

    # (no warning for non-RTL terminals — Arabic reshaping handles display)

    # ── Detect backend from model/api override ──
    from localcoder.backends import (
        check_backend_installed,
        check_backend_running,
        start_llama_server,
        start_ollama_serve,
        stop_conflicting_backends,
        get_system_ram_gb,
        get_gpu_memory_info,
        can_run_simultaneously,
        MODELS,
        BACKENDS,
    )

    backend_id = cfg.get("backend", "llamacpp")

    # If user specified --api with Ollama port, switch backend
    if args.api and "11434" in args.api:
        backend_id = "ollama"
    elif args.api and "8089" in args.api:
        backend_id = "llamacpp"
    # If user specified an Ollama-style model name, switch backend (only when no --api)
    if args.model and ":" in args.model and not args.api:
        backend_id = "ollama"
        api_base = "http://127.0.0.1:11434/v1"

    # ── Detect if this is a remote API (Modal, OpenRouter, etc.) ──
    is_remote = args.api and not any(
        local in args.api for local in ["127.0.0.1", "localhost", "0.0.0.0"]
    )

    # ── Check backend is running, auto-start if needed (LOCAL only) ──
    if not is_remote and not check_backend_running(backend_id):
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
            console.print(
                f"  [dim]RAM: {ram}GB · GPU limit: ~{gpu['total_mb'] // 1024}GB · {other_name} is using GPU[/]"
            )
            console.print(
                f"  [dim]Need ~{model_size}GB for model — not enough with {other_name} loaded.[/]"
            )
            console.print()
            console.print(
                f"  [bold]1.[/] Stop {other_name} and use {BACKENDS[backend_id]['name']} [dim](recommended)[/]"
            )
            console.print(
                f"  [bold]2.[/] Try anyway [dim](will be very slow — swap thrashing)[/]"
            )
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
                    console.print(
                        "  [yellow]No backend available — launching setup wizard...[/]"
                    )
                    from localcoder.setup import wizard

                    cfg = wizard()
                    if not cfg:
                        return
                    api_base = cfg.get("api_base", "http://127.0.0.1:8089/v1")
                    model = cfg.get("model", model)
                    backend_id = cfg.get("backend", "llamacpp")
        else:
            if not check_backend_running("ollama"):
                if not start_ollama_serve():
                    console.print("  [red]Failed to start Ollama.[/]")
                    if check_backend_running("llamacpp"):
                        console.print("  [yellow]Falling back to llama.cpp...[/]")
                        backend_id = "llamacpp"
                        api_base = "http://127.0.0.1:8089/v1"
                    else:
                        console.print(
                            "  [yellow]No backend available — launching setup wizard...[/]"
                        )
                        from localcoder.setup import wizard

                        cfg = wizard()
                        if not cfg:
                            return
                        api_base = cfg.get("api_base", "http://127.0.0.1:8089/v1")
                        model = cfg.get("model", model)
                        backend_id = cfg.get("backend", "llamacpp")

    # ── Boot sequence (like an OS POST screen) ──
    from localcoder.backends import (
        get_machine_specs,
        diagnose_gpu_health,
        get_swap_usage_mb,
        get_metal_gpu_stats,
        get_llama_server_config,
        _detect_model_info,
        get_top_memory_processes,
        cleanup_gpu_memory,
    )
    import time as _t

    boot_mode = (
        os.environ.get("LOCALCODER_BOOT_MODE")
        or cfg.get("boot_mode")
        or ("fast" if not args.prompt else "full")
    ).strip().lower()
    skip_boot = cfg.get("skip_boot_health", False) or args.api or boot_mode != "full"

    # Ensure cfg reflects current args (not stale config file)
    if args.model:
        cfg["model"] = args.model
        cfg["model_id"] = args.model
    if args.api:
        cfg["api_base"] = args.api

    if skip_boot:
        if not args.prompt:
            if _launch_inline_images_enabled(cfg):
                _render_terminal_launch_logo(cfg, lang=ui_lang)
        # Fast mode — one line
        specs = get_machine_specs()
        metal = get_metal_gpu_stats()
        diag = diagnose_gpu_health(cfg.get("model_id"))
        swap_mb = get_swap_usage_mb()
        ga = metal.get("alloc_mb", 0) or diag.get("gpu_alloc_mb", 0)
        gt = (
            metal.get("total_mb", 0)
            or specs.get("gpu_total_mb", 0)
            or diag.get("gpu_total_mb", 0)
        )
        gc = "green" if ga < gt * 0.8 else "yellow" if ga < gt else "red"
        sc = "red" if swap_mb > 4000 else "green"
        sc2 = {"healthy": "green", "degraded": "yellow", "critical": "red"}.get(
            diag["status"], "dim"
        )
        gi = "[green]●[/]" if diag["on_gpu"] else "[red]●[/]"
        console.print(
            f"   {gi} [{gc}]GPU {ga // 1024}/{gt // 1024}GB[/{gc}]  [{sc}]swap {swap_mb // 1024}GB[/{sc}]  [{sc2}]{diag['status']}[/{sc2}]"
        )
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
        sc2 = {"healthy": "green", "degraded": "yellow", "critical": "red"}.get(
            status, "dim"
        )

        used_image_logo = False
        if _launch_inline_images_enabled(cfg):
            used_image_logo = _render_terminal_launch_logo(cfg, lang=ui_lang)

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
            border_color, verdict = "green", _ui_text(ui_lang, "READY", "جاهز")
        elif model_fits:
            border_color, verdict = "yellow", _ui_text(ui_lang, "READY", "جاهز")
        else:
            border_color, verdict = "red", _ui_text(ui_lang, "SLOW", "بطيء")

        # Dashboard table inside a panel (logo stays above)
        t = Table(show_header=False, show_edge=False, box=None, padding=0, expand=False)
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
            f"[{sc}]{swap_mb // 1024}GB[/{sc}]"
            + (" [dim]close apps to fix[/]" if swap_mb > 2000 else ""),
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
            cache_info = (
                f"  [dim]cache {di['hf_cache_gb']}GB[/]"
                if di["hf_cache_gb"] > 0
                else ""
            )
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
            t.add_row(
                "Model", f"[cyan]{mn}{mq}{ms}[/]", f"{gb}  ctx {srv['n_ctx'] // 1024}K"
            )
        else:
            # Check Ollama for loaded models
            _ollama_model = None
            try:
                import urllib.request as _ur_mdl, json as _j_mdl

                _or = _ur_mdl.urlopen("http://127.0.0.1:11434/api/ps", timeout=2)
                _od = _j_mdl.loads(_or.read())
                if _od.get("models"):
                    _om = _od["models"][0]
                    _ollama_model = _om.get("name", "")
                    _osize = _om.get("size", 0) // (1024**3)
            except Exception:
                pass
            if _ollama_model:
                t.add_row(
                    "Model",
                    f"[cyan]{_ollama_model}[/]",
                    f"[green]● Ollama[/]  {_osize}GB GPU",
                )
            else:
                t.add_row(
                    "Model", "[dim]not running[/]", "[dim]type /models to start[/]"
                )

        # Machine
        t.add_row(
            "Machine",
            f"[dim]{specs['chip']}[/]",
            f"[dim]{specs['ram_gb']}GB  {specs.get('gpu_cores', '?')} GPU cores[/]",
        )

        if used_image_logo:
            # Detect actual connection status - check configured API
            _online = srv["running"]
            if not _online:
                # Check Ollama
                try:
                    import urllib.request as _ur_boot

                    _ur_boot.urlopen("http://127.0.0.1:11434/v1/models", timeout=1)
                    _online = True
                except Exception:
                    pass
            if not _online:
                # Check the API we were launched with
                try:
                    import urllib.request as _ur_boot2

                    _ur_boot2.urlopen(f"{api_base}/models", timeout=1)
                    _online = True
                except Exception:
                    pass
            _status_text = (
                _ui_text(ui_lang, "online", "متصل")
                if _online
                else _ui_text(ui_lang, "offline", "دون اتصال")
            )
            _status_color = "#53d6ff" if _online else "red"
            _dot = "✓" if _online else "✗"
            console.print(
                f"  [#d7b46a]✦[/] [dim]{_ui_text(ui_lang, 'Command-line interface', 'واجهة سطر الأوامر')}[/]  [{_status_color}]{_dot} {_status_text}[/{_status_color}]"
            )
            console.print()
            console.print(
                Panel(
                    t,
                    title=f"[bold #53d6ff] {_ui_text(ui_lang, 'system status', 'حالة النظام')} [/]",
                    subtitle=f"[bold {border_color}] {verdict} [/]",
                    border_style=border_color,
                    box=box.DOUBLE,
                    padding=(1, 2),
                    width=86,
                )
            )
        else:
            console.print(
                _render_retro_launcher(
                    specs,
                    srv,
                    verdict,
                    border_color,
                    used_mb,
                    gpu_total,
                    free_mb,
                    swap_mb,
                    model_fits and swap_mb < 2000,
                    ui_lang=ui_lang,
                )
            )

        # One verdict line
        if model_fits and swap_mb < 2000:
            console.print(
                f"  [green]{_ui_text(ui_lang, 'All good. Full speed.', 'كل شيء جاهز. السرعة كاملة.')}[/]"
            )
        elif model_fits:
            console.print(
                f"  [yellow]{_ui_text(ui_lang, 'AI runs fine. Mac slow from other apps using RAM.', 'الذكاء الاصطناعي يعمل جيداً، لكن الجهاز أبطأ بسبب تطبيقات أخرى تستهلك الذاكرة.')}[/]"
            )
        else:
            console.print(
                f"  [red]{_ui_text(ui_lang, 'Model too big — ~5 tok/s instead of ~49. Try --simulate for alternatives.', 'النموذج كبير جداً — حوالي 5 رمز/ث بدلاً من 49. استخدم --simulate لرؤية البدائل.')}[/]"
            )

        # ── Trending models (fetched live) ──
        try:
            from localcoder.backends import (
                fetch_unsloth_top_models,
                COMMUNITY_CODING_MODELS,
            )
            from rich.markup import escape as _esc
            from localcoder.backends import get_disk_info

            # Detect what's already installed
            di = get_disk_info()
            installed_names = {
                m["name"].lower().replace(".gguf", "").replace("-", "").replace("_", "")
                for m in di.get("models", [])
            }

            all_downloadable = []

            # Show installed models first
            if di.get("models"):
                console.print(
                    f"\n  [dim]── Installed ({len(di['models'])} models, {sum(m['size_gb'] for m in di['models']):.0f}GB) ──[/]"
                )
                for m in di["models"][:5]:
                    name = _esc(m["name"].replace(".gguf", ""))
                    console.print(f"  [green]✓[/]  {name:<28} [dim]{m['size_gb']}GB[/]")

            # Trending
            console.print(f"\n  [dim]── Trending (live from HuggingFace) ──[/]")
            trending = fetch_unsloth_top_models(limit=5)
            num = 1
            for m in trending:
                dl = m["downloads"]
                dl_str = (
                    f"{dl // 1000}K" if dl < 1_000_000 else f"{dl / 1_000_000:.1f}M"
                )
                label = _esc(m["label"])
                # Capability icons
                caps = m.get("caps", [])
                cap_str = ""
                if "vision" in caps:
                    cap_str += " [magenta]img[/]"
                if "code" in caps:
                    cap_str += " [cyan]code[/]"
                if "MoE" in caps:
                    cap_str += " [green]MoE[/]"
                if "audio" in caps:
                    cap_str += " [yellow]audio[/]"

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
                    console.print(
                        f"  [green]✓[/]  {label:<22}{cap_str}  [dim]{dl_str} dl  installed[/]"
                    )
                else:
                    console.print(
                        f"  [bold cyan]{num}[/]  {label:<22}{cap_str}  [dim]{dl_str} dl[/]{fit_tag}"
                    )
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
                        dl_str = (
                            f"{dl // 1000}K"
                            if dl < 1_000_000
                            else f"{dl / 1_000_000:.1f}M"
                        )
                        caps = lm.get("caps", [])
                        cap_str = ""
                        if "vision" in caps:
                            cap_str += " [magenta]img[/]"
                        if "code" in caps:
                            cap_str += " [cyan]code[/]"
                        if "MoE" in caps:
                            cap_str += " [green]MoE[/]"
                        est = lm.get("est_smallest_gb")
                        fit_tag = ""
                        if est:
                            if est * 1024 > gpu_total:
                                fit_tag = f" [red]~{est}GB min · won't fit[/]"
                            else:
                                fit_tag = f" [green]~{est}GB min · fits[/]"
                        base = lm["label"].lower().replace("-", "").replace("_", "")
                        if any(base in inst for inst in installed_names):
                            console.print(
                                f"  [green]✓[/]  {label:<22}{cap_str}  [dim]{dl_str} dl  installed[/]"
                            )
                        else:
                            console.print(
                                f"  [bold cyan]{num}[/]  {label:<22}{cap_str}  [dim]{dl_str} dl[/]{fit_tag}"
                            )
                        all_downloadable.append(
                            {"label": lm["label"], "repo": lm["repo_id"]}
                        )
                        num += 1
            except Exception:
                pass

        except Exception:
            all_downloadable = []

        console.print(f"\n  [bold]enter[/] start coding")
        console.print(
            f"  [bold]1-{max(1, len(all_downloadable))}[/]   try a model [dim](shows quants, downloads if needed)[/]"
        )
        console.print(
            f"  [bold]c[/]     cleanup GPU  [bold]s[/] skip boot  [bold]q[/] quit"
        )

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
            console.print(
                "\n  [bold]Freeing memory...[/]  [dim](only unloading unused AI models — your apps are safe)[/]"
            )
            result = cleanup_gpu_memory(force=False)
            if result["ollama_unloaded"]:
                console.print(
                    f"  [green]✓[/] Unloaded: {', '.join(result['ollama_unloaded'])}"
                )
            else:
                console.print(f"  [dim]No unused models to unload.[/]")

            # Show what user could close
            big_hogs = [
                p for p in procs if p["category"] in ("app", "bloat") and p["mb"] >= 500
            ]
            if big_hogs:
                console.print(
                    f"\n  [bold]{_ui_text(ui_lang, 'Want more speed?', 'هل تريد سرعة أكبر؟')}[/] {_ui_text(ui_lang, 'Close these apps when you do not need them:', 'أغلق هذه التطبيقات عندما لا تحتاجها:')}"
                )
                for p in big_hogs:
                    n = p["name"] + (f" ×{p['count']}" if p.get("count", 1) > 1 else "")
                    console.print(f"    {n}  [dim]({p['mb'] // 1024}GB)[/]")

            console.print(
                f"\n  [dim]{_ui_text(ui_lang, 'Press Enter to start coding...', 'اضغط إدخال لبدء البرمجة...')}[/]"
            )
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
                console.print(
                    f"  [dim]Got it. Run localcoder --health anytime to see this again.[/]"
                )
                import time as _t2

                _t2.sleep(1)
            except Exception:
                pass

        # Clear boot screen, start fresh
        os.system("clear" if os.name != "nt" else "cls")

    if not args.prompt:
        os.system("clear" if os.name != "nt" else "cls")

    # ── Set env and run the agent ──
    os.environ["GEMMA_API_BASE"] = api_base
    os.environ["GEMMA_MODEL"] = model
    os.environ["LOCALCODER_UI_LANG"] = ui_lang

    from localcoder.agent import run_agent

    try:
        run_agent(api_base, model, args)
    finally:
        if restore_terminal_font:
            try:
                restore_terminal_font()
            except Exception:
                pass


if __name__ == "__main__":
    main()
