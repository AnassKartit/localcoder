"""Agent runner for the bundled localcoder agent module."""
import os


def run_agent(api_base, model, args):
    """Run the localcoder agent with the given config."""
    from localcoder.localcoder_agent import main as agent_main

    os.environ["GEMMA_API_BASE"] = api_base
    os.environ["GEMMA_MODEL"] = model

    argv = []
    if args.prompt:
        argv += ["-p", args.prompt]
    if args.cont:
        argv += ["-c"]
    if args.model:
        argv += ["-m", model]
    if args.yolo or args.bypass:
        argv += ["--yolo"]
    if args.ask:
        argv += ["--ask"]
    if args.api:
        argv += ["--api", api_base]

    if getattr(args, "unrestricted", False):
        argv += ["--unrestricted"]

    agent_main(argv)
