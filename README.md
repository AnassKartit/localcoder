# localcoder

**The local coding CLI that does the obvious things nobody else does.**

```bash
pipx install localcoder
```

I wanted to paste a screenshot into my coding assistant and see it inline. No tool did that locally. So I built one.

## Cost: $1.30/month vs $110/month

Running local saves 85-141x compared to cloud APIs:

| Usage | Claude Sonnet | Claude Opus | Local (US) | Local (India) |
|-------|--------------|-------------|------------|---------------|
| 4h/day | $55/mo | $91/mo | **$0.65/mo** | $0.29/mo |
| 8h/day | $110/mo | $183/mo | **$1.30/mo** | $0.58/mo |
| 10h/day | $137/mo | $228/mo | **$1.62/mo** | $0.72/mo |

*Based on: Gemma 4 26B at 47 tok/s, 30% active generation, M4 Pro 30W. Electricity: [worldpopulationreview.com](https://worldpopulationreview.com/country-rankings/cost-of-electricity-by-country). API: [anthropic.com](https://www.anthropic.com/pricing).*

**Annual savings: ~$1,300-$2,700** depending on usage and API choice.

## What's Actually Different

| Feature | localcoder | aider | OpenCode | Claude Code |
|---------|-----------|-------|----------|-------------|
| Paste image, see it inline | **Ctrl+V → shows in terminal** | no | no | cloud only |
| Voice input (local) | **Ctrl+R → Whisper, no cloud** | no | no | no |
| See GPU memory while coding | **/gpu → live stats** | no | no | no |
| Computer use (screenshot + click) | **built-in** | no | no | cloud only |
| Free GPU when it's slow | **/clean → before/after** | no | no | n/a |
| Browse HuggingFace models | **built-in model browser** | no | no | n/a |
| Works offline | **100%** | partial | partial | no |
| Cost | **$0.00** | API costs | API costs | $20/mo+ |

## Demo

```
❯ localcoder

  localcoder  ·  local AI coding agent  ·  $0.00 forever

  ┌──────────────────────────────────────────────────┐
  │  LOCAL CODER                                     │
  └──────────────────────────────────────────────────┘

  ● Gemma 4 26B Q3_K_XL  ·  llama.cpp  ·  128K  ·  ● GPU  ·  47 tok/s
  ✓ offline  ·  no API keys  ·  no data sent

  ctrl+r voice  ctrl+v image  /gpu stats  /clean free  /models switch

❯ /gpu
  GPU  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  12/16GB  3GB free
  Swap 3GB  Pressure normal
  Model Gemma 4 26B Q3_K_XL  GPU  ctx 128K  footprint 2311MB
```

## Benchmark — M4 Pro 24GB

Real tests, real hardware, no synthetic benchmarks:

| Model | Size | tok/s | Notes |
|-------|------|-------|-------|
| **Gemma 4 26B** Q3_K_XL | 12.0GB | 47 | Best overall — vision + tool calling |
| **Qwen3.5-35B** MoE Q2_K_XL | 11.3GB | 46 | Best coding quality |
| **Qwen3.5-4B** Q4_K_XL | 2.7GB | 46 | Quick tasks |
| Gemma 4 E4B Q4_K_M | 5.0GB | 56 | Fastest — good for 16GB Macs |
| ~~Qwen3.5-27B Dense~~ | ~~13.4GB~~ | ~~7~~ | ~~Swap thrashing — don't use on 24GB~~ |

## Install

```bash
# macOS (Apple Silicon)
pipx install localcoder

# First run — auto-detects hardware, shows what fits, starts model
localcoder
```

Needs [llama.cpp](https://github.com/ggml-org/llama.cpp) or [Ollama](https://ollama.com). First run wizard handles this.

## Commands

```bash
localcoder                           # interactive coding
localcoder -p "build a react app"    # one-shot
localcoder --yolo                    # auto-approve tools
```

### While Coding

| Command | What |
|---------|------|
| `Ctrl+V` | Paste + display image from clipboard |
| `Ctrl+R` | Toggle voice input (local Whisper) |
| `/gpu` | GPU memory, swap, model status |
| `/clean` | Free GPU memory with before/after |
| `/models` | Switch model (includes HuggingFace trending) |
| `/clear` | Clear conversation |

### Also works with Claude Code

Don't want localcoder's agent? Use Claude Code with your local model instead:

```bash
pip install localfit
localfit --launch claude --model gemma4-26b
```

One command: starts model → configures Claude Code → launches with `--bare` flag.
See [localfit](https://github.com/AnassKartit/localfit) for details.

### GPU Toolkit (localfit inside)

```bash
localcoder --simulate               # will this model fit my GPU?
localcoder --fetch unsloth/...      # check all quants from HuggingFace
localcoder --bench                  # benchmark models on YOUR hardware
localcoder --health                 # GPU health dashboard
localcoder --config opencode        # auto-configure OpenCode for local models
localcoder --config aider           # auto-configure aider
```

Also available standalone: `pipx install localfit`

## Hardware

| Mac | RAM | Best Model | Speed |
|-----|-----|-----------|-------|
| Air M2 | 8 GB | Qwen 3.5 4B | 50 tok/s |
| Air M3 | 16 GB | Gemma 4 E4B | 57 tok/s |
| **Pro M4** | **24 GB** | **Gemma 4 26B Q3_K_XL** | **47 tok/s** |

## License

Apache-2.0

## Security

Sandbox mode is **ON by default**. Protects against destructive model outputs:

| Blocked | Examples |
|---------|----------|
| Destructive commands | `rm -rf`, `sudo`, `kill`, `mkfs` |
| Pipe to shell | `curl ... \| bash`, `wget ... \| sh` |
| Protected paths | `~/.ssh`, `~/.aws`, `~/.env`, `/etc/` |
| Path traversal | `../../etc/passwd` |
| Computer use | Disabled in sandbox |

```bash
localcoder                    # sandboxed (default)
localcoder --yolo             # auto-approve but sandbox ON
localcoder --unrestricted     # sandbox OFF (shows warning)
```

Approved tools are remembered across sessions (`~/.localcoder/approved_tools.json`).

## Tests

```bash
pip install pytest
pytest tests/ -v      # 19 tests
```
