# localcoder

**Local AI coding agent that generates images, builds apps, and runs 100% on your GPU.**

```bash
pip install localcoder
localcoder
```

## What It Does

A CLI coding agent like Claude Code — but runs locally with zero API keys, zero cloud, zero cost.

- **Generates images locally** — Flux AI on your GPU, not stock photos or placeholders
- **Builds full web apps** — landing pages, AI-powered backends, interactive games
- **MCP tool support** — connect any MCP server (image gen, databases, etc.)
- **Voice input** — Ctrl+R for local Whisper STT
- **Vision** — Ctrl+V to paste screenshots, PDFs, clipboard images
- **Session persistence** — resume conversations with `-c`
- **100% offline** — works on airplane mode

## Demo

```
❯ localcoder --compact --yolo

❯ create a pet shop landing page with 3 animal icons

  ⚡ generate_image → dog-icon.png (4s)
  ⚡ generate_image → cat-icon.png (4s)
  ⚡ generate_image → rabbit-icon.png (4s)
  ← Writing index.html (176 lines)
  ⚡ preview_app → screenshot captured

  ✦ 45s · 850 tokens
```

Images generated locally with Flux. HTML written with dark glassmorphism theme. Zero internet.

## Cost: $0.00 vs $110/month

| Usage | Claude Sonnet | Claude Opus | localcoder |
|-------|--------------|-------------|------------|
| 4h/day | $55/mo | $91/mo | **$0.00** |
| 8h/day | $110/mo | $183/mo | **$0.00** |

*Electricity cost: ~$1.30/mo on M4 Pro at 30W.*

## Install

```bash
# macOS (Apple Silicon)
pip install localcoder

# First run — auto-detects hardware, downloads model, starts
localcoder
```

Needs [Ollama](https://ollama.com) or [llama.cpp](https://github.com/ggml-org/llama.cpp). First run wizard handles setup.

## Usage

```bash
localcoder                                          # interactive
localcoder -p "build a react landing page"          # one-shot
localcoder -m gemma4:e4b --compact --yolo           # E4B with compact prompt
localcoder -m gemma4:26b --yolo                     # 26B model (needs 24GB+)
localcoder --system "You are a React expert" --yolo # custom system prompt
localcoder --system ~/my-prompt.txt --yolo          # system prompt from file
localcoder -c                                       # continue last session
```

## Tools

The agent has these built-in tools:

| Tool | What |
|------|------|
| `generate_image` | Generate images locally with Flux AI (icons, heroes, portraits) |
| `write_file` | Write complete files (HTML, JS, Python, etc.) |
| `read_file` | Read files |
| `edit_file` | Find and replace in files |
| `preview_app` | Open HTML in browser, take screenshot to verify |
| `bash` | Run any shell command |
| `web_search` | Search the web via DuckDuckGo |
| `fetch_url` | Fetch any webpage |

### MCP Tools

Connect external MCP servers for additional capabilities:

```json
// ~/.localcoder/mcp.json
{
  "servers": {
    "localfit-image": {
      "command": "python3",
      "args": ["-m", "localfit.mcp_image"]
    }
  }
}
```

MCP tools are auto-discovered at startup and available to the agent as `mcp__servername__toolname`.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+V` | Paste + display image from clipboard |
| `Ctrl+R` | Toggle voice input (local Whisper) |
| `Ctrl+C` | Clear input / double-tap to quit |

## Slash Commands

| Command | Action |
|---------|--------|
| `/gpu` | GPU memory, swap, model status |
| `/clean` | Free GPU memory |
| `/models` | Switch model |
| `/mcp` | Show MCP servers and tools |
| `/sessions` | List saved sessions |
| `/handoff` | Generate continuation prompt for new session |
| `/think` | Toggle reasoning level |
| `/context` | Show token usage |
| `/clear` | Clear conversation |
| `/undo` | Revert last file change |

## Image Generation

localcoder generates images locally using Flux models — no internet, no API keys, no stock photos.

```bash
# Start the image server (needs localfit)
pip install localfit
localfit serve-image klein-4b

# localcoder auto-detects it at localhost:8189
localcoder --compact --yolo
❯ create a landing page with cute cat icons
  → generate_image("cute cat icon, kawaii, pastel pink") → cat.png (4s)
```

| Model | Speed (M4 Pro) | Quality |
|-------|---------------|---------|
| klein-4b | ~4s/image | Good for icons |
| schnell | ~15s/image | Better quality |

## Hardware

| Mac | RAM | Best Model | Speed |
|-----|-----|-----------|-------|
| Air M2 | 8 GB | Qwen 3.5 4B | 50 tok/s |
| Air M3 | 16 GB | Gemma 4 E4B | 57 tok/s |
| **Pro M4** | **24 GB** | **Gemma 4 26B Q3** | **47 tok/s** |
| Pro M4 | 48 GB | Gemma 4 26B Q4 | 47 tok/s |

## New in 0.4.0

- **Local image generation** — generate_image calls local Flux server
- **MCP tool support** — connect any MCP server via stdio
- **Session persistence** — JSONL sessions in `~/.localcoder/sessions/`
- **Smart compaction** — LLM-based structured summarization
- **Safe command auto-approval** — read-only bash commands don't need confirmation
- **Compact prompt mode** — `--compact` for small models (E4B)
- **Custom system prompts** — `--system` flag
- **Preview tool** — `preview_app` opens HTML and takes screenshot
- **Tool aliases** — handles hallucinated tool names gracefully
- **Better error handling** — streaming timeout, partial JSON recovery

## Security

Sandbox mode is **ON by default**:

| Blocked | Examples |
|---------|----------|
| Destructive commands | `rm -rf`, `sudo`, `kill` |
| Pipe to shell | `curl ... \| bash` |
| Protected paths | `~/.ssh`, `~/.aws`, `/etc/` |
| Write outside project | Only CWD and /tmp allowed |

```bash
localcoder                    # sandboxed (default)
localcoder --yolo             # auto-approve but sandbox ON
localcoder --unrestricted     # sandbox OFF (dangerous)
```

## Architecture

```
┌─────────────────────────────────────────┐
│  localcoder CLI                         │
│  ┌───────────┐  ┌──────────────────┐    │
│  │ Agent Loop │→│ Gemma 4 (Ollama) │    │
│  │ 10 turns   │  └──────────────────┘    │
│  └─────┬─────┘                          │
│        │ tool calls                      │
│  ┌─────┴──────────────────────────┐     │
│  │ generate_image → Flux (local)  │     │
│  │ write_file → disk              │     │
│  │ preview_app → browser + screenshot│  │
│  │ bash → shell                   │     │
│  │ mcp__* → MCP servers (stdio)   │     │
│  └────────────────────────────────┘     │
│                                         │
│  Sessions: ~/.localcoder/sessions/*.jsonl│
│  Skills: .agents/skills/*/SKILL.md      │
│  MCP: ~/.localcoder/mcp.json            │
└─────────────────────────────────────────┘
```

## License

Apache-2.0
