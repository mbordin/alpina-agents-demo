"""Agente "Analista Vendite" di Alpina Forniture S.p.A. (LangGraph ReAct).

* LLM: SEMPRE tramite l'AI Gateway di WSO2 Agent Manager. URL e chiave sono
  iniettati dalla piattaforma (LLM_PROVIDER_URL / LLM_PROVIDER_KEY) quando si
  associa un "LLM Service Provider" all'agente. Cambiare provider (OpenAI ->
  Anthropic Claude e ritorno) dalla console NON richiede modifiche al codice:
  il tipo di API (OpenAI o Anthropic) e il modello vengono riconosciuti da soli
  (LLM_KIND=auto, default).
* Dati: tool MCP dell'ERP, scoperti a runtime tramite l'MCP Proxy del gateway
  (ERP_MCP_URL). L'autorizzazione per-tool dipende dai ruoli AgentID.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from mcp_http import AgentIdTokenProvider, MCPHttpClient, build_langchain_tools

log = logging.getLogger("sales-analyst")

SYSTEM_PROMPT = """Sei l'Analista Vendite AI di Alpina Forniture S.p.A., distributore B2B italiano di
forniture per ufficio, arredo ergonomico, tecnologia e DPI. Oggi e' il 9 ottobre 2026.
I dati ERP coprono il periodo 1 ottobre 2025 - 30 settembre 2026 (Q4 2025 = ott-dic 2025;
Q1 2026 = gen-mar; Q2 = apr-giu; Q3 = lug-set 2026).

COME LAVORI
- Usa SEMPRE i tool ERP per ottenere i numeri: non inventare mai dati.
- Per spiegare un calo o una crescita usa `confronta_periodi`, poi scendi nel dettaglio
  (regione -> categoria -> prodotto) e controlla il `catalogo_prodotti` (scorte!).
- Per domande non coperte dai tool specifici usa `esegui_query_sql` (solo SELECT sulla vista v_vendite).
- Rispondi in italiano, in modo conciso e da consulente: prima la sintesi (2-3 righe),
  poi una tabella markdown con i numeri chiave, poi 1-3 azioni consigliate.
- Formatta gli importi in euro con separatore delle migliaia (es. 1.234.567 EUR).

AZIONI DI SCRITTURA
- Se l'utente ti chiede di registrare uno sconto o una nota di credito, prova a eseguire il
  tool corrispondente (`crea_sconto_commerciale`, `emetti_nota_credito`): e' il gateway
  WSO2 a decidere se hai il permesso. Se l'accesso viene negato, spiegalo chiaramente
  ("il mio ruolo non mi autorizza a ...") e suggerisci di chiedere l'abilitazione al
  responsabile AI della piattaforma. Non fingere mai che l'operazione sia andata a buon fine.
"""


class AnalystRuntime:
    """Costruisce l'agente in modo pigro (AgentID e proxy possono non essere pronti al boot)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agent: Any = None
        self._model_name: Optional[str] = None
        self._llm_kind: Optional[str] = None
        self.denials: list[dict[str, str]] = []
        self.memory = MemorySaver()
        self.token_provider: Optional[AgentIdTokenProvider] = None

    # -- LLM -----------------------------------------------------------------
    def _llm_settings(self) -> tuple[str, str, str]:
        url = os.environ.get("LLM_PROVIDER_URL", "").rstrip("/")
        key = os.environ.get("LLM_PROVIDER_KEY", "")
        header = os.environ.get("LLM_API_KEY_HEADER", "API-Key")
        if not url:
            raise RuntimeError(
                "LLM_PROVIDER_URL non impostata: associa un LLM Service Provider all'agente "
                "(Configure -> LLM Configurations) con nomi variabili LLM_PROVIDER_URL / LLM_PROVIDER_KEY."
            )
        return url, key, header

    def _list_models(self, url: str, key: str, header: str) -> list[str]:
        """Elenca i modelli esposti dal gateway (API OpenAI /models o Anthropic /v1/models)."""
        import requests

        headers = {header: key, "anthropic-version": "2023-06-01"}
        for path in ("/models", "/v1/models"):
            try:
                r = requests.get(url + path, headers=headers, timeout=10)
                if r.ok:
                    ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
                    if ids:
                        return ids
            except Exception as exc:  # noqa: BLE001
                log.debug("GET %s%s fallita: %s", url, path, exc)
        return []

    def resolve_llm(self) -> tuple[str, str]:
        """Restituisce (tipo, modello). tipo = 'openai' | 'anthropic'.

        Ordine: LLM_KIND esplicito -> modelli offerti dal gateway -> indizi nell'URL -> openai.
        Il modello: LLM_MODEL (OpenAI) o ANTHROPIC_MODEL (Claude) se offerti, altrimenti il primo disponibile.
        """
        url, key, header = self._llm_settings()
        kind = os.environ.get("LLM_KIND", "auto").strip().lower()
        openai_model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        claude_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        ids = self._list_models(url, key, header)
        if kind not in ("openai", "anthropic"):
            if ids:
                kind = "anthropic" if any(i.startswith("claude") for i in ids) and not any(i.startswith("gpt") for i in ids) else "openai"
            elif any(h in url.lower() for h in ("anthropic", "claude")):
                kind = "anthropic"
            else:
                kind = "openai"
        wanted = claude_model if kind == "anthropic" else openai_model
        if ids and wanted not in ids:
            family = "claude" if kind == "anthropic" else "gpt"
            pick = next((i for i in ids if i.startswith(family)), ids[0])
            log.info("Modello %s non offerto dal provider; uso %s", wanted, pick)
            wanted = pick
        return kind, wanted

    def _build_llm(self) -> BaseChatModel:
        url, key, header = self._llm_settings()
        kind, self._model_name = self.resolve_llm()
        self._llm_kind = kind
        log.info("LLM via AI Gateway: %s (API %s, modello %s)", url, kind, self._model_name)
        if kind == "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=self._model_name,
                temperature=0,
                max_tokens=2000,
                base_url=url,
                api_key="unused-key-held-by-gateway",
                default_headers={header: key},
                timeout=120,
                max_retries=1,
            )
        return ChatOpenAI(
            model=self._model_name,
            temperature=0,
            base_url=url,
            api_key="unused",
            default_headers={header: key, "Authorization": ""},
            timeout=120,
            max_retries=1,
        )

    # -- MCP -----------------------------------------------------------------
    def _build_mcp(self) -> MCPHttpClient:
        url = os.environ.get("ERP_MCP_URL", "").split(",")[0].strip()
        if not url:
            raise RuntimeError("ERP_MCP_URL non impostata: associa l'MCP proxy 'alpina-erp' all'agente (Tool Configurations).")
        api_key = os.environ.get("ERP_MCP_API_KEY", "").strip()
        provider = AgentIdTokenProvider(url)
        self.token_provider = provider
        if not api_key and not provider.ready:
            log.warning("Ne' API key ne' credenziali AgentID disponibili per l'MCP proxy (AgentID in provisioning?)")
        return MCPHttpClient(url, api_key=api_key, api_key_header=os.environ.get("ERP_MCP_API_KEY_HEADER", "API-Key"),
                             token_provider=None if api_key else provider)

    def _record_denial(self, tool: str, detail: str) -> None:
        log.warning("ACCESSO NEGATO a %s: %s", tool, detail)
        self.denials.append({"tool": tool, "detail": detail[:300]})
        self.denials[:] = self.denials[-20:]

    def agent(self) -> Any:
        with self._lock:
            if self._agent is None:
                tools = build_langchain_tools(self._build_mcp(), on_denied=self._record_denial)
                self._agent = create_react_agent(
                    model=self._build_llm(), tools=tools, prompt=SYSTEM_PROMPT, checkpointer=self.memory
                )
            return self._agent

    def reset(self) -> None:
        with self._lock:
            self._agent = None

    def info(self) -> dict[str, Any]:
        return {
            "model": self._model_name,
            "llm_api": self._llm_kind,
            "llm_gateway_url": os.environ.get("LLM_PROVIDER_URL"),
            "mcp_url": os.environ.get("ERP_MCP_URL"),
            "agentid_client_id": os.environ.get("AMP_AGENTID_CLIENT_ID"),
            "agentid_requested_scopes": os.environ.get("AMP_AGENTID_SCOPES"),
            "agentid_granted_scopes": self.token_provider.last_granted_scopes if self.token_provider else None,
            "recent_denials": self.denials[-5:],
        }
