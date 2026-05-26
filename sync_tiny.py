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
    """Busca pedidos em aberto com itens de cada pedido"""
    pedidos = []
    pagina = 1
    while pagina <= 5:
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
                'id':       str(p.get('id', '')),
            })
        num_paginas = int(data.get('numero_paginas', 1))
        if pagina >= num_paginas:
            break
        pagina += 1

    print(f'Total pedidos encontrados: {len(pedidos)}')

    # Busca itens de cada pedido (com delay para nao estourar rate limit)
    print('Buscando itens dos pedidos...')
    pedidos_com_itens = []
    for i, ped in enumerate(pedidos):
        if not ped['id']:
            pedidos_com_itens.append(ped)
            continue
        detail = tiny_get('pedido.obter.php', {'id': ped['id']})
        time.sleep(DELAY_ENTRE_CHAMADAS)
        if detail:
            pedido_det = detail.get('pedido', {})
            itens_raw = pedido_det.get('itens', [])
            itens = []
            for it in itens_raw:
                item_d = it.get('item', {})
                itens.append({
                    'sku':    str(item_d.get('codigo', '')),
                    'desc':   str(item_d.get('descricao', ''))[:60],
                    'qtd':    float(item_d.get('quantidade', 0) or 0),
                    'un':     str(item_d.get('unidade', 'UN')),
                    'valor':  float(item_d.get('valor_unitario', 0) or 0),
                })
            ped['itens'] = itens
            
            # Capture canal/plataforma from tags or ecommerce field
            canal = ''
            # Try ecommerce field first
            ecommerce = pedido_det.get('ecommerce', {})
            if ecommerce:
                canal = str(ecommerce.get('canal', '') or ecommerce.get('nome_loja', '') or '')
            
            # Try tags/marcadores
            if not canal:
                marcadores = pedido_det.get('marcadores', [])
                if isinstance(marcadores, list) and marcadores:
                    tags = [str(m.get('marcador', {}).get('descricao', '') if isinstance(m, dict) else m) for m in marcadores]
                    canal = ', '.join([t for t in tags if t])
            
            # Try forma_envio or obs for channel hints
            if not canal:
                obs = str(pedido_det.get('obs', '') or '')
                for plat in ['mercado livre', 'mercadolivre', 'shopee', 'magazine', 'magalu', 'amazon', 'tiktok', 'b2w', 'americanas']:
                    if plat in obs.lower():
                        canal = plat.title()
                        break
            
            if canal:
                ped['canal'] = canal.encode('ascii', 'ignore').decode('ascii').strip()[:40]
        else:
            ped['itens'] = []
        pedidos_com_itens.append(ped)
        if (i+1) % 20 == 0:
            print(f'  Itens: {i+1}/{len(pedidos)} pedidos processados...')
            time.sleep(10)

    print(f'Total pedidos com itens: {len(pedidos_com_itens)}')
    return pedidos_com_itens

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

    def detectar_plataforma(canal):
        c = sanitize_str(str(canal or '')).lower()
        if not c:
            return 'outros'
        if 'mercado' in c or 'mercadolivre' in c or 'meli' in c or c.startswith('ml'):
            return 'Mercado Livre'
        if 'shopee' in c:
            return 'Shopee'
        if 'magalu' in c or 'magazine' in c or 'luiza' in c:
            return 'Magalu'
        if 'amazon' in c:
            return 'Amazon'
        if 'tiktok' in c or 'tik tok' in c:
            return 'TikTok'
        if 'americanas' in c or 'b2w' in c:
            return 'Americanas'
        if 'condominio' in c or 'condom' in c or 'b2b' in c or 'atacado' in c:
            return 'Condominio'
        return sanitize_str(canal) or 'outros'

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
    
    # Save to pedidos_abertos (for reference)
    pedidos_dict = {str(i): p for i, p in enumerate(pedidos)}
    ref.child('pedidos_abertos').set({
        'items': pedidos_dict,
        'total': len(pedidos),
        'atualizado': agora
    })
    
    # Also save directly to /pedidos/ with itens so WMS can use without manual import
    def sanitize_key(k):
        return str(k).replace('.','_').replace('#','_').replace('$','_').replace('[','_').replace(']','_').replace('/','_') or 'SEM_KEY'
    
    pedidos_wms = {}
    for p in pedidos:
        num = sanitize_key(p.get('numero','') or p.get('id','') or 'SEM_NUM')
        # Sanitize item descriptions
        itens_clean = []
        for it in (p.get('itens') or []):
            itens_clean.append({
                'sku':   str(it.get('sku','')).encode('ascii','ignore').decode('ascii'),
                'desc':  str(it.get('desc','')).encode('ascii','ignore').decode('ascii')[:55],
                'qtd':   float(it.get('qtd',0) or 0),
                'un':    str(it.get('un','UN')).encode('ascii','ignore').decode('ascii'),
                'valor': float(it.get('valor',0) or 0),
            })
        canal_raw = str(p.get('canal','') or p.get('ecommerce','') or '')
        pedidos_wms[num] = {
            'numero':  num,
            'id_tiny': str(p.get('id','') or ''),
            'cliente': str(p.get('cliente','') or p.get('nome_contato','')).encode('ascii','ignore').decode('ascii')[:60],
            'valor':   float(p.get('valor',0) or 0),
            'data':    str(p.get('data','') or p.get('data_pedido','')),
            'status':  'pendente',
            'origem':  'tiny',
            'canal':   canal_raw.encode('ascii','ignore').decode('ascii').strip()[:40],
            'plataforma': detectar_plataforma(canal_raw),
            'itens':   itens_clean,
            'ts':      int(datetime.datetime.now().timestamp() * 1000),
        }
    
    # Save in chunks to avoid size limits
    pedidos_items = list(pedidos_wms.items())
    for i in range(0, len(pedidos_items), 20):
        chunk = dict(pedidos_items[i:i+20])
        ref.child('pedidos').update(chunk)
        print(f'  pedidos WMS: {min(i+20, len(pedidos_items))}/{len(pedidos_items)} salvos')
    
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
