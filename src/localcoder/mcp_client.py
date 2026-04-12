"""MCP (Model Context Protocol) client for localcoder.

Spawns MCP servers as subprocesses, communicates over stdio using JSON-RPC 2.0.
Discovers tools from servers and makes them callable from the agent loop.

Config file: ~/.localcoder/mcp.json
Example:
{
  "servers": {
    "localfit-image": {
      "command": "python3",
      "args": ["-m", "localfit.mcp_image"],
      "env": {"LOCALFIT_IMAGE_ENDPOINT": "http://127.0.0.1:8189"}
    }
  }
}
"""

import json
import logging
import os
import select
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("localcoder")

MCP_CONFIG_PATH = Path.home() / ".localcoder" / "mcp.json"
PROTOCOL_VERSION = "2024-11-05"

# Aggressive timeouts — MCP must not block agent startup
HANDSHAKE_TIMEOUT = 5  # seconds for full init + tools/list
READLINE_TIMEOUT = 3   # seconds per line read
TOOL_CALL_TIMEOUT = 300  # seconds for tool execution


class MCPServer:
    """A running MCP server subprocess."""

    def __init__(self, name, command, args=None, env=None, cwd=None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.process = None
        self.tools = {}
        self._id_counter = 0
        self._lock = threading.Lock()

    def start(self):
        """Spawn the server and run MCP handshake. Returns False on any failure."""
        full_env = {**os.environ, **self.env}
        cmd = [self.command] + self.args

        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                cwd=self.cwd,
            )
        except FileNotFoundError:
            logger.error(f"MCP '{self.name}': command not found: {self.command}")
            return False
        except Exception as e:
            logger.error(f"MCP '{self.name}': spawn failed: {e}")
            return False

        # Run handshake with hard timeout
        try:
            return self._handshake()
        except Exception as e:
            logger.error(f"MCP '{self.name}': handshake failed: {e}")
            self.stop()
            return False

    def _handshake(self):
        """Initialize + discover tools. Must complete within HANDSHAKE_TIMEOUT."""
        deadline = time.time() + HANDSHAKE_TIMEOUT

        # 1. Send initialize
        resp = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "localcoder", "version": "1.0"},
        }, timeout=HANDSHAKE_TIMEOUT)

        if not resp:
            logger.error(f"MCP '{self.name}': no init response (server may use incompatible transport)")
            return False

        server_name = resp.get("serverInfo", {}).get("name", self.name)
        logger.info(f"MCP '{self.name}': initialized ({server_name})")

        # 2. Send initialized notification
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3. Discover tools
        remaining = max(1, deadline - time.time())
        tools_resp = self._request("tools/list", timeout=remaining)
        if tools_resp and "tools" in tools_resp:
            for tool in tools_resp["tools"]:
                self.tools[tool["name"]] = tool
            logger.info(f"MCP '{self.name}': {len(self.tools)} tools")
        return True

    def call_tool(self, tool_name, arguments):
        """Call a tool. Returns text result."""
        if not self.process or self.process.poll() is not None:
            return f"Error: MCP server '{self.name}' not running"

        try:
            resp = self._request("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            }, timeout=TOOL_CALL_TIMEOUT)

            if resp is None:
                return f"Error: no response from MCP '{self.name}'"

            # Extract text from content parts
            parts = resp.get("content", [])
            texts = []
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif isinstance(p, dict) and p.get("type") == "image":
                    texts.append("[image returned]")
                elif isinstance(p, str):
                    texts.append(p)
            return "\n".join(texts) if texts else str(resp)

        except Exception as e:
            return f"Error: MCP tool call failed: {e}"

    def stop(self):
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

    def _next_id(self):
        self._id_counter += 1
        return self._id_counter

    def _request(self, method, params=None, timeout=5):
        """Send JSON-RPC request, read response with timeout."""
        msg = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            msg["params"] = params

        with self._lock:
            self._send(msg)
            return self._read_response(msg["id"], timeout)

    def _send(self, msg):
        if not self.process or not self.process.stdin:
            return
        try:
            line = json.dumps(msg) + "\n"
            self.process.stdin.write(line.encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _read_response(self, expected_id, timeout):
        """Read lines from stdout until we get our response or timeout."""
        if not self.process or not self.process.stdout:
            return None

        deadline = time.time() + timeout
        fd = self.process.stdout.fileno()

        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())

            # Use select() for non-blocking read with timeout
            try:
                ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            except (ValueError, OSError):
                return None

            if not ready:
                # Check if process died
                if self.process.poll() is not None:
                    logger.error(f"MCP '{self.name}': process died")
                    return None
                continue

            try:
                line = self.process.stdout.readline()
            except Exception:
                return None

            if not line:
                return None  # EOF

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                resp = json.loads(text)
            except json.JSONDecodeError:
                # Skip non-JSON lines (banners, logs)
                continue

            # Skip notifications
            if "id" not in resp:
                continue

            if resp.get("id") == expected_id:
                if "error" in resp:
                    logger.error(f"MCP error: {resp['error']}")
                    return None
                return resp.get("result")

        logger.warning(f"MCP '{self.name}': timeout after {timeout}s")
        return None


class MCPManager:
    """Manages multiple MCP servers."""

    def __init__(self):
        self.servers = {}
        self._tool_map = {}  # qualified_name -> (server_name, tool_name)

    def load_config(self, config_path=None):
        path = Path(config_path) if config_path else MCP_CONFIG_PATH
        if not path.exists():
            return

        try:
            with open(path) as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"MCP config error: {e}")
            return

        servers_config = config.get("servers", config.get("mcpServers", {}))
        for name, cfg in servers_config.items():
            command = cfg.get("command", "")
            if not command:
                continue

            server = MCPServer(
                name, command,
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
                cwd=cfg.get("cwd"),
            )
            if server.start():
                self.servers[name] = server
                for tool_name in server.tools:
                    self._tool_map[f"mcp__{name}__{tool_name}"] = (name, tool_name)
            else:
                logger.warning(f"MCP '{name}': failed to start (skipping)")
                server.stop()

    def get_tool_schemas(self):
        schemas = []
        for qname, (srv_name, tool_name) in self._tool_map.items():
            tool_def = self.servers[srv_name].tools[tool_name]
            schemas.append({
                "type": "function",
                "function": {
                    "name": qname,
                    "description": tool_def.get("description", f"MCP tool from {srv_name}"),
                    "parameters": tool_def.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
        return schemas

    def call_tool(self, qname, arguments):
        if qname not in self._tool_map:
            return f"Error: unknown MCP tool: {qname}"
        srv_name, tool_name = self._tool_map[qname]
        return self.servers[srv_name].call_tool(tool_name, arguments)

    def is_mcp_tool(self, name):
        return name in self._tool_map

    def stop_all(self):
        for s in self.servers.values():
            s.stop()
        self.servers.clear()
        self._tool_map.clear()


_manager = None


def get_mcp_manager():
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


def init_mcp(config_path=None):
    """Initialize MCP. Non-blocking — if a server fails, it's skipped."""
    manager = get_mcp_manager()
    try:
        manager.load_config(config_path)
    except Exception as e:
        logger.error(f"MCP init failed: {e}")
    return manager
