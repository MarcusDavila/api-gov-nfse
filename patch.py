import os
import requests
import base64
import gzip
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime
import psycopg2
from dotenv import load_dotenv
import io
import sys
import re

# Configuração de Saída do Console para UTF-8
if sys.stdout.encoding != 'utf-8':
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())

load_dotenv()

# Configurações das Empresas (Filiais)
COMPANIES = [
    {
        "id": 1,
        "nome": "INTERLINK",
        "cert_pem": os.getenv('CERT_PATH_pem_INTERLINK'),
        "cert_key": os.getenv('CERT_PATH_key_INTERLINK'),
        "cert_pwd": os.getenv('CERT_PASSWORD_INTERLINK'),
    },
    {
        "id": 2,
        "nome": "ECARGO",
        "cert_pem": os.getenv('CERT_PATH_pem_ECARGO'),
        "cert_key": os.getenv('CERT_PATH_key_ECARGO'),
        "cert_pwd": os.getenv('CERT_PASSWORD_ECARGO'),
    }
]

DB_CONFIG = {
    "dbname":   os.getenv('DB_DATABASE'),
    "user":     os.getenv('DB_USER'),
    "password": os.getenv('DB_PASSWORD'),
    "host":     os.getenv('DB_HOST'),
    "port":     os.getenv('DB_PORT'),
}

class SSLAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, cert_pem, cert_key, cert_pwd, *args, **kwargs):
        self.cert_pem, self.cert_key, self.cert_pwd = cert_pem, cert_key, cert_pwd
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=self.cert_pem, keyfile=self.cert_key, password=self.cert_pwd)
        kwargs['ssl_context'] = context
        return super(SSLAdapter, self).init_poolmanager(*args, **kwargs)

class VerificadorEventosNFSe:
    def __init__(self, config):
        self.config = config
        self.base_url = "https://adn.nfse.gov.br/contribuintes/DFe"
        self.session = requests.Session()
        self.session.mount("https://", SSLAdapter(config['cert_pem'], config['cert_key'], config['cert_pwd']))
        self.conn = psycopg2.connect(**DB_CONFIG)
        self.conn.set_client_encoding('UTF8')
        self.log_file = f"ultimo_nsu_eventos_filial_{config['id']}.txt"

    def salvar_progresso(self, nsu):
        with open(self.log_file, "w") as f:
            f.write(str(nsu))

    def carregar_progresso(self):
        if os.path.exists(self.log_file):
            with open(self.log_file, "r") as f:
                content = f.read().strip()
                return int(content) if content else None
        return None

    def obter_ultimo_nsu_banco(self):
        """Busca o maior NSU já processado para esta filial no banco."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT MAX(nsu) FROM public.pub_nfse_notas_api WHERE filial = %s;", (self.config['id'],))
            resultado = cur.fetchone()[0]
        return resultado if resultado else 0

    def atualizar_banco(self, chave_alvo, novo_status, data_evento):
        chave = chave_alvo.replace("NFSe", "").replace("NFS", "").strip()
        with self.conn.cursor() as cur:
            # O filtro por filial é essencial para garantir que alteramos a nota da empresa certa
            cur.execute("""
                UPDATE public.pub_nfse_notas_api 
                SET situacao = %s, data_cancelamento = %s
                WHERE (chave_acesso = %s OR chave_acesso = %s) AND filial = %s
                AND (situacao IS NULL OR situacao != %s);
            """, (novo_status, data_evento, chave, "NFSe" + chave, self.config['id'], novo_status))
            
            if cur.rowcount > 0:
                print(f"\n      [✓] ATUALIZADO: {chave} -> {novo_status}")
        self.conn.commit()

    def processar_xml(self, xml_bruto, nsu):
        try:
            # 1. Limpeza de Namespaces para garantir que o parser encontre as tags
            xml_clean = re.sub(r'\sxmlns[^=]*="[^"]+"', '', xml_bruto)
            xml_clean = re.sub(r'[a-zA-Z0-9]+:', '', xml_clean)
            root = ET.fromstring(xml_clean)

            # 2. Mapear todas as tags para um dicionário em minúsculo
            dados = {elem.tag.lower(): elem.text for elem in root.iter() if elem.text}

            # 3. Extração da data
            dh_str = dados.get('dhevento') or dados.get('dhproc') or dados.get('dhemi')
            data_e = datetime.now()
            if dh_str:
                try: data_e = datetime.fromisoformat(dh_str[:19])
                except: pass

            # --- LÓGICA DE DETECÇÃO ---

            # CASO A: Evento de Cancelamento por Substituição (e105102) - O que você enviou no debug
            if 'e105102' in dados or root.find(".//e105102") is not None:
                # No evento e105102, a tag chNFSe é a nota que está sendo invalidada
                chave_canc = dados.get('chnfse')
                if chave_canc:
                    self.atualizar_banco(chave_canc, "SUBSTITUIDA", data_e)
                    return f"SUBSTITUIÇÃO (e105102) capturada! Nota: {chave_canc}"

            # CASO B: Evento de Cancelamento Padrão (101101 ou 110111)
            tp_evento = dados.get('tpevento')
            if tp_evento in ["101101", "110111"]:
                chave_canc = dados.get('chnfse') or dados.get('chdfse')
                if chave_canc:
                    self.atualizar_banco(chave_canc, "CANCELADA", data_e)
                    return f"CANCELAMENTO (101101) capturado! Nota: {chave_canc}"

            # CASO C: Evento de Substituição Genérico (101103)
            if tp_evento == "101103":
                # Geralmente a tag chNFSeSubst indica a nota antiga
                chave_antiga = dados.get('chnfsesubst') or dados.get('chnfse')
                if chave_antiga:
                    self.atualizar_banco(chave_antiga, "SUBSTITUIDA", data_e)
                    return f"SUBSTITUIÇÃO (101103) capturada! Nota: {chave_antiga}"

            # Se for uma Nota Fiscal comum, apenas ignora
            if 'nfse' in root.tag.lower():
                return "NOTA FISCAL (Emissão)"

            return f"EVENTO {tp_evento if tp_evento else root.tag}"

        except Exception as e:
            return f"Erro ao processar XML: {e}"

    def executar(self):
        nsu_atual = self.carregar_progresso()
        
        if nsu_atual is None:
            # Se não há log, tenta pegar o último do banco e retroceder um pouco por segurança
            ultimo_banco = self.obter_ultimo_nsu_banco()
            nsu_atual = max(1, ultimo_banco - 5000)

        print(f"[*] Varrendo {self.config['nome']} a partir do NSU {nsu_atual}...")

        while True:
            url = f"{self.base_url}/{nsu_atual}"
            try:
                res = self.session.get(url, timeout=60)
                
                if res.status_code == 200:
                    lote = res.json().get('LoteDFe', [])
                    if not lote:
                        nsu_atual += 1
                        continue
                    
                    maior_nsu_lote = nsu_atual
                    for item in lote:
                        nsu_item = int(item.get('nNSU') or item.get('NSU') or 0)
                        maior_nsu_lote = max(maior_nsu_lote, nsu_item)
                        
                        xml_b64 = item.get('ArquivoXml')
                        if xml_b64:
                            xml_bruto = gzip.decompress(base64.b64decode(xml_b64)).decode('utf-8')
                            msg = self.processar_xml(xml_bruto, nsu_item)
                            # Somente imprime se for algo relevante (Cancelamento/Substituição)
                            if "capturada" in msg or "capturado" in msg:
                                print(f"    [NSU {nsu_item}] {msg}")
                        
                        # Log discreto de progresso
                        sys.stdout.write(f"\r    Processando NSU: {nsu_item}...")
                        sys.stdout.flush()

                    nsu_atual = maior_nsu_lote + 1
                    self.salvar_progresso(nsu_atual)
                    time.sleep(1.5) # Delay para evitar Erro 429

                elif res.status_code == 404:
                    print(f"\n[✔] {self.config['nome']}: Fim da fila atingido (NSU {nsu_atual}).")
                    break
                elif res.status_code == 429:
                    print(f"\n[!] Limite atingido. Aguardando 60s...")
                    time.sleep(60)
                else:
                    print(f"\n[!] Erro API {res.status_code}. Tentando próximo...")
                    nsu_atual += 1
                    time.sleep(5)

            except Exception as e:
                print(f"\n[!] Erro de conexão em {self.config['nome']}: {e}")
                time.sleep(10)

    def fechar(self):
        if self.conn:
            self.conn.close()

if __name__ == "__main__":
    for config in COMPANIES:
        print(f"\n{'='*60}\nINICIANDO EMPRESA: {config['nome']}\n{'='*60}")
        verificador = VerificadorEventosNFSe(config)
        try:
            verificador.executar()
        finally:
            verificador.fechar()

    print("\nProcesso finalizado para todas as filiais.")