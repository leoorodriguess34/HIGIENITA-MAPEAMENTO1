#!/usr/bin/env python3
"""
Higienita WMS - Sincronizacao Tiny ERP -> Firebase
Modo somente leitura: consulta Tiny e grava apenas no Firebase/WMS.
"""

import os
import json
import time
import datetime
import unicodedata

# ── CONFIG ──
TINY_TOKEN = os.environ.get('TINY_TOKEN', '')
FIREBASE_CRED_JSON = os.environ.get('FIREBASE_CREDENTIALS', '')
FIREBASE_DB_URL = 'https://higienita-f2b22-default-rtdb.firebaseio.com'
SYNC_MODE = os.environ.get('SYNC_MODE', 'full').lower()
ENABLE_TINY_READONLY = os.environ.get('ENABLE_TINY_READONLY', '').lower() == 'true'
TINY_PEDIDOS_DIAS = int(os.environ.get('TINY_PEDIDOS_DIAS', '30') or 30)
TINY_PEDIDOS_MAX_PAGINAS = int(os.environ.get('TINY_PEDIDOS_MAX_PAGINAS', '20') or 20)

if __name__ == '__main__' and not ENABLE_TINY_READONLY:
    print('Integracao Tiny somente leitura desligada. Nenhuma chamada ao Tiny ou Firebase sera executada.')
    exit(0)

import requests
import firebase_admin
from firebase_admin import credentials, db

# Delay entre chamadas da API Tiny (segundos)
# Tiny permite ~30 req/min, entao 2s entre chamadas = safe
DELAY_ENTRE_CHAMADAS = 2.5
PEDIDO_SITUACOES = [
    'aberto',
    'aprovado',
    'preparando envio',
    'faturado',
    'pronto envio',
    'enviado',
    'entregue',
    'nao entregue',
    'cancelado',
]

STATUS_WMS = {
    'aberto': 'pendente',
    'aprovado': 'aprovado',
    'preparando_envio': 'em_separacao',
    'faturado': 'concluido',
    'pronto_envio': 'pronto',
    'enviado': 'enviado',
    'entregue': 'entregue',
    'nao_entregue': 'nao_entregue',
    'cancelado': 'cancelado',
}

def first_text(*vals):
    for val in vals:
        if val is None:
            continue
        txt = str(val).strip()
        if txt:
            return txt
    return ''

def to_float(val):
    if val in (None, ''):
        return 0
    try:
        if isinstance(val, str):
            val = val.replace('.', '').replace(',', '.') if ',' in val else val
        return float(val or 0)
    except Exception:
        return 0

def sanitize_str(val, limit=None):
    if not isinstance(val, str):
        val = '' if val is None else str(val)
    txt = ''.join(ch for ch in val if ch in ['\n', '\t'] or ord(ch) >= 32).strip()
    return txt[:limit] if limit else txt

def normalizar_texto(txt):
    txt = sanitize_str(txt).lower()
    txt = unicodedata.normalize('NFKD', txt).encode('ascii', 'ignore').decode('ascii')
    return txt

def normalizar_situacao(situacao):
    txt = normalizar_texto(situacao).replace('-', '_').replace(' ', '_')
    while '__' in txt:
        txt = txt.replace('__', '_')
    return txt.strip('_')

def status_wms(situacao):
    return STATUS_WMS.get(normalizar_situacao(situacao), 'pendente')

def detectar_tipo_produto(prod):
    raw = first_text(prod.get('tipo_estoque'), prod.get('tipo_produto'), prod.get('tipo'), prod.get('classe_produto'), prod.get('classe'))
    raw_l = raw.lower()
    grupo = first_text(prod.get('grupo'), prod.get('categoria')).lower()
    sku = first_text(prod.get('sku'), prod.get('codigo')).lower()
    nome = first_text(prod.get('desc'), prod.get('descricao'), prod.get('nome'), prod.get('produto')).lower()
    base = f'{raw_l} {grupo}'

    if any(x in base for x in ['materia', 'matéria', 'prima']) or raw_l in ['mp', 'm']:
        return 'materia_prima', bool(raw)
    if any(x in base for x in ['kit', 'composto', 'composicao', 'composição']) or raw_l == 'k':
        return 'kits', bool(raw)
    if raw_l in ['simples', 'normal', 's', 'p'] or 'simples' in raw_l:
        return 'simples', True
    if 'kit' in sku or nome.startswith('kit ') or ' kit ' in nome:
        return 'kits', False
    if 'mp' in sku or 'materia prima' in nome or 'matéria prima' in nome:
        return 'materia_prima', False
    return 'simples', bool(raw)

def init_firebase():
    cred_dict = json.loads(FIREBASE_CRED_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})
    print('Firebase conectado')

TINY_BASE = 'https://api.tiny.com.br/api2'
TINY_READONLY_ENDPOINTS = {
    'produtos.pesquisa.php',
    'produto.obter.estoque.php',
    'pedidos.pesquisa.php',
    'pedido.obter.php',
}

def tiny_get(endpoint, params=None, tentativas=3):
    """Chamada somente leitura a API Tiny com retry em caso de rate limit"""
    if endpoint not in TINY_READONLY_ENDPOINTS:
        raise RuntimeError(f'Endpoint Tiny bloqueado no modo somente leitura: {endpoint}')
    params = dict(params or {})
    params['token'] = TINY_TOKEN
    params['formato'] = 'json'
    
    for tentativa in range(tentativas):
        try:
            r = requests.get(f'{TINY_BASE}/{endpoint}', params=params, timeout=30)
            
            # Rate limit - aguarda e tenta novamente
            if r.status_code == 429:
                espera = 60 * (tentativa + 1)
                print(f'  Rate limit atingido. Aguardando {espera}s...')
                time.sleep(espera)
                continue
            
            r.raise_for_status()
            data = r.json()
            
            retorno = data.get('retorno', {})
            if retorno.get('status') == 'Erro':
                erros = retorno.get('erros', [])
                # Verifica se e rate limit no retorno JSON
                err_str = str(erros)
                if 'Bloqueada' in err_str or 'acessos' in err_str:
                    espera = 60 * (tentativa + 1)
                    print(f'  API bloqueada. Aguardando {espera}s...')
                    time.sleep(espera)
                    continue
                return None
            
            return retorno
            
        except requests.exceptions.Timeout:
            print(f'  Timeout na tentativa {tentativa+1}')
            time.sleep(10)
        except Exception as e:
            print(f'  Erro na tentativa {tentativa+1}: {e}')
            time.sleep(5)
    
    return None

def buscar_produtos():
    """Busca todos os produtos ativos"""
    produtos = []
    pagina = 1
    while True:
        print(f'Buscando produtos - pagina {pagina}...')
        data = tiny_get('produtos.pesquisa.php', {
            'situacao': 'A',
            'pagina': pagina
        })
        time.sleep(DELAY_ENTRE_CHAMADAS)
        
        if not data:
            break
        items = data.get('produtos', [])
        if not items:
            break
        for item in items:
            p = item.get('produto', {})
            if p.get('codigo'):
                desc = first_text(p.get('descricao'), p.get('nome'), p.get('produto'))
                tipo = first_text(p.get('tipo_estoque'), p.get('tipo_produto'), p.get('tipo'), p.get('classe_produto'), p.get('classe'))
                grupo = first_text(p.get('grupo'), p.get('categoria'))
                produtos.append({
                    'id':  str(p.get('id', '')),
                    'sku': p.get('codigo', ''),
                    'codigo': p.get('codigo', ''),
                    'desc': desc,
                    'nome': desc,
                    'descricao': desc,
                    'un':  first_text(p.get('unidade'), p.get('un')),
                    'preco': p.get('preco', p.get('preco_venda', 0)) or 0,
                    'preco_custo': p.get('preco_custo', 0) or 0,
                    'tipo': tipo,
                    'tipo_produto': p.get('tipo_produto', ''),
                    'classe_produto': p.get('classe_produto', ''),
                    'grupo': grupo,
                    'categoria': p.get('categoria', '') or grupo,
                    'loc': first_text(p.get('localizacao'), p.get('localizacao_estoque'), p.get('loc')),
                    'localizacao': first_text(p.get('localizacao'), p.get('localizacao_estoque'), p.get('loc')),
                    'gtin': first_text(p.get('gtin'), p.get('ean'), p.get('codigo_barras')),
                    'situacao': p.get('situacao', ''),
                })
        num_paginas = int(data.get('numero_paginas', 1))
        if pagina >= num_paginas:
            break
        pagina += 1
    
    print(f'Total produtos: {len(produtos)}')
    return produtos

def buscar_estoque(produto_id):
    """Busca estoque de um produto com delay"""
    data = tiny_get('produto.obter.estoque.php', {'id': produto_id})
    time.sleep(DELAY_ENTRE_CHAMADAS)
    
    if not data:
        return {'disponivel': 0, 'reservado': 0, 'saldo': 0}
    
    depositos = data.get('produto', {}).get('depositos', [])
    total_saldo = 0
    total_res   = 0
    
    for dep in depositos:
        d = dep.get('deposito', {})
        total_saldo += float(d.get('saldo',     0) or 0)
        total_res   += float(d.get('reservado', 0) or 0)
    
    return {
        'disponivel': round(total_saldo - total_res, 2),
        'reservado':  round(total_res, 2),
        'saldo':      round(total_saldo, 2)
    }

def extrair_lista_marcadores(pedido_det):
    marcadores = pedido_det.get('marcadores', [])
    tags = []
    if isinstance(marcadores, list):
        for m in marcadores:
            if isinstance(m, dict):
                md = m.get('marcador', m)
                tags.append(first_text(md.get('descricao'), md.get('nome'), md.get('marcador')))
            else:
                tags.append(str(m))
    return [sanitize_str(t, 80) for t in tags if sanitize_str(t)]

def extrair_textos_pedido(pedido_det):
    campos = []
    for chave in ['obs', 'observacoes', 'forma_envio', 'forma_frete', 'transportador', 'nome_transportador']:
        campos.append(first_text(pedido_det.get(chave)))
    for chave in ['ecommerce', 'transportador', 'forma_envio']:
        valor = pedido_det.get(chave)
        if isinstance(valor, dict):
            campos.extend([first_text(v) for v in valor.values() if not isinstance(v, (dict, list))])
    campos.extend(extrair_lista_marcadores(pedido_det))
    return ' '.join([c for c in campos if c])

def detectar_plataforma(canal):
    c = normalizar_texto(canal)
    if not c:
        return 'outros'
    if 'mercado' in c or 'mercadolivre' in c or 'meli' in c or c.startswith('ml') or 'mhlg' in c:
        return 'Mercado Livre'
    if 'shopee' in c:
        return 'Shopee'
    if 'magalu' in c or 'magazine' in c or 'luiza' in c:
        return 'Magalu'
    if 'amazon' in c or 'fba' in c:
        return 'Amazon'
    if 'tiktok' in c or 'tik tok' in c:
        return 'TikTok'
    if 'americanas' in c or 'b2w' in c:
        return 'Americanas'
    if 'olist' in c:
        return 'Olist'
    if 'tray' in c:
        return 'Tray'
    if 'leroy' in c:
        return 'Leroy Merlin'
    if 'jodda' in c:
        return 'Jodda'
    if 'condominio' in c or 'condom' in c or 'b2b' in c or 'atacado' in c:
        return 'Condominio'
    return sanitize_str(canal, 40) or 'outros'

def extrair_canal(pedido_det):
    ecommerce = pedido_det.get('ecommerce', {})
    candidatos = []
    if isinstance(ecommerce, dict):
        candidatos.extend([
            ecommerce.get('canal'), ecommerce.get('nome_loja'), ecommerce.get('nome'),
            ecommerce.get('plataforma'), ecommerce.get('marketplace'), ecommerce.get('loja'),
            ecommerce.get('numeroPedidoEcommerce'), ecommerce.get('numero_pedido_ecommerce'),
        ])
    candidatos.extend(extrair_lista_marcadores(pedido_det))
    candidatos.append(extrair_textos_pedido(pedido_det))
    texto = ' '.join([first_text(c) for c in candidatos if first_text(c)])
    plataforma = detectar_plataforma(texto)
    if plataforma != 'outros':
        return plataforma
    return first_text(*candidatos)[:40] or 'outros'

def extrair_rastreio(pedido_det):
    chaves = ['codigo_rastreamento', 'codigo_rastreio', 'rastreamento', 'tracking', 'tracking_code', 'numero_objeto']
    for chave in chaves:
        valor = first_text(pedido_det.get(chave))
        if valor:
            return sanitize_str(valor, 80)
    for raiz in ['ecommerce', 'transportador', 'forma_envio']:
        obj = pedido_det.get(raiz)
        if isinstance(obj, dict):
            for chave in chaves:
                valor = first_text(obj.get(chave))
                if valor:
                    return sanitize_str(valor, 80)
    return ''

def limpar_item_pedido(item_d):
    sku = sanitize_str(first_text(item_d.get('codigo'), item_d.get('sku')), 80)
    desc = sanitize_str(first_text(item_d.get('descricao'), item_d.get('desc'), item_d.get('nome'), item_d.get('produto'), sku), 140)
    return {
        'sku': sku,
        'codigo': sku,
        'desc': desc,
        'nome': desc,
        'qtd': to_float(first_text(item_d.get('quantidade'), item_d.get('qtd'), 0)),
        'un': sanitize_str(item_d.get('unidade', 'UN'), 12) or 'UN',
        'valor': to_float(first_text(item_d.get('valor_unitario'), item_d.get('valor'), 0)),
    }

def buscar_pedidos_atualizados():
    """Busca pedidos recentes em varias situacoes e enriquece com detalhe do Tiny."""
    pedidos_por_id = {}
    data_final = datetime.datetime.now()
    data_inicial = data_final - datetime.timedelta(days=TINY_PEDIDOS_DIAS)
    periodo = {
        'dataInicial': data_inicial.strftime('%d/%m/%Y'),
        'dataFinal': data_final.strftime('%d/%m/%Y'),
    }

    for situacao in PEDIDO_SITUACOES:
        pagina = 1
        while pagina <= TINY_PEDIDOS_MAX_PAGINAS:
            print(f'Buscando pedidos {situacao} - pagina {pagina}...')
            data = tiny_get('pedidos.pesquisa.php', {
                'situacao': situacao,
                'pagina': pagina,
                **periodo,
            })
            time.sleep(DELAY_ENTRE_CHAMADAS)
            if not data:
                break
            items = data.get('pedidos', [])
            if not items:
                break
            for item in items:
                p = item.get('pedido', {})
                pedido_id = str(p.get('id', '') or p.get('numero', ''))
                if not pedido_id:
                    continue
                pedidos_por_id[pedido_id] = {
                    'numero': str(p.get('numero', '')),
                    'situacao': p.get('situacao', situacao),
                    'cliente': p.get('nome_contato', ''),
                    'valor': to_float(p.get('valor', 0)),
                    'data': p.get('data_pedido', ''),
                    'id': str(p.get('id', '')),
                }
            num_paginas = int(data.get('numero_paginas', 1) or 1)
            if pagina >= num_paginas:
                break
            pagina += 1

    pedidos = list(pedidos_por_id.values())
    print(f'Total pedidos encontrados: {len(pedidos)}')
    print('Buscando detalhes dos pedidos...')

    pedidos_com_itens = []
    for i, ped in enumerate(pedidos):
        pedido_det = {}
        if ped.get('id'):
            detail = tiny_get('pedido.obter.php', {'id': ped['id']})
            time.sleep(DELAY_ENTRE_CHAMADAS)
            if detail:
                pedido_det = detail.get('pedido', {})

        cliente = pedido_det.get('cliente', {})
        ecommerce = pedido_det.get('ecommerce', {})
        if not isinstance(cliente, dict):
            cliente = {}
        if not isinstance(ecommerce, dict):
            ecommerce = {}

        itens = []
        for it in pedido_det.get('itens', []) or []:
            item_d = it.get('item', {}) if isinstance(it, dict) else {}
            itens.append(limpar_item_pedido(item_d))

        canal = extrair_canal(pedido_det) if pedido_det else first_text(ped.get('canal'), 'outros')
        marcadores = extrair_lista_marcadores(pedido_det) if pedido_det else []
        ped.update({
            'numero': first_text(pedido_det.get('numero'), ped.get('numero')),
            'numero_ecommerce': first_text(
                ecommerce.get('numeroPedidoEcommerce'),
                ecommerce.get('numero_pedido_ecommerce'),
                pedido_det.get('numero_ecommerce'),
                pedido_det.get('numero_pedido_ecommerce'),
            ),
            'situacao': first_text(pedido_det.get('situacao'), ped.get('situacao')),
            'cliente': first_text(cliente.get('nome'), pedido_det.get('nome_contato'), ped.get('cliente')),
            'documento': first_text(cliente.get('cpf_cnpj'), cliente.get('cnpj'), cliente.get('cpf'), pedido_det.get('cpf_cnpj')),
            'valor': to_float(first_text(pedido_det.get('valor'), pedido_det.get('valor_total'), ped.get('valor'))),
            'data': first_text(pedido_det.get('data_pedido'), ped.get('data')),
            'data_prevista': first_text(pedido_det.get('data_prevista'), pedido_det.get('data_entrega')),
            'data_limite': first_text(pedido_det.get('data_limite'), pedido_det.get('data_limite_despacho'), pedido_det.get('data_prevista')),
            'canal': canal,
            'plataforma': detectar_plataforma(canal),
            'loja': first_text(ecommerce.get('nome_loja'), ecommerce.get('loja'), ecommerce.get('nome')),
            'forma_envio': first_text(pedido_det.get('forma_envio'), pedido_det.get('forma_frete')),
            'transportador': first_text(pedido_det.get('transportador'), pedido_det.get('nome_transportador')),
            'rastreio': extrair_rastreio(pedido_det),
            'marcadores': marcadores,
            'itens': itens,
        })
        pedidos_com_itens.append(ped)
        if (i + 1) % 20 == 0:
            print(f'  Detalhes: {i+1}/{len(pedidos)} pedidos processados...')
            time.sleep(10)

    print(f'Total pedidos com detalhes: {len(pedidos_com_itens)}')
    return pedidos_com_itens

def buscar_pedidos_abertos():
    return buscar_pedidos_atualizados()

def sanitize_key(key):
    if not key:
        return 'SEM_KEY'
    key = str(key)
    for char in ['.', '#', '$', '[', ']', '/', ' ', '@', '!', '%', '&', '*', '+', '=', '?', '<', '>', ',', ';', ':', "'", '"']:
        key = key.replace(char, '_')
    return key.strip('_') or 'SEM_KEY'

def salvar_pedidos_firebase(ref, pedidos, agora):
    pedidos_dict = {str(i): p for i, p in enumerate(pedidos)}
    ref.child('pedidos_abertos').set({
        'items': pedidos_dict,
        'total': len(pedidos),
        'atualizado': agora
    })

    existentes = ref.child('pedidos').get() or {}
    pedidos_wms = {}
    agora_ms = int(datetime.datetime.now().timestamp() * 1000)

    for p in pedidos:
        numero_original = first_text(p.get('numero'), p.get('id'), 'SEM_NUM')
        num = sanitize_key(numero_original)
        existente = existentes.get(num, {}) if isinstance(existentes, dict) else {}
        itens_clean = [limpar_item_pedido(it) for it in (p.get('itens') or [])]
        situacao = sanitize_str(p.get('situacao', ''))
        status_erp = status_wms(situacao)
        status_final = status_erp
        if existente.get('status') == 'em_separacao' and status_erp in ['pendente', 'aprovado']:
            status_final = 'em_separacao'

        canal = sanitize_str(p.get('canal') or p.get('plataforma') or 'outros', 80)
        plataforma = detectar_plataforma(canal)
        integracoes = []
        for valor in [plataforma, canal, p.get('loja')]:
            valor = sanitize_str(valor, 80)
            if valor and valor not in integracoes and valor != 'outros':
                integracoes.append(valor)

        pedidos_wms[num] = {
            'numero': sanitize_str(numero_original, 40),
            'id_tiny': sanitize_str(p.get('id', ''), 40),
            'numero_ecommerce': sanitize_str(p.get('numero_ecommerce', ''), 80),
            'cliente': sanitize_str(p.get('cliente', ''), 120),
            'documento': sanitize_str(p.get('documento', ''), 40),
            'valor': to_float(p.get('valor', 0)),
            'data': sanitize_str(p.get('data', ''), 20),
            'data_prevista': sanitize_str(p.get('data_prevista', ''), 20),
            'data_limite': sanitize_str(p.get('data_limite', ''), 20),
            'status': status_final,
            'status_erp': status_erp,
            'situacao_tiny': situacao,
            'origem': 'tiny',
            'canal': canal,
            'plataforma': plataforma,
            'loja': sanitize_str(p.get('loja', ''), 80),
            'forma_envio': sanitize_str(p.get('forma_envio', ''), 80),
            'transportador': sanitize_str(p.get('transportador', ''), 80),
            'rastreio': sanitize_str(p.get('rastreio', ''), 80),
            'codigo_rastreio': sanitize_str(p.get('rastreio', ''), 80),
            'marcadores': [sanitize_str(m, 80) for m in (p.get('marcadores') or [])],
            'integracoes': integracoes,
            'itens': itens_clean,
            'bipados': existente.get('bipados', {}),
            'separador': existente.get('separador', ''),
            'ts_inicio': existente.get('ts_inicio', 0),
            'ts_fim': existente.get('ts_fim', 0),
            'ts': existente.get('ts') or agora_ms,
            'ts_sync': agora_ms,
        }

    pedidos_items = list(pedidos_wms.items())
    for i in range(0, len(pedidos_items), 20):
        chunk = dict(pedidos_items[i:i+20])
        ref.child('pedidos').update(chunk)
        print(f'  pedidos WMS: {min(i+20, len(pedidos_items))}/{len(pedidos_items)} salvos')

    ref.child('pedidos_meta').set({
        'total': len(pedidos_wms),
        'atualizado': agora,
        'dias': TINY_PEDIDOS_DIAS,
        'situacoes': PEDIDO_SITUACOES,
        'modo': SYNC_MODE,
    })
    print('  pedidos salvos')

def sincronizar():
    print('=== Iniciando sincronizacao Tiny -> Firebase ===')
    print(f'Horario: {datetime.datetime.now().isoformat()}')
    
    # 1. Buscar lista de produtos
    produtos = buscar_produtos()
    if not produtos:
        print('Nenhum produto. Abortando.')
        return
    
    # 2. Buscar estoque de cada produto (com delay entre chamadas)
    print(f'\nBuscando estoque de {len(produtos)} produtos...')
    print('(Isso pode demorar alguns minutos por causa do limite da API Tiny)')
    
    catalogo = {}
    alertas  = []
    
    for i, p in enumerate(produtos):
        estoque = buscar_estoque(p['id'])
        
        item = {
            'sku':        p['sku'],
            'codigo':     p.get('codigo', p['sku']),
            'id_tiny':    p.get('id', ''),
            'desc':       p['desc'],
            'nome':       p.get('nome', p['desc']),
            'descricao':  p.get('descricao', p['desc']),
            'un':         p['un'],
            'preco':      p.get('preco', 0) or 0,
            'preco_custo': p.get('preco_custo', 0) or 0,
            'tipo':       p.get('tipo', ''),
            'tipo_produto': p.get('tipo_produto', ''),
            'classe_produto': p.get('classe_produto', ''),
            'grupo':      p.get('grupo', ''),
            'categoria':  p.get('categoria', ''),
            'loc':        p.get('loc', ''),
            'localizacao': p.get('localizacao', ''),
            'gtin':       p.get('gtin', ''),
            'situacao':   p.get('situacao', ''),
            'disponivel': estoque['disponivel'],
            'reservado':  estoque['reservado'],
            'saldo':      estoque['saldo'],
            'alerta':     estoque['disponivel'] < estoque['reservado']
        }
        tipo_norm, tipo_oficial = detectar_tipo_produto(item)
        item['tipo_normalizado'] = tipo_norm
        item['tipo_oficial'] = tipo_oficial
        catalogo[p['sku']] = item
        
        if item['alerta'] and estoque['reservado'] > 0:
            alertas.append({
                'sku':        p['sku'],
                'desc':       p['desc'],
                'disponivel': estoque['disponivel'],
                'reservado':  estoque['reservado'],
                'deficit':    round(estoque['reservado'] - estoque['disponivel'], 2)
            })
        
        if (i+1) % 20 == 0:
            print(f'  Progresso: {i+1}/{len(produtos)} ({round((i+1)/len(produtos)*100)}%)')
            # Pausa extra a cada 20 produtos para evitar rate limit
            print('  Pausa de 10s para respeitar limite da API...')
            time.sleep(10)
    
    print(f'\nCatalogo: {len(catalogo)} produtos')
    print(f'Alertas:  {len(alertas)} produtos com disponivel < reservado')
    
    # 3. Buscar pedidos abertos
    print('\nBuscando pedidos abertos...')
    pedidos = buscar_pedidos_abertos()
    
    # 4. Salvar tudo no Firebase
    print('\nSalvando no Firebase...')
    agora = datetime.datetime.now().isoformat()
    ref = db.reference('/')
    
    def sanitize_item(item):
        return {
            'sku':        sanitize_str(item.get('sku', '')),
            'codigo':     sanitize_str(item.get('codigo', item.get('sku', ''))),
            'id_tiny':    sanitize_str(item.get('id_tiny', '')),
            'desc':       sanitize_str(item.get('desc', '')),
            'nome':       sanitize_str(item.get('nome', item.get('desc', ''))),
            'descricao':  sanitize_str(item.get('descricao', item.get('desc', ''))),
            'un':         sanitize_str(item.get('un', '')),
            'preco':      float(item.get('preco', 0) or 0),
            'preco_custo': float(item.get('preco_custo', 0) or 0),
            'tipo':       sanitize_str(item.get('tipo', '')),
            'tipo_produto': sanitize_str(item.get('tipo_produto', '')),
            'classe_produto': sanitize_str(item.get('classe_produto', '')),
            'tipo_normalizado': sanitize_str(item.get('tipo_normalizado', '')),
            'tipo_oficial': bool(item.get('tipo_oficial', False)),
            'grupo':      sanitize_str(item.get('grupo', '')),
            'categoria':  sanitize_str(item.get('categoria', '')),
            'loc':        sanitize_str(item.get('loc', '')),
            'localizacao': sanitize_str(item.get('localizacao', '')),
            'gtin':       sanitize_str(item.get('gtin', '')),
            'situacao':   sanitize_str(item.get('situacao', '')),
            'disponivel': float(item.get('disponivel', 0) or 0),
            'reservado':  float(item.get('reservado', 0) or 0),
            'saldo':      float(item.get('saldo', 0) or 0),
            'alerta':     bool(item.get('alerta', False)),
        }

    catalogo_safe = {}
    for k, v in catalogo.items():
        safe_key = sanitize_key(k)
        catalogo_safe[safe_key] = sanitize_item(v)

    auditoria = {
        'total': len(catalogo_safe),
        'simples': 0,
        'kits': 0,
        'materia_prima': 0,
        'sem_nome': 0,
        'sem_tipo_oficial': 0,
        'sem_localizacao': 0,
        'revisar': [],
        'atualizado': agora
    }
    for item in catalogo_safe.values():
        tipo_norm = item.get('tipo_normalizado') or 'simples'
        if tipo_norm in auditoria:
            auditoria[tipo_norm] += 1
        sku = item.get('sku', '')
        nome = item.get('desc', '') or item.get('nome', '')
        sem_nome = (not nome) or (nome.upper() == sku.upper())
        sem_tipo = not item.get('tipo_oficial', False)
        sem_loc = not (item.get('loc') or item.get('localizacao'))
        if sem_nome:
            auditoria['sem_nome'] += 1
        if sem_tipo:
            auditoria['sem_tipo_oficial'] += 1
        if sem_loc:
            auditoria['sem_localizacao'] += 1
        if sem_nome or sem_tipo or sem_loc:
            auditoria['revisar'].append({
                'sku': sku,
                'desc': nome,
                'tipo': tipo_norm,
                'sem_nome': sem_nome,
                'sem_tipo_oficial': sem_tipo,
                'sem_localizacao': sem_loc,
            })
    auditoria['revisar'] = auditoria['revisar'][:300]

    # Save in chunks of 200 to avoid large payload issues
    # Salva em chunks pequenos de 50 produtos para evitar limite de 10MB
    catalogo_items = list(catalogo_safe.items())
    chunk_size = 50
    total_chunks = (len(catalogo_items) + chunk_size - 1) // chunk_size
    for i in range(0, len(catalogo_items), chunk_size):
        chunk = dict(catalogo_items[i:i+chunk_size])
        chunk_num = i // chunk_size + 1
        try:
            ref.child('catalogo').update(chunk)
            print(f'  catalogo: chunk {chunk_num}/{total_chunks} salvo ({min(i+chunk_size, len(catalogo_items))}/{len(catalogo_items)})')
        except Exception as e:
            print(f'  ERRO no chunk {chunk_num}: {e}')
            # Tenta salvar item por item neste chunk
            for k, v in chunk.items():
                try:
                    ref.child('catalogo').child(k).set(v)
                except Exception as e2:
                    print(f'    Ignorando SKU {k}: {e2}')
        time.sleep(0.5)  # Pequena pausa entre chunks
    print(f'  catalogo completo: {len(catalogo_safe)} produtos')
    ref.child('catalogo_meta').set(auditoria)
    print(f"  auditoria catalogo: simples={auditoria['simples']} kits={auditoria['kits']} revisar={len(auditoria['revisar'])}")
    
    ref.child('alertas_estoque').set({
        'items': alertas,
        'total': len(alertas),
        'atualizado': agora
    })
    print('  alertas salvos')
    
    salvar_pedidos_firebase(ref, pedidos, agora)
    
    ref.child('ultima_sincronizacao').set(agora)
    
    print(f'\n=== Sync concluido: {len(catalogo)} produtos, {len(alertas)} alertas, {len(pedidos)} pedidos ===')

def sincronizar_pedidos():
    print('=== Iniciando sincronizacao de pedidos Tiny -> Firebase ===')
    print(f'Horario: {datetime.datetime.now().isoformat()}')
    pedidos = buscar_pedidos_atualizados()
    agora = datetime.datetime.now().isoformat()
    ref = db.reference('/')
    salvar_pedidos_firebase(ref, pedidos, agora)
    ref.child('ultima_sincronizacao_pedidos').set(agora)
    print(f'\n=== Sync pedidos concluido: {len(pedidos)} pedidos ===')

if __name__ == '__main__':
    if not TINY_TOKEN:
        print('ERRO: TINY_TOKEN nao configurado')
        exit(1)
    if not FIREBASE_CRED_JSON:
        print('ERRO: FIREBASE_CREDENTIALS nao configurado')
        exit(1)
    init_firebase()
    if SYNC_MODE == 'pedidos':
        sincronizar_pedidos()
    else:
        sincronizar()
