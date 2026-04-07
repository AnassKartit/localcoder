"""
localcoder_display.py — Memory-safe animation and display system for localcoder.

Uses rich.live for all animations (no background threads).
All functions are standalone and can be dropped into the main script.

Requirements: Python 3.10+, rich >= 13.0
"""

import time
from contextlib import contextmanager
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
)
from rich.spinner import Spinner
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# 1. THINKING SPINNER — shows during LLM inference
# ---------------------------------------------------------------------------

class ThinkingSpinner:
    """Single-line spinner that shows elapsed time, token count, and tok/s.

    Usage:
        spinner = ThinkingSpinner(console)
        spinner.start()
        # ... in your streaming loop ...
        spinner.update(tokens=42, tps=18.5)
        # ... when done ...
        spinner.stop(total_tokens=120, tps=22.0, elapsed=5.3)

    Or as a context manager:
        with ThinkingSpinner(console) as sp:
            sp.update(tokens=10)
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._live: Optional[Live] = None
        self._start_time: float = 0.0
        self._tokens: int = 0
        self._tps: float = 0.0

    # -- rendering ----------------------------------------------------------

    def _render(self) -> Text:
        elapsed = time.time() - self._start_time
        if elapsed < 60:
            time_str = f"{elapsed:.0f}s"
        else:
            m, s = divmod(elapsed, 60)
            time_str = f"{m:.0f}m {s:.0f}s"

        line = Text()
        line.append("  ")
        # spinner glyph — cycle through 4 frames based on elapsed
        frames = "◐◓◑◒"
        frame = frames[int(elapsed * 4) % len(frames)]
        line.append(frame, style="bold magenta")
        line.append(" ")
        line.append("Thinking…", style="italic magenta")
        line.append(f"  {time_str}", style="dim")

        if self._tokens > 0:
            line.append(f"  ↓ {self._tokens} tokens", style="dim")
        if self._tps > 0:
            line.append(f"  {self._tps:.0f} tok/s", style="dim cyan")

        return line

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        self._start_time = time.time()
        self._tokens = 0
        self._tps = 0.0
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            transient=True,
        )
        self._live.start()

    def update(self, tokens: int = 0, tps: float = 0.0) -> None:
        """Update token count and speed. Call as often as you like."""
        if tokens > 0:
            self._tokens = tokens
        if tps > 0:
            self._tps = tps
        if self._live is not None:
            self._live.update(self._render())

    def stop(
        self,
        total_tokens: int = 0,
        tps: float = 0.0,
        elapsed: Optional[float] = None,
    ) -> None:
        """Stop the spinner and print a final summary line."""
        try:
            if self._live is not None:
                self._live.stop()
                self._live = None
        except Exception:
            pass

        if elapsed is None:
            elapsed = time.time() - self._start_time
        if elapsed < 60:
            t = f"{elapsed:.0f}s"
        else:
            m, s = divmod(elapsed, 60)
            t = f"{m:.0f}m {s:.0f}s"

        tok = total_tokens or self._tokens
        speed = tps or self._tps
        self._console.print(
            f"\n  [dim]✦ {t} · {tok} tokens · {speed:.0f} tok/s[/]"
        )

    def __enter__(self) -> "ThinkingSpinner":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._live is not None:
                self._live.stop()
                self._live = None
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2. STARTUP ANIMATION — gradient banner + connect/ready transition
# ---------------------------------------------------------------------------

_GRADIENT_COLORS = [
    "#ff6ec7", "#d16bff", "#9b6bff", "#6b8bff", "#6bcfff",
    "#6bffcf", "#6bff8b", "#b5ff6b", "#ffef6b", "#ffb86b",
]


def _gradient_text(text: str, colors: list[str]) -> Text:
    """Apply a gradient across characters of *text*."""
    result = Text()
    n = max(len(colors), 1)
    for i, ch in enumerate(text):
        color = colors[i % n]
        result.append(ch, style=Style(color=color, bold=True))
    return result


def show_startup_animation(console: Console, backend_info: dict) -> None:
    """Elegant startup: gradient banner -> connecting -> ready.

    Total animation time < 500 ms.  Uses Live for smooth updates.
    """
    banner_text = "localcoder"
    model_name = backend_info.get("model_name", "unknown")
    backend = backend_info.get("backend", "unknown")
    ctx = backend_info.get("ctx", "")
    quant = backend_info.get("quant", "")
    size = backend_info.get("size", "")

    model_label = f"Gemma 4 {size}" if size else model_name
    if quant:
        model_label += f" {quant}"

    # Phase 1: gradient banner + "connecting..."
    def _frame_connecting() -> Panel:
        title = Text()
        title.append("◆ ", style="bold magenta")
        title.append_text(_gradient_text(banner_text, _GRADIENT_COLORS))

        inner = Text()
        inner.append(f" {model_label} ", style="bold white on rgb(60,20,80)")
        inner.append("  ", style="dim")
        inner.append("connecting…", style="italic yellow")

        return Panel(
            inner,
            title=title,
            title_align="left",
            border_style="magenta",
            padding=(0, 1),
        )

    # Phase 2: gradient banner + ready info
    def _frame_ready() -> Panel:
        title = Text()
        title.append("◆ ", style="bold magenta")
        title.append_text(_gradient_text(banner_text, _GRADIENT_COLORS))

        inner = Text()
        inner.append(f" {model_label} ", style="bold white on rgb(60,20,80)")
        inner.append("  ", style="dim")
        inner.append(backend, style="green")
        inner.append(" (local)", style="dim green")
        if ctx:
            inner.append("  ", style="dim")
            inner.append(ctx, style="bold green")
        inner.append("  ", style="dim")
        inner.append("$0.00", style="bold green")

        return Panel(
            inner,
            title=title,
            title_align="left",
            border_style="magenta",
            padding=(0, 1),
        )

    console.print()
    try:
        with Live(
            _frame_connecting(),
            console=console,
            refresh_per_second=10,
            transient=True,
        ) as live:
            time.sleep(0.25)
            live.update(_frame_ready())
            time.sleep(0.15)
    except KeyboardInterrupt:
        pass

    # Print the final frame permanently
    console.print(_frame_ready())


# ---------------------------------------------------------------------------
# 3. TOOL CALL ANIMATIONS — single-line, non-blocking
# ---------------------------------------------------------------------------

def show_tool_animation(
    console: Console,
    tool_name: str,
    args: dict,
) -> None:
    """Display a tool call indicator. Single line, returns immediately.

    Supported tools: bash, write_file, read_file, edit_file, web_search, fetch_url.
    """
    if tool_name == "bash":
        cmd = args.get("command", "")[:120]
        # Brief syntax-highlighted command display
        try:
            syn = Syntax(
                f"$ {cmd}",
                "bash",
                theme="monokai",
                line_numbers=False,
                word_wrap=True,
            )
            console.print(
                Panel(
                    syn,
                    title="[bold yellow]⚙ bash[/]",
                    title_align="left",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
        except Exception:
            console.print(
                Panel(
                    Text(f"$ {cmd}", style="cyan"),
                    title="[bold yellow]⚙ bash[/]",
                    title_align="left",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )

    elif tool_name == "write_file":
        path = args.get("path", "?")
        sz = len(args.get("content", ""))
        line = Text()
        line.append("  ← ", style="bold green")
        line.append("Writing ", style="green")
        line.append(path, style="bold white")
        line.append(f"  ({sz} chars)", style="dim")
        console.print(line)

    elif tool_name == "read_file":
        path = args.get("path", "?")
        line = Text()
        line.append("  → ", style="bold blue")
        line.append("Reading ", style="blue")
        line.append(path, style="bold white")
        console.print(line)

    elif tool_name == "edit_file":
        path = args.get("path", "?")
        line = Text()
        line.append("  ← ", style="bold green")
        line.append("Editing ", style="green")
        line.append(path, style="bold white")
        console.print(line)

    elif tool_name == "web_search":
        query = args.get("query", "")
        line = Text()
        line.append("  🔍 ", style="bold magenta")
        line.append("Searching ", style="magenta")
        line.append(f'"{query}"', style="bold white")
        line.append(" ···", style="dim magenta")
        console.print(line)

    elif tool_name == "fetch_url":
        url = args.get("url", "")[:80]
        line = Text()
        line.append("  🌐 ", style="bold blue")
        line.append("Fetching ", style="blue")
        line.append(url, style="dim")
        console.print(line)

    else:
        console.print(f"  [yellow]⚡ {tool_name}[/]")


@contextmanager
def tool_running_indicator(console: Console, tool_name: str):
    """Context manager: shows a brief 'running...' indicator while a tool executes.

    Usage:
        with tool_running_indicator(console, "bash"):
            result = exec_tool("bash", args)
    """
    labels = {
        "bash": "running…",
        "write_file": "writing…",
        "read_file": "reading…",
        "edit_file": "editing…",
        "web_search": "searching…",
        "fetch_url": "fetching…",
    }
    label = labels.get(tool_name, "running…")

    spinner_text = Text()
    spinner_text.append("  ")
    spinner_text.append_text(Spinner("dots").render(time.time()))
    spinner_text.append(f" {label}", style="dim italic")

    live = Live(
        spinner_text,
        console=console,
        refresh_per_second=8,
        transient=True,
    )
    try:
        live.start()
        yield
    finally:
        try:
            live.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 4. TOKEN STREAM DISPLAY — "generating..." then full markdown render
# ---------------------------------------------------------------------------

@contextmanager
def generating_indicator(console: Console):
    """Shows a live 'generating...' indicator. Stop it, then render markdown.

    Usage:
        with generating_indicator(console):
            response = chat_api(messages)
        show_response(console, response_text)
    """
    line = Text()
    line.append("  ")
    line.append("◐", style="bold magenta")
    line.append(" generating…", style="italic dim")

    live = Live(
        line,
        console=console,
        refresh_per_second=4,
        transient=True,
    )
    try:
        live.start()
        yield live
    finally:
        try:
            live.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5. CONTEXT USAGE BAR
# ---------------------------------------------------------------------------

def context_usage_bar(
    console: Console,
    used_tokens: int,
    max_tokens: int,
) -> None:
    """Print a thin context-usage bar. Green <50%, yellow 50-80%, red >80%.

    Example output:   ▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱  4.2K / 128K tokens
    """
    if max_tokens <= 0:
        return

    ratio = min(used_tokens / max_tokens, 1.0)
    pct = ratio * 100

    if pct < 50:
        bar_style = "green"
    elif pct < 80:
        bar_style = "yellow"
    else:
        bar_style = "red"

    # 30-char bar
    bar_width = 30
    filled = int(ratio * bar_width)
    empty = bar_width - filled

    bar = Text()
    bar.append("  ")
    bar.append("▰" * filled, style=bar_style)
    bar.append("▱" * empty, style="dim")
    bar.append("  ", style="dim")

    # Format token counts: 4200 -> "4.2K", 128000 -> "128K"
    def _fmt(n: int) -> str:
        if n >= 1000:
            k = n / 1000
            return f"{k:.1f}K" if k < 100 else f"{k:.0f}K"
        return str(n)

    bar.append(f"{_fmt(used_tokens)} / {_fmt(max_tokens)} tokens", style="dim")
    bar.append(f"  ({pct:.0f}%)", style=f"dim {bar_style}")

    console.print(bar)


def context_usage_bar_compact(
    used_tokens: int,
    max_tokens: int,
) -> Text:
    """Return a Text renderable (for embedding in panels/toolbars)."""
    if max_tokens <= 0:
        return Text("? / ? tokens", style="dim")

    ratio = min(used_tokens / max_tokens, 1.0)
    pct = ratio * 100

    if pct < 50:
        bar_style = "green"
    elif pct < 80:
        bar_style = "yellow"
    else:
        bar_style = "red"

    bar_width = 15
    filled = int(ratio * bar_width)
    empty = bar_width - filled

    def _fmt(n: int) -> str:
        if n >= 1000:
            k = n / 1000
            return f"{k:.1f}K" if k < 100 else f"{k:.0f}K"
        return str(n)

    result = Text()
    result.append("▰" * filled, style=bar_style)
    result.append("▱" * empty, style="dim")
    result.append(f" {_fmt(used_tokens)}/{_fmt(max_tokens)}", style="dim")
    return result


# ---------------------------------------------------------------------------
# 6. CONVENIENCE: drop-in replacements for existing localcoder functions
# ---------------------------------------------------------------------------

def print_thinking_live(
    console: Console,
    tokens: int = 0,
    tps: float = 0.0,
    start_time: float = 0.0,
) -> Text:
    """Return a renderable for the thinking state (for use with Live)."""
    elapsed = time.time() - start_time if start_time else 0
    if elapsed < 60:
        time_str = f"{elapsed:.0f}s"
    else:
        m, s = divmod(elapsed, 60)
        time_str = f"{m:.0f}m {s:.0f}s"

    frames = "◐◓◑◒"
    frame = frames[int(elapsed * 4) % len(frames)]

    line = Text()
    line.append("  ")
    line.append(frame, style="bold magenta")
    line.append(" ")
    line.append("Thinking…", style="italic magenta")
    line.append(f"  {time_str}", style="dim")
    if tokens > 0:
        line.append(f"  ↓ {tokens} tokens", style="dim")
    if tps > 0:
        line.append(f"  {tps:.0f} tok/s", style="dim cyan")
    return line


# ---------------------------------------------------------------------------
# DEMO / SELF-TEST
# ---------------------------------------------------------------------------

def _demo() -> None:
    """Run a visual demo of all components."""
    c = Console()

    c.print("\n[bold underline]1. Startup Animation[/]\n")
    show_startup_animation(c, {
        "backend": "llama.cpp",
        "model_name": "gemma-4-26B-A4B-it-UD-Q3_K_XL",
        "quant": "Q3_K_XL",
        "size": "26B",
        "ctx": "32K",
    })

    c.print("\n[bold underline]2. Thinking Spinner[/]\n")
    with ThinkingSpinner(c) as sp:
        for i in range(12):
            time.sleep(0.15)
            sp.update(tokens=i * 8, tps=18.0 + i * 0.5)
    sp.stop(total_tokens=96, tps=24.0)

    c.print("\n[bold underline]3. Tool Call Animations[/]\n")
    show_tool_animation(c, "bash", {"command": "find . -name '*.py' | head -20"})
    show_tool_animation(c, "write_file", {"path": "src/app.py", "content": "x" * 1420})
    show_tool_animation(c, "read_file", {"path": "package.json"})
    show_tool_animation(c, "edit_file", {"path": "main.rs"})
    show_tool_animation(c, "web_search", {"query": "rust async trait 2025"})
    show_tool_animation(c, "fetch_url", {"url": "https://docs.rs/tokio/latest/tokio/"})

    c.print("\n[bold underline]4. Tool Running Indicator[/]\n")
    with tool_running_indicator(c, "bash"):
        time.sleep(0.6)
    c.print("  [dim]done[/]")

    c.print("\n[bold underline]5. Generating Indicator[/]\n")
    with generating_indicator(c):
        time.sleep(0.8)
    c.print("  [dim](response would render here)[/]")

    c.print("\n[bold underline]6. Context Usage Bar[/]\n")
    context_usage_bar(c, 2100, 131072)   # green
    context_usage_bar(c, 78000, 131072)  # yellow
    context_usage_bar(c, 118000, 131072) # red

    c.print("\n[bold underline]7. Compact Bar (for toolbar)[/]\n")
    c.print(Text("  "), context_usage_bar_compact(4200, 131072))

    c.print()


if __name__ == "__main__":
    _demo()
