#!/usr/bin/env python3
"""
Higienita WMS - Sincronizacao Tiny ERP -> Firebase
Roda via GitHub Actions de hora em hora
"""

import os
import json
import requests
import firebase_admin
from firebase_admin import credentials, db

# ── CONFIG ──
TINY_TOKEN = os.environ.get('TINY_TOKEN', '')
FIREBASE_CRED_JSON = os.environ.get('FIREBASE_CREDENTIALS', '')
FIREBASE_DB_URL = 'https://higienita-f2b22-default-rtdb.firebaseio.com'

# ── INIT FIREBASE ──
def init_firebase():
    cred_dict = json.loads(FIREBASE_CRED_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})
    print('Firebase conectado')

# ── TINY API ──
TINY_BASE = 'https://api.tiny.com.br/api2'

def tiny_get(endpoint, params={}):
    params['token'] = TINY_TOKEN
    params['formato'] = 'json'
    r = requests.get(f'{TINY_BASE}/{endpoint}', params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get('retorno', {}).get('status') == 'Erro':
        erros = data['retorno'].get('erros', [])
        print(f'Erro Tiny ({endpoint}):', erros)
        return None
    return data.get('retorno', {})

def buscar_produtos():
    """Busca todos os produtos ativos com estoque"""
    produtos = []
    pagina = 1
    while True:
        print(f'Buscando produtos - pagina {pagina}...')
        data = tiny_get('produtos.pesquisa.php', {
            'situacao': 'A',
            'pagina': pagina
        })
        if not data:
            break
        items = data.get('produtos', [])
        if not items:
            break
        for item in items:
            p = item.get('produto', {})
            produtos.append({
                'id': str(p.get('id', '')),
                'sku': p.get('codigo', ''),
                'desc': p.get('descricao', ''),
                'situacao': p.get('situacao', ''),
                'un': p.get('unidade', ''),
            })
        # Check if there are more pages
        num_paginas = int(data.get('numero_paginas', 1))
        if pagina >= num_paginas:
            break
        pagina += 1
    print(f'Total produtos encontrados: {len(produtos)}')
    return produtos

def buscar_estoque(produto_id):
    """Busca estoque disponivel e reservado de um produto"""
    data = tiny_get('produto.obter.estoque.php', {'id': produto_id})
    if not data:
        return {'disponivel': 0, 'reservado': 0, 'saldo': 0}
    
    depositos = data.get('produto', {}).get('depositos', [])
    total_disp = 0
    total_res  = 0
    total_saldo = 0
    
    for dep in depositos:
        d = dep.get('deposito', {})
        total_disp   += float(d.get('saldo',     0) or 0)
        total_res    += float(d.get('reservado', 0) or 0)
        total_saldo  += float(d.get('saldo',     0) or 0)
    
    return {
        'disponivel': total_disp - total_res,
        'reservado':  total_res,
        'saldo':      total_saldo
    }

def buscar_pedidos_reservados():
    """Busca pedidos em aberto que estao reservando estoque"""
    pedidos = []
    pagina = 1
    while True:
        data = tiny_get('pedidos.pesquisa.php', {
            'situacao': 'aberto',
            'pagina': pagina
        })
        if not data:
            break
        items = data.get('pedidos', [])
        if not items:
            break
        for item in items:
            p = item.get('pedido', {})
            pedidos.append({
                'numero': str(p.get('numero', '')),
                'situacao': p.get('situacao', ''),
                'cliente': p.get('nome_contato', ''),
                'valor': float(p.get('valor', 0) or 0),
                'data': p.get('data_pedido', ''),
            })
        num_paginas = int(data.get('numero_paginas', 1))
        if pagina >= num_paginas:
            break
        pagina += 1
    print(f'Total pedidos abertos: {len(pedidos)}')
    return pedidos

def sincronizar():
    print('=== Iniciando sincronizacao Tiny -> Firebase ===')
    
    # 1. Buscar produtos
    produtos = buscar_produtos()
    if not produtos:
        print('Nenhum produto encontrado. Abortando.')
        return
    
    # 2. Buscar estoque de cada produto
    print('Buscando estoques...')
    catalogo = {}
    alertas = []
    
    for i, p in enumerate(produtos):
        if not p['sku']:
            continue
        
        estoque = buscar_estoque(p['id'])
        
        item = {
            'sku':        p['sku'],
            'desc':       p['desc'],
            'un':         p['un'],
            'disponivel': estoque['disponivel'],
            'reservado':  estoque['reservado'],
            'saldo':      estoque['saldo'],
            'alerta':     estoque['disponivel'] < estoque['reservado']
        }
        catalogo[p['sku']] = item
        
        if item['alerta']:
            alertas.append({
                'sku':       p['sku'],
                'desc':      p['desc'],
                'disponivel': estoque['disponivel'],
                'reservado':  estoque['reservado'],
                'deficit':    estoque['reservado'] - estoque['disponivel']
            })
        
        # Progress log every 50 products
        if (i+1) % 50 == 0:
            print(f'  {i+1}/{len(produtos)} produtos processados...')
    
    print(f'Catalogo montado: {len(catalogo)} produtos')
    print(f'Alertas de estoque: {len(alertas)} produtos com disponivel < reservado')
    
    # 3. Buscar pedidos reservados
    pedidos = buscar_pedidos_reservados()
    
    # 4. Salvar no Firebase
    import datetime
    agora = datetime.datetime.now().isoformat()
    
    ref = db.reference('/')
    
    # Save catalog
    ref.child('catalogo').set(catalogo)
    print('Catalogo salvo no Firebase')
    
    # Save alerts
    ref.child('alertas_estoque').set({
        'items': alertas,
        'total': len(alertas),
        'atualizado': agora
    })
    print('Alertas salvos no Firebase')
    
    # Save open orders
    pedidos_dict = {str(i): p for i, p in enumerate(pedidos)}
    ref.child('pedidos_abertos').set({
        'items': pedidos_dict,
        'total': len(pedidos),
        'atualizado': agora
    })
    print('Pedidos salvos no Firebase')
    
    # Save sync timestamp
    ref.child('ultima_sincronizacao').set(agora)
    print(f'Sincronizacao concluida em {agora}')
    print(f'=== Sync completo: {len(catalogo)} produtos, {len(alertas)} alertas, {len(pedidos)} pedidos ===')

if __name__ == '__main__':
    if not TINY_TOKEN:
        print('ERRO: TINY_TOKEN nao configurado')
        exit(1)
    if not FIREBASE_CRED_JSON:
        print('ERRO: FIREBASE_CREDENTIALS nao configurado')
        exit(1)
    init_firebase()
    sincronizar()
