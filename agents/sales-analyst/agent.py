"""Agente "Analista Vendite" di Alpina Forniture S.p.A. (LangGraph ReAct).

* LLM: SEMPRE tramite l'AI Gateway di WSO2 Agent Manager. URL e chiave sono
  iniettati dalla piattaforma (LLM_PROVIDER_URL / LLM_PROVIDER_KEY) quando si
  associa un "LLM Service Provider" all'agente. Cambiare provider (OpenAI ->
  Ollama -> ...) dalla console NON richiede modifiche al codice: il modello
  viene scelto interrogando il gateway (/models).
* Dati: tool MCP dell'ERP, scoperti a runtime tramite l'MCP Proxy del gateway
  (ERP_MCP_URL). L'autorizzazione per-tool dipende dai ruoli AgentID.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from openai import OpenAI

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

    def resolve_model(self) -> str:
        """Usa LLM_MODEL se il provider lo offre, altrimenti il primo modello disponibile."""
        wanted = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        preferred_local = os.environ.get("LOCAL_MODEL", "qwen2.5:7b")
        url, key, header = self._llm_settings()
        try:
            client = OpenAI(base_url=url, api_key="unused", default_headers={header: key, "Authorization": ""}, timeout=10)
            ids = [m.id for m in client.models.list().data]
            if wanted in ids:
                return wanted
            if preferred_local in ids:
                return preferred_local
            if ids:
                log.info("Modello %s non offerto dal provider; uso %s", wanted, ids[0])
                return ids[0]
        except Exception as exc:  # noqa: BLE001
            log.info("Impossibile elencare i modelli via gateway (%s); uso %s", exc, wanted)
        return wanted

    def _build_llm(self) -> ChatOpenAI:
        url, key, header = self._llm_settings()
        self._model_name = self.resolve_model()
        log.info("LLM via AI Gateway: %s (modello %s)", url, self._model_name)
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
            "llm_gateway_url": os.environ.get("LLM_PROVIDER_URL"),
            "mcp_url": os.environ.get("ERP_MCP_URL"),
            "agentid_client_id": os.environ.get("AMP_AGENTID_CLIENT_ID"),
            "agentid_requested_scopes": os.environ.get("AMP_AGENTID_SCOPES"),
            "agentid_granted_scopes": self.token_provider.last_granted_scopes if self.token_provider else None,
            "recent_denials": self.denials[-5:],
        }
