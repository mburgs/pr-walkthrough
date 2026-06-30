"""Minimal async JSON-RPC client over stdio for the Language Server Protocol.

Only implements the methods we actually need for cross-repo context
retrieval: `initialize`, `initialized` notification, `textDocument/didOpen`,
`textDocument/definition`, `textDocument/references`, `shutdown`, `exit`.

Single reader task drains the server's stdout, parses LSP-framed messages
(Content-Length header + JSON body), and dispatches by request id to
per-request asyncio.Futures. Notifications are dropped (we don't subscribe
to diagnostics / progress).

Designed to fail closed: a server crash or hung response surfaces as an
LSPError to the caller, who can fall back to the next retriever in the
chain rather than blocking the chunk worker forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class LSPError(RuntimeError):
    """Raised on protocol errors, timeouts, or server crashes."""


# Most LSP requests resolve in tens of milliseconds; we give them 10s to
# account for first-request warmup (pyright indexes the workspace on the
# first call). A hung server beyond this triggers fallback.
_DEFAULT_TIMEOUT = 10.0


class LSPClient:
    """Owns one subprocess + one reader task. Not thread-safe; call from
    a single event loop."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    async def spawn(
        cls,
        cmd: list[str],
        cwd: Path | None = None,
    ) -> "LSPClient":
        """Spawn the language server subprocess and start the reader."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdin is None or proc.stdout is None:
            raise LSPError(f"failed to open pipes for {cmd[0]}")
        client = cls(proc)
        client._reader_task = asyncio.create_task(
            client._read_loop(), name=f"lsp-reader-{cmd[0]}",
        )
        return client

    # ----------------------------------------------------------- framing

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        if self._closed or self._proc.stdin is None:
            raise LSPError("client closed")
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + body)
        await self._proc.stdin.drain()

    async def _read_message(self) -> dict[str, Any] | None:
        """Read one LSP-framed message. Returns None on clean EOF."""
        assert self._proc.stdout is not None
        # Read header lines until blank line
        content_length = 0
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
        if content_length <= 0:
            return None
        body = await self._proc.stdout.readexactly(content_length)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            log.warning("LSP server sent malformed JSON: %s", exc)
            return None

    async def _read_loop(self) -> None:
        """Drain stdout forever. Dispatch responses to pending futures."""
        try:
            while True:
                msg = await self._read_message()
                if msg is None:
                    break
                # Server -> client requests (rare for our use case) are
                # ignored. Notifications carry no id. Responses carry an
                # id matching one of our outgoing requests.
                if "id" in msg and ("result" in msg or "error" in msg):
                    rid = msg["id"]
                    fut = self._pending.pop(rid, None)
                    if fut is None or fut.done():
                        continue
                    if "error" in msg:
                        fut.set_exception(LSPError(str(msg["error"])))
                    else:
                        fut.set_result(msg["result"])
        except (asyncio.IncompleteReadError, BrokenPipeError):
            pass
        except Exception:
            log.exception("LSP reader crashed")
        finally:
            # Fail any in-flight requests so callers don't hang
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(LSPError("server reader exited"))
            self._pending.clear()

    # ----------------------------------------------------------- requests

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> Any:
        if self._closed:
            raise LSPError("client closed")
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send_raw({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        })
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise LSPError(f"{method} timed out after {timeout}s")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send_raw({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    # ----------------------------------------------------------- lifecycle

    async def initialize(self, root_uri: str, capabilities: dict[str, Any] | None = None) -> Any:
        """Send `initialize` + `initialized`. Returns the server's
        capabilities response."""
        result = await self.request("initialize", {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": capabilities or _CLIENT_CAPABILITIES,
            "workspaceFolders": [{"uri": root_uri, "name": "root"}],
        }, timeout=30.0)
        await self.notify("initialized", {})
        return result

    async def did_open(self, uri: str, language_id: str, text: str, version: int = 1) -> None:
        await self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": version,
                "text": text,
            },
        })

    async def definition(self, uri: str, line: int, character: int) -> list[dict[str, Any]]:
        """0-indexed line/character. Returns a list of Location objects."""
        result = await self.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })
        return _normalise_locations(result)

    async def references(
        self, uri: str, line: int, character: int, include_declaration: bool = False,
    ) -> list[dict[str, Any]]:
        result = await self.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration},
        })
        return _normalise_locations(result)

    async def shutdown(self) -> None:
        """Politely tell the server to wrap up."""
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.wait_for(self.request("shutdown"), timeout=2.0)
        except Exception:
            pass
        try:
            await self.notify("exit")
        except Exception:
            pass
        # Give the server a moment to exit cleanly, then kill if needed
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            self._proc.kill()
        if self._reader_task is not None:
            self._reader_task.cancel()


def _normalise_locations(result: Any) -> list[dict[str, Any]]:
    """LSP `definition`/`references` can return a single Location, a list
    of Locations, or a list of LocationLinks (newer LSP). Normalise to a
    plain list of Location dicts so callers don't case-split."""
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    if not isinstance(result, list):
        return []
    out: list[dict[str, Any]] = []
    for item in result:
        if "targetUri" in item:
            # LocationLink → flatten to Location shape
            out.append({"uri": item["targetUri"], "range": item.get("targetRange") or item["targetSelectionRange"]})
        else:
            out.append(item)
    return out


# A trimmed client-capabilities object — we don't need diagnostics,
# semantic tokens, etc. Pyright + tsserver tolerate omitted capabilities.
_CLIENT_CAPABILITIES: dict[str, Any] = {
    "textDocument": {
        "definition": {"linkSupport": True},
        "references": {},
        "synchronization": {"didSave": False, "willSave": False},
    },
    "workspace": {
        "workspaceFolders": True,
    },
}
