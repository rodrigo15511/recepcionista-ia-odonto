"""
simulacao.py — a pergunta que precisa ser respondida ANTES de escrever
o resto do sistema: com 25 requisições por hora, esse atendimento cabe?

Roda um dia inteiro de conversas com relógio falso, duas vezes:
  A) sem cache, sem orçamento — cada pergunta vira requisição
  B) com cache e orçamento com prioridade

Mede: requisições gastas, pico na pior hora, quantas vezes o sistema
teve que recusar ou responder com dado velho, e quantos agendamentos
foram perdidos.

Agendamento perdido é a métrica que importa. As outras são diagnóstico.
"""

import random

from agenda import Agenda, BackendFalso
from limite import Cache, Cliente, Orcamento, SemOrcamento

PROCS = ["limpeza", "avaliacao", "clareamento", "restauracao",
         "implante", "aparelho", "protese", "extracao"]


class Relogio:
    def __init__(self, inicio=0.0):
        self.agora = inicio

    def __call__(self):
        return self.agora


def gerar_dia(n_conversas, semente):
    """
    Distribui conversas ao longo de 10h (8h-18h), com pico no meio da
    manhã e no fim da tarde — que é quando clínica recebe mensagem.
    """
    rnd = random.Random(semente)
    pesos = [3, 6, 8, 7, 5, 3, 4, 6, 8, 5]      # uma entrada por hora
    conversas = []
    for _ in range(n_conversas):
        hora = rnd.choices(range(10), weights=pesos)[0]
        segundo = 8 * 3600 + hora * 3600 + rnd.randrange(3600)
        tipo = rnd.choices(
            ["institucional", "agendar", "remarcar", "confirmar"],
            weights=[40, 35, 10, 15],
        )[0]
        conversas.append({
            "t": segundo,
            "tipo": tipo,
            "telefone": f"6799{rnd.randrange(1000000, 9999999)}",
            "procedimento": rnd.choice(PROCS),
        })
    return sorted(conversas, key=lambda c: c["t"])


def rodar(n_conversas, semente, com_cache, adiar_paciente=False):
    relogio = Relogio()
    backend = BackendFalso()

    if com_cache:
        orc = Orcamento(arquivo="/tmp/sim_orc.json", relogio=relogio)
        orc.carimbos = []
        cache = Cache(arquivo="/tmp/sim_cache.json", relogio=relogio)
        cache.itens = {}
        agenda = Agenda(backend, Cliente(orc, cache))
    else:
        agenda = None

    marcas = []          # instante de cada requisição, para calcular o pico
    perdidos = 0
    velhos = 0
    marcados = 0

    def bater():
        marcas.append(relogio.agora)

    antes = 0
    for c in gerar_dia(n_conversas, semente):
        relogio.agora = c["t"]
        proc = c["procedimento"]

        if c["tipo"] == "institucional":
            continue

        if not com_cache:
            # caminho ingênuo: tudo direto no backend
            backend.patient_get(c["telefone"])
            for d in agenda_dentistas(proc):
                backend.get_avaliable_days(d)
            if c["tipo"] in ("agendar", "remarcar"):
                dia = sorted(backend.agenda[agenda_dentistas(proc)[0]])[0]
                for d in agenda_dentistas(proc):
                    backend.get_avaliable_times_calendar(d, dia)
                backend.create_appointment_by_api(
                    agenda_dentistas(proc)[0], dia, "08:00", "P1", proc)
                marcados += 1
            for _ in range(backend.chamadas - antes):
                bater()
            antes = backend.chamadas
            continue

        # caminho com cache
        if not adiar_paciente:
            agenda.paciente_por_telefone(c["telefone"])
        r = agenda.dias_livres(proc)
        if r.get("erro"):
            perdidos += 1
            registrar_marcas(agenda, backend, antes, bater)
            antes = backend.chamadas
            continue
        velhos += 1 if r.get("aproximado") else 0

        if c["tipo"] in ("agendar", "remarcar"):
            dia = r["dias"][0] if r["dias"] else None
            h = agenda.horarios(proc, dia) if dia else {"erro": "sem_dia"}
            if h.get("erro"):
                perdidos += 1
            else:
                escolha = next(iter(h["por_dentista"].items()), None)
                if escolha:
                    dentista, horas = escolha
                    if adiar_paciente:
                        agenda.paciente_por_telefone(c["telefone"])
                    res = agenda.agendar(dentista, dia, horas[0], "P1", proc)
                    if res.get("ok"):
                        marcados += 1
                    else:
                        perdidos += 1
        registrar_marcas(agenda, backend, antes, bater)
        antes = backend.chamadas

    return {
        "requisicoes": backend.chamadas,
        "pico_hora": pico(marcas),
        "agendados": marcados,
        "perdidos": perdidos,
        "respostas_aproximadas": velhos,
    }


def agenda_dentistas(proc):
    from agenda import MAX_DENTISTAS_POR_CONSULTA, PROCEDIMENTOS
    for chave, lista in PROCEDIMENTOS.items():
        if chave in proc or proc in chave:
            return lista[:MAX_DENTISTAS_POR_CONSULTA]
    return PROCEDIMENTOS["avaliacao"][:MAX_DENTISTAS_POR_CONSULTA]


def registrar_marcas(agenda, backend, antes, bater):
    for _ in range(backend.chamadas - antes):
        bater()


def pico(marcas):
    """Maior número de requisições em qualquer janela de 60 minutos."""
    maior = 0
    for i, m in enumerate(marcas):
        j = i
        while j < len(marcas) and marcas[j] - m < 3600:
            j += 1
        maior = max(maior, j - i)
    return maior


if __name__ == "__main__":
    print(f"{'conversas/dia':>14} | {'':>12} | {'reqs':>5} | "
          f"{'pico/h':>6} | {'agendados':>9} | {'perdidos':>8}")
    print("-" * 72)
    estrategias = [
        ("ingênuo", dict(com_cache=False)),
        ("cache", dict(com_cache=True)),
        ("cache+adiar", dict(com_cache=True, adiar_paciente=True)),
    ]
    for n in (20, 40, 60, 90):
        for rotulo, kw in estrategias:
            r = rodar(n, semente=n, **kw)
            alerta = "  <-- ESTOURA" if r["pico_hora"] > 25 else ""
            print(f"{n:>14} | {rotulo:>12} | {r['requisicoes']:>5} | "
                  f"{r['pico_hora']:>6} | {r['agendados']:>9} | "
                  f"{r['perdidos']:>8}{alerta}")
        print("-" * 72)
