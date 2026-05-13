import os
import requests
import ssl
import xml.etree.ElementTree as ET
import io
import base64
import gzip
from fastapi import FastAPI, HTTPException, Response
from fpdf import FPDF
from dotenv import load_dotenv

load_dotenv()

# --- ADAPTER SSL (Original do seu script) ---
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

# --- CLASSE PARA GERAR O PDF (Layout Simples DANFSe) ---
class DANFSeGenerator(FPDF):
    def header(self):
        self.set_font("Arial", "B", 12)
        self.cell(0, 10, "DANFSe - Documento Auxiliar da NFS-e Nacional", 1, 1, "C")
        self.ln(5)

    def draw_box(self, title, content):
        self.set_font("Arial", "B", 8)
        self.cell(0, 5, title, "TLR", 1)
        self.set_font("Arial", "", 10)
        self.multi_cell(0, 7, str(content), "BLR", "L")
        self.ln(2)

app = FastAPI()

COMPANIES = {
    1: {
        "nome": "INTERLINK",
        "cert_pem": os.getenv('CERT_PATH_pem_INTERLINK'),
        "cert_key": os.getenv('CERT_PATH_key_INTERLINK'),
        "cert_pwd": os.getenv('CERT_PASSWORD_INTERLINK'),
    },
    2: {
        "nome": "E-CARGO",
        "cert_pem": os.getenv('CERT_PATH_pem_ECARGO'),
        "cert_key": os.getenv('CERT_PATH_key_ECARGO'),
        "cert_pwd": os.getenv('CERT_PASSWORD_ECARGO'),
    }
}

# Função para extrair dados do XML (Baseada no seu script original)
def parse_xml_nfse(xml_string):
    it = ET.iterparse(io.StringIO(xml_string))
    for _, el in it:
        if '}' in el.tag: el.tag = el.tag.split('}', 1)[1]
    root = it.root

    def get_t(node, tag):
        if node is None: return ""
        found = node.find(f".//{tag}")
        return found.text.strip() if found is not None and found.text else ""

    emit = root.find(".//emit")
    toma = root.find(".//toma")
    
    return {
        "chave": get_t(root, "chNFSe") or "N/A",
        "numero": get_t(root, "nNFSe") or "N/A",
        "emissao": get_t(root, "dhEmi") or "N/A",
        "emit_nome": get_t(emit, "xNome"),
        "emit_cnpj": get_t(emit, "CNPJ") or get_t(emit, "CPF"),
        "toma_nome": get_t(toma, "xNome"),
        "toma_cnpj": get_t(toma, "CNPJ") or get_t(toma, "CPF"),
        "servico": get_t(root, "xDescServ"),
        "valor": get_t(root, "vServ") or "0.00",
        "valor_liq": get_t(root, "vLiq") or "0.00",
        "iss": get_t(root, "vISSQN") or "0.00"
    }

@app.get("/nfse/pdf/{empresa_id}/{chave_acesso}")
async def api_gerar_pdf(empresa_id: int, chave_acesso: str):
    if empresa_id not in COMPANIES:
        raise HTTPException(status_code=400, detail="Empresa não cadastrada.")
    
    config = COMPANIES[empresa_id]
    chave_limpa = "".join(filter(str.isdigit, chave_acesso))
    id_recurso = f"NFSe{chave_limpa}"

    # 1. BUSCAR XML NO GOVERNO
    session = requests.Session()
    session.mount("https://", SSLAdapter(config['cert_pem'], config['cert_key'], config['cert_pwd']))
    
    # Endpoint de consulta de XML pela chave
    url = f"https://adn.nfse.gov.br/contribuintes/NFSe/{id_recurso}"
    
    try:
        response = session.get(url, timeout=30)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Não foi possível obter o XML do governo.")
        
        xml_bruto = response.text
        
        # Se o governo retornar um JSON (como no seu script original de NSU), 
        # precisamos extrair o 'ArquivoXml' em base64/gzip. 
        # Mas no endpoint direto de chave, ele costuma retornar o XML puro.
        if response.headers.get('Content-Type') == 'application/json':
            xml_base64 = response.json().get('ArquivoXml')
            xml_bruto = gzip.decompress(base64.b64decode(xml_base64)).decode('utf-8')

        # 2. PROCESSAR DADOS
        dados = parse_xml_nfse(xml_bruto)

        # 3. GERAR PDF LOCALMENTE
        pdf = DANFSeGenerator()
        pdf.add_page()
        
        pdf.draw_box("CHAVE DE ACESSO", dados['chave'])
        pdf.draw_box("NÚMERO DA NOTA", dados['numero'])
        pdf.draw_box("DATA DE EMISSÃO", dados['emissao'])
        
        pdf.ln(5)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 10, "DADOS DO EMITENTE", 0, 1)
        pdf.draw_box("Nome/Razão Social", dados['emit_nome'])
        pdf.draw_box("CNPJ/CPF", dados['emit_cnpj'])

        pdf.ln(5)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 10, "DADOS DO TOMADOR", 0, 1)
        pdf.draw_box("Nome/Razão Social", dados['toma_nome'])
        pdf.draw_box("CNPJ/CPF", dados['toma_cnpj'])

        pdf.ln(5)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 10, "DESCRIÇÃO DOS SERVIÇOS", 0, 1)
        pdf.draw_box("Serviços", dados['servico'])

        pdf.ln(5)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(0, 10, "VALORES", 0, 1)
        pdf.draw_box("Valor Bruto: R$", dados['valor'])
        pdf.draw_box("Valor Líquido: R$", dados['valor_liq'])
        pdf.draw_box("Valor ISS: R$", dados['iss'])

        # Retornar o PDF binário
        pdf_bytes = pdf.output()
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=DANFSe_{chave_limpa}.pdf"}
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao processar: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)