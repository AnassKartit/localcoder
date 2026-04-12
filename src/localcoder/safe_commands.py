"""Safe command allowlist for auto-approving read-only bash commands.

Parses bash commands with shlex and checks against an allowlist of
clearly read-only commands. Blocks auto-approval for anything with
pipes, redirects, subshells, semicolons, or unknown commands.

Inspired by kon's permission system.
"""

import shlex
import re

# Commands that are always safe (read-only, no side effects)
SAFE_COMMANDS = frozenset({
    # File inspection
    "cat", "head", "tail", "less", "more", "wc", "file", "stat",
    "md5sum", "sha256sum", "shasum",
    # Directory listing
    "ls", "tree", "du", "df",
    # Search
    "find", "grep", "rg", "ag", "fd", "fzf", "which", "where",
    "whereis", "type", "command",
    # Text processing (read-only)
    "sort", "uniq", "cut", "tr", "awk", "sed",  # sed is read-only when not -i
    "diff", "comm", "join", "paste", "column",
    # System info
    "pwd", "whoami", "hostname", "uname", "arch", "sw_vers",
    "date", "cal", "uptime", "id", "env", "printenv",
    "sysctl", "ioreg", "system_profiler",
    # Process info
    "ps", "top", "htop", "lsof", "pgrep",
    # Network info (read-only)
    "ping", "host", "dig", "nslookup", "ifconfig", "networksetup",
    # Dev tools (read-only)
    "git", "node", "python3", "python", "ruby", "go",
    "npm", "yarn", "pnpm", "pip", "cargo",
    "jq", "yq", "xargs",
    # macOS specific
    "mdfind", "mdls", "defaults", "plutil",
    "open",  # opens files/URLs, generally safe
    "pbpaste", "osascript",
    # Compilers/interpreters in check mode
    "tsc", "eslint", "prettier", "black", "ruff", "mypy", "pyright",
})

# Git subcommands that are read-only
SAFE_GIT_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "branch", "tag", "remote",
    "stash", "ls-files", "ls-tree", "rev-parse", "describe",
    "blame", "shortlog", "reflog", "config", "rev-list",
    "cat-file", "name-rev", "for-each-ref",
})

# Git subcommands that modify state
UNSAFE_GIT_SUBCOMMANDS = frozenset({
    "push", "reset", "rebase", "merge", "cherry-pick",
    "clean", "rm", "mv",
})

# npm/yarn/pip subcommands that are read-only
SAFE_PKG_SUBCOMMANDS = frozenset({
    "list", "ls", "show", "info", "view", "outdated",
    "audit", "why", "search", "help", "version",
    "run",  # npm run is generally safe (runs project scripts)
    "test", "start", "build",  # common safe scripts
})

# Commands that are always dangerous
DANGEROUS_COMMANDS = frozenset({
    "rm", "rmdir", "mkfs", "dd", "shred",
    "sudo", "su", "doas",
    "chmod", "chown", "chgrp",
    "kill", "killall", "pkill",
    "reboot", "shutdown", "halt",
    "launchctl",
    "curl",  # can exfiltrate data
    "wget",  # can exfiltrate data
})

# Shell operators that block auto-approval
DANGEROUS_OPERATORS = frozenset({
    ";", "&&", "||", "|", ">", ">>", "<", "<<",
    "&", "$(", "`",
})


def is_safe_command(command_str):
    """Check if a bash command is safe to auto-approve.

    Returns:
        (bool, str): (is_safe, reason)
    """
    if not command_str or not command_str.strip():
        return False, "empty command"

    cmd = command_str.strip()

    # Quick check for dangerous shell operators
    # But allow && for chained safe commands
    for op in (";", "|", ">", ">>", "<", "<<", "$(", "`"):
        if op in cmd:
            # Allow pipe to safe commands like grep/head/tail
            if op == "|":
                if _is_safe_pipeline(cmd):
                    continue
                return False, f"contains pipe operator"
            return False, f"contains shell operator '{op}'"

    # Handle && chains — all parts must be safe
    if "&&" in cmd:
        parts = cmd.split("&&")
        for part in parts:
            safe, reason = _check_single_command(part.strip())
            if not safe:
                return False, f"chained command not safe: {reason}"
        return True, "all chained commands are safe"

    return _check_single_command(cmd)


def _is_safe_pipeline(cmd):
    """Check if a pipeline (cmd1 | cmd2 | ...) is safe."""
    parts = cmd.split("|")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        safe, _ = _check_single_command(part)
        if not safe:
            return False
    return True


def _check_single_command(cmd):
    """Check a single command (no pipes/chains)."""
    if not cmd:
        return False, "empty"

    # Handle environment variable prefixes (KEY=val cmd ...)
    while re.match(r'^[A-Za-z_][A-Za-z0-9_]*=\S+\s+', cmd):
        cmd = re.sub(r'^[A-Za-z_][A-Za-z0-9_]*=\S+\s+', '', cmd, count=1)

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False, "unparseable command"

    if not tokens:
        return False, "empty after parsing"

    program = tokens[0]

    # Strip path prefix (e.g., /usr/bin/git → git)
    base = program.rsplit("/", 1)[-1] if "/" in program else program

    # Dangerous commands — always block
    if base in DANGEROUS_COMMANDS:
        return False, f"dangerous command: {base}"

    # Check if command is in safe list
    if base not in SAFE_COMMANDS:
        return False, f"unknown command: {base}"

    # Special handling for git — check subcommand
    if base == "git" and len(tokens) > 1:
        # Skip flags before subcommand (git -C /path status)
        subcmd_idx = 1
        while subcmd_idx < len(tokens) and tokens[subcmd_idx].startswith("-"):
            subcmd_idx += 1
            if subcmd_idx < len(tokens) and not tokens[subcmd_idx].startswith("-"):
                # This was a flag with a value (git -C /path)
                subcmd_idx += 1

        if subcmd_idx < len(tokens):
            subcmd = tokens[subcmd_idx]
            if subcmd in UNSAFE_GIT_SUBCOMMANDS:
                return False, f"git {subcmd} modifies state"
            if subcmd not in SAFE_GIT_SUBCOMMANDS:
                # Unknown git subcommand — be conservative
                return False, f"unknown git subcommand: {subcmd}"

    # Special handling for sed — only safe without -i
    if base == "sed":
        if "-i" in tokens or any(t.startswith("-i") for t in tokens):
            return False, "sed -i modifies files"

    # Special handling for npm/yarn/pip — check subcommand
    if base in ("npm", "yarn", "pnpm", "pip") and len(tokens) > 1:
        subcmd = tokens[1]
        if subcmd == "install" or subcmd == "i" or subcmd == "add" or subcmd == "remove" or subcmd == "uninstall":
            return False, f"{base} {subcmd} modifies packages"

    # Special handling for python/node — check for -c (inline code)
    if base in ("python", "python3", "node") and len(tokens) > 1:
        if tokens[1] in ("-c", "-e"):
            return False, f"{base} -c/-e runs arbitrary code"
        # Running a script file is okay for reading/testing
        # but could have side effects — block by default
        if not tokens[1].startswith("-"):
            return False, f"{base} runs a script (may have side effects)"

    # Special handling for open — only safe for files/URLs
    if base == "open":
        # Block open with -a (launch app) unless it's a browser
        for i, t in enumerate(tokens):
            if t == "-a" and i + 1 < len(tokens):
                app = tokens[i + 1].lower()
                if "safari" not in app and "chrome" not in app and "firefox" not in app:
                    return False, f"open -a {tokens[i+1]} launches an application"

    return True, f"safe command: {base}"


def classify_command(command_str):
    """Classify a command for permission UI.

    Returns one of: 'safe', 'write', 'dangerous', 'unknown'
    """
    safe, reason = is_safe_command(command_str)
    if safe:
        return "safe"

    cmd = command_str.strip()
    try:
        tokens = shlex.split(cmd)
        base = tokens[0].rsplit("/", 1)[-1] if tokens else ""
    except (ValueError, IndexError):
        return "unknown"

    if base in DANGEROUS_COMMANDS:
        return "dangerous"

    if base in ("git",) and len(tokens) > 1:
        if tokens[1] in UNSAFE_GIT_SUBCOMMANDS:
            return "dangerous"

    # Write operations
    if base in ("mkdir", "touch", "cp", "mv", "ln", "install"):
        return "write"
    if base in ("npm", "yarn", "pip") and len(tokens) > 1:
        if tokens[1] in ("install", "i", "add"):
            return "write"

    return "unknown"
