"""Start the MCP server over stdio, as a client would, and check it answers:
initialize, then tools/list must include read. Usage: install_check_mcp.py PATH_TO_frankensurf-mcp"""
import json
import subprocess
import sys

server = subprocess.Popen([sys.argv[1]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def send(message):
    server.stdin.write(json.dumps(message) + "\n")
    server.stdin.flush()


def answer(expected_id):
    while True:
        line = server.stdout.readline()
        if not line:
            sys.exit("MCP server closed before answering")
        message = json.loads(line)
        if message.get("id") == expected_id:
            return message


send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "install-check", "version": "1"}}})
info = answer(1)["result"]
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools = [tool["name"] for tool in answer(2)["result"]["tools"]]
server.terminate()
print("server:", info.get("serverInfo"), "| instructions:", len(info.get("instructions") or ""), "chars")
print("tools:", ", ".join(sorted(tools)))
assert "read" in tools, tools
