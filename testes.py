"""
testes.py — prova que o orçamento e o cache fazem o que prometem.

Rodar:  python testes.py

Repare no que NÃO está aqui: nenhum teste do modelo. LLM é
probabilístico; `assert` em resposta de modelo é teste que passa hoje e
falha amanhã sem nada ter mudado. O que se testa com assert é a camada
determinística — que é exatamente esta.
"""

import os
import tempfile

from agenda import Agenda, BackendFalso
from limite import (JANELA_SEGUNDOS, TETO_ESCRITA, TETO_LEITURA, Cache,
                    Cliente, Orcamento, SemOrcamento)


class Relogio:
    def __init__(self, t=1000.0):
        self.agora = t

    def __call__(self):
        return self.agora


def arquivo_temp(nome):
    return os.path.join(tempfile.mkdtemp(), nome)


def teste_teto_de_leitura_e_menor_que_o_de_escrita():
    r = Relogio()
    o = Orcamento(arquivo_temp("o.json"), relogio=r)
    for _ in range(TETO_LEITURA):
        o.registrar()
    assert not o.pode("leitura"), "leitura deveria ter acabado"
    assert o.pode("escrita"), "escrita ainda deveria ter reserva"
    print("ok  reserva de escrita protege quem já decidiu agendar")


def teste_escrita_tambem_tem_limite():
    r = Relogio()
    o = Orcamento(arquivo_temp("o.json"), relogio=r)
    for _ in range(TETO_ESCRITA):
        o.registrar()
    assert not o.pode("escrita")
    try:
        o.exigir("escrita")
    except SemOrcamento as e:
        assert e.segundos_ate_liberar > 0
        print("ok  escrita trava no teto e informa quando libera")
    else:
        raise AssertionError("deveria ter levantado SemOrcamento")


def teste_janela_desliza():
    r = Relogio()
    o = Orcamento(arquivo_temp("o.json"), relogio=r)
    for _ in range(TETO_LEITURA):
        o.registrar()
    assert not o.pode("leitura")
    r.agora += JANELA_SEGUNDOS + 1
    assert o.usados() == 0, "carimbos velhos deveriam ter sido podados"
    assert o.pode("leitura")
    print("ok  janela de 1h desliza e libera cota")


def teste_sobrevive_a_reinicio():
    caminho = arquivo_temp("o.json")
    r = Relogio()
    o1 = Orcamento(caminho, relogio=r)
    for _ in range(10):
        o1.registrar()
    o2 = Orcamento(caminho, relogio=r)          # "programa reiniciou"
    assert o2.usados() == 10, f"esperava 10, veio {o2.usados()}"
    print("ok  contador não zera quando o programa reinicia")


def teste_cache_fresco_velho_vazio():
    r = Relogio()
    c = Cache(arquivo_temp("c.json"), relogio=r)
    assert c.obter("x") == (None, "vazio")
    c.guardar("x", [1, 2], ttl=60)
    assert c.obter("x") == ([1, 2], "fresco")
    r.agora += 61
    valor, estado = c.obter("x")
    assert estado == "velho" and valor == [1, 2], "valor velho deve continuar acessível"
    print("ok  cache distingue fresco / velho / vazio")


def teste_valor_velho_salva_quando_a_cota_acaba():
    r = Relogio()
    o = Orcamento(arquivo_temp("o.json"), relogio=r)
    c = Cache(arquivo_temp("c.json"), relogio=r)
    cli = Cliente(o, c)

    cli.ler("k", ttl=10, buscar=lambda: ["08:00"])
    r.agora += 11                                  # cache envelheceu
    for _ in range(TETO_LEITURA):
        o.registrar()                              # cota de leitura acabou

    valor, origem = cli.ler("k", ttl=10, buscar=lambda: ["NUNCA"])
    assert origem == "velho" and valor == ["08:00"]
    print("ok  sem cota, responde com dado velho em vez de falhar")


def teste_requisicao_conta_mesmo_se_der_erro():
    r = Relogio()
    o = Orcamento(arquivo_temp("o.json"), relogio=r)
    cli = Cliente(o, Cache(arquivo_temp("c.json"), relogio=r))

    def explode():
        raise TimeoutError("estourou")

    try:
        cli.ler("k", ttl=10, buscar=explode)
    except TimeoutError:
        pass
    assert o.usados() == 1, "timeout também chegou no servidor deles"
    print("ok  requisição que falha continua consumindo cota")


def teste_remendo_evita_requisicao_nova():
    r = Relogio()
    backend = BackendFalso()
    cli = Cliente(Orcamento(arquivo_temp("o.json"), relogio=r),
                  Cache(arquivo_temp("c.json"), relogio=r))
    ag = Agenda(backend, cli)

    dia = ag.dias_livres("limpeza")["dias"][0]
    h = ag.horarios("limpeza", dia)
    dentista, horas = next(iter(h["por_dentista"].items()))
    gasto_antes = backend.chamadas

    ag.agendar(dentista, dia, horas[0], "P1", "limpeza")
    depois = ag.horarios("limpeza", dia)

    gasto = backend.chamadas - gasto_antes
    assert gasto == 1, f"só a escrita deveria gastar; gastou {gasto}"
    assert horas[0] not in depois["por_dentista"].get(dentista, []), \
        "o horário reservado deveria ter sumido do cache"
    print("ok  remendo mantém o cache correto sem gastar requisição")


def teste_recusa_do_clinicorp_invalida_o_cache():
    r = Relogio()
    backend = BackendFalso()
    cli = Cliente(Orcamento(arquivo_temp("o.json"), relogio=r),
                  Cache(arquivo_temp("c.json"), relogio=r))
    ag = Agenda(backend, cli)

    dia = ag.dias_livres("limpeza")["dias"][0]
    h = ag.horarios("limpeza", dia)
    dentista, horas = next(iter(h["por_dentista"].items()))

    backend.agenda[dentista][dia].remove(horas[0])       # alguém marcou no balcão
    res = ag.agendar(dentista, dia, horas[0], "P1", "limpeza")
    assert not res["ok"]
    valor, estado = cli.cache.obter(f"horarios:{dentista}:{dia}")
    assert estado == "vazio", "cache errado tem que ser jogado fora, não remendado"
    print("ok  recusa do Clinicorp invalida o cache mentiroso")


def teste_roteamento_limita_dentistas_consultados():
    ag = Agenda(BackendFalso())
    assert ag.dentistas_para("implante") == ["rafael"]
    assert len(ag.dentistas_para("avaliacao")) <= 2, \
        "avaliação genérica não pode consultar os 4 dentistas"
    print("ok  roteamento por procedimento segura o número de consultas")


if __name__ == "__main__":
    for nome, fn in list(globals().items()):
        if nome.startswith("teste_"):
            fn()
    print("\ntudo passou.")
