# pyRevit MCP connector

Lets AI clients (Copilot, Claude, etc.) query and drive a running Revit session through
[mcp-server-for-revit-python](https://github.com/mcp-servers-for-revit/mcp-server-for-revit-python).

```text
AI client --stdio--> main.py (MCP server) --HTTP localhost:48884--> pyRevit Routes (in Revit)
```

## One-time setup (Windows, Revit 2024+)

1. Install [uv](https://docs.astral.sh/uv/) and clone the server:
   `git clone https://github.com/mcp-servers-for-revit/mcp-server-for-revit-python %USERPROFILE%\mcp-server-for-revit-python`
2. In Revit: pyRevit > Settings > **Routes** > enable *Routes Server* (port 48884).
3. Install the server's `revit-mcp-python.extension` (pyRevit > Extensions, or add its folder
   under **Custom Extension Directories**), then reload pyRevit.
4. In Revit choose **MCP > Bridge > Connect** to pair (Disconnect revokes).

## Client config

- VS Code / Copilot: `.vscode/mcp.json` in this repo defines the `pyrevit-mcp` stdio server.
  You are prompted once for the clone path and the Routes port (`REVIT_PORT`; each running Revit
  gets its own port, 48884 for the first).
- Claude Code: `claude mcp add -s user "Revit-Connector" -- uv run --frozen mcp run <path>\main.py`

## Known issue: 401 "Bridge is disconnected or pairing is invalid"

Stock pyRevit (6.0.0 and upstream `develop`) does not copy HTTP headers onto the route `Request`, so the
extension's `Authorization` check always fails. Fix: in
`%APPDATA%\pyRevit-Master\pyrevitlib\pyrevit\routes\server\server.py`, make `_prepare_request` keep the
`Request` in a variable, call `request.add_header(key, value)` for every `self.headers.items()`, and return
it. Restart Revit (Reload is not enough). A pyRevit update overwrites the edit; reapply it.
Avoid patching `startup.py` at runtime: reloads hung while that was being tried (cause unconfirmed).

## Safety

- Model-changing requests and `execute_revit_code` show their payload for review in Revit.
- Bridge accepts loopback only; never expose the ports to a network.
- Test model-changing tools in a disposable project first.
