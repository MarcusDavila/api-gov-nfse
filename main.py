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

# Configurações das Empresas
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
        "nome": "E-CARGO",
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

class GovNFSeAPI:
    def __init__(self, config):
        self.config = config
        self.base_url = "https://adn.nfse.gov.br/contribuintes/DFe"
        self.session = requests.Session()
        self.session.mount("https://", SSLAdapter(config['cert_pem'], config['cert_key'], config['cert_pwd']))
        self.conn = psycopg2.connect(**DB_CONFIG)
        self.conn.set_client_encoding('UTF8')

    def obter_ultimo_nsu(self):
        with self.conn.cursor() as cur:
            # Filtra pelo ID da filial
            cur.execute("SELECT MAX(nsu) FROM public.pub_nfse_notas_api WHERE filial = %s;", (self.config['id'],))
            resultado = cur.fetchone()[0]
        return resultado

    def nsu_existe(self, nsu):
        with self.conn.cursor() as cur:
            # Verifica existência combinando NSU e Filial
            cur.execute("SELECT 1 FROM public.pub_nfse_notas_api WHERE nsu = %s AND filial = %s;", (nsu, self.config['id']))
            return cur.fetchone() is not None

    def limpar_chave(self, chave):
        if not chave: return ""
        return chave.replace("NFSe", "").replace("NFS", "").strip()

    def limpar_numero_nota(self, num_str):
        if not num_str: return ""
        try:
            sequencia = num_str[-9:]
            return str(int(sequencia))
        except:
            return num_str

    def salvar_nota(self, nsu, dados):
        if not dados: return


        # --- GARANTIR QUE A TABELA EXISTE ---
        create_table_sql = """
            CREATE TABLE IF NOT EXISTS public.pub_nfse_notas_api (
            nsu integer NOT NULL,
            numero_nota character varying(50),
            data_emissao timestamp without time zone,
            emitente_cnpj character varying(20),
            emitente_nome character varying(255),
            tomador_cnpj character varying(20),
            tomador_nome character varying(255),
            servico_descricao text,
            valor_servico numeric(15,2),
            valor_bc numeric(15,2),
            iss_retido character varying(10),
            valor_iss numeric(15,2),
            valor_liquido numeric(15,2),
            chave_acesso character varying(100),
            dtinc timestamp without time zone DEFAULT now(),
            situacao character varying(20) DEFAULT 'AUTORIZADA'::character varying,
            data_cancelamento timestamp without time zone,
            filial integer NOT NULL,
            CONSTRAINT pub_nfse_notas_api_pkey PRIMARY KEY (nsu, filial)
            );
        """
        # --- CANCELAMENTO ---
        if dados.get("tipo") == "CANCELAMENTO":
            chave = self.limpar_chave(dados.get("Chave_Acesso"))
            with self.conn.cursor() as cur:
                # O update também deve considerar a filial para garantir que está cancelando a nota certa
                cur.execute("""
                    UPDATE public.pub_nfse_notas_api 
                    SET situacao = 'CANCELADA', data_cancelamento = %s
                    WHERE (chave_acesso = %s OR chave_acesso = %s) AND filial = %s;
                """, (dados.get("Data_Cancelamento"), chave, "NFSe" + chave, self.config['id']))
            self.conn.commit()
            if cur.rowcount > 0:
                print(f"  [{self.config['nome']}] [CANCELADO] Chave {chave[:20]}... NSU {nsu}")
            else:
                print(f"  [{self.config['nome']}] [AVISO] Cancelamento (NSU {nsu}): Nota não encontrada nesta filial.")
            return

        # --- EMISSÃO ---
        if self.nsu_existe(nsu): return

        insert_sql = """
            INSERT INTO public.pub_nfse_notas_api (
                nsu, numero_nota, data_emissao,
                emitente_cnpj, emitente_nome,
                tomador_cnpj, tomador_nome,
                servico_descricao,
                valor_servico, valor_bc, iss_retido,
                valor_iss, valor_liquido, chave_acesso, situacao, filial
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'AUTORIZADA', %s
            )
        """
        with self.conn.cursor() as cur:
            cur.execute(insert_sql, (
                nsu, dados.get("Numero_Nota"), dados.get("Data_Emissao"),
                dados.get("Emitente_CNPJ"), dados.get("Emitente_Nome"),
                dados.get("Tomador_CNPJ"), dados.get("Tomador_Nome"),
                dados.get("Servico_Descricao"), dados.get("Valor_Servico"),
                dados.get("Valor_BC"), dados.get("ISS_Retido"),
                dados.get("Valor_ISS"), dados.get("Valor_Liquido"),
                self.limpar_chave(dados.get("Chave_Acesso")),
                self.config['id']
            ))
        self.conn.commit()
        print(f"  [{self.config['nome']}] NSU {nsu} salvo: Nota {dados.get('Numero_Nota')}")

    def consultar_e_processar(self, nsu):
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
                        dados_processados = self.extrair_dados_xml(xml_bruto)
                        if dados_processados:
                            self.salvar_nota(nsu_item, dados_processados)
                return ("OK", ultimo_nsu_lote)
            elif response.status_code == 404: return "FIM"
            elif response.status_code == 429: return "WAIT"
            else:
                print(f"Erro API {self.config['nome']}: Status {response.status_code}")
        except Exception as e:
            print(f"Erro no NSU {nsu} ({self.config['nome']}): {e}")
            self.conn.rollback()  
        return "ERROR"

    def _parse_data(self, valor: str):
        if not valor: return None
        try: return datetime.fromisoformat(valor[:19])
        except: return None

    def extrair_dados_xml(self, xml_string):
        try:
            it = ET.iterparse(io.StringIO(xml_string))
            for _, el in it:
                if '}' in el.tag: el.tag = el.tag.split('}', 1)[1]
            root = it.root

            def get_t(node, tag):
                if node is None: return ""
                found = node.find(f".//{tag}")
                return found.text.replace('’', "'").strip() if found is not None and found.text else ""

            tp_evento = get_t(root, "tpEvento")
            dh_evento = get_t(root, "dhEvento") or get_t(root, "dhProc")

            # --- 1. DETECÇÃO DE SUBSTITUIÇÃO (Evento e105102 - Debug NSU 3707) ---
            if root.find(".//e105102") is not None:
                return {
                    "tipo": "SUBSTITUICAO",
                    "Chave_Acesso": self.limpar_chave(get_t(root, "chNFSe")),
                    "Chave_Nova": self.limpar_chave(get_t(root, "chSubstituta")),
                    "Data_Cancelamento": self._parse_data(dh_evento)
                }

            # --- 2. DETECÇÃO DE SUBSTITUIÇÃO (Evento padrão 101103) ---
            if tp_evento == "101103":
                return {
                    "tipo": "SUBSTITUICAO",
                    "Chave_Acesso": self.limpar_chave(get_t(root, "chNFSeSubst") or get_t(root, "chNFSe")),
                    "Data_Cancelamento": self._parse_data(dh_evento)
                }

            # --- 3. DETECÇÃO DE CANCELAMENTO PADRÃO ---
            if root.find(".//e101101") is not None or tp_evento in ["110111", "101101"]:
                ch = get_t(root, "chNFSe") or get_t(root, "chDFSe")
                return {
                "tipo": "CANCELAMENTO",
                    "Chave_Acesso": self.limpar_chave(ch),
                    "Data_Cancelamento": self._parse_data(dh_evento)
                }

            # --- 4. DETECÇÃO DE EMISSÃO ---
            if root.find(".//nNFSe") is not None or root.find(".//infNFSe") is not None:
                infNFSe = root.find(".//infNFSe")
                chave = infNFSe.attrib.get('Id', '') if infNFSe is not None else get_t(root, "chNFSe")
                emit, toma = root.find(".//emit"), root.find(".//toma")
                
                # Verifica se esta nota nova está substituindo uma antiga
                chave_substituida = get_t(root, "chNFSeSubst")

                return {
                    "tipo": "EMISSAO",
                    "Chave_Acesso": self.limpar_chave(chave),
                    "Chave_Substituida": self.limpar_chave(chave_substituida), # Opcional: para log
                    "Numero_Nota": self.limpar_numero_nota(get_t(root, "nNFSe")),
                    "Data_Emissao": self._parse_data(get_t(root, "dhEmi")),
                    "Emitente_CNPJ": get_t(emit, "CNPJ") if emit is not None else "",
                    "Emitente_Nome": get_t(emit, "xNome") if emit is not None else "",
                    "Tomador_CNPJ": (get_t(toma, "CNPJ") or get_t(toma, "CPF")) if toma is not None else "",
                    "Tomador_Nome": get_t(toma, "xNome") if toma is not None else "",
                    "Servico_Descricao": get_t(root, "xDescServ"),
                    "Valor_Servico": float(get_t(root, "vServ") or 0),
                    "Valor_BC": float(get_t(root, "vBC") or 0),
                    "ISS_Retido": get_t(root, "tpRetISSQN"),
                    "Valor_ISS": float(get_t(root, "vISSQN") or 0),
                    "Valor_Liquido": float(get_t(root, "vLiq") or 0)
                }
            
            return None
        except Exception as e: 
            return None

    def fechar(self):
        if self.conn: self.conn.close()

if __name__ == "__main__":
    for comp_config in COMPANIES:
        print(f"\n>>> Iniciando consulta para: {comp_config['nome']}")
        api = GovNFSeAPI(comp_config)
        try:
            ultimo = api.obter_ultimo_nsu()
            nsu_atual = (ultimo + 1) if ultimo else 1
            print(f"Iniciando a partir do NSU: {nsu_atual}")

            while True:
                res = api.consultar_e_processar(nsu_atual)
                if res == "FIM": 
                    print(f"Fim de dados para {comp_config['nome']}")
                    break
                elif res == "WAIT": 
                    print("Limite de requisições atingido (429). Aguardando...")
                    time.sleep(15)
                    continue
                elif isinstance(res, tuple): 
                    nsu_atual = res[1] + 1
                else: 
                    nsu_atual += 1

                time.sleep(2)
        finally:
            api.fechar()