"""Structured compaction for conversation history.

Instead of naive truncation (80-char summaries), uses LLM to create
structured summaries preserving: Goal, Instructions, Discoveries,
Accomplished work, and Relevant files.

Falls back to improved truncation if LLM call fails.
"""

import json
import logging
import re
import time
import urllib.request

logger = logging.getLogger("localcoder")

# Structured compaction prompt — inspired by kon
COMPACTION_PROMPT = """Summarize our conversation so far into a structured context block.
Be specific and preserve details that would be needed to continue working.

Use EXACTLY this format:

## Goal
What the user is trying to accomplish (1-2 sentences).

## Key Instructions
Specific constraints, preferences, or rules the user stated (bullet list).

## Discoveries
Important things learned during the conversation — errors found, file contents,
API responses, test results (bullet list, be specific with file paths and values).

## Accomplished
What has been completed so far (bullet list with file paths where applicable).

## Relevant Files
Files that were read, created, or modified (list paths only).

## Next Steps
What still needs to be done (bullet list).

Be concise but preserve ALL technical details (paths, error messages, values).
Do NOT include pleasantries or meta-commentary. Just the structured summary."""


def compact_with_llm(messages, api_base, model, max_retries=1):
    """Use the LLM to create a structured compaction summary.

    Args:
        messages: The conversation messages to compact.
        api_base: LLM API base URL.
        model: Model name.
        max_retries: Number of retries on failure.

    Returns:
        str: Structured summary text, or None if LLM call fails.
    """
    # Build a condensed version of the conversation for the compaction call
    conv_parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multi-part content — extract text only
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(text_parts)
        if not content:
            continue

        if role == "system":
            conv_parts.append(f"[System]: {content[:300]}")
        elif role == "user":
            conv_parts.append(f"[User]: {content[:500]}")
        elif role == "assistant":
            # Include tool calls if present
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                tools_str = ", ".join(
                    f"{tc['function']['name']}({tc['function'].get('arguments', '')[:80]})"
                    for tc in tool_calls
                )
                conv_parts.append(f"[Assistant called]: {tools_str}")
            if content:
                conv_parts.append(f"[Assistant]: {content[:500]}")
        elif role == "tool":
            conv_parts.append(f"[Tool result]: {content[:200]}")

    conversation_text = "\n".join(conv_parts)

    # Limit to ~8K chars to avoid blowing up the compaction call itself
    if len(conversation_text) > 8000:
        conversation_text = conversation_text[:4000] + "\n...[middle truncated]...\n" + conversation_text[-4000:]

    compact_messages = [
        {"role": "system", "content": COMPACTION_PROMPT},
        {"role": "user", "content": f"Here is the conversation to summarize:\n\n{conversation_text}"},
    ]

    for attempt in range(max_retries + 1):
        try:
            body = {
                "model": model,
                "messages": compact_messages,
                "temperature": 0.3,
                "max_tokens": 1024,
                "stream": False,
            }
            payload = json.dumps(body).encode()
            req = urllib.request.Request(
                f"{api_base}/chat/completions",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                summary = data["choices"][0]["message"]["content"].strip()
                if summary and len(summary) > 50:
                    logger.info(f"Compaction via LLM: {len(summary)} chars")
                    return summary
        except Exception as e:
            logger.warning(f"Compaction LLM call failed (attempt {attempt + 1}): {e}")
            if attempt < max_retries:
                time.sleep(1)

    return None


def compact_fallback(messages):
    """Improved fallback compaction without LLM.

    Extracts structured information from messages heuristically.
    Much better than the old 80-char truncation.
    """
    goal = ""
    discoveries = []
    accomplished = []
    files_seen = set()
    errors = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )

        if role == "user" and not goal:
            goal = content[:300]

        elif role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                fname = tc.get("function", {}).get("name", "")
                try:
                    args = json.loads(tc["function"].get("arguments", "{}"))
                except (json.JSONDecodeError, KeyError):
                    args = {}

                if fname in ("write_file", "edit_file"):
                    path = args.get("path", "")
                    if path:
                        files_seen.add(path)
                        accomplished.append(f"Modified {path}")
                elif fname == "read_file":
                    path = args.get("path", "")
                    if path:
                        files_seen.add(path)
                elif fname == "bash":
                    cmd = args.get("command", "")[:100]
                    if cmd:
                        accomplished.append(f"Ran: {cmd}")

        elif role == "tool":
            if content and ("error" in content.lower() or "Error" in content):
                errors.append(content[:150])
            # Extract file paths mentioned
            for match in re.findall(r'(?:Written|Edited|Read):\s*(\S+)', content):
                files_seen.add(match)

    parts = ["## Conversation Summary (auto-compacted)\n"]

    if goal:
        parts.append(f"## Goal\n{goal}\n")

    if accomplished:
        parts.append("## Accomplished")
        for item in accomplished[-10:]:  # Keep last 10
            parts.append(f"- {item}")
        parts.append("")

    if errors:
        parts.append("## Errors Encountered")
        for err in errors[-5:]:
            parts.append(f"- {err}")
        parts.append("")

    if files_seen:
        parts.append("## Relevant Files")
        for f in sorted(files_seen):
            parts.append(f"- {f}")
        parts.append("")

    return "\n".join(parts)


def compress_messages(messages, max_tokens=12000, api_base=None, model=None):
    """Smart message compression with structured compaction.

    Replaces the old naive truncation. Tries LLM-based compaction first,
    falls back to heuristic extraction.

    Args:
        messages: Full message list (system + conversation).
        max_tokens: Target token budget.
        api_base: LLM API base for structured compaction.
        model: Model name for compaction call.

    Returns:
        Compressed message list.
    """
    from localcoder.localcoder_agent import estimate_tokens

    total = estimate_tokens(json.dumps(messages))
    if total <= max_tokens:
        return messages

    # Separate system prompt from conversation
    system = messages[0] if messages and messages[0].get("role") == "system" else None
    conv = messages[1:] if system else messages

    if len(conv) <= 4:
        return messages  # Too few to compact

    # Keep the last few turns intact (they're most relevant)
    keep_count = min(6, len(conv) // 2)
    old = conv[:-keep_count]
    keep = conv[-keep_count:]

    # Try LLM-based compaction
    summary = None
    if api_base and model:
        all_to_compact = ([system] if system else []) + old
        summary = compact_with_llm(all_to_compact, api_base, model)

    # Fallback to heuristic compaction
    if not summary:
        all_to_compact = ([system] if system else []) + old
        summary = compact_fallback(all_to_compact)

    # Build compacted message list
    compacted = []
    if system:
        compacted.append(system)

    compacted.append({
        "role": "user",
        "content": f"[Previous conversation summary — {len(old)} messages compacted]\n\n{summary}",
    })
    compacted.append({
        "role": "assistant",
        "content": "Understood. I have the full context from our previous work. Continuing.",
    })
    compacted.extend(keep)

    # Verify we're under budget
    new_total = estimate_tokens(json.dumps(compacted))
    if new_total > max_tokens and len(compacted) > 5:
        # Emergency: drop more messages
        logger.warning(f"Post-compaction still over budget ({new_total} > {max_tokens}), trimming further")
        return compacted[:3] + compacted[-3:]

    logger.info(f"Compacted {len(messages)} → {len(compacted)} msgs ({total} → {new_total} tokens)")
    return compacted
