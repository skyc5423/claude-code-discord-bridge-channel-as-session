"""MCP (Model Context Protocol) server for Discord approval UI.

This sub-package provides the in-process MCP server that handles
Claude Code permission-prompt-tool callbacks, routing them through
Discord for user approval.

Phase A-1: skeleton — errors + stub server only.
Full broker integration (A-2) and CLI wiring (A-3) follow later.
"""

from __future__ import annotations
