"""Agent runner for the bundled localcoder agent module."""
import os


def run_agent(api_base, model, args):
    """Run the localcoder agent with the given config."""
    from localcoder.localcoder_agent import main as agent_main

    os.environ["GEMMA_API_BASE"] = api_base
    os.environ["GEMMA_MODEL"] = model
    if getattr(args, "api_key", None):
        os.environ["LOCALCODER_API_KEY"] = args.api_key
    if getattr(args, "arabic", False):
        os.environ["LOCALCODER_UI_LANG"] = "ar"

    argv = []
    if args.prompt:
        argv += ["-p", args.prompt]
    if args.cont:
        argv += ["-c"]
    if args.model:
        argv += ["-m", model]
    if getattr(args, "arabic", False):
        argv += ["-ar"]
    if args.yolo or args.bypass:
        argv += ["--yolo"]
    if args.ask:
        argv += ["--ask"]
    if args.api:
        argv += ["--api", api_base]
    if getattr(args, "api_key", None):
        argv += ["--api-key", args.api_key]

    if getattr(args, "unrestricted", False):
        argv += ["--unrestricted"]
    if getattr(args, "compact", False):
        argv += ["--compact"]
    if getattr(args, "system", None):
        argv += ["--system", args.system]

    agent_main(argv)
