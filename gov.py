import os
import requests
import base64
import gzip
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv


load_dotenv()

CERT_PEM = os.getenv('CERT_PATH_pem')
CERT_KEY = os.getenv('CERT_PATH_key')
CERT_PWD = os.getenv('CERT_PASSWORD')

DB_CONFIG = {
    "dbname":   os.getenv('DB_DATABASE'),
    "user":     os.getenv('DB_USER'),
    "password": os.getenv('DB_PASSWORD'),
    "host":     os.getenv('DB_HOST'),
    "port":     os.getenv('DB_PORT'),
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.pub_nfse_notas_api (
    nsu             INTEGER PRIMARY KEY,
    numero_nota     VARCHAR(50),
    data_emissao    TIMESTAMP,
    emitente_cnpj   VARCHAR(20),
    emitente_nome   VARCHAR(255),
    tomador_cnpj    VARCHAR(20),
    tomador_nome    VARCHAR(255),
    servico_descricao TEXT,
    valor_servico   NUMERIC(15,2),
    valor_bc        NUMERIC(15,2),
    iss_retido      VARCHAR(10),
    valor_iss       NUMERIC(15,2),
    valor_liquido   NUMERIC(15,2),
    chave_acesso    VARCHAR(100),
    criado_em       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class SSLAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.load_cert_chain(certfile=CERT_PEM, keyfile=CERT_KEY, password=CERT_PWD)
        kwargs['ssl_context'] = context
        return super(SSLAdapter, self).init_poolmanager(*args, **kwargs)


class GovNFSeAPI:
    def __init__(self):
        self.base_url = "https://adn.nfse.gov.br/contribuintes/DFe"
        self.session = requests.Session()
        self.session.mount("https://", SSLAdapter())
        self.conn = psycopg2.connect(**DB_CONFIG)
        self._criar_tabela()

    def _criar_tabela(self):
       
        with self.conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        self.conn.commit()
        print("[DB] Tabela 'public.pub_nfse_notas_api' verificada/criada com sucesso.")

    def obter_ultimo_nsu(self):
       
        with self.conn.cursor() as cur:
            cur.execute("SELECT MAX(nsu) FROM public.pub_nfse_notas_api;")
            resultado = cur.fetchone()[0]
        return resultado

    def nsu_existe(self, nsu):
      
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.pub_nfse_notas_api WHERE nsu = %s;", (nsu,))
            return cur.fetchone() is not None

    def salvar_nota(self, nsu, dados):
     
        if self.nsu_existe(nsu):
            print(f"  [SKIP] NSU {nsu} já existe no banco.")
            return

        insert_sql = """
            INSERT INTO public.pub_nfse_notas_api (
                nsu, numero_nota, data_emissao,
                emitente_cnpj, emitente_nome,
                tomador_cnpj, tomador_nome,
                servico_descricao,
                valor_servico, valor_bc, iss_retido,
                valor_iss, valor_liquido, chave_acesso
            ) VALUES (
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s,
                %s, %s, %s,
                %s, %s, %s
            )
        """
        with self.conn.cursor() as cur:
            cur.execute(insert_sql, (
                nsu,
                dados.get("Numero_Nota"),
                dados.get("Data_Emissao"),
                dados.get("Emitente_CNPJ"),
                dados.get("Emitente_Nome"),
                dados.get("Tomador_CNPJ"),
                dados.get("Tomador_Nome"),
                dados.get("Servico_Descricao"),
                dados.get("Valor_Servico"),
                dados.get("Valor_BC"),
                dados.get("ISS_Retido"),
                dados.get("Valor_ISS"),
                dados.get("Valor_Liquido"),
                dados.get("Chave_Acesso"),
            ))
        self.conn.commit()
        print(f"  [DB] NSU {nsu} salvo: Nota {dados.get('Numero_Nota')} | Emitente: {dados.get('Emitente_Nome')}")

    def consultar_e_processar(self, nsu):
        url = f"{self.base_url}/{nsu}"
        try:
            response = self.session.get(url, timeout=30)
            if response.status_code == 200:
                dados_json = response.json()
                lote = dados_json.get('LoteDFe', [])

                for item in lote:
                    nsu_item = item.get('nNSU') or item.get('NSU') or nsu
                    try:
                        nsu_item = int(nsu_item)
                    except (TypeError, ValueError):
                        nsu_item = nsu

                    xml_base64 = item.get('ArquivoXml')
                    if xml_base64:
                        conteudo_binario = base64.b64decode(xml_base64)
                        xml_bruto = gzip.decompress(conteudo_binario).decode('utf-8')

                        dados_nota = self.extrair_dados_xml(xml_bruto)
                        if dados_nota:
                            self.salvar_nota(nsu_item, dados_nota)

            
                nsus_lote = []
                for item in lote:
                    v = item.get('nNSU') or item.get('NSU')
                    try:
                        nsus_lote.append(int(v))
                    except (TypeError, ValueError):
                        pass
                ultimo_nsu_lote = max(nsus_lote) if nsus_lote else nsu
                return ("OK", ultimo_nsu_lote)
            elif response.status_code == 404:
                return "FIM"
            elif response.status_code == 429:
                return "WAIT"
        except Exception as e:
            print(f"Erro no NSU {nsu}: {e}")
            self.conn.rollback()  
        return "ERROR"

    def _parse_data(self, valor: str):
        if not valor:
            return None
        try:
            dt = datetime.fromisoformat(valor)
            return dt.replace(microsecond=0, tzinfo=None)
        except ValueError:
            return None

    def extrair_dados_xml(self, xml_string):
        
        try:
            root = ET.fromstring(xml_string)
            ns = {'n': 'http://www.sped.fazenda.gov.br/nfse'}

            def get_txt(path):
                node = root.find(path, ns)
                return node.text if node is not None else ""

            dados = {
                "Numero_Nota":       get_txt(".//n:nNFSe"),
                "Data_Emissao":      self._parse_data(get_txt(".//n:dhEmi")),
                "Emitente_CNPJ":     get_txt(".//n:emit/n:CNPJ"),
                "Emitente_Nome":     get_txt(".//n:emit/n:xNome"),
                "Tomador_CNPJ":      get_txt(".//n:toma/n:CNPJ"),
                "Tomador_Nome":      get_txt(".//n:toma/n:xNome"),
                "Servico_Descricao": get_txt(".//n:serv/n:cServ/n:xDescServ"),
                "Valor_Servico":     float(get_txt(".//n:valores/n:vServPrest/n:vServ") or 0),
                "Valor_BC":          float(get_txt(".//n:valores/n:vBC") or 0),
                "ISS_Retido":        get_txt(".//n:trib/n:tribMun/n:tpRetISSQN"),
                "Valor_ISS":         float(get_txt(".//n:valores/n:vISSQN") or 0),
                "Valor_Liquido":     float(get_txt(".//n:valores/n:vLiq") or 0),
                "Chave_Acesso":      root.find(".//n:infNFSe", ns).attrib.get('Id', "")
                                     if root.find(".//n:infNFSe", ns) is not None else "",
            }
            return dados
        except Exception as e:
            print(f"Erro ao ler campos do XML: {e}")
            return None

    def fechar(self):
        if self.conn:
            self.conn.close()



if __name__ == "__main__":
    NSU_INICIAL = 2000       
    DELAY_ENTRE_NSUs = 2     
    DELAY_RATE_LIMIT = 10    

    api = GovNFSeAPI()

    try:
        ultimo_nsu = api.obter_ultimo_nsu()

        if ultimo_nsu is None:
            nsu_atual = NSU_INICIAL
            print(f"[INFO] Banco vazio. Iniciando pelo NSU padrão: {nsu_atual}")
        else:
            nsu_atual = ultimo_nsu + 1
            print(f"[INFO] Último NSU no banco: {ultimo_nsu}. Continuando a partir de: {nsu_atual}")

        while True:
            print(f"\nProcessando NSU {nsu_atual}...")
            resultado = api.consultar_e_processar(nsu_atual)

            if resultado == "FIM":
                print(f"[FIM] NSU {nsu_atual} retornou 404. Não há mais registros disponíveis.")
                break
            elif resultado == "WAIT":
                print(f"[WAIT] Limite de requisições atingido. Aguardando {DELAY_RATE_LIMIT}s...")
                time.sleep(DELAY_RATE_LIMIT)
                continue  
            elif resultado == "ERROR":
                print(f"[ERRO] Falha ao processar NSU {nsu_atual}. Pulando para o próximo.")
                nsu_atual += 1
            elif isinstance(resultado, tuple) and resultado[0] == "OK":
                _, ultimo_nsu_lote = resultado
                nsu_atual = ultimo_nsu_lote + 1

            time.sleep(DELAY_ENTRE_NSUs)

    finally:
        api.fechar()
        print("\n[DB] Conexão encerrada.")
