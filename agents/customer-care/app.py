"""Agente "Assistente Clienti" di Alpina Forniture — agente ESTERNO (gira sul Mac).

Dimostra la gestione federata: l'agente NON e' deployato da Agent Manager, ma
  * e' registrato come "Externally-Hosted Agent" (inventario centrale),
  * invia trace OpenTelemetry alla piattaforma (`amp-instrument`, zero-code),
  * chiama Claude SOLO tramite l'AI Gateway (guardrail, rate limit, chiave reale mai esposta),
  * usa i tool ERP tramite un MCP Proxy con API key e access-control.

Avvio:  amp-instrument uvicorn app:app --host 127.0.0.1 --port 8001
Esposto da nginx su https://oxy26.profesia.it/demo/assistente/ (pagina chat per il pubblico in sala).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

from mcp_http import MCPHttpClient, build_langchain_tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("customer-care")

SYSTEM_PROMPT = """Sei l'Assistente Clienti virtuale di Alpina Forniture S.p.A. Rispondi in italiano,
con tono cordiale e professionale, in modo breve. Aiuti i clienti B2B a:
- verificare lo stato di un ordine (`stato_ordine`, es. ORD-2609-03120),
- consultare catalogo, prezzi di listino e disponibilita' (`catalogo_prodotti`),
- recuperare informazioni sul proprio account (`scheda_cliente`).
Se un prodotto e' esaurito, proponi un'alternativa della stessa categoria.
Non inventare mai dati: se un tool non e' disponibile o restituisce un errore, dillo.
Oggi e' il 9 ottobre 2026."""


def _llm():
    url = os.environ["LLM_URL"].rstrip("/")
    key = os.environ["LLM_API_KEY"]
    header = os.environ.get("LLM_API_KEY_HEADER", "API-Key")
    kind = os.environ.get("LLM_KIND", "anthropic")
    model = os.environ.get("LLM_MODEL", "claude-sonnet-4-5")
    log.info("LLM %s via AI Gateway %s (modello %s)", kind, url, model)
    if kind == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model, base_url=url, api_key="unused-key-held-by-gateway",
            default_headers={header: key}, temperature=0, max_tokens=1500, timeout=120, max_retries=1,
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model, base_url=url, api_key="unused",
        default_headers={header: key, "Authorization": ""}, temperature=0, timeout=120, max_retries=1,
    )


_agent: Any = None
_memory = MemorySaver()


def get_agent() -> Any:
    global _agent
    if _agent is None:
        mcp = MCPHttpClient(
            os.environ["MCP_URL"],
            api_key=os.environ.get("MCP_API_KEY", ""),
            api_key_header=os.environ.get("MCP_API_KEY_HEADER", "X-API-Key"),
        )
        tools = build_langchain_tools(mcp)
        _agent = create_react_agent(model=_llm(), tools=tools, prompt=SYSTEM_PROMPT, checkpointer=_memory)
    return _agent


app = FastAPI(title="Alpina - Assistente Clienti AI (esterno)", version="1.0.0")


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None


class ChatResponse(BaseModel):
    response: str
    session_id: Optional[str] = None


CHAT_PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Alpina · Assistente Clienti AI</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14213d;--muted:#5c677d;--acc:#ff7300;--me:#14213d}
@media (prefers-color-scheme:dark){:root{--bg:#0d1828;--card:#15233a;--ink:#e8edf5;--muted:#a9b8ce;--me:#2e4466}}
*{box-sizing:border-box}body{margin:0;font:16px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--ink);display:flex;flex-direction:column;height:100dvh}
header{padding:14px 16px;background:var(--card);border-bottom:3px solid var(--acc)}header b{color:var(--acc)}header small{display:block;color:var(--muted)}
#log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.m{max-width:85%;padding:10px 13px;border-radius:14px;white-space:pre-wrap;word-wrap:break-word}
.u{align-self:flex-end;background:var(--me);color:#fff}.a{align-self:flex-start;background:var(--card)}.e{align-self:center;color:var(--muted);font-size:14px}
form{display:flex;gap:8px;padding:12px 16px;background:var(--card)}input{flex:1;padding:12px;border-radius:10px;border:1px solid #8884;font-size:16px;background:var(--bg);color:var(--ink)}
button{padding:0 18px;border:0;border-radius:10px;background:var(--acc);color:#fff;font-weight:600;font-size:16px}
.chips{display:flex;gap:6px;flex-wrap:wrap;padding:0 16px 8px}.chips button{background:transparent;color:var(--acc);border:1px solid var(--acc);padding:6px 10px;font-size:14px;font-weight:500}
</style></head><body>
<header><b>Alpina Forniture</b> · Assistente Clienti AI<small>Agente esterno governato da WSO2 Agent Manager — ogni messaggio è tracciato, valutato e protetto dai guardrail del gateway</small></header>
<div id="log"><div class="m a">Buongiorno! Posso aiutarla con ordini, catalogo e disponibilità dei prodotti.</div></div>
<div class="chips"><button data-q="La sedia ErgoPlus Pro è disponibile?">ErgoPlus Pro?</button><button data-q="Che monitor avete a catalogo?">Monitor</button><button data-q="A che punto è l'ordine ORD-2609-03120?">Stato ordine</button></div>
<form id="f"><input id="q" placeholder="Scriva un messaggio…" autocomplete="off" maxlength="500"><button>Invia</button></form>
<script>
const log=document.getElementById('log'),q=document.getElementById('q'),sid='web-'+Math.random().toString(36).slice(2);
function add(t,c){const d=document.createElement('div');d.className='m '+c;d.textContent=t;log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
async function send(text){if(!text)return;add(text,'u');q.value='';const w=add('…','a');
 try{const r=await fetch('chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text,session_id:sid})});
  if(r.status===429){w.className='m e';w.textContent='Troppi messaggi: il gateway applica un limite. Riprovi tra poco.';return}
  const j=await r.json();w.textContent=j.response||j.detail||('Errore '+r.status)}catch(e){w.className='m e';w.textContent='Servizio non raggiungibile'}}
document.getElementById('f').onsubmit=e=>{e.preventDefault();send(q.value.trim())};
document.querySelectorAll('.chips button').forEach(b=>b.onclick=()=>send(b.dataset.q));
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def chat_page() -> str:
    return CHAT_PAGE


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "customer-care"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    global _agent
    sid = req.session_id or str(uuid.uuid4())
    try:
        result = get_agent().invoke(
            {"messages": [HumanMessage(content=req.message)]},
            config={"configurable": {"thread_id": sid}, "recursion_limit": 25},
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        log.exception("invocazione fallita")
        if "GUARDRAIL" in msg.upper() or " 422" in msg or "status_code: 422" in msg:
            return ChatResponse(response=f"La richiesta e' stata bloccata da un guardrail dell'AI Gateway WSO2. ({msg[:300]})", session_id=sid)
        if "429" in msg:
            return ChatResponse(response="Limite di utilizzo raggiunto (rate limit dell'AI Gateway WSO2). Riprova tra poco.", session_id=sid)
        _agent = None
        raise HTTPException(status_code=500, detail=msg[:800]) from exc
    text = "(nessuna risposta)"
    for m in reversed(result.get("messages", [])):
        if isinstance(m, AIMessage) and m.content:
            c = m.content
            text = "\n".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else str(c)
            break
    return ChatResponse(response=text, session_id=sid)
