"""ERP MCP Server di "Alpina Forniture S.p.A." — backend dati per la demo.

Espone i dati ERP (clienti, catalogo, ordini, fatture) come tool MCP via
Streamable HTTP su /mcp. Gli agenti NON lo chiamano mai direttamente: passano
sempre dall'MCP Proxy di WSO2 Agent Manager (AI Gateway), che applica
autenticazione, autorizzazione per-tool (scope erp:read / erp:write),
access-control e rewrite.

Tool di sola lettura (scope erp:read):
  panoramica_kpi, vendite_aggregate, confronta_periodi, top_prodotti,
  catalogo_prodotti, stato_ordine, scheda_cliente, crediti_scaduti,
  esegui_query_sql
Tool di scrittura (scope erp:write):
  crea_sconto_commerciale, emetti_nota_credito
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

DB_PATH = Path(os.environ.get("ERP_DB_PATH", "/data/erp.db"))
OGGI = os.environ.get("ERP_TODAY", "2026-10-09")  # data "corrente" della demo
DEFAULT_DA, DEFAULT_A = "2025-10-01", "2026-09-30"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("erp-mcp")

_kwargs: dict[str, Any] = dict(
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8000")),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
)
try:  # mcp >= 1.13: la protezione DNS-rebinding rifiuterebbe gli Host del cluster
    from mcp.server.transport_security import TransportSecuritySettings

    _kwargs["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
except Exception:  # noqa: BLE001
    pass

mcp = FastMCP(
    "alpina-erp",
    instructions=(
        "ERP di Alpina Forniture S.p.A. (distributore B2B italiano). Importi in EUR, "
        f"date in formato YYYY-MM-DD. Dati disponibili dal {DEFAULT_DA} al {DEFAULT_A}; oggi e' il {OGGI}."
    ),
    **_kwargs,
)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _rows(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with closing(_db()) as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def _check_date(s: Optional[str], default: str) -> str:
    if not s:
        return default
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"Data non valida '{s}': usa il formato YYYY-MM-DD") from exc


def _filters(regione: Optional[str], categoria: Optional[str], canale: Optional[str]) -> tuple[str, list]:
    where, params = [], []
    if regione:
        where.append("lower(regione) = lower(?)")
        params.append(regione)
    if categoria:
        where.append("lower(categoria) = lower(?)")
        params.append(categoria)
    if canale:
        where.append("lower(canale) = lower(?)")
        params.append(canale)
    return ("".join(f" AND {w}" for w in where), params)


def _pct(a: float, b: float) -> Optional[float]:
    return None if not b else round((a - b) / b * 100, 1)


# ----------------------------------------------------------------------------
# READ tools  (scope erp:read)
# ----------------------------------------------------------------------------
@mcp.tool()
def panoramica_kpi(data_da: Optional[str] = None, data_a: Optional[str] = None) -> dict[str, Any]:
    """KPI commerciali sintetici nel periodo: fatturato, margine lordo, numero ordini,
    ticket medio, clienti attivi. Default: ultimi 12 mesi disponibili."""
    da, a = _check_date(data_da, DEFAULT_DA), _check_date(data_a, DEFAULT_A)
    r = _rows(
        """SELECT round(sum(importo_eur),2) fatturato, round(sum(importo_eur-costo_eur),2) margine,
                  count(DISTINCT ordine_id) ordini, count(DISTINCT cliente_id) clienti_attivi
           FROM v_vendite WHERE data BETWEEN ? AND ?""",
        (da, a),
    )[0]
    fatt = r["fatturato"] or 0
    r["margine_pct"] = round((r["margine"] or 0) / fatt * 100, 1) if fatt else None
    r["ticket_medio"] = round(fatt / r["ordini"], 2) if r["ordini"] else None
    return {"periodo": {"da": da, "a": a}, **r}


@mcp.tool()
def vendite_aggregate(
    raggruppa_per: Literal["mese", "regione", "categoria", "canale", "cliente", "prodotto", "segmento"] = "mese",
    data_da: Optional[str] = None,
    data_a: Optional[str] = None,
    regione: Optional[str] = None,
    categoria: Optional[str] = None,
    canale: Optional[str] = None,
    limite: int = 25,
) -> dict[str, Any]:
    """Fatturato, margine e numero ordini aggregati per una dimensione, con filtri opzionali
    per regione (es. Lombardia), categoria (es. Ergonomia) e canale (agenti, e-commerce,
    telefono, gare pubbliche). Ordinato per fatturato decrescente (per 'mese' in ordine cronologico)."""
    col = {
        "mese": "mese", "regione": "regione", "categoria": "categoria", "canale": "canale",
        "cliente": "ragione_sociale", "prodotto": "prodotto", "segmento": "segmento",
    }[raggruppa_per]
    da, a = _check_date(data_da, DEFAULT_DA), _check_date(data_a, DEFAULT_A)
    extra, params = _filters(regione, categoria, canale)
    order = "1" if raggruppa_per == "mese" else "fatturato DESC"
    righe = _rows(
        f"""SELECT {col} AS {raggruppa_per}, round(sum(importo_eur),2) fatturato,
                   round(sum(importo_eur-costo_eur),2) margine, count(DISTINCT ordine_id) ordini
            FROM v_vendite WHERE data BETWEEN ? AND ? {extra}
            GROUP BY 1 ORDER BY {order} LIMIT ?""",
        [da, a, *params, max(1, min(limite, 200))],
    )
    return {"periodo": {"da": da, "a": a}, "filtri": {"regione": regione, "categoria": categoria, "canale": canale}, "righe": righe}


@mcp.tool()
def confronta_periodi(
    dimensione: Literal["regione", "categoria", "canale", "prodotto", "cliente", "segmento"],
    periodo_a_da: str,
    periodo_a_a: str,
    periodo_b_da: str,
    periodo_b_a: str,
    regione: Optional[str] = None,
    categoria: Optional[str] = None,
    limite: int = 15,
) -> dict[str, Any]:
    """Confronta il fatturato di due periodi (A = riferimento, B = periodo da analizzare)
    lungo una dimensione e restituisce variazione assoluta e percentuale, ordinata per
    variazione piu' negativa. Utile per spiegare cali o crescite (es. Q2 vs Q3 2026)."""
    col = {"regione": "regione", "categoria": "categoria", "canale": "canale", "prodotto": "prodotto",
           "cliente": "ragione_sociale", "segmento": "segmento"}[dimensione]
    extra, params = _filters(regione, categoria, None)
    a_da, a_a = _check_date(periodo_a_da, DEFAULT_DA), _check_date(periodo_a_a, DEFAULT_A)
    b_da, b_a = _check_date(periodo_b_da, DEFAULT_DA), _check_date(periodo_b_a, DEFAULT_A)
    righe = _rows(
        f"""SELECT {col} AS {dimensione},
                   round(sum(CASE WHEN data BETWEEN ? AND ? THEN importo_eur ELSE 0 END),2) periodo_a,
                   round(sum(CASE WHEN data BETWEEN ? AND ? THEN importo_eur ELSE 0 END),2) periodo_b
            FROM v_vendite WHERE 1=1 {extra} GROUP BY 1""",
        [a_da, a_a, b_da, b_a, *params],
    )
    for r in righe:
        r["delta_eur"] = round(r["periodo_b"] - r["periodo_a"], 2)
        r["delta_pct"] = _pct(r["periodo_b"], r["periodo_a"])
    righe.sort(key=lambda r: r["delta_eur"])
    tot_a = round(sum(r["periodo_a"] for r in righe), 2)
    tot_b = round(sum(r["periodo_b"] for r in righe), 2)
    return {
        "periodo_a": [a_da, a_a], "periodo_b": [b_da, b_a],
        "totale": {"periodo_a": tot_a, "periodo_b": tot_b, "delta_pct": _pct(tot_b, tot_a)},
        "righe": righe[: max(1, min(limite, 100))],
    }


@mcp.tool()
def top_prodotti(
    data_da: Optional[str] = None,
    data_a: Optional[str] = None,
    limite: int = 10,
    regione: Optional[str] = None,
    categoria: Optional[str] = None,
) -> dict[str, Any]:
    """Classifica dei prodotti piu' venduti per fatturato nel periodo, con quantita' e margine."""
    da, a = _check_date(data_da, DEFAULT_DA), _check_date(data_a, DEFAULT_A)
    extra, params = _filters(regione, categoria, None)
    righe = _rows(
        f"""SELECT sku, prodotto, categoria, sum(quantita) quantita, round(sum(importo_eur),2) fatturato,
                   round(sum(importo_eur-costo_eur),2) margine
            FROM v_vendite WHERE data BETWEEN ? AND ? {extra}
            GROUP BY sku ORDER BY fatturato DESC LIMIT ?""",
        [da, a, *params, max(1, min(limite, 50))],
    )
    return {"periodo": {"da": da, "a": a}, "righe": righe}


@mcp.tool()
def catalogo_prodotti(categoria: Optional[str] = None) -> list[dict[str, Any]]:
    """Catalogo prodotti con prezzo di listino, giacenza e stato scorte (es. esaurito)."""
    if categoria:
        return _rows(
            "SELECT sku,nome,categoria,prezzo_listino,giacenza,stato_scorte FROM prodotti WHERE lower(categoria)=lower(?)",
            (categoria,),
        )
    return _rows("SELECT sku,nome,categoria,prezzo_listino,giacenza,stato_scorte FROM prodotti")


@mcp.tool()
def stato_ordine(ordine_id: str) -> dict[str, Any]:
    """Stato di un ordine (es. ORD-2609-03120) con righe, totale e fattura collegata."""
    o = _rows(
        """SELECT o.id, o.data, o.canale, o.stato, o.totale_eur, c.ragione_sociale cliente
           FROM ordini o JOIN clienti c ON c.id=o.cliente_id WHERE o.id = ?""",
        (ordine_id.strip(),),
    )
    if not o:
        return {"errore": f"Ordine {ordine_id} non trovato"}
    righe = _rows(
        """SELECT r.sku, p.nome, r.quantita, r.prezzo_unitario, r.sconto_pct, r.importo_eur
           FROM righe_ordine r JOIN prodotti p ON p.sku=r.sku WHERE r.ordine_id=?""",
        (ordine_id.strip(),),
    )
    fatt = _rows("SELECT id, data_scadenza, stato FROM fatture WHERE ordine_id=?", (ordine_id.strip(),))
    return {**o[0], "righe": righe, "fattura": fatt[0] if fatt else None}


@mcp.tool()
def scheda_cliente(cliente: str) -> dict[str, Any]:
    """Scheda anagrafica e commerciale di un cliente (cerca per codice, es. C0007, o per
    parte della ragione sociale). Include referente, contatti, fido, fatturato 12 mesi,
    ultimi ordini e situazione fatture."""
    q = cliente.strip()
    c = _rows("SELECT * FROM clienti WHERE id = upper(?)", (q,)) or _rows(
        "SELECT * FROM clienti WHERE ragione_sociale LIKE ? ORDER BY ragione_sociale LIMIT 5", (f"%{q}%",)
    )
    if not c:
        return {"errore": f"Nessun cliente trovato per '{cliente}'"}
    if len(c) > 1:
        return {"risultati_multipli": [{"id": x["id"], "ragione_sociale": x["ragione_sociale"], "citta": x["citta"]} for x in c]}
    cli = c[0]
    cid = cli["id"]
    kpi = _rows(
        "SELECT round(sum(importo_eur),2) fatturato_12m, count(DISTINCT ordine_id) ordini_12m FROM v_vendite WHERE cliente_id=?",
        (cid,),
    )[0]
    fatture = _rows(
        "SELECT stato, count(*) n, round(sum(importo_eur),2) importo FROM fatture WHERE cliente_id=? GROUP BY stato",
        (cid,),
    )
    ultimi = _rows("SELECT id, data, stato, totale_eur FROM ordini WHERE cliente_id=? ORDER BY data DESC LIMIT 5", (cid,))
    sconti = _rows("SELECT percentuale, motivazione, valido_fino FROM sconti_commerciali WHERE cliente_id=?", (cid,))
    return {**cli, **kpi, "fatture": fatture, "ultimi_ordini": ultimi, "sconti_attivi": sconti}


@mcp.tool()
def crediti_scaduti(giorni_minimi: int = 0, limite: int = 10) -> dict[str, Any]:
    """Clienti con fatture scadute e non pagate, con importo scaduto e giorni di ritardo massimo
    rispetto a oggi. Filtra per ritardo minimo in giorni (es. 90)."""
    righe = _rows(
        """SELECT c.id cliente_id, c.ragione_sociale, c.regione, count(*) fatture_scadute,
                  round(sum(f.importo_eur),2) importo_scaduto,
                  CAST(max(julianday(?) - julianday(f.data_scadenza)) AS INTEGER) giorni_ritardo_max,
                  c.fido_eur
           FROM fatture f JOIN clienti c ON c.id=f.cliente_id
           WHERE f.stato='scaduta' AND julianday(?) - julianday(f.data_scadenza) >= ?
           GROUP BY c.id ORDER BY importo_scaduto DESC LIMIT ?""",
        (OGGI, OGGI, giorni_minimi, max(1, min(limite, 100))),
    )
    tot = round(sum(r["importo_scaduto"] for r in righe), 2)
    return {"oggi": OGGI, "totale_scaduto_eur": tot, "righe": righe}


_SQL_OK = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)
_SQL_BAD = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|vacuum)\b", re.IGNORECASE)


@mcp.tool()
def esegui_query_sql(sql: str) -> dict[str, Any]:
    """Esegue una query SQL di SOLA LETTURA (SELECT/WITH, SQLite) sul database ERP. Max 200 righe.
    Tabelle: clienti(id, ragione_sociale, partita_iva, segmento, regione, citta, referente, email,
    telefono, fido_eur, cliente_dal), prodotti(sku, nome, categoria, prezzo_listino, costo, giacenza,
    stato_scorte), ordini(id, data, cliente_id, canale, stato, totale_eur), righe_ordine(ordine_id, sku,
    quantita, prezzo_unitario, sconto_pct, importo_eur), fatture(id, ordine_id, cliente_id,
    data_emissione, data_scadenza, importo_eur, stato, data_incasso).
    Vista consigliata: v_vendite(ordine_id, data, mese, canale, stato, cliente_id, ragione_sociale,
    regione, citta, segmento, sku, prodotto, categoria, quantita, importo_eur, costo_eur)."""
    if not _SQL_OK.match(sql) or _SQL_BAD.search(sql) or ";" in sql.strip().rstrip(";"):
        return {"errore": "Sono ammesse solo singole query SELECT/WITH di sola lettura."}
    with closing(_db()) as con:
        con.execute("PRAGMA query_only = ON")
        cur = con.execute(sql.strip().rstrip(";"))
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(200)
    return {"colonne": cols, "righe": [list(r) for r in rows], "troncato": len(rows) == 200}


# ----------------------------------------------------------------------------
# WRITE tools  (scope erp:write)
# ----------------------------------------------------------------------------
@mcp.tool()
def crea_sconto_commerciale(cliente_id: str, percentuale: float, motivazione: str, valido_fino: str) -> dict[str, Any]:
    """[SCRITTURA] Registra uno sconto commerciale per un cliente (massimo 30%).
    Operazione con impatto economico: richiede il permesso erp:write."""
    if not 0 < percentuale <= 30:
        return {"errore": "La percentuale deve essere compresa tra 0 e 30."}
    vf = _check_date(valido_fino, "2026-12-31")
    with closing(_db()) as con:
        if not con.execute("SELECT 1 FROM clienti WHERE id=upper(?)", (cliente_id,)).fetchone():
            return {"errore": f"Cliente {cliente_id} inesistente"}
        cur = con.execute(
            "INSERT INTO sconti_commerciali (cliente_id, percentuale, motivazione, valido_fino, creato_da, creato_il) VALUES (upper(?),?,?,?,?,?)",
            (cliente_id, percentuale, motivazione, vf, "agente-ai via MCP", datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        log.warning("SCRITTURA: sconto %s%% per %s (%s)", percentuale, cliente_id, motivazione)
        return {"esito": "registrato", "id_sconto": cur.lastrowid, "cliente_id": cliente_id.upper(), "percentuale": percentuale, "valido_fino": vf}


@mcp.tool()
def emetti_nota_credito(fattura_id: str, importo_eur: float, motivazione: str) -> dict[str, Any]:
    """[SCRITTURA] Emette una nota di credito su una fattura esistente (importo non superiore alla fattura).
    Operazione con impatto contabile: richiede il permesso erp:write."""
    with closing(_db()) as con:
        f = con.execute("SELECT importo_eur FROM fatture WHERE id=?", (fattura_id,)).fetchone()
        if not f:
            return {"errore": f"Fattura {fattura_id} inesistente"}
        if not 0 < importo_eur <= f[0]:
            return {"errore": f"Importo non valido: deve essere tra 0 e {f[0]} EUR"}
        cur = con.execute(
            "INSERT INTO note_credito (fattura_id, importo_eur, motivazione, creato_da, creato_il) VALUES (?,?,?,?,?)",
            (fattura_id, importo_eur, motivazione, "agente-ai via MCP", datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        log.warning("SCRITTURA: nota di credito %s EUR su %s", importo_eur, fattura_id)
        return {"esito": "emessa", "id_nota_credito": cur.lastrowid, "fattura_id": fattura_id, "importo_eur": importo_eur}


READ_TOOLS = ["panoramica_kpi", "vendite_aggregate", "confronta_periodi", "top_prodotti", "catalogo_prodotti",
              "stato_ordine", "scheda_cliente", "crediti_scaduti", "esegui_query_sql"]
WRITE_TOOLS = ["crea_sconto_commerciale", "emetti_nota_credito"]

if __name__ == "__main__":
    if not DB_PATH.exists():
        from seed_data import build

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        build(DB_PATH)
    log.info("ERP MCP server su http://%s:%s/mcp (db=%s)", _kwargs["host"], _kwargs["port"], DB_PATH)
    mcp.run(transport="streamable-http")
