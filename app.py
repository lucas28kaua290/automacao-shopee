"""
Shopee Affiliate Bot — app.py
Uso:
    python app.py fetch     → busca + curadoria + enfileira
    python app.py send      → envia próximo produto da fila
    python app.py run       → fetch + send (ciclo completo)
    python app.py status    → resumo da fila
"""
import hashlib
import json
import logging
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv
import os

load_dotenv()

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════

SHOPEE_APP_ID       = os.getenv("SHOPEE_APP_ID", "")
SHOPEE_SECRET       = os.getenv("SHOPEE_SECRET", "")
SHOPEE_API_URL      = "https://open-api.affiliate.shopee.com.br/graphql"

EVOLUTION_URL       = os.getenv("EVOLUTION_URL", "")
EVOLUTION_KEY       = os.getenv("EVOLUTION_KEY", "")
EVOLUTION_INSTANCE  = os.getenv("EVOLUTION_INSTANCE", "")
WHATSAPP_GROUP_ID   = os.getenv("WHATSAPP_GROUP_ID", "")

DRY_RUN             = os.getenv("DRY_RUN", "true").lower() == "true"
DB_PATH             = os.getenv("DB_PATH", "shopee.db")

# Filtros de curadoria
MIN_RATING          = 4.0
MIN_SALES           = 100
MIN_PRICE           = 29.90
MAX_PRICE           = 500.0
MIN_DISCOUNT_PCT    = 10.0
MIN_COMMISSION_PCT  = 8.0
MIN_COMMISSION_BRL  = 5.0
ANTI_REPEAT_DAYS    = 7
MIN_INTERVAL_MIN    = 20
MAX_PER_CYCLE       = 15

# ══════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("shopee_bot")

# ══════════════════════════════════════════════════════
# BANCO DE DADOS
# ══════════════════════════════════════════════════════

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS enviados (
                item_id     TEXT NOT NULL,
                nome        TEXT,
                data_envio  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS fila (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     TEXT NOT NULL,
                dados       TEXT NOT NULL,
                agendado    TIMESTAMP,
                status      TEXT DEFAULT 'pending'
            );
        """)


def ja_enviado(item_id: str) -> bool:
    cutoff = datetime.now() - timedelta(days=ANTI_REPEAT_DAYS)
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM enviados WHERE item_id=? AND data_envio>=?",
            (item_id, cutoff)
        ).fetchone() is not None


def marcar_enviado(item_id: str, nome: str):
    with db() as conn:
        conn.execute("INSERT INTO enviados (item_id, nome) VALUES (?,?)", (item_id, nome))


def enfileirar(produtos: list[dict]):
    intervalo = timedelta(minutes=MIN_INTERVAL_MIN)
    base = datetime.now()
    with db() as conn:
        for i, p in enumerate(produtos):
            conn.execute(
                "INSERT INTO fila (item_id, dados, agendado) VALUES (?,?,?)",
                (str(p["itemId"]), json.dumps(p, ensure_ascii=False), base + intervalo * i)
            )
    log.info("%d produtos adicionados à fila.", len(produtos))


def proximo_pendente() -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT id, dados FROM fila WHERE status='pending' AND agendado<=? ORDER BY agendado LIMIT 1",
            (datetime.now(),)
        ).fetchone()
    if not row:
        return None
    p = json.loads(row["dados"])
    p["_queue_id"] = row["id"]
    return p


def atualizar_fila(queue_id: int, status: str):
    with db() as conn:
        conn.execute("UPDATE fila SET status=? WHERE id=?", (status, queue_id))


def status_fila() -> dict:
    with db() as conn:
        row = conn.execute("""
            SELECT
                SUM(status='pending') AS pending,
                SUM(status='sent')    AS sent,
                SUM(status='failed')  AS failed
            FROM fila
        """).fetchone()
    return {"pending": row["pending"] or 0, "sent": row["sent"] or 0, "failed": row["failed"] or 0}

# ══════════════════════════════════════════════════════
# API SHOPEE
# ══════════════════════════════════════════════════════

def _auth_headers(payload_json: str) -> dict:
    ts = int(time.time())
    raw = f"{SHOPEE_APP_ID}{ts}{payload_json}{SHOPEE_SECRET}"
    sig = hashlib.sha256(raw.encode()).hexdigest()
    return {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={ts}, Signature={sig}",
    }


def buscar_produtos() -> list[dict]:
    query = """
    {
      productOfferV2(listType:1, sortType:2, isAMSOffer:true, limit:50) {
        nodes {
          itemId productName imageUrl offerLink
          priceMin priceDiscountRate sales ratingStar
          commissionRate commission
        }
      }
    }
    """
    payload_json = json.dumps({"query": query}, separators=(",", ":"))
    headers = _auth_headers(payload_json)

    log.info("Buscando produtos na API Shopee...")
    try:
        r = httpx.post(SHOPEE_API_URL, headers=headers, content=payload_json, timeout=30)
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.error("Erro na API Shopee: %s", e)
        return []

    if "errors" in body:
        for err in body["errors"]:
            log.error("GraphQL erro [%s]: %s",
                      err.get("extensions", {}).get("code", "?"), err.get("message"))
        return []

    nodes = body.get("data", {}).get("productOfferV2", {}).get("nodes", [])
    log.info("API retornou %d produtos.", len(nodes))
    return nodes

# ══════════════════════════════════════════════════════
# CURADORIA
# ══════════════════════════════════════════════════════

def _f(v, d=0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (ValueError, TypeError):
        return d


def _preco(v) -> float:
    raw = _f(v)
    return raw / 100_000 if raw > 10_000 else raw


def _pct(v) -> float:
    raw = _f(v)
    return raw * 100 if raw <= 1.0 else raw


def _score(p: dict) -> float:
    return (
        _pct(p.get("priceDiscountRate")) * 0.4 +
        _pct(p.get("commissionRate"))    * 0.3 +
        min(_f(p.get("sales")) / 1000, 10) * 0.2 +
        _f(p.get("ratingStar"))          * 0.1
    )


def curar(produtos: list[dict]) -> list[dict]:
    aprovados = []
    for p in produtos:
        rating   = _f(p.get("ratingStar"))
        sales    = _f(p.get("sales"))
        preco    = _preco(p.get("priceMin"))
        desconto = _pct(p.get("priceDiscountRate"))
        comis_p  = _pct(p.get("commissionRate"))
        comis_r  = _f(p.get("commission"))
        item_id  = str(p.get("itemId", ""))

        if rating < MIN_RATING:             continue
        if sales < MIN_SALES:               continue
        if not (MIN_PRICE <= preco <= MAX_PRICE): continue
        if desconto < MIN_DISCOUNT_PCT:     continue
        if comis_p < MIN_COMMISSION_PCT and comis_r < MIN_COMMISSION_BRL: continue
        if ja_enviado(item_id):             continue

        p["_score"]    = _score(p)
        p["_preco"]    = preco
        p["_desconto"] = desconto
        p["_comis"]    = comis_p
        aprovados.append(p)

    aprovados.sort(key=lambda x: x["_score"], reverse=True)
    top = aprovados[:MAX_PER_CYCLE]
    log.info("Curadoria: %d recebidos → %d aprovados → %d selecionados",
             len(produtos), len(aprovados), len(top))
    return top

# ══════════════════════════════════════════════════════
# COPY
# ══════════════════════════════════════════════════════

def _brl(v: float) -> str:
    return f"R$ {v:.2f}".replace(".", ",")


def _preco_antigo(preco: float, desc_pct: float) -> float:
    return preco / (1 - desc_pct / 100) if desc_pct < 100 else preco


def gerar_copy(p: dict) -> str:
    preco   = p["_preco"]
    desc    = p["_desconto"]
    antigo  = _preco_antigo(preco, desc)
    nome    = p.get("productName", "Produto")
    link    = p.get("offerLink", "")
    rating  = p.get("ratingStar", "")
    vendas  = int(_f(p.get("sales")))

    templates = [
        (
            f"🔥 *{int(desc)}% OFF — CORRE!*\n\n"
            f"*{nome}*\n\n"
            f"~~{_brl(antigo)}~~ ➡️ *{_brl(preco)}*\n\n"
            f"⭐ {rating} | 🛒 {vendas} vendidos\n\n"
            f"👇 Garanta agora:\n{link}"
        ),
        (
            f"💰 *Economize {_brl(antigo - preco)} nessa oferta!*\n\n"
            f"📦 {nome}\n\n"
            f"De ~~{_brl(antigo)}~~ por apenas *{_brl(preco)}*\n"
            f"🏷️ *{int(desc)}% de desconto*\n\n"
            f"⭐ {rating} | {vendas} vendidos\n\n"
            f"🔗 {link}"
        ),
        (
            f"✨ *Oferta em Destaque* ✨\n\n"
            f"*{nome}*\n\n"
            f"💥 *{int(desc)}% OFF*\n"
            f"De {_brl(antigo)} por *{_brl(preco)}*\n\n"
            f"🌟 {rating}⭐ · {vendas} vendidos\n\n"
            f"👉 {link}"
        ),
        (
            f"🎯 *Achado do dia — {int(desc)}% mais barato!*\n\n"
            f"{nome}\n\n"
            f"💲 ~~{_brl(antigo)}~~ → *{_brl(preco)}*\n\n"
            f"📊 {vendas} pessoas já compraram · {rating}⭐\n\n"
            f"🛍️ {link}"
        ),
        (
            f"⚡ *Oferta Relâmpago!*\n\n"
            f"{nome}\n\n"
            f"Por apenas *{_brl(preco)}* 👇\n"
            f"🏷️ {int(desc)}% OFF — de ~~{_brl(antigo)}~~\n\n"
            f"🛒 {vendas} já levaram · ⭐ {rating}\n\n"
            f"{link}"
        ),
        (
            f"👀 *Viu esse preço?*\n\n"
            f"*{nome}*\n\n"
            f"Tá saindo por *{_brl(preco)}* com {int(desc)}% OFF\n"
            f"~~Antes: {_brl(antigo)}~~\n\n"
            f"⭐ {rating} · {vendas} vendidos\n\n"
            f"👉 {link}"
        ),
        (
            f"🛍️ *Peguei esse pra você!*\n\n"
            f"{nome}\n\n"
            f"💸 De ~~{_brl(antigo)}~~ por *{_brl(preco)}*\n"
            f"Isso é {int(desc)}% de desconto real\n\n"
            f"📦 {vendas} pedidos · {rating}⭐\n\n"
            f"{link}"
        ),
        (
            f"💎 *Qualidade + Preço bom — achei!*\n\n"
            f"{nome}\n\n"
            f"*{_brl(preco)}* — {int(desc)}% mais barato\n"
            f"~~Era {_brl(antigo)}~~\n\n"
            f"⭐ {rating} com {vendas} vendas comprovadas\n\n"
            f"🔗 {link}"
        ),
    ]

    idx = int(hashlib.md5(str(p.get("itemId", "0")).encode()).hexdigest(), 16) % len(templates)
    return templates[idx]

# ══════════════════════════════════════════════════════
# ENVIO
# ══════════════════════════════════════════════════════

def enviar(p: dict) -> bool:
    copy = gerar_copy(p)

    if DRY_RUN:
        log.info("[DRY RUN] Item %s — %s (score %.2f)",
                 p.get("itemId"), p.get("productName", "")[:50], p.get("_score", 0))
        print("\n" + "─" * 55)
        print(f"📸 {p.get('imageUrl', '')}")
        print(copy)
        print("─" * 55 + "\n")
        return True

    url = f"{EVOLUTION_URL}/message/sendMedia/{EVOLUTION_INSTANCE}"
    payload = {
        "number": WHATSAPP_GROUP_ID,
        "mediatype": "image",
        "mimetype": "image/jpeg",
        "media": p.get("imageUrl", ""),
        "caption": copy,
    }
    try:
        r = httpx.post(url, json=payload, headers={"apikey": EVOLUTION_KEY}, timeout=30)
        r.raise_for_status()
        log.info("✅ Enviado: %s", p.get("productName", "")[:60])
        return True
    except Exception as e:
        log.error("❌ Falha ao enviar item %s: %s", p.get("itemId"), e)
        return False

# ══════════════════════════════════════════════════════
# AÇÕES
# ══════════════════════════════════════════════════════

def cmd_fetch():
    raw = buscar_produtos()
    if not raw:
        log.warning("Nenhum produto retornado.")
        return
    selecionados = curar(raw)
    if not selecionados:
        log.warning("Nenhum produto passou na curadoria. Silêncio > oferta fraca.")
        return
    enfileirar(selecionados)


def cmd_send():
    p = proximo_pendente()
    if not p:
        log.info("Fila vazia ou nada agendado para agora.")
        return
    qid = p.pop("_queue_id")
    ok = enviar(p)
    atualizar_fila(qid, "sent" if ok else "failed")
    if ok:
        marcar_enviado(str(p["itemId"]), p.get("productName", ""))


def cmd_run():
    log.info("══ Ciclo completo | Modo: %s ══", "DRY RUN 🧪" if DRY_RUN else "PRODUÇÃO 🚀")
    cmd_fetch()
    cmd_send()
    log.info("Fila: %s", status_fila())


def cmd_status():
    s = status_fila()
    print(f"\n⏳ Pendentes: {s['pending']}  ✅ Enviados: {s['sent']}  ❌ Falhas: {s['failed']}\n")


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════

CMDS = {"fetch": cmd_fetch, "send": cmd_send, "run": cmd_run, "status": cmd_status}

if __name__ == "__main__":
    if not SHOPEE_APP_ID or not SHOPEE_SECRET:
        print("Erro: SHOPEE_APP_ID e SHOPEE_SECRET precisam estar no .env")
        sys.exit(1)

    init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd not in CMDS:
        print(f"Comandos: {' | '.join(CMDS)}")
        sys.exit(1)

    CMDS[cmd]()