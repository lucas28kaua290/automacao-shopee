"""
teste.py — Explora shopeeOfferV2 (campanhas flash) e tenta
           buscar produtos reais dentro dessas campanhas.

Uso:
    python teste.py
"""
import hashlib
import json
import os
import time
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

SHOPEE_APP_ID  = os.getenv("SHOPEE_APP_ID", "")
SHOPEE_SECRET  = os.getenv("SHOPEE_SECRET", "")
SHOPEE_API_URL = "https://open-api.affiliate.shopee.com.br/graphql"


# ── Auth ─────────────────────────────────────────────

def auth_headers(payload_json: str) -> dict:
    ts  = int(time.time())
    raw = f"{SHOPEE_APP_ID}{ts}{payload_json}{SHOPEE_SECRET}"
    sig = hashlib.sha256(raw.encode()).hexdigest()
    return {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={ts}, Signature={sig}",
    }


def post(query: str) -> dict:
    payload_json = json.dumps({"query": query}, separators=(",", ":"))
    try:
        r = httpx.post(SHOPEE_API_URL, headers=auth_headers(payload_json), content=payload_json, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Erro na requisição: {e}")
        return {}


# ── ETAPA 1: Buscar campanhas ativas agora ───────────

def buscar_campanhas_flash() -> list[dict]:
    query = """
    {
      shopeeOfferV2(
        sortType: 1,
        limit: 20
      ) {
        nodes {
          offerName
          offerType
          collectionId
          categoryId
          commissionRate
          imageUrl
          offerLink
          periodStartTime
          periodEndTime
        }
      }
    }
    """
    print("\n[ETAPA 1] Buscando campanhas com keyword 'relampago'...")
    body = post(query)

    if "errors" in body:
        print("Erros:", body["errors"])
        return []

    nodes = body.get("data", {}).get("shopeeOfferV2", {}).get("nodes", [])
    print(f"Total de campanhas retornadas: {len(nodes)}")

    # Filtra campanhas ativas agora
    agora = int(time.time())
    ativas = [
        n for n in nodes
        if n.get("periodStartTime", 0) <= agora <= n.get("periodEndTime", 9999999999)
    ]

    print(f"Campanhas ativas agora: {len(ativas)}")
    print("\n--- Campanhas encontradas (raw) ---")
    print(json.dumps(nodes, indent=2, ensure_ascii=False))

    return ativas


# ── ETAPA 2: Tentar buscar produtos pelo collectionId ─

def buscar_produtos_da_campanha(campanha: dict) -> list[dict]:
    collection_id = campanha.get("collectionId")
    nome          = campanha.get("offerName", "?")

    print(f"\n[ETAPA 2] Tentando buscar produtos da campanha: '{nome}'")
    print(f"  collectionId : {collection_id}")
    print(f"  offerType    : {campanha.get('offerType')}")
    print(f"  válida até   : {datetime.fromtimestamp(campanha.get('periodEndTime', 0))}")

    # Tenta 1: com collectionId direto no productOfferV2
    query_collection = f"""
    {{
      productOfferV2(
        listType: 2,
        sortType: 2,
        limit: 10
      ) {{
        nodes {{
          itemId
          productName
          priceMin
          priceDiscountRate
          commissionRate
          ratingStar
          sales
          offerLink
          imageUrl
        }}
      }}
    }}
    """

    print("\n  [Tentativa] productOfferV2 padrão (sem filtro de campanha)...")
    body = post(query_collection)

    if "errors" in body:
        print("  Erros:", body["errors"])
        return []

    nodes = body.get("data", {}).get("productOfferV2", {}).get("nodes", [])
    print(f"  Produtos retornados: {len(nodes)}")

    if nodes:
        print("\n  --- Primeiro produto (raw) ---")
        print(json.dumps(nodes[0], indent=2, ensure_ascii=False))

    return nodes


# ── MAIN ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("TESTE — Campanhas Flash + Produtos")
    print("=" * 55)

    campanhas = buscar_campanhas_flash()

    if not campanhas:
        print("\nNenhuma campanha flash ativa agora.")
        print("Tente rodar em outro horário ou mude o keyword.")
    else:
        # Testa com a primeira campanha ativa
        buscar_produtos_da_campanha(campanhas[0])

    print("\n" + "=" * 55)
    print("Teste concluído. Me manda o output completo.")
    print("=" * 55)