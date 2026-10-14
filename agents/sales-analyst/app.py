"""FastAPI: contratto "Chat Agent" di WSO2 Agent Manager.

POST /chat  {message, session_id, context} -> {response, session_id}
GET  /health, GET /whoami (diagnostica per la demo)
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from agent import AnalystRuntime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sales-analyst")

RUNTIME = AnalystRuntime()
app = FastAPI(title="Alpina - Analista Vendite AI", version="1.0.0")


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None


class ChatResponse(BaseModel):
    response: str
    session_id: Optional[str] = None


def _final_text(result: dict[str, Any]) -> str:
    for m in reversed(result.get("messages", [])):
        if isinstance(m, AIMessage) and m.content:
            c = m.content
            if isinstance(c, list):
                return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
            return str(c)
    return "(nessuna risposta)"


CHAT_PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Alpina · Analista Vendite AI</title>
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
<header><b>Alpina Forniture</b> · Analista Vendite AI<small>Agente governato da WSO2 Agent Manager — ogni messaggio è tracciato, valutato e protetto dai guardrail del gateway</small></header>
<div id="log"><div class="m a">Ciao! Sono l'analista vendite: chiedimi KPI, trend, cali di fatturato o crediti scaduti.</div></div>
<div class="chips"><button data-q="Dammi i KPI degli ultimi 12 mesi">KPI</button><button data-q="Perché la categoria Ergonomia è calata nel Q3 2026?">Calo Ergonomia</button><button data-q="Quali clienti hanno crediti scaduti da più di 90 giorni?">Crediti scaduti</button></div>
<form id="f"><input id="q" placeholder="Fai una domanda sui dati…" autocomplete="off" maxlength="500"><button>Invia</button></form>
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
def health() -> dict[str, Any]:
    return {"status": "ok", "agent": "sales-analyst"}


@app.get("/whoami")
def whoami() -> dict[str, Any]:
    return RUNTIME.info()


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    sid = req.session_id or str(uuid.uuid4())
    try:
        agent = RUNTIME.agent()
    except Exception as exc:  # noqa: BLE001
        log.exception("inizializzazione agente fallita")
        RUNTIME.reset()
        raise HTTPException(status_code=503, detail=f"Agente non pronto: {exc}") from exc
    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=req.message)]},
            config={"configurable": {"thread_id": sid}, "recursion_limit": 30},
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("invocazione agente fallita")
        detail = str(exc)
        if "GUARDRAIL" in detail.upper() or "422" in detail:
            return ChatResponse(
                response=f"Richiesta bloccata da un guardrail dell'AI Gateway WSO2: {detail[:400]}",
                session_id=sid,
            )
        RUNTIME.reset()  # es. provider LLM cambiato a caldo: ricostruisci al prossimo giro
        raise HTTPException(status_code=500, detail=detail[:800]) from exc
    return ChatResponse(response=_final_text(result), session_id=sid)
