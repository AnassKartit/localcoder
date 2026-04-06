"""Voice input — hold Space to talk, release to transcribe."""
import os, subprocess, signal, tempfile, time, shutil
from pathlib import Path

from rich.console import Console

console = Console()

# ── Paths ──
WHISPER_BIN = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
WHISPER_MODEL_DIR = Path.home() / ".local/share/whisper"
WHISPER_MODEL = WHISPER_MODEL_DIR / "ggml-base.bin"
SOX_REC = shutil.which("rec") or "/opt/homebrew/bin/rec"


def is_voice_available():
    """Check if voice input dependencies are installed."""
    return os.path.exists(WHISPER_BIN) and WHISPER_MODEL.exists() and os.path.exists(SOX_REC)


def check_mic_permission():
    """Check macOS microphone permission by attempting a quick recording."""
    try:
        tmp = tempfile.mktemp(suffix=".wav")
        proc = subprocess.run(
            [SOX_REC, "-q", "-r", "16000", "-c", "1", "-b", "16", tmp, "trim", "0", "0.5"],
            capture_output=True, text=True, timeout=5
        )
        if os.path.exists(tmp):
            size = os.path.getsize(tmp)
            os.unlink(tmp)
            if size > 100:
                return True
        # Check stderr for permission errors
        if "permission" in proc.stderr.lower() or "not authorized" in proc.stderr.lower():
            return False
        return proc.returncode == 0
    except:
        return False


def setup_voice():
    """Interactive setup for voice input."""
    console.print(f"\n  [bold magenta]Voice Input Setup[/]\n")

    # Step 1: Check sox/rec
    if not os.path.exists(SOX_REC):
        console.print(f"  [yellow]Installing sox (audio recorder)...[/]")
        r = subprocess.run(["brew", "install", "sox"], timeout=120)
        if r.returncode != 0:
            console.print(f"  [red]Failed to install sox. Run: brew install sox[/]")
            return False
        console.print(f"  [green]✓ sox installed[/]")
    else:
        console.print(f"  [green]✓ sox already installed[/]")

    # Step 2: Check whisper-cli
    whisper_bin = shutil.which("whisper-cli")
    if not whisper_bin:
        console.print(f"  [yellow]Installing whisper-cpp...[/]")
        r = subprocess.run(["brew", "install", "whisper-cpp"], timeout=300)
        if r.returncode != 0:
            console.print(f"  [red]Failed to install whisper-cpp. Run: brew install whisper-cpp[/]")
            return False
        console.print(f"  [green]✓ whisper-cpp installed[/]")
    else:
        console.print(f"  [green]✓ whisper-cpp already installed[/]")

    # Step 3: Download whisper model
    if not WHISPER_MODEL.exists():
        console.print(f"  [yellow]Downloading whisper base model (148MB)...[/]")
        console.print(f"  [dim]Supports: English, French, Arabic + 95 more languages[/]")
        WHISPER_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([
            "curl", "-L", "--progress-bar",
            "-o", str(WHISPER_MODEL),
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
        ], timeout=300)
        if r.returncode != 0 or not WHISPER_MODEL.exists():
            console.print(f"  [red]Failed to download whisper model[/]")
            return False
        console.print(f"  [green]✓ Whisper base model downloaded ({WHISPER_MODEL.stat().st_size // 1024 // 1024}MB)[/]")
    else:
        console.print(f"  [green]✓ Whisper model ready ({WHISPER_MODEL.stat().st_size // 1024 // 1024}MB)[/]")

    # Step 4: Check microphone permission
    console.print(f"\n  [dim]Testing microphone access...[/]")
    if check_mic_permission():
        console.print(f"  [green]✓ Microphone access granted[/]")
    else:
        console.print(f"  [yellow]⚠ Microphone access needed[/]")
        console.print(f"  [dim]macOS will prompt for permission on first recording.[/]")
        console.print(f"  [dim]If denied, go to: System Settings → Privacy & Security → Microphone[/]")
        console.print(f"  [dim]and enable access for your terminal app (iTerm2 / Terminal).[/]")

    console.print(f"\n  [green]✓ Voice input ready![/]")
    console.print(f"  [dim]Hold Space while typing prompt to record, release to transcribe.[/]")
    return True


def record_audio(max_seconds=30):
    """Record from mic until stopped. Returns path to WAV file."""
    wav_path = tempfile.mktemp(suffix=".wav")
    proc = subprocess.Popen(
        [SOX_REC, "-q", "-r", "16000", "-c", "1", "-b", "16", wav_path,
         "trim", "0", str(max_seconds)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, wav_path


def stop_recording(proc):
    """Stop the recording process."""
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=3)
    except:
        proc.kill()


def transcribe(wav_path, language="auto"):
    """Transcribe audio using whisper-cli (CPU-only, no GPU conflict)."""
    whisper = shutil.which("whisper-cli") or WHISPER_BIN
    if not os.path.exists(whisper):
        return None, "whisper-cli not found"

    model = str(WHISPER_MODEL)
    if not os.path.exists(model):
        return None, "whisper model not found"

    try:
        # Use Metal GPU if llama-server has headroom (base model ~200MB),
        # otherwise fall back to CPU-only
        gpu_flags = []
        try:
            # Check free GPU headroom
            out = subprocess.run(["pgrep", "-f", "llama-server"], capture_output=True, text=True)
            if out.stdout.strip():
                # llama-server running — whisper base needs ~200MB, check if safe
                # With 2.7GB headroom on 24GB Mac, Metal whisper is fine
                gpu_flags = []  # let whisper use Metal (default)
            else:
                gpu_flags = []  # no conflict, use Metal
        except:
            gpu_flags = ["--no-gpu"]  # safe fallback

        result = subprocess.run(
            [whisper,
             "--model", model,
             "--language", language,
             "--no-timestamps",
             "--threads", "8",
             "--file", wav_path] + gpu_flags,
            capture_output=True, text=True, timeout=30
        )

        # Parse output — whisper-cli outputs text lines (with --no-timestamps, just raw text)
        lines = []
        detected_lang = None
        for line in result.stderr.split("\n"):
            if "auto-detected language:" in line:
                detected_lang = line.split("auto-detected language:")[-1].strip().split()[0]

        for line in result.stdout.split("\n"):
            line = line.strip()
            # Skip empty lines and metadata
            if line and not line.startswith("[") and not line.startswith("whisper_"):
                lines.append(line)

        text = " ".join(lines).strip()
        # Clean up common whisper artifacts
        text = text.replace("(silence)", "").replace("[BLANK_AUDIO]", "").strip()

        return text, detected_lang

    except subprocess.TimeoutExpired:
        return None, "transcription timed out"
    except Exception as e:
        return None, str(e)
    finally:
        if os.path.exists(wav_path):
            try:
                os.unlink(wav_path)
            except:
                pass
