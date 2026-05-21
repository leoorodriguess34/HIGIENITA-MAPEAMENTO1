#!/usr/bin/env python3
"""
Higienita WMS - Sincronizacao Tiny ERP -> Firebase
Roda via GitHub Actions de hora em hora
"""

import os
import json
import time
import requests
import firebase_admin
from firebase_admin import credentials, db

# ── CONFIG ──
TINY_TOKEN = os.environ.get('TINY_TOKEN', '')
FIREBASE_CRED_JSON = os.environ.get('FIREBASE_CREDENTIALS', '')
FIREBASE_DB_URL = 'https://higienita-f2b22-default-rtdb.firebaseio.com'

# Delay entre chamadas da API Tiny (segundos)
# Tiny permite ~30 req/min, entao 2s entre chamadas = safe
DELAY_ENTRE_CHAMADAS = 2.5

def init_firebase():
    cred_dict = json.loads(FIREBASE_CRED_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})
    print('Firebase conectado')

TINY_BASE = 'https://api.tiny.com.br/api2'

def tiny_get(endpoint, params={}, tentativas=3):
    """Chamada a API Tiny com retry em caso de rate limit"""
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
                produtos.append({
                    'id':  str(p.get('id', '')),
                    'sku': p.get('codigo', ''),
                    'desc': p.get('descricao', ''),
                    'un':  p.get('unidade', ''),
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

def buscar_pedidos_abertos():
    """Busca pedidos em aberto"""
    pedidos = []
    pagina = 1
    while pagina <= 3:  # Limita a 3 paginas para nao estourar rate limit
        print(f'Buscando pedidos - pagina {pagina}...')
        data = tiny_get('pedidos.pesquisa.php', {
            'situacao': 'aberto',
            'pagina': pagina
        })
        time.sleep(DELAY_ENTRE_CHAMADAS)
        
        if not data:
            break
        items = data.get('pedidos', [])
        if not items:
            break
        for item in items:
            p = item.get('pedido', {})
            pedidos.append({
                'numero':   str(p.get('numero', '')),
                'situacao': p.get('situacao', ''),
                'cliente':  p.get('nome_contato', ''),
                'valor':    float(p.get('valor', 0) or 0),
                'data':     p.get('data_pedido', ''),
            })
        num_paginas = int(data.get('numero_paginas', 1))
        if pagina >= num_paginas:
            break
        pagina += 1
    
    print(f'Total pedidos abertos: {len(pedidos)}')
    return pedidos

def sincronizar():
    import datetime
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
            'desc':       p['desc'],
            'un':         p['un'],
            'disponivel': estoque['disponivel'],
            'reservado':  estoque['reservado'],
            'saldo':      estoque['saldo'],
            'alerta':     estoque['disponivel'] < estoque['reservado']
        }
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
    
    # Firebase nao aceita chaves com . # $ [ ] /
    def sanitize_key(key):
        if not key:
            return 'SEM_SKU'
        for char in ['.', '#', '$', '[', ']', '/', ' ', '@', '!', '%', '&', '*', '+', '=', '?', '<', '>', ',', ';', ':', "'", '"']:
            key = key.replace(char, '_')
        return key.strip('_') or 'SEM_SKU'

    def sanitize_str(val):
        if not isinstance(val, str):
            return val
        # Remove control chars and problematic Unicode
        return val.encode('ascii', 'ignore').decode('ascii').strip()

    def sanitize_item(item):
        return {
            'sku':        sanitize_str(item.get('sku', '')),
            'desc':       sanitize_str(item.get('desc', '')),
            'un':         sanitize_str(item.get('un', '')),
            'disponivel': float(item.get('disponivel', 0) or 0),
            'reservado':  float(item.get('reservado', 0) or 0),
            'saldo':      float(item.get('saldo', 0) or 0),
            'alerta':     bool(item.get('alerta', False)),
        }

    catalogo_safe = {}
    for k, v in catalogo.items():
        safe_key = sanitize_key(k)
        catalogo_safe[safe_key] = sanitize_item(v)

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
    
    ref.child('alertas_estoque').set({
        'items': alertas,
        'total': len(alertas),
        'atualizado': agora
    })
    print('  alertas salvos')
    
    pedidos_dict = {str(i): p for i, p in enumerate(pedidos)}
    ref.child('pedidos_abertos').set({
        'items': pedidos_dict,
        'total': len(pedidos),
        'atualizado': agora
    })
    print('  pedidos salvos')
    
    ref.child('ultima_sincronizacao').set(agora)
    
    print(f'\n=== Sync concluido: {len(catalogo)} produtos, {len(alertas)} alertas, {len(pedidos)} pedidos ===')

if __name__ == '__main__':
    if not TINY_TOKEN:
        print('ERRO: TINY_TOKEN nao configurado')
        exit(1)
    if not FIREBASE_CRED_JSON:
        print('ERRO: FIREBASE_CREDENTIALS nao configurado')
        exit(1)
    init_firebase()
    sincronizar()
