"""
Recepcionista da clínica - versão 3.

O que mudou em relação à v2, e só isso:

  1. A agenda falsa saiu deste arquivo e virou `agenda.BackendFalso`.
     Quando as credenciais do Clinicorp chegarem, você troca UMA classe
     e este arquivo não muda nenhuma linha. Esse é o teste de que a
     separação está certa.

  2. Toda consulta passa pelo orçamento de 25 req/h e pelo cache.

  3. Uma ferramenta nova: `consultar_dias`. Antes de perguntar horários
     de um dia específico, a recepcionista descobre quais dias existem.
     Sai mais barato do que o modelo chutar datas.

  4. As ferramentas agora pedem PROCEDIMENTO, não dentista. Quem decide
     qual dentista consultar é a tabela em agenda.py, não o modelo.
     Motivo: o modelo não deve saber escolher profissional, e cada
     dentista a mais na consulta é requisição a mais.

  5. Estado novo de falha: "limite_temporario". Não é erro, é o sistema
     protegendo a cota. A recepcionista precisa saber conversar nesse
     estado sem mentir e sem assustar o paciente.

Precisa de chave.txt na mesma pasta.  Rodar:  python recepcionista_v3.py
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from agenda import DENTISTAS, Agenda, BackendFalso
from limite import Cache, Cliente, Orcamento

MODELO = "gemini-3-flash-preview"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent"

MAX_VOLTAS = 5

# ===============================================================
# A CAMADA DE DADOS
# ===============================================================

AGENDA = Agenda(
    backend=BackendFalso(),
    cliente=Cliente(Orcamento(), Cache(), verboso=True),
)


# ===============================================================
# AS FERRAMENTAS
# ===============================================================

DECLARACOES = [
    {
        "name": "consultar_dias",
        "description": (
            "Descobre em quais dias a clínica tem horário livre para um "
            "procedimento. Use ANTES de consultar_horarios. Exige saber "
            "o procedimento: se o paciente não disse, pergunte primeiro."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "procedimento": {
                    "type": "STRING",
                    "description": "limpeza, avaliacao, clareamento, restauracao, "
                                   "implante, extracao, aparelho, protese, lentes",
                }
            },
            "required": ["procedimento"],
        },
    },
    {
        "name": "consultar_horarios",
        "description": (
            "Consulta os horários livres em uma data. Use só com uma data "
            "que veio de consultar_dias. Nunca invente horários."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "procedimento": {"type": "STRING", "description": "o mesmo procedimento"},
                "data": {"type": "STRING", "description": "Data AAAA-MM-DD"},
            },
            "required": ["procedimento", "data"],
        },
    },
    {
        "name": "criar_agendamento",
        "description": (
            "Reserva o horário. Só chame depois que o paciente confirmar "
            "explicitamente dia, hora e procedimento, e você souber o nome dele."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "dentista": {"type": "STRING",
                             "description": "chave do dentista devolvida por consultar_horarios"},
                "data": {"type": "STRING", "description": "Data AAAA-MM-DD"},
                "hora": {"type": "STRING", "description": "Hora HH:MM"},
                "nome": {"type": "STRING", "description": "Nome do paciente"},
                "telefone": {"type": "STRING", "description": "Telefone do paciente"},
                "procedimento": {"type": "STRING", "description": "o procedimento"},
            },
            "required": ["dentista", "data", "hora", "nome", "telefone", "procedimento"],
        },
    },
]


def ferramenta_consultar_dias(args):
    r = AGENDA.dias_livres(str(args.get("procedimento", "")))
    if r.get("erro") == "limite_temporario":
        return {"erro": "sistema_ocupado", "minutos": r["segundos"] // 60 + 1}
    return {"dias": r["dias"][:8], "aproximado": r.get("aproximado", False)}


def ferramenta_consultar_horarios(args):
    r = AGENDA.horarios(str(args.get("procedimento", "")),
                        str(args.get("data", "")).strip())
    if r.get("erro") == "limite_temporario":
        return {"erro": "sistema_ocupado", "minutos": r["segundos"] // 60 + 1}
    if not r["por_dentista"]:
        return {"erro": "sem horário livre nessa data", "por_dentista": {}}
    return {
        "data": r["data"],
        "por_dentista": {
            chave: {"nome": DENTISTAS.get(chave, chave), "horarios": horas}
            for chave, horas in r["por_dentista"].items()
        },
        "aproximado": r.get("aproximado", False),
    }


def ferramenta_criar_agendamento(args):
    """
    O modelo já DECIDIU agendar. Tudo aqui é regra determinística que
    decide se ele PODE. Repare que a validação não confia em nada do
    que o modelo mandou: revalida contra a fonte.
    """
    dentista = str(args.get("dentista", "")).strip().lower()
    dia = str(args.get("data", "")).strip()
    hora = str(args.get("hora", "")).strip()
    nome = str(args.get("nome", "")).strip()
    telefone = str(args.get("telefone", "")).strip()
    proc = str(args.get("procedimento", "")).strip()

    if not nome:
        return {"ok": False, "motivo": "nome do paciente não informado"}
    if not telefone:
        return {"ok": False, "motivo": "telefone não informado"}
    if dentista not in DENTISTAS:
        return {"ok": False, "motivo": "dentista desconhecido"}
    if dentista not in AGENDA.dentistas_para(proc):
        return {"ok": False, "motivo": "esse dentista não faz esse procedimento"}
    try:
        h = int(hora.split(":")[0])
    except ValueError:
        return {"ok": False, "motivo": "hora em formato inválido"}
    if not (8 <= h < 18):
        return {"ok": False, "motivo": "fora do horário de funcionamento"}

    paciente = AGENDA.paciente_por_telefone(telefone)
    resultado = AGENDA.agendar(dentista, dia, hora,
                               paciente.get("patient_id"), proc)
    if resultado.get("motivo") == "limite_temporario":
        return {"ok": False, "motivo": "sistema_ocupado",
                "minutos": resultado["segundos"] // 60 + 1}
    registrar_acao("criar_agendamento", args, resultado)
    return resultado


FERRAMENTAS = {
    "consultar_dias": ferramenta_consultar_dias,
    "consultar_horarios": ferramenta_consultar_horarios,
    "criar_agendamento": ferramenta_criar_agendamento,
}


# ===============================================================
# INSTRUÇÕES
# ===============================================================

CONHECIMENTO = """
Endereço: Rua Exemplo, 123 - Centro.
Horário: segunda a sexta das 8h às 18h; sábado das 8h às 12h. Domingo fechado.
Estacionamento: próprio e gratuito.
Pagamento: dinheiro, Pix, débito e crédito.
Procedimentos: limpeza, restauração, clareamento, lentes, prótese, implante,
extração, aparelho e alinhadores.
"""

INSTRUCOES = f"""
Você é a recepcionista virtual de uma clínica odontológica.
Fala por WhatsApp, em português brasileiro, curto e cordial.
Hoje é {date.today().isoformat()} ({date.today():%A}).

INFORMAÇÕES DA CLÍNICA:
{CONHECIMENTO}

REGRAS ABSOLUTAS:
- Nunca diagnostique, nunca interprete sintoma, nunca indique tratamento.
- Nunca informe preço fechado. O valor depende de avaliação.
- Dúvida clínica: diga que precisa de avaliação e ofereça agendar.

REGRAS DE AGENDAMENTO:
- Descubra o PROCEDIMENTO antes de consultar qualquer agenda. Uma pergunta
  a mais para o paciente custa menos que uma consulta errada ao sistema.
- Ordem obrigatória: consultar_dias -> consultar_horarios -> criar_agendamento.
- NUNCA ofereça um horário que não veio de consultar_horarios.
- Peça nome e confirme o telefone antes de criar_agendamento.
- Ofereça no máximo 3 horários por vez. Lista longa confunde no WhatsApp.

QUANDO A FERRAMENTA DEVOLVER erro "sistema_ocupado":
- Não repita a chamada. Não invente horário. Não prometa.
- Diga que vai confirmar a agenda em alguns minutos e que retorna.

QUANDO A FERRAMENTA DEVOLVER "aproximado": true:
- Os horários podem estar desatualizados. Ofereça normalmente, mas diga
  que confirma a reserva em seguida. Nunca diga "está reservado".
"""


# ===============================================================
# CHAMADA AO MODELO  (igual à v2)
# ===============================================================

def carregar_chave():
    chave = os.environ.get("GEMINI_API_KEY")
    if chave:
        return chave.strip()
    try:
        with open("chave.txt", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise SystemExit("Não achei chave.txt na pasta.")


def chamar_modelo(historico, chave, tentativas=3):
    corpo = {
        "systemInstruction": {"parts": [{"text": INSTRUCOES}]},
        "contents": historico,
        "tools": [{"functionDeclarations": DECLARACOES}],
    }
    for n in range(tentativas):
        req = urllib.request.Request(
            URL,
            data=json.dumps(corpo).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": chave},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detalhe = e.read().decode("utf-8", "replace")[:800]
            print(f"  [HTTP {e.code}] {detalhe}")
            if e.code < 500 and e.code != 429:
                raise
            if n == tentativas - 1:
                raise
            time.sleep(2 ** n)
        except (TimeoutError, urllib.error.URLError) as e:
            if n == tentativas - 1:
                raise
            espera = 2 ** n
            print(f"  (falha: {type(e).__name__}. tentando em {espera}s)")
            time.sleep(espera)


def separar_partes(resposta_api):
    partes = resposta_api["candidates"][0]["content"].get("parts", [])
    texto, chamadas = [], []
    for p in partes:
        if "text" in p:
            texto.append(p["text"])
        elif "functionCall" in p:
            chamadas.append(p["functionCall"])
    return "".join(texto).strip(), chamadas, partes


# ===============================================================
# VALIDAÇÃO E LOG
# ===============================================================

TERMOS_PROIBIDOS = [
    "r$", "reais", "custa ",
    "você tem", "voce tem", "parece ser", "provavelmente é",
    "recomendo que você", "tome ", "use ",
]

RESPOSTA_SEGURA = ("Vou passar sua mensagem para a equipe da clínica, "
                   "eles te respondem em breve.")


def validar(resposta):
    r = resposta.lower()
    for termo in TERMOS_PROIBIDOS:
        if termo in r:
            return False, f"termo proibido: {termo.strip()!r}"
    return True, "ok"


def registrar(tipo, detalhe):
    with open("conversas.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {tipo} | {detalhe}\n")


def registrar_acao(nome, args, resultado):
    registrar("ACAO", f"{nome} args={args} -> {resultado}")


# ===============================================================
# O LAÇO DO AGENTE  (igual à v2)
# ===============================================================

def processar(mensagem, historico, chave):
    historico.append({"role": "user", "parts": [{"text": mensagem}]})

    for volta in range(MAX_VOLTAS):
        try:
            bruto = chamar_modelo(historico, chave)
            texto, chamadas, partes_originais = separar_partes(bruto)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            registrar("erro_rede", f"{type(e).__name__}: {e}")
            return RESPOSTA_SEGURA
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            registrar("erro_formato", f"{type(e).__name__}: {e}")
            return RESPOSTA_SEGURA

        if not chamadas:
            historico.append({"role": "model", "parts": [{"text": texto}]})
            aprovado, motivo = validar(texto)
            final = texto if aprovado else RESPOSTA_SEGURA
            registrar("RESPOSTA", f"aprovado={aprovado} | {motivo} | {final!r}")
            return final

        partes_resultado = []
        for c in chamadas:
            nome = c.get("name")
            args = c.get("args", {}) or {}
            print(f"  [ferramenta] {nome}({args})")
            if nome not in FERRAMENTAS:
                resultado = {"erro": "ferramenta inexistente"}
            else:
                resultado = FERRAMENTAS[nome](args)
            partes_resultado.append(
                {"functionResponse": {"name": nome, "response": resultado}}
            )

        historico.append({"role": "model", "parts": partes_originais})
        historico.append({"role": "user", "parts": partes_resultado})

    registrar("erro_loop", f"passou de {MAX_VOLTAS} voltas")
    return RESPOSTA_SEGURA


def main():
    chave = carregar_chave()
    historico = []
    print("Recepcionista v3. 'agenda' mostra reservas, 'cota' mostra o "
          "orçamento, 'sair' encerra.\n")
    while True:
        mensagem = input("paciente> ").strip()
        if mensagem.lower() in ("sair", "exit", "quit", ""):
            print("Até logo.")
            break
        if mensagem.lower() == "agenda":
            print(json.dumps(AGENDA.backend.reservas, indent=2, ensure_ascii=False), "\n")
            continue
        if mensagem.lower() == "cota":
            print(json.dumps(AGENDA.cliente.resumo(), indent=2), "\n")
            continue
        print(f"bot> {processar(mensagem, historico, chave)}\n")


if __name__ == "__main__":
    main()
