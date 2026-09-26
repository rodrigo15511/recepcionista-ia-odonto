"""
limite.py — orçamento de requisições e cache para a API do Clinicorp.

O problema: 25 requisições por hora. Não é pouco, é MUITO pouco.
Uma conversa de agendamento gasta de 3 a 7 requisições. Sem controle,
três pacientes simultâneos derrubam o atendimento da clínica inteira
pela hora seguinte.

Três ideias aqui dentro, nessa ordem de importância:

  1. ORÇAMENTO COM PRIORIDADE — não é um contador que bloqueia em 25.
     Leitura e escrita não valem a mesma coisa. Um paciente perguntando
     "quais dias vocês têm?" pode esperar; um paciente confirmando
     "quinta às 14h, pode marcar" não pode. Por isso a leitura tem um
     teto MAIS BAIXO que a escrita: sempre sobra orçamento para fechar
     o agendamento de quem já decidiu.

  2. JANELA DESLIZANTE — a gente não sabe se o Clinicorp conta por hora
     cheia (zera às 15h00) ou pelos últimos 60 minutos. A janela
     deslizante é a hipótese conservadora: se eles contam por hora cheia,
     a gente só gasta menos do que podia. O contrário quebraria.

  3. CACHE COM INVALIDAÇÃO EXPLÍCITA — TTL é o plano B. O plano A é o
     webhook do Clinicorp: quando alguém marca ou cancela pelo balcão,
     ele avisa, e aí a gente apaga só o que mudou. Por isso `invalidar()`
     existe desde já, mesmo sem webhook ligado ainda.

Estado persiste em disco. Se o programa reiniciar, o contador NÃO pode
voltar a zero — o Clinicorp continua contando.
"""

import fnmatch
import json
import time
from pathlib import Path

# ---------------------------------------------------------------
# Os números. Todos conservadores, e o motivo de cada um.
# ---------------------------------------------------------------

LIMITE_POR_HORA = 25      # o que o suporte do Clinicorp confirmou
MARGEM_SEGURANCA = 3      # não sabemos o relógio deles; retry conta; erro de contagem acontece
TETO_ESCRITA = LIMITE_POR_HORA - MARGEM_SEGURANCA          # 22
RESERVA_ESCRITA = 6       # ~6 agendamentos/hora é o pico realista de uma clínica de 4 dentistas
TETO_LEITURA = TETO_ESCRITA - RESERVA_ESCRITA              # 16
JANELA_SEGUNDOS = 3600


class SemOrcamento(Exception):
    """Acabou a cota da hora. Não é bug: é o sistema se protegendo."""

    def __init__(self, prioridade, segundos_ate_liberar):
        self.prioridade = prioridade
        self.segundos_ate_liberar = int(segundos_ate_liberar)
        super().__init__(
            f"sem orçamento de {prioridade}; libera em "
            f"{self.segundos_ate_liberar}s"
        )


# ---------------------------------------------------------------
# ORÇAMENTO
# ---------------------------------------------------------------

class Orcamento:
    """
    Guarda o carimbo de tempo de cada requisição feita na última hora.

    `relogio` é injetável só para os testes: na simulação eu passo um
    relógio falso que anda rápido. Em produção fica o time.time mesmo.
    """

    def __init__(self, arquivo="estado_orcamento.json", relogio=time.time):
        self.arquivo = Path(arquivo)
        self.relogio = relogio
        self.carimbos = []
        self._carregar()

    def _carregar(self):
        if not self.arquivo.exists():
            return
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
            self.carimbos = [float(c) for c in dados.get("carimbos", [])]
        except (json.JSONDecodeError, ValueError, OSError):
            # arquivo corrompido: melhor recomeçar do zero do que travar.
            # O risco é gastar a mais nesta hora; aceitável, e raro.
            self.carimbos = []

    def _salvar(self):
        try:
            self.arquivo.write_text(
                json.dumps({"carimbos": self.carimbos}), encoding="utf-8"
            )
        except OSError:
            pass  # não vale derrubar o atendimento por falha de escrita de log

    def _podar(self):
        corte = self.relogio() - JANELA_SEGUNDOS
        self.carimbos = [c for c in self.carimbos if c > corte]

    def usados(self):
        self._podar()
        return len(self.carimbos)

    def teto(self, prioridade):
        return TETO_ESCRITA if prioridade == "escrita" else TETO_LEITURA

    def restante(self, prioridade="leitura"):
        return max(0, self.teto(prioridade) - self.usados())

    def pode(self, prioridade="leitura"):
        return self.usados() < self.teto(prioridade)

    def espera(self, prioridade="leitura"):
        """Quantos segundos até liberar uma vaga para essa prioridade."""
        self._podar()
        teto = self.teto(prioridade)
        if len(self.carimbos) < teto:
            return 0
        # a vaga abre quando o carimbo mais antigo *acima do teto* expirar
        alvo = sorted(self.carimbos)[len(self.carimbos) - teto]
        return max(0, alvo + JANELA_SEGUNDOS - self.relogio())

    def registrar(self):
        """
        Chame ANTES da requisição, nunca depois.

        Uma chamada que dá timeout mesmo assim chegou no servidor deles
        e mesmo assim contou. Registrar só no sucesso é a forma mais
        comum de estourar o limite sem entender por quê.
        """
        self.carimbos.append(self.relogio())
        self._salvar()

    def exigir(self, prioridade="leitura"):
        if not self.pode(prioridade):
            raise SemOrcamento(prioridade, self.espera(prioridade))
        self.registrar()


# ---------------------------------------------------------------
# CACHE
# ---------------------------------------------------------------

FRESCO, VELHO, VAZIO = "fresco", "velho", "vazio"


class Cache:
    """
    Cache chave -> valor com validade.

    Estados de uma consulta:
      FRESCO — dentro do TTL, use sem pensar
      VELHO  — expirou, mas o valor antigo ainda está aqui. Serve como
               rede de segurança quando a API caiu ou o orçamento acabou.
      VAZIO  — nunca vimos essa chave

    A distinção VELHO/VAZIO é o que permite degradar em vez de falhar.
    """

    def __init__(self, arquivo="estado_cache.json", relogio=time.time):
        self.arquivo = Path(arquivo)
        self.relogio = relogio
        self.itens = {}
        self.acertos = 0
        self.erros = 0
        self._carregar()

    def _carregar(self):
        if not self.arquivo.exists():
            return
        try:
            self.itens = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.itens = {}

    def _salvar(self):
        try:
            self.arquivo.write_text(
                json.dumps(self.itens, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def obter(self, chave):
        item = self.itens.get(chave)
        if item is None:
            self.erros += 1
            return None, VAZIO
        idade = self.relogio() - item["gravado_em"]
        if idade <= item["ttl"]:
            self.acertos += 1
            return item["valor"], FRESCO
        self.erros += 1
        return item["valor"], VELHO

    def guardar(self, chave, valor, ttl):
        self.itens[chave] = {
            "valor": valor,
            "ttl": ttl,
            "gravado_em": self.relogio(),
        }
        self._salvar()

    def remendar(self, chave, transformar):
        """
        Atualiza o valor guardado SEM gastar requisição.

        Depois de um agendamento nosso, a gente sabe exatamente o que mudou:
        um horário saiu da lista. Jogar o cache fora nesse momento é
        desperdício — a próxima pessoa que perguntar vai gastar uma
        requisição para descobrir algo que já sabíamos.

        Repare que `gravado_em` NÃO é atualizado. O remendo corrige o que
        nós fizemos; ele não nos deixa mais informados sobre o que a
        secretária fez no balcão. A idade do dado continua a mesma, e o
        TTL continua contando. Isso é proposital.
        """
        item = self.itens.get(chave)
        if item is None:
            return False
        item["valor"] = transformar(item["valor"])
        self._salvar()
        return True

    def invalidar(self, padrao):
        """
        Apaga tudo que casa com o padrão. Aceita curinga:
            invalidar("horarios:lucas:*")

        É isso que o webhook do Clinicorp vai chamar quando a secretária
        marcar alguém pelo balcão. Enquanto o webhook não existe, chamamos
        manualmente depois de toda escrita nossa.
        """
        alvos = [c for c in self.itens if fnmatch.fnmatch(c, padrao)]
        for c in alvos:
            del self.itens[c]
        if alvos:
            self._salvar()
        return len(alvos)


# ---------------------------------------------------------------
# O CLIENTE — junta os dois
# ---------------------------------------------------------------

class Cliente:
    """
    Toda conversa com o Clinicorp passa por aqui. Nenhuma exceção.

    Se algum dia você escrever um `requests.get` direto para o Clinicorp
    em qualquer outro lugar do projeto, o orçamento vira ficção.
    """

    def __init__(self, orcamento=None, cache=None, verboso=False):
        self.orcamento = orcamento or Orcamento()
        self.cache = cache or Cache()
        self.verboso = verboso
        self.origens = {"cache": 0, "api": 0, "velho": 0, "recusado": 0}

    def _log(self, msg):
        if self.verboso:
            print(f"    [clinicorp] {msg}")

    def ler(self, chave, ttl, buscar, aceitar_velho=True):
        """
        Leitura: cache primeiro, API só se precisar, valor velho se não puder.

        Devolve (valor, origem). A origem importa para a resposta ao
        paciente: com valor velho, a recepcionista deve dizer "vou
        confirmar e já te falo", não "está reservado".
        """
        valor, estado = self.cache.obter(chave)
        if estado == FRESCO:
            self.origens["cache"] += 1
            self._log(f"cache {chave}")
            return valor, "cache"

        if not self.orcamento.pode("leitura"):
            if estado == VELHO and aceitar_velho:
                self.origens["velho"] += 1
                self._log(f"SEM ORÇAMENTO -> valor velho de {chave}")
                return valor, "velho"
            self.origens["recusado"] += 1
            raise SemOrcamento("leitura", self.orcamento.espera("leitura"))

        self.orcamento.registrar()   # antes da chamada, sempre
        self._log(f"API {chave}  (usados={self.orcamento.usados()})")
        try:
            novo = buscar()
        except Exception:
            if estado == VELHO and aceitar_velho:
                self.origens["velho"] += 1
                self._log(f"erro na API -> valor velho de {chave}")
                return valor, "velho"
            raise
        self.origens["api"] += 1
        self.cache.guardar(chave, novo, ttl)
        return novo, "api"

    def escrever(self, executar, invalidar=()):
        """
        Escrita: nunca vem do cache, sempre gasta orçamento, e invalida
        o que ficou desatualizado logo depois.
        """
        self.orcamento.exigir("escrita")
        self._log(f"API ESCRITA  (usados={self.orcamento.usados()})")
        resultado = executar()
        for padrao in invalidar:
            self.cache.invalidar(padrao)
        return resultado

    def resumo(self):
        total = sum(self.origens.values())
        return {
            **self.origens,
            "total_pedidos": total,
            "requisicoes_gastas": self.orcamento.usados(),
        }
