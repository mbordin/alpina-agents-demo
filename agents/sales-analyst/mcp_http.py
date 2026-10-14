"""Client MCP minimale (Streamable HTTP, JSON-RPC 2.0) + conversione in tool LangChain.

Volutamente semplice e sincrono: rende evidenti, durante la demo, le risposte
del gateway WSO2 (401/403 quando l'agente non ha lo scope richiesto, 422 quando
interviene un guardrail) invece di nasconderle dentro un SDK asincrono.

L'autenticazione verso l'MCP Proxy dell'AI Gateway puo' essere:
  * API key  -> header (default "API-Key") con la chiave emessa dalla piattaforma
  * AgentID  -> token OAuth2 client_credentials emesso da ThunderID per QUESTO
                agente, con `resource` = URL del proxy (RFC 8707). Gli scope nel
                token dipendono dai ruoli assegnati all'agente in Agent Manager.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import time
from typing import Any, Callable, Optional

import requests
from langchain_core.tools import StructuredTool

log = logging.getLogger("mcp-http")
PROTOCOL_VERSION = "2025-06-18"


class MCPAccessDenied(Exception):
    pass


class AgentIdTokenProvider:
    """Conia token AgentID (client_credentials) per una risorsa MCP.

    Il token viene ri-coniato spesso (TTL breve) cosi' che, durante la demo,
    una modifica ai ruoli dell'agente in console abbia effetto quasi subito.
    """

    def __init__(self, resource: str, ttl_seconds: int = 30) -> None:
        self.resource = resource
        self.ttl = ttl_seconds
        self.client_id = os.environ.get("AMP_AGENTID_CLIENT_ID", "")
        self.client_secret = os.environ.get("AMP_AGENTID_CLIENT_SECRET", "")
        self.token_endpoint = os.environ.get("AMP_AGENTID_TOKEN_ENDPOINT", "")
        # Chiediamo sempre anche gli scope "extra" (es. erp:read erp:write): ThunderID
        # li filtra al momento del conio in base ai ruoli assegnati all'agente.
        requested = os.environ.get("AMP_AGENTID_SCOPES", "").split() + os.environ.get("EXTRA_SCOPES", "").split()
        self.scopes = " ".join(dict.fromkeys(s for s in requested if s))
        self._token: Optional[str] = None
        self._exp = 0.0
        self.last_granted_scopes: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.client_id and self.client_secret and self.token_endpoint)

    def token(self) -> str:
        if self._token and time.time() < self._exp:
            return self._token
        data = {"grant_type": "client_credentials", "scope": self.scopes}
        if os.environ.get("AGENTID_SEND_RESOURCE", "true").lower() == "true":
            data["resource"] = self.resource  # RFC 8707 (self-hosted 1.0); il cloud puo' non richiederlo
        verify = os.environ.get("AMP_AGENTID_VERIFY_TLS", "true").lower() == "true"
        resp = requests.post(self.token_endpoint, data=data, auth=(self.client_id, self.client_secret), timeout=15, verify=verify)
        if resp.status_code == 400 and "resource" in data:
            log.info("token endpoint rifiuta 'resource' (%s): riprovo senza", resp.text[:120])
            data.pop("resource")
            resp = requests.post(self.token_endpoint, data=data, auth=(self.client_id, self.client_secret), timeout=15, verify=verify)
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self.last_granted_scopes = data.get("scope", "")
        self._exp = time.time() + min(self.ttl, int(data.get("expires_in", 3600) * 0.75))
        log.info("AgentID token coniato (scope concessi: %s)", self.last_granted_scopes or "n/d")
        return self._token


class MCPHttpClient:
    def __init__(
        self,
        url: str,
        api_key: str = "",
        api_key_header: str = "API-Key",
        token_provider: Optional[AgentIdTokenProvider] = None,
        timeout: int = 60,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.api_key_header = api_key_header
        self.token_provider = token_provider
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._ids = itertools.count(1)
        self._initialized = False

    # -- trasporto ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.token_provider is not None and self.token_provider.ready:
            h["Authorization"] = f"Bearer {self.token_provider.token()}"
        elif self.api_key:
            h[self.api_key_header] = self.api_key
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse(resp: requests.Response) -> dict[str, Any]:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            last = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload:
                        last = json.loads(payload)
            return last or {}
        return resp.json() if resp.content else {}

    def _rpc(self, method: str, params: Optional[dict] = None, notify: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = next(self._ids)
        resp = requests.post(self.url, headers=self._headers(), json=body, timeout=self.timeout)
        if resp.headers.get("Mcp-Session-Id"):
            self.session_id = resp.headers["Mcp-Session-Id"]
        if resp.status_code in (401, 403):
            raise MCPAccessDenied(
                f"HTTP {resp.status_code} dal gateway WSO2 per '{params.get('name') if params else method}': "
                f"{resp.text[:300] or 'accesso negato'}"
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} dal gateway MCP: {resp.text[:500]}")
        if notify:
            return {}
        data = self._parse(resp)
        if "error" in data:
            err = data["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if any(k in msg.lower() for k in ("forbidden", "unauthor", "scope", "denied", "not permitted", "insufficient")):
                raise MCPAccessDenied(msg)
            raise RuntimeError(f"Errore MCP: {msg}")
        return data.get("result", {})

    def initialize(self) -> None:
        if self._initialized:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "alpina-agent", "version": "1.0"},
            },
        )
        try:
            self._rpc("notifications/initialized", notify=True)
        except Exception:  # noqa: BLE001 - alcuni server stateless rispondono 202/400: ignorabile
            pass
        self._initialized = True

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        return self._rpc("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("structuredContent") is not None:
            text = json.dumps(result["structuredContent"], ensure_ascii=False, default=str)
        else:
            text = "\n".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        if result.get("isError"):
            if any(k in text.lower() for k in ("forbidden", "scope", "denied", "unauthor")):
                raise MCPAccessDenied(text)
            return f"ERRORE dal tool {name}: {text}"
        return text[:20000]


def build_langchain_tools(client: MCPHttpClient, on_denied: Optional[Callable[[str, str], None]] = None) -> list[StructuredTool]:
    """Trasforma i tool MCP (scoperti a runtime tramite il proxy) in tool LangChain."""
    tools: list[StructuredTool] = []
    for t in client.list_tools():
        name = t["name"]
        schema = t.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema["title"] = name

        def _fn(_name: str = name, **kwargs: Any) -> str:
            args = {k: v for k, v in kwargs.items() if v is not None}
            try:
                return client.call_tool(_name, args)
            except MCPAccessDenied as exc:
                if on_denied:
                    on_denied(_name, str(exc))
                return (
                    f"ACCESSO NEGATO dal gateway WSO2 Agent Manager per il tool '{_name}'. "
                    f"Questo agente non possiede il permesso necessario. Dettaglio: {exc}"
                )

        tools.append(
            StructuredTool.from_function(
                func=_fn,
                name=name,
                description=(t.get("description") or name)[:1024],
                args_schema=schema,
            )
        )
    log.info("Tool MCP disponibili: %s", [t.name for t in tools])
    return tools
