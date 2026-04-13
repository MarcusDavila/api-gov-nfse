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
        self.cert_pem = cert_pem
        self.cert_key = cert_key
        self.cert_pwd = cert_pwd
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=self.cert_pem, keyfile=self.cert_key, password=self.cert_pwd)
        kwargs['ssl_context'] = context
        return super(SSLAdapter, self).init_poolmanager(*args, **kwargs)

class VerificadorCancelamentos:
    def __init__(self, config):
        self.config = config
        self.base_url = "https://adn.nfse.gov.br/contribuintes/DFe"
        self.session = requests.Session()
        self.session.mount("https://", SSLAdapter(config['cert_pem'], config['cert_key'], config['cert_pwd']))
        self.conn = psycopg2.connect(**DB_CONFIG)
        self.conn.set_client_encoding('UTF8')
        # Arquivo de log individual por filial
        self.log_file = f"ultimo_nsu_cancelamento_filial_{config['id']}.txt"

    def obter_ultimo_nsu_banco(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT MAX(nsu) FROM public.pub_nfse_notas_api WHERE filial = %s;", (self.config['id'],))
            resultado = cur.fetchone()[0]
        return resultado if resultado else 0

    def salvar_progresso(self, nsu):
        with open(self.log_file, "w") as f:
            f.write(str(nsu))

    def carregar_progresso(self):
        if os.path.exists(self.log_file):
            with open(self.log_file, "r") as f:
                content = f.read().strip()
                return int(content) if content else None
        return None

    def limpar_chave(self, chave):
        if not chave: return ""
        return chave.replace("NFSe", "").replace("NFS", "").strip()

    def atualizar_para_cancelada(self, nsu_evento, dados):
        chave = self.limpar_chave(dados.get("Chave_Acesso"))
        with self.conn.cursor() as cur:
            # Importante: Filial no WHERE garante que não cancelemos a nota da empresa errada
            cur.execute("""
                UPDATE public.pub_nfse_notas_api 
                SET situacao = 'CANCELADA', 
                    data_cancelamento = %s
                WHERE (chave_acesso = %s OR chave_acesso = %s)
                AND filial = %s
                AND (situacao IS NULL OR situacao != 'CANCELADA');
            """, (dados.get("Data_Cancelamento"), chave, "NFSe" + chave, self.config['id']))
            if cur.rowcount > 0:
                print(f"\n  [{self.config['nome']}] [✓] NOTA CANCELADA: {chave} (NSU Evento {nsu_evento})")
        self.conn.commit()

    def extrair_apenas_cancelamento(self, xml_string):
        try:
            it = ET.iterparse(io.StringIO(xml_string))
            for _, el in it:
                if '}' in el.tag: el.tag = el.tag.split('}', 1)[1]
            root = it.root
            def get_t(node, tag):
                found = node.find(f".//{tag}")
                return found.text.strip() if found is not None and found.text else ""

            tp_evento = get_t(root, "tpEvento")
            # Verifica padrões de cancelamento no XML
            if root.find(".//e101101") is not None or tp_evento in ["110111", "101101"]:
                ch = get_t(root, "chNFSe") or get_t(root, "chDFSe")
                dh = get_t(root, "dhEvento") or get_t(root, "dhProc")
                try:
                    data_c = datetime.fromisoformat(dh[:19])
                except:
                    data_c = datetime.now()
                return {"tipo": "CANCELAMENTO", "Chave_Acesso": ch, "Data_Cancelamento": data_c}
            return None
        except: return None

    def consultar_e_verificar(self, nsu):
        url = f"{self.base_url}/{nsu}"
        try:
            response = self.session.get(url, timeout=300)
            if response.status_code == 200:
                lote = response.json().get('LoteDFe', [])
                ultimo_nsu_lote = nsu
                for item in lote:
                    nsu_item = int(item.get('nNSU') or item.get('NSU') or nsu)
                    ultimo_nsu_lote = max(ultimo_nsu_lote, nsu_item)
                    xml_base64 = item.get('ArquivoXml')
                    if xml_base64:
                        xml_bruto = gzip.decompress(base64.b64decode(xml_base64)).decode('utf-8')
                        dados_cancelamento = self.extrair_apenas_cancelamento(xml_bruto)
                        if dados_cancelamento:
                            self.atualizar_para_cancelada(nsu_item, dados_cancelamento)
                return ("OK", ultimo_nsu_lote)
            elif response.status_code == 404: return "FIM"
            elif response.status_code == 429: return "WAIT"
        except Exception as e:
            print(f"\nErro ao consultar NSU {nsu} ({self.config['nome']}): {e}")
            self.conn.rollback()
        return "ERROR"

    def fechar(self):
        if self.conn: self.conn.close()

if __name__ == "__main__":
    for config in COMPANIES:
        print(f"\n{'='*60}")
        print(f"VERIFICANDO CANCELAMENTOS: {config['nome']}")
        print(f"{'='*60}")
        
        verificador = VerificadorCancelamentos(config)
        
        try:
            # 1. Tenta carregar de onde parou no arquivo específico da filial
            nsu_atual = verificador.carregar_progresso()
            
            if nsu_atual is None:
                # Se não tem log, pega o último do banco e retrocede 5000 para segurança
                ultimo_banco = verificador.obter_ultimo_nsu_banco()
                retroceder_nsu = 5000 
                nsu_atual = max(1, ultimo_banco - retroceder_nsu)
            
            print(f"Ponto de partida para {config['nome']}: NSU {nsu_atual}")

            while True:
                sys.stdout.write(f"\r[{config['nome']}] Processando NSU: {nsu_atual}...")
                sys.stdout.flush()
                
                res = verificador.consultar_e_verificar(nsu_atual)
                
                if res == "FIM":
                    print(f"\n[FIM] Eventos de {config['nome']} conferidos até o final.")
                    break
                elif res == "WAIT":
                    print(f"\n[!] Limite da API atingido. Aguardando 60s...")
                    time.sleep(60)
                    continue
                elif isinstance(res, tuple):
                    nsu_atual = res[1] + 1
                    verificador.salvar_progresso(nsu_atual)
                else:
                    nsu_atual += 1
                
                time.sleep(0.8) # Delay para evitar erro 429
        finally:
            verificador.fechar()

    print("\n\nProcesso finalizado para todas as filiais.")