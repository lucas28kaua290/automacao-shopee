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
LOG_GROUP_ID        = os.getenv("LOG_GROUP_ID", "")

DRY_RUN             = os.getenv("DRY_RUN", "true").lower() == "true"
DB_PATH             = os.getenv("DB_PATH", "shopee.db")

# Filtros de curadoria
MIN_RATING          = 4.5
MIN_SALES           = 100
MIN_PRICE           = 25.00
MAX_PRICE           = 500.0
MIN_DISCOUNT_PCT    = 10.0
MIN_COMMISSION_PCT  = 8.0
MIN_COMMISSION_BRL  = 5.0
ANTI_REPEAT_DAYS    = 7
MIN_INTERVAL_MIN    = 20
MAX_PER_CYCLE       = 15
PRODUCTS_PER_SLOT   = 1

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
            CREATE TABLE IF NOT EXISTS keywords_usadas (
                keyword     TEXT NOT NULL,
                tema        TEXT NOT NULL,
                data_uso    DATE NOT NULL
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


def keyword_ja_usada_hoje(keyword: str, tema: str) -> bool:
    cutoff = (datetime.now() - timedelta(days=2)).date()
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM keywords_usadas WHERE keyword=? AND tema=? AND data_uso>=?",
            (keyword, tema, cutoff)
        ).fetchone() is not None


def marcar_keyword_usada(keyword: str, tema: str):
    hoje = datetime.now().date()
    with db() as conn:
        conn.execute(
            "INSERT INTO keywords_usadas (keyword, tema, data_uso) VALUES (?,?,?)",
            (keyword, tema, hoje)
        )


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


def _busca_api(keyword: str) -> list[dict]:
    query = f"""
    {{
      productOfferV2(listType:1, sortType:2, isAMSOffer:true, limit:20, keyword:"{keyword}") {{
        nodes {{
          itemId productName imageUrl offerLink
          priceMin priceDiscountRate sales ratingStar
          commissionRate commission
        }}
      }}
    }}
    """
    payload_json = json.dumps({"query": query}, separators=(",", ":"))
    try:
        r = httpx.post(SHOPEE_API_URL, headers=_auth_headers(payload_json), content=payload_json, timeout=30)
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

    return body.get("data", {}).get("productOfferV2", {}).get("nodes", [])


def buscar_produtos(tema: str) -> list[dict]:
    import random
    keywords = TEMAS[tema].copy()

    # Remove keywords já usadas hoje nesse tema
    disponiveis = [kw for kw in keywords if not keyword_ja_usada_hoje(kw, tema)]

    # Se todas já foram usadas hoje, reseta (recomeça do zero)
    if not disponiveis:
        log.warning("Todas as keywords do tema [%s] já usadas hoje. Resetando.", tema.upper())
        disponiveis = keywords

    kw = random.choice(disponiveis)
    marcar_keyword_usada(kw, tema)
    log.info("Buscando produtos [%s] — keyword: '%s'", tema.upper(), kw)
    resultado = _busca_api(kw)
    for item in resultado:
        item["_keyword"] = kw

    log.info("Pool total para curadoria: %d produtos.", len(resultado))
    return resultado

# ══════════════════════════════════════════════════════
# CURADORIA
# ══════════════════════════════════════════════════════

def _f(v, d=0.0) -> float:
    try:
        return float(v) if v is not None else d
    except (ValueError, TypeError):
        return d


def _preco(v) -> float:
    return _f(v)


def _pct(v) -> float:
    raw = _f(v)
    return raw * 100 if raw <= 1.0 else raw


TEMAS = {
    "manha": [
        "café solúvel", "chá termogênico", "garrafa squeeze", "agenda executiva",
        "planner 2026", "necessaire viagem", "hidratante facial", "protetor solar facial",
        "fone bluetooth", "carregador turbo", "mochila notebook", "lancheira térmica",
        "home office", "caderno espiral", "mousepad gamer", "luminária led mesa",
        "vitamina c", "whey protein", "smartwatch", "caneta gel",
    ],
    "almoco": [
        "marmita", "pote", "fitness", "academia", "whey", "creatina", "proteína",
        "tênis", "camiseta", "dry fit", "fone bluetooth", "power bank", "suporte celular",
        "caneca", "garrafa térmica", "lanche", "snack", "bolsa térmica", "relógio",
    ],
    "noite": [
        "casa", "decoração", "luminária", "difusor", "almofada", "manta", "pijama",
        "chinelo", "pantufa", "hidratante", "skincare", "sono", "relaxamento", "aroma",
        "vela", "guarda-roupa", "fone", "cama", "banho", "self care", "autocuidado",
    ],
}


def _detectar_tema() -> str:
    hora = datetime.now().hour
    if 6 <= hora < 11:
        return "manha"
    elif 11 <= hora < 16:
        return "almoco"
    else:
        return "noite"


def _theme_score(nome: str, tema: str) -> float:
    nome_lower = nome.lower()
    keywords = TEMAS.get(tema, [])
    matches = sum(1 for kw in keywords if kw in nome_lower)
    return min(matches / 3, 1.0) * 10  # normalizado 0–10


def _score_qualidade(p: dict) -> float:
    desc   = _pct(p.get("priceDiscountRate"))
    comis  = _pct(p.get("commissionRate"))
    sales  = min(_f(p.get("sales")) / 1000, 10)
    rating = _f(p.get("ratingStar"))
    return desc * 0.4 + comis * 0.3 + sales * 0.2 + rating * 0.1


def _score_final(p: dict, tema: str) -> float:
    sq = _score_qualidade(p)
    st = _theme_score(p.get("productName", ""), tema)
    # Meio-dia: peso maior em desconto e vendas (urgência)
    if tema == "almoco":
        sq_almoco = (
            _pct(p.get("priceDiscountRate")) * 0.5 +
            _pct(p.get("commissionRate"))    * 0.2 +
            min(_f(p.get("sales")) / 1000, 10) * 0.25 +
            _f(p.get("ratingStar"))          * 0.05
        )
        return sq_almoco * 0.65 + st * 0.35
    return sq * 0.65 + st * 0.35


def curar(produtos: list[dict]) -> tuple[list[dict], list[dict]]:
    tema = _detectar_tema()
    log.info("Tema do horário: %s", tema.upper())

    aprovados = []
    reprovados = []
    for p in produtos:
        rating   = _f(p.get("ratingStar"))
        sales    = _f(p.get("sales"))
        preco    = _preco(p.get("priceMin"))
        desconto = _pct(p.get("priceDiscountRate"))
        comis_p  = _pct(p.get("commissionRate"))
        comis_r  = _f(p.get("commission"))
        item_id  = str(p.get("itemId", ""))
        nome     = p.get("productName", "")

        if rating < MIN_RATING:
            log.info("REPROVADO rating %.1f — %s", rating, nome[:40])
            reprovados.append({"nome": nome, "motivo": f"rating {rating:.1f}"}); continue
        if sales < MIN_SALES:
            log.info("REPROVADO vendas %.0f — %s", sales, nome[:40])
            reprovados.append({"nome": nome, "motivo": f"vendas {int(sales)}"}); continue
        if not (MIN_PRICE <= preco <= MAX_PRICE):
            log.info("REPROVADO preço %.2f — %s", preco, nome[:40])
            reprovados.append({"nome": nome, "motivo": f"preço R${preco:.2f}"}); continue
        if desconto < MIN_DISCOUNT_PCT:
            log.info("REPROVADO desconto %.1f%% — %s", desconto, nome[:40])
            reprovados.append({"nome": nome, "motivo": f"desconto {desconto:.1f}%"}); continue
        if comis_p < MIN_COMMISSION_PCT and comis_r < MIN_COMMISSION_BRL:
            log.info("REPROVADO comissão %.1f%% / R$%.2f — %s", comis_p, comis_r, nome[:40])
            reprovados.append({"nome": nome, "motivo": f"comissão {comis_p:.1f}% / R${comis_r:.2f}"}); continue
        if ja_enviado(item_id):
            log.info("REPROVADO já enviado — %s", nome[:40])
            reprovados.append({"nome": nome, "motivo": "já enviado"}); continue

        p["_tema"]     = tema
        p["_score"]    = _score_final(p, tema)
        p["_preco"]    = preco
        p["_desconto"] = desconto
        p["_comis"]    = comis_p
        aprovados.append(p)

    aprovados.sort(key=lambda x: x["_score"], reverse=True)

    if not aprovados:
        log.warning("Nenhum produto aprovado na curadoria. Nada será enviado.")
        return [], reprovados

    selecionados = aprovados[:PRODUCTS_PER_SLOT]

    log.info("Curadoria [%s]: %d recebidos → %d aprovados → 1 selecionado (score %.2f)",
             tema.upper(), len(produtos), len(aprovados), selecionados[0]["_score"])
    return selecionados, reprovados

# ══════════════════════════════════════════════════════
# COPY
# ══════════════════════════════════════════════════════

def _brl(v: float) -> str:
    return f"R$ {v:.2f}".replace(".", ",")


def _preco_antigo(preco: float, desc_pct: float) -> float:
    return preco / (1 - desc_pct / 100) if desc_pct < 100 else preco


TEMPLATES_COPY = {
    "manha": [
        (
            f"🌅 *Bom dia com economia!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* só pra você ☀️\n\n"
            f"⭐ {'{rating}'} | 🛒 {'{vendas}'} vendidos\n\n"
            f"👇 Garanta o seu:\n{'{link}'}"
        ),
        (
            f"☀️ *Achado da manhã!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — desconto real pra começar bem 💪\n\n"
            f"⭐ {'{rating}'} · 🛒 {'{vendas}'} pedidos\n\n"
            f"🔗 {'{link}'}"
        ),
        (
            f"🚀 *Comece o dia com o pé direito!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* esperando por você 👀\n\n"
            f"📦 {'{vendas}'} pessoas já têm esse · ⭐ {'{rating}'}\n\n"
            f"👉 {'{link}'}"
        ),
        (
            f"💡 *Oferta que faz sentido!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — economia de verdade logo cedo 🌄\n\n"
            f"⭐ {'{rating}'} | 🛒 {'{vendas}'} vendidos\n\n"
            f"👇 Aproveita:\n{'{link}'}"
        ),
        (
            f"🎯 *Não passa não!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* pra você que tá ligado 🔔\n\n"
            f"⭐ {'{rating}'} · 🛒 {'{vendas}'} pedidos\n\n"
            f"👉 {'{link}'}"
        ),
        (
            f"☕ *Melhor que café da manhã!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — {'{desc}'}% a menos no seu bolso 💰\n\n"
            f"📦 {'{vendas}'} pedidos · ⭐ {'{rating}'}\n\n"
            f"🔗 {'{link}'}"
        ),
    ],
    "almoco": [
        (
            f"⚡ *Oferta que pode acabar hoje!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* só enquanto tem estoque 🔥\n\n"
            f"🛒 {'{vendas}'} já levaram · ⭐ {'{rating}'}\n\n"
            f"👇 Corre antes de acabar:\n{'{link}'}"
        ),
        (
            f"🎯 *Pausa do almoço + oferta boa!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — vale demais pra ignorar 👊\n\n"
            f"⭐ {'{rating}'} | 🛒 {'{vendas}'} vendidos\n\n"
            f"🔗 {'{link}'}"
        ),
        (
            f"🏃 *Ideal pro seu dia corrido!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* esperando no carrinho 🛒\n\n"
            f"📊 {'{vendas}'} pedidos · ⭐ {'{rating}'}\n\n"
            f"👉 {'{link}'}"
        ),
        (
            f"💥 *Desconto pesado no almoço!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — {'{desc}'}% a menos pra você ✅\n\n"
            f"⭐ {'{rating}'} · 🛒 {'{vendas}'} vendidos\n\n"
            f"👇 Garante agora:\n{'{link}'}"
        ),
        (
            f"🍽️ *Enquanto você almoça, economiza!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* só pra quem tá esperto 👀\n\n"
            f"⭐ {'{rating}'} | 🛒 {'{vendas}'} pedidos\n\n"
            f"🔗 {'{link}'}"
        ),
        (
            f"⏰ *Não deixa pra depois!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — oferta de meio-dia 🌞\n\n"
            f"📦 {'{vendas}'} levaram · ⭐ {'{rating}'}\n\n"
            f"👉 {'{link}'}"
        ),
    ],
    "noite": [
        (
            f"🌙 *Você merece esse!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* só pra você essa noite ✨\n\n"
            f"⭐ {'{rating}'} · 🛒 {'{vendas}'} pessoas amando\n\n"
            f"👇 Trata-se:\n{'{link}'}"
        ),
        (
            f"🛋️ *Hora de cuidar de você!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — desconto real pra fechar o dia bem 💜\n\n"
            f"⭐ {'{rating}'} | 🛒 {'{vendas}'} vendidos\n\n"
            f"🔗 {'{link}'}"
        ),
        (
            f"✨ *Achado da noite!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* esperando no carrinho 🛒\n\n"
            f"🌟 {'{rating}'}⭐ · {'{vendas}'} pedidos\n\n"
            f"👉 {'{link}'}"
        ),
        (
            f"🌃 *Oferta pra fechar o dia no lucro!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — {'{desc}'}% de economia pra você 💸\n\n"
            f"⭐ {'{rating}'} | 🛒 {'{vendas}'} vendidos\n\n"
            f"👇 Garanta antes de dormir:\n{'{link}'}"
        ),
        (
            f"😴 *Última chance do dia!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* só pra quem não perdeu 👀\n\n"
            f"⭐ {'{rating}'} · 🛒 {'{vendas}'} pedidos\n\n"
            f"🔗 {'{link}'}"
        ),
        (
            f"🔮 *Nem acredito nesse preço!*\n\n"
            f"*{'{nome}'}*\n\n"
            f"~~{'{antigo}'}~~ 🏷️ *{'{desc}'}% OFF*\n"
            f"*{'{preco}'}* — vale muito mais que isso 🤩\n\n"
            f"📦 {'{vendas}'} pedidos · ⭐ {'{rating}'}\n\n"
            f"👉 {'{link}'}"
        ),
    ],
}


def gerar_copy(p: dict) -> str:
    preco  = p["_preco"]
    desc   = p["_desconto"]
    antigo = _preco_antigo(preco, desc)
    tema   = p.get("_tema", _detectar_tema())

    valores = {
        "nome":   p.get("productName", "Produto"),
        "link":   p.get("offerLink", ""),
        "rating": p.get("ratingStar", ""),
        "vendas": str(int(_f(p.get("sales")))),
        "preco":  _brl(preco),
        "antigo": _brl(antigo),
        "desc":   str(int(desc)),
    }

    templates = TEMPLATES_COPY[tema]
    idx = int(hashlib.md5(str(p.get("itemId", "0")).encode()).hexdigest(), 16) % len(templates)
    copy = templates[idx]

    for chave, valor in valores.items():
        copy = copy.replace(f"{'{' + chave + '}'}", valor)

    return copy

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

def enviar_log_whatsapp(p: dict | None, ok: bool, tema: str, keyword: str, reprovados: list[dict] = []):
    if not LOG_GROUP_ID or DRY_RUN:
        return

    linhas_reprovados = ""
    if reprovados:
        linhas = []
        for r in reprovados:
            linhas.append(f"  • {r['nome'][:35]} → {r['motivo']}")
        linhas_reprovados = "\n\n*❌ Reprovados:*\n" + "\n".join(linhas)

    if ok and p:
        texto = (
            f"🤖 *Shopee Bot — Ciclo concluído*\n\n"
            f"✅ *Enviado com sucesso*\n"
            f"📦 {p.get('productName', '')[:60]}\n"
            f"💰 R$ {p.get('_preco', 0):.2f} | {int(p.get('_desconto', 0))}% OFF\n"
            f"⭐ {p.get('ratingStar', '')} | 🛒 {int(_f(p.get('sales', 0)))} vendidos\n"
            f"🏷️ Keyword: `{keyword}`\n"
            f"🎨 Tema: {tema.upper()}\n"
            f"📊 Score: {p.get('_score', 0):.2f}"
            f"{linhas_reprovados}"
        )
    else:
        texto = (
            f"🤖 *Shopee Bot — Ciclo concluído*\n\n"
            f"❌ *Falha ou sem produto aprovado*\n"
            f"🏷️ Keyword: `{keyword}`\n"
            f"🎨 Tema: {tema.upper()}"
            f"{linhas_reprovados}"
        )

    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    payload = {"number": LOG_GROUP_ID, "text": texto}
    try:
        httpx.post(url, json=payload, headers={"apikey": EVOLUTION_KEY}, timeout=15)
    except Exception as e:
        log.error("Falha ao enviar log WhatsApp: %s", e)


# ══════════════════════════════════════════════════════
# AÇÕES
# ══════════════════════════════════════════════════════

def cmd_fetch():
    # Mantido por compatibilidade, mas não usado no fluxo principal
    log.info("cmd_fetch chamado diretamente — use cmd_run para o ciclo completo.")


def cmd_send():
    # Mantido por compatibilidade, mas não usado no fluxo principal
    log.info("cmd_send chamado diretamente — use cmd_run para o ciclo completo.")


def cmd_run():
    log.info("══ Ciclo completo | Modo: %s ══", "DRY RUN 🧪" if DRY_RUN else "PRODUÇÃO 🚀")
    tema = _detectar_tema()
    raw = buscar_produtos(tema)
    if not raw:
        log.warning("Nenhum produto retornado da API.")
        enviar_log_whatsapp(None, False, tema, "—")
        return
    selecionados, reprovados = curar(raw)
    if not selecionados:
        log.warning("Nenhum produto passou na curadoria. Silêncio > oferta fraca.")
        enviar_log_whatsapp(None, False, tema, "—", reprovados)
        return
    p = selecionados[0]
    keyword_usada = p.get("_keyword", "—")
    ok = enviar(p)
    if ok:
        marcar_enviado(str(p["itemId"]), p.get("productName", ""))
    log.info("Status: %s", "✅ Enviado" if ok else "❌ Falha")
    enviar_log_whatsapp(p, ok, tema, keyword_usada, reprovados)


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