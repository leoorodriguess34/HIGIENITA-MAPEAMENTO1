#!/usr/bin/env python3
"""
Atualiza localizacoes do Tiny ERP a partir do mapa WMS salvo no Firebase.

Integracao desligada por padrao. Para qualquer chamada ao Tiny, defina:
ENABLE_TINY_WRITEBACK=true

Para gravar no Tiny, alem disso defina:
UPDATE_TINY_LOCATIONS=true
"""

import json
import os
import time
from datetime import datetime
from xml.etree import ElementTree as ET

TINY_TOKEN = os.environ.get("TINY_TOKEN", "")
FIREBASE_CRED_JSON = os.environ.get("FIREBASE_CREDENTIALS", "")
FIREBASE_DB_URL = "https://higienita-f2b22-default-rtdb.firebaseio.com"
TINY_BASE = "https://api.tiny.com.br/api2"
ENABLE_TINY_WRITEBACK = os.environ.get("ENABLE_TINY_WRITEBACK", "").lower() == "true"
APLICAR = os.environ.get("UPDATE_TINY_LOCATIONS", "false").lower() == "true"
DELAY = float(os.environ.get("TINY_UPDATE_DELAY", "2.5") or 2.5)
MAX_UPDATES = int(os.environ.get("TINY_MAX_LOCATION_UPDATES", "0") or 0)

if __name__ == "__main__" and not ENABLE_TINY_WRITEBACK:
    print("Integracao WMS -> Tiny desligada. Nenhuma chamada ao Tiny ou Firebase sera executada.")
    raise SystemExit(0)

import firebase_admin
import requests
from firebase_admin import credentials, db


def first_text(*vals):
    for val in vals:
        if val is None:
            continue
        txt = str(val).strip()
        if txt:
            return txt
    return ""


def to_float(val, default=0):
    try:
        if val in (None, ""):
            return default
        if isinstance(val, str):
            val = val.replace(".", "").replace(",", ".") if "," in val else val
        return float(val)
    except Exception:
        return default


def tiny_get(endpoint, params=None, tentativas=3):
    params = dict(params or {})
    params["token"] = TINY_TOKEN
    params["formato"] = "json"
    for tentativa in range(tentativas):
        try:
            resp = requests.get(f"{TINY_BASE}/{endpoint}", params=params, timeout=40)
            if resp.status_code == 429:
                espera = 60 * (tentativa + 1)
                print(f"  Rate limit. Aguardando {espera}s...")
                time.sleep(espera)
                continue
            resp.raise_for_status()
            retorno = resp.json().get("retorno", {})
            if retorno.get("status") == "Erro":
                print(f"  Tiny erro em {endpoint}: {retorno.get('erros')}")
                return None
            return retorno
        except Exception as exc:
            print(f"  Erro Tiny GET tentativa {tentativa + 1}: {exc}")
            time.sleep(5)
    return None


def tiny_post(endpoint, params=None, tentativas=3):
    params = dict(params or {})
    params["token"] = TINY_TOKEN
    params["formato"] = "json"
    for tentativa in range(tentativas):
        try:
            resp = requests.post(f"{TINY_BASE}/{endpoint}", data=params, timeout=60)
            if resp.status_code == 429:
                espera = 60 * (tentativa + 1)
                print(f"  Rate limit. Aguardando {espera}s...")
                time.sleep(espera)
                continue
            resp.raise_for_status()
            retorno = resp.json().get("retorno", {})
            if retorno.get("status") == "Erro":
                return {"ok": False, "erro": retorno.get("erros", retorno)}
            return {"ok": True, "retorno": retorno}
        except Exception as exc:
            print(f"  Erro Tiny POST tentativa {tentativa + 1}: {exc}")
            time.sleep(5)
    return {"ok": False, "erro": "Falha apos tentativas"}


def init_firebase():
    cred_dict = json.loads(FIREBASE_CRED_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
    print("Firebase conectado")


def normaliza_sku(sku):
    return str(sku or "").strip().upper()


def normaliza_loc(loc):
    return str(loc or "").upper().replace(" ", "").replace(";", ",")


def firebase_key(key):
    key = str(key or "SEM_SKU")
    for char in ['.', '#', '$', '[', ']', '/', ' ', '@', '!', '%', '&', '*', '+', '=', '?', '<', '>', ',', ';', ':', "'", '"']:
        key = key.replace(char, "_")
    return key.strip("_") or "SEM_SKU"


def montar_localizacao(enderecos):
    enderecos = sorted(set([normaliza_loc(e) for e in enderecos if normaliza_loc(e)]))
    texto = ",".join(enderecos)
    if len(texto) <= 50:
        return texto
    usados = []
    for addr in enderecos:
        tentativa = ",".join(usados + [addr])
        restante = len(enderecos) - len(usados) - 1
        sufixo = f"+{restante}" if restante else ""
        if len(tentativa + sufixo) > 50:
            break
        usados.append(addr)
    restante = len(enderecos) - len(usados)
    return ",".join(usados) + (f"+{restante}" if restante else "")


def carregar_localizacoes_wms(ref):
    paletes = ref.child("paletes").get() or {}
    por_sku = {}
    for addr, payload in paletes.items():
        produtos = (payload or {}).get("produtos", [])
        if isinstance(produtos, dict):
            produtos = list(produtos.values())
        for prod in produtos or []:
            sku = normaliza_sku(prod.get("sku"))
            if not sku:
                continue
            por_sku.setdefault(sku, set()).add(addr)
    return {sku: montar_localizacao(addrs) for sku, addrs in por_sku.items()}


def carregar_catalogo(ref):
    catalogo = ref.child("catalogo").get() or {}
    por_sku = {}
    for key, prod in catalogo.items():
        sku = normaliza_sku(first_text(prod.get("sku"), prod.get("codigo"), key))
        if sku:
            por_sku[sku] = prod
    return por_sku


def obter_produto_tiny(prod):
    tiny_id = first_text(prod.get("id_tiny"), prod.get("id"))
    codigo = first_text(prod.get("sku"), prod.get("codigo"))
    params = {"id": tiny_id} if tiny_id else {"codigo": codigo}
    detail = tiny_get("produto.obter.php", params)
    time.sleep(DELAY)
    if detail:
        return detail.get("produto", {}) or {}
    return {}


def payload_alterar_produto(seq, catalog_item, tiny_detail, nova_loc):
    sku = first_text(tiny_detail.get("codigo"), catalog_item.get("sku"), catalog_item.get("codigo"))
    nome = first_text(
        tiny_detail.get("nome"),
        tiny_detail.get("descricao"),
        catalog_item.get("nome"),
        catalog_item.get("desc"),
        catalog_item.get("descricao"),
        sku,
    )
    unidade = first_text(tiny_detail.get("unidade"), catalog_item.get("un"), "UN")[:3] or "UN"
    preco = to_float(first_text(tiny_detail.get("preco"), tiny_detail.get("preco_venda"), catalog_item.get("preco")), 0)
    produto = {
        "sequencia": str(seq),
        "codigo": sku,
        "nome": nome[:120],
        "unidade": unidade,
        "preco": f"{preco:.2f}",
        "origem": first_text(tiny_detail.get("origem"), catalog_item.get("origem"), "0")[:1] or "0",
        "situacao": first_text(tiny_detail.get("situacao"), catalog_item.get("situacao"), "A")[:1] or "A",
        "tipo": first_text(tiny_detail.get("tipo"), "P")[:1] or "P",
        "localizacao": nova_loc[:50],
    }
    tiny_id = first_text(tiny_detail.get("id"), catalog_item.get("id_tiny"), catalog_item.get("id"))
    if tiny_id:
        produto["id"] = tiny_id
    classe = first_text(tiny_detail.get("classe_produto"), catalog_item.get("classe_produto"), catalog_item.get("tipo_produto"))
    if classe:
        produto["classe_produto"] = classe[:1]
    gtin = first_text(tiny_detail.get("gtin"), catalog_item.get("gtin"))
    if gtin:
        produto["gtin"] = gtin[:14]
    return {"produtos": [{"produto": produto}]}


def produto_payload_xml(payload):
    root = ET.Element("produtos")
    for item in payload.get("produtos", []):
        produto_data = item.get("produto", {})
        prod_el = ET.SubElement(root, "produto")
        for chave, valor in produto_data.items():
            child = ET.SubElement(prod_el, chave)
            child.text = "" if valor is None else str(valor)
    return ET.tostring(root, encoding="unicode")


def sincronizar_localizacoes():
    print("=== WMS -> Tiny: localizacoes de produtos ===")
    print("Modo:", "GRAVAR NO TINY" if APLICAR else "SIMULACAO")
    ref = db.reference("/")
    wms_locs = carregar_localizacoes_wms(ref)
    catalogo = carregar_catalogo(ref)
    print(f"SKUs com localizacao no WMS: {len(wms_locs)}")
    print(f"Produtos no catalogo Firebase: {len(catalogo)}")

    candidatos = []
    ignorados = []
    for sku, nova_loc in sorted(wms_locs.items()):
        prod = catalogo.get(sku)
        if not prod:
            ignorados.append({"sku": sku, "motivo": "SKU sem catalogo Tiny no Firebase", "nova_localizacao": nova_loc})
            continue
        tiny_id = first_text(prod.get("id_tiny"), prod.get("id"))
        if not tiny_id:
            ignorados.append({"sku": sku, "motivo": "Produto sem id_tiny", "nova_localizacao": nova_loc})
            continue
        atual = normaliza_loc(first_text(prod.get("localizacao"), prod.get("loc"), prod.get("localizacao_estoque")))
        if atual == normaliza_loc(nova_loc):
            continue
        candidatos.append({"sku": sku, "id_tiny": tiny_id, "atual": atual, "nova": nova_loc, "catalogo": prod})

    if MAX_UPDATES:
        candidatos = candidatos[:MAX_UPDATES]

    print(f"Produtos para atualizar: {len(candidatos)}")
    print(f"Ignorados: {len(ignorados)}")
    for item in candidatos[:20]:
        print(f"  {item['sku']}: '{item['atual'] or '--'}' -> '{item['nova']}'")
    if len(candidatos) > 20:
        print(f"  ... +{len(candidatos) - 20} produtos")

    rel = {
        "ts": datetime.now().isoformat(),
        "modo": "aplicar" if APLICAR else "simulacao",
        "total_wms": len(wms_locs),
        "total_candidatos": len(candidatos),
        "total_ignorados": len(ignorados),
        "atualizados": [],
        "erros": [],
        "ignorados": ignorados[:500],
    }

    if not APLICAR:
        ref.child("tiny_location_sync_preview").set(rel | {"candidatos": [{k: v for k, v in c.items() if k != "catalogo"} for c in candidatos[:500]]})
        print("Simulacao salva em /tiny_location_sync_preview")
        return

    for i, item in enumerate(candidatos, 1):
        print(f"Atualizando {i}/{len(candidatos)} {item['sku']} -> {item['nova']}")
        detail = obter_produto_tiny(item["catalogo"])
        payload = payload_alterar_produto(i, item["catalogo"], detail, item["nova"])
        result = tiny_post("produto.alterar.php", {"produto": produto_payload_xml(payload)})
        time.sleep(DELAY)
        if result.get("ok"):
            rel["atualizados"].append({k: v for k, v in item.items() if k != "catalogo"})
            safe_key = firebase_key(item["sku"])
            ref.child("catalogo").child(safe_key).update({"loc": item["nova"], "localizacao": item["nova"], "localizacao_wms_sync": True})
        else:
            erro = {k: v for k, v in item.items() if k != "catalogo"}
            erro["erro"] = result.get("erro")
            rel["erros"].append(erro)
            print(f"  ERRO {item['sku']}: {result.get('erro')}")

    ref.child("tiny_location_sync_last").set(rel)
    print(f"Concluido: {len(rel['atualizados'])} atualizados, {len(rel['erros'])} erros")


if __name__ == "__main__":
    if not TINY_TOKEN:
        print("ERRO: TINY_TOKEN nao configurado")
        raise SystemExit(1)
    if not FIREBASE_CRED_JSON:
        print("ERRO: FIREBASE_CREDENTIALS nao configurado")
        raise SystemExit(1)
    init_firebase()
    sincronizar_localizacoes()
