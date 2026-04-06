"""Agent runner — delegates to the localcoder agent script."""
import os, sys


def run_agent(api_base, model, args):
    """Run the localcoder agent with the given config."""
    os.environ["GEMMA_API_BASE"] = api_base
    os.environ["GEMMA_MODEL"] = model

    # Find the agent script — bundled with the package
    script = os.path.join(os.path.dirname(__file__), "localcoder_agent.py")
    if not os.path.exists(script):
        # Fallback
        script = os.path.expanduser("~/Projects/gemma4-research/localcoder")

    if not os.path.exists(script):
        from rich.console import Console
        Console().print("[red]Agent script not found.[/]")
        return

    cmd = [sys.executable, script]
    if args.prompt:
        cmd += ["-p", args.prompt]
    if args.cont:
        cmd += ["-c"]
    if args.model:
        cmd += ["-m", model]
    if args.yolo or args.bypass:
        cmd += ["--yolo"]
    if args.ask:
        cmd += ["--ask"]
    if args.api:
        cmd += ["--api", api_base]

    os.execvp(sys.executable, cmd)
