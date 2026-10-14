"""Genera il database ERP fittizio di "Alpina Forniture S.p.A." (SQLite).

Distributore B2B di forniture per ufficio, arredo ergonomico e materiale
industriale. I dati sono deterministici (seed fisso) e contengono alcune
"storie" che gli agenti possono scoprire durante la demo:

  1. Calo delle vendite della categoria "Ergonomia" in Lombardia nel Q3 2026
     (rottura di stock della sedia ErgoPlus Pro dal 1 luglio 2026).
  2. Il cliente "Officine Meccaniche Brembo-Valli S.r.l." ha fatture scadute
     da oltre 90 giorni per un importo rilevante.
  3. Crescita forte del canale "e-commerce" nel 2026 rispetto al 2025.
  4. Picco stagionale di settembre (rientro dalle ferie, back-to-office).

Tutti i nomi, le partite IVA, le email e i telefoni sono INVENTATI.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 20261015
START = date(2025, 10, 1)
END = date(2026, 9, 30)

REGIONI = {
    "Lombardia": ["Milano", "Bergamo", "Brescia", "Monza", "Varese", "Como"],
    "Veneto": ["Padova", "Verona", "Vicenza", "Treviso", "Venezia"],
    "Piemonte": ["Torino", "Novara", "Cuneo", "Alessandria"],
    "Emilia-Romagna": ["Bologna", "Modena", "Parma", "Reggio Emilia"],
    "Lazio": ["Roma", "Latina", "Frosinone"],
    "Toscana": ["Firenze", "Prato", "Pisa", "Livorno"],
    "Campania": ["Napoli", "Salerno", "Caserta"],
}
PESO_REGIONE = {
    "Lombardia": 0.30, "Veneto": 0.16, "Piemonte": 0.12, "Emilia-Romagna": 0.14,
    "Lazio": 0.12, "Toscana": 0.09, "Campania": 0.07,
}
SEGMENTI = ["Manifattura", "Servizi professionali", "Pubblica amministrazione", "Retail", "Sanità", "Logistica"]

PREFISSI = ["Officine", "Studio", "Gruppo", "Tecno", "Nuova", "Industrie", "Servizi", "Logistica", "Centro", "Farmacia"]
NOMI = ["Orobie", "Adige", "Lario", "Monviso", "Berici", "Appennino", "Tirreno", "Euganea", "Sesia", "Ticino",
        "Garda", "Dolomiti", "Arno", "Vesuvio", "Po", "Langhe", "Brenta", "Riviera", "Sila", "Etna"]
SUFFISSI = ["S.r.l.", "S.p.A.", "S.n.c.", "S.r.l.s."]
NOMI_PERSONA = ["Giulia", "Marco", "Francesca", "Luca", "Chiara", "Andrea", "Sara", "Matteo", "Elena", "Davide",
                "Laura", "Paolo", "Valentina", "Stefano", "Martina", "Alessandro", "Federica", "Simone"]
COGNOMI = ["Rossi", "Bianchi", "Colombo", "Ferrari", "Esposito", "Romano", "Ricci", "Marino", "Greco", "Bruno",
           "Gallo", "Conti", "Costa", "Fontana", "Moretti", "Barbieri", "Lombardi", "Galli"]

CATALOGO = [
    # sku, nome, categoria, prezzo_listino, costo
    ("ERG-001", "Sedia ErgoPlus Pro", "Ergonomia", 389.0, 205.0),
    ("ERG-002", "Sedia ErgoPlus Basic", "Ergonomia", 219.0, 118.0),
    ("ERG-003", "Scrivania elettrica Sit&Stand 160", "Ergonomia", 649.0, 360.0),
    ("ERG-004", "Poggiapiedi regolabile", "Ergonomia", 39.0, 14.0),
    ("ERG-005", "Braccio monitor doppio", "Ergonomia", 129.0, 52.0),
    ("ERG-006", "Lampada LED da scrivania", "Ergonomia", 69.0, 27.0),
    ("CAN-001", "Carta A4 80g (risma 500 fogli)", "Cancelleria", 4.9, 2.6),
    ("CAN-002", "Penne a sfera blu (conf. 50)", "Cancelleria", 14.5, 5.2),
    ("CAN-003", "Raccoglitori ad anelli (conf. 10)", "Cancelleria", 22.0, 9.4),
    ("CAN-004", "Post-it 76x76 (conf. 24)", "Cancelleria", 27.0, 11.0),
    ("CAN-005", "Evidenziatori assortiti (conf. 40)", "Cancelleria", 31.0, 12.5),
    ("CAN-006", "Buste a sacco (conf. 250)", "Cancelleria", 36.0, 15.0),
    ("TEC-001", "Monitor 27\" QHD", "Tecnologia", 289.0, 198.0),
    ("TEC-002", "Docking station USB-C", "Tecnologia", 179.0, 112.0),
    ("TEC-003", "Tastiera + mouse wireless", "Tecnologia", 59.0, 31.0),
    ("TEC-004", "Cuffie con microfono ANC", "Tecnologia", 149.0, 82.0),
    ("TEC-005", "Webcam Full HD", "Tecnologia", 79.0, 38.0),
    ("TEC-006", "Stampante laser multifunzione", "Tecnologia", 429.0, 301.0),
    ("TEC-007", "Toner nero alta capacità", "Tecnologia", 89.0, 41.0),
    ("IND-001", "Guanti antitaglio (conf. 12 paia)", "Sicurezza e DPI", 58.0, 24.0),
    ("IND-002", "Occhiali protettivi (conf. 10)", "Sicurezza e DPI", 45.0, 17.0),
    ("IND-003", "Scarpe antinfortunistiche S3", "Sicurezza e DPI", 92.0, 46.0),
    ("IND-004", "Kit primo soccorso aziendale", "Sicurezza e DPI", 74.0, 33.0),
    ("IND-005", "Gilet alta visibilità (conf. 10)", "Sicurezza e DPI", 49.0, 19.0),
    ("PUL-001", "Detergente multiuso 5L", "Pulizia e igiene", 18.0, 7.2),
    ("PUL-002", "Carta asciugamani (conf. 12 rotoli)", "Pulizia e igiene", 34.0, 15.0),
    ("PUL-003", "Gel igienizzante 1L (conf. 6)", "Pulizia e igiene", 29.0, 11.0),
    ("PUL-004", "Sacchi rifiuti 110L (conf. 200)", "Pulizia e igiene", 39.0, 16.0),
    ("ARR-001", "Armadio metallico 2 ante", "Arredo", 349.0, 190.0),
    ("ARR-002", "Cassettiera su ruote", "Arredo", 159.0, 79.0),
    ("ARR-003", "Tavolo riunioni 8 posti", "Arredo", 899.0, 520.0),
    ("ARR-004", "Pannello fonoassorbente", "Arredo", 119.0, 48.0),
]
CANALI = ["agenti", "e-commerce", "telefono", "gare pubbliche"]


def piva(rng: random.Random) -> str:
    return "IT" + "".join(str(rng.randint(0, 9)) for _ in range(11))


def build(db_path: Path) -> None:
    rng = random.Random(SEED)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE clienti (
            id TEXT PRIMARY KEY, ragione_sociale TEXT, partita_iva TEXT, segmento TEXT,
            regione TEXT, citta TEXT, referente TEXT, email TEXT, telefono TEXT,
            fido_eur REAL, cliente_dal TEXT
        );
        CREATE TABLE prodotti (
            sku TEXT PRIMARY KEY, nome TEXT, categoria TEXT, prezzo_listino REAL, costo REAL,
            giacenza INTEGER, stato_scorte TEXT
        );
        CREATE TABLE ordini (
            id TEXT PRIMARY KEY, data TEXT, cliente_id TEXT, canale TEXT, stato TEXT,
            totale_eur REAL, FOREIGN KEY(cliente_id) REFERENCES clienti(id)
        );
        CREATE TABLE righe_ordine (
            ordine_id TEXT, sku TEXT, quantita INTEGER, prezzo_unitario REAL, sconto_pct REAL,
            importo_eur REAL
        );
        CREATE TABLE fatture (
            id TEXT PRIMARY KEY, ordine_id TEXT, cliente_id TEXT, data_emissione TEXT,
            data_scadenza TEXT, importo_eur REAL, stato TEXT, data_incasso TEXT
        );
        CREATE TABLE sconti_commerciali (
            id INTEGER PRIMARY KEY AUTOINCREMENT, cliente_id TEXT, percentuale REAL,
            motivazione TEXT, valido_fino TEXT, creato_da TEXT, creato_il TEXT
        );
        CREATE TABLE note_credito (
            id INTEGER PRIMARY KEY AUTOINCREMENT, fattura_id TEXT, importo_eur REAL,
            motivazione TEXT, creato_da TEXT, creato_il TEXT
        );
        """
    )

    # --- Clienti -----------------------------------------------------------
    clienti = []
    regioni = list(PESO_REGIONE)
    pesi = [PESO_REGIONE[r] for r in regioni]
    usati = set()
    for i in range(1, 121):
        while True:
            nome = f"{rng.choice(PREFISSI)} {rng.choice(NOMI)} {rng.choice(SUFFISSI)}"
            if nome not in usati:
                usati.add(nome)
                break
        if i == 7:  # storia n.2: cliente con insoluti
            nome = "Officine Meccaniche Brembo-Valli S.r.l."
            regione, citta = "Lombardia", "Bergamo"
        else:
            regione = rng.choices(regioni, pesi)[0]
            citta = rng.choice(REGIONI[regione])
        ref_n, ref_c = rng.choice(NOMI_PERSONA), rng.choice(COGNOMI)
        dominio = nome.split()[1].lower().replace("-", "") + ".example.it"
        clienti.append((
            f"C{i:04d}", nome, piva(rng), rng.choice(SEGMENTI), regione, citta,
            f"{ref_n} {ref_c}", f"{ref_n.lower()}.{ref_c.lower()}@{dominio}",
            f"+39 3{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(1000, 9999)}",
            float(rng.choice([5000, 10000, 20000, 35000, 50000, 80000])),
            (date(2015, 1, 1) + timedelta(days=rng.randint(0, 3600))).isoformat(),
        ))
    cur.executemany("INSERT INTO clienti VALUES (?,?,?,?,?,?,?,?,?,?,?)", clienti)
    # Il cliente C0007 e' un grande cliente
    cur.execute("UPDATE clienti SET fido_eur = 80000, segmento='Manifattura' WHERE id='C0007'")

    # --- Prodotti ----------------------------------------------------------
    prodotti = []
    for sku, nome, cat, prezzo, costo in CATALOGO:
        giacenza = rng.randint(40, 900)
        stato = "disponibile"
        if sku == "ERG-001":  # storia n.1: rottura di stock
            giacenza, stato = 0, "esaurito - riassortimento previsto 20/10/2026"
        elif giacenza < 80:
            stato = "scorte basse"
        prodotti.append((sku, nome, cat, prezzo, costo, giacenza, stato))
    cur.executemany("INSERT INTO prodotti VALUES (?,?,?,?,?,?,?)", prodotti)

    # --- Ordini ------------------------------------------------------------
    cli_by_id = {c[0]: c for c in clienti}
    cli_ids = list(cli_by_id)
    cli_pesi = [3.0 if cid == "C0007" else rng.uniform(0.3, 2.0) for cid in cli_ids]
    ordine_n = 0
    fattura_n = 0
    oggi = END + timedelta(days=9)  # "oggi" per la demo: 9 ottobre 2026

    d = START
    while d <= END:
        if d.weekday() >= 5:  # niente ordini nel weekend
            d += timedelta(days=1)
            continue
        stagione = {8: 0.35, 9: 1.45, 12: 0.8, 1: 0.9}.get(d.month, 1.0)
        n_ordini = max(0, int(rng.gauss(14 * stagione, 3)))
        for _ in range(n_ordini):
            ordine_n += 1
            cid = rng.choices(cli_ids, cli_pesi)[0]
            cli = cli_by_id[cid]
            regione = cli[4]
            # storia n.3: e-commerce in crescita nel 2026
            w_ecom = 0.18 if d.year == 2025 else 0.36
            canale = rng.choices(CANALI, [0.45, w_ecom, 0.22, 0.10 if cli[3] == "Pubblica amministrazione" else 0.02])[0]
            oid = f"ORD-{d.strftime('%y%m')}-{ordine_n:05d}"
            righe = []
            for _ in range(rng.randint(1, 5)):
                sku, nome, cat, prezzo, costo = rng.choice(CATALOGO)
                # storia n.1: ErgoPlus Pro non vendibile da luglio 2026; in Lombardia
                # la categoria Ergonomia crolla (i clienti non trovano l'alternativa)
                if sku == "ERG-001" and d >= date(2026, 7, 1):
                    if regione == "Lombardia" or rng.random() < 0.7:
                        continue
                    sku, nome, cat, prezzo, costo = CATALOGO[1]
                if cat == "Ergonomia" and regione == "Lombardia" and d >= date(2026, 7, 1) and rng.random() < 0.65:
                    continue
                qta = rng.randint(1, 12) if prezzo > 100 else rng.randint(5, 80)
                sconto = rng.choice([0, 0, 0, 5, 10, 15]) if canale != "e-commerce" else rng.choice([0, 0, 5])
                imp = round(qta * prezzo * (1 - sconto / 100), 2)
                righe.append((oid, sku, qta, prezzo, float(sconto), imp))
            if not righe:
                ordine_n -= 1
                continue
            totale = round(sum(r[5] for r in righe), 2)
            eta = (oggi - d).days
            stato = "consegnato" if eta > 6 else rng.choice(["in preparazione", "spedito", "consegnato"])
            if rng.random() < 0.015:
                stato = "annullato"
            cur.execute("INSERT INTO ordini VALUES (?,?,?,?,?,?)", (oid, d.isoformat(), cid, canale, stato, totale))
            cur.executemany("INSERT INTO righe_ordine VALUES (?,?,?,?,?,?)", righe)
            if stato == "annullato":
                continue
            fattura_n += 1
            fid = f"FT-{d.year}-{fattura_n:05d}"
            scad = d + timedelta(days=rng.choice([30, 60, 90]))
            if cid == "C0007" and d >= date(2026, 3, 1):
                # storia n.2: il cliente ha smesso di pagare da marzo
                stato_f, incasso = ("scaduta" if scad < oggi else "da incassare"), None
            elif scad < oggi:
                if rng.random() < 0.06:
                    stato_f, incasso = "scaduta", None
                else:
                    stato_f = "pagata"
                    incasso = (scad + timedelta(days=rng.randint(-10, 20))).isoformat()
            else:
                stato_f, incasso = "da incassare", None
            cur.execute(
                "INSERT INTO fatture VALUES (?,?,?,?,?,?,?,?)",
                (fid, oid, cid, d.isoformat(), scad.isoformat(), totale, stato_f, incasso),
            )
        d += timedelta(days=1)

    cur.executescript(
        """
        CREATE INDEX ix_ordini_data ON ordini(data);
        CREATE INDEX ix_ordini_cliente ON ordini(cliente_id);
        CREATE INDEX ix_righe_ordine ON righe_ordine(ordine_id);
        CREATE INDEX ix_fatture_cliente ON fatture(cliente_id);
        CREATE VIEW v_vendite AS
          SELECT o.id AS ordine_id, o.data, substr(o.data,1,7) AS mese, o.canale, o.stato,
                 c.id AS cliente_id, c.ragione_sociale, c.regione, c.citta, c.segmento,
                 p.sku, p.nome AS prodotto, p.categoria, r.quantita, r.importo_eur,
                 round(r.quantita * p.costo, 2) AS costo_eur
          FROM ordini o JOIN clienti c ON c.id = o.cliente_id
          JOIN righe_ordine r ON r.ordine_id = o.id JOIN prodotti p ON p.sku = r.sku
          WHERE o.stato <> 'annullato';
        """
    )
    con.commit()
    n = cur.execute("SELECT count(*) FROM ordini").fetchone()[0]
    fatt = cur.execute("SELECT round(sum(importo_eur)) FROM v_vendite").fetchone()[0]
    con.close()
    print(f"Database creato: {db_path}  ordini={n}  fatturato={fatt:,.0f} EUR")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "erp.db"))
