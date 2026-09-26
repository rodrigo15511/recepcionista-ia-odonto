"""
agenda.py — a camada que fala com o Clinicorp.

Esta é a única parte do sistema que sabe que o Clinicorp existe. O agente
(recepcionista) nunca chama o Clinicorp: ele chama funções daqui.

Duas decisões de projeto que valem mais que o código:

  1. ROTEAMENTO POR PROCEDIMENTO.
     `get_avaliable_times_calendar` exige professionalId. Com 4 dentistas,
     "quero marcar uma avaliação" viraria 4 requisições — 25% da cota da
     hora em UMA pergunta. A tabela PROCEDIMENTOS resolve: limpeza só
     precisa consultar quem faz limpeza.

     Consequência não óbvia: a pergunta que a recepcionista faz É
     planejamento de capacidade. "Você já sabe qual procedimento?" antes
     de consultar a agenda economiza 3 requisições. O prompt do modelo
     virou parte da arquitetura de custo.

  2. TTL POR VOLATILIDADE, NÃO UM TTL SÓ.
     Lista de dentistas muda uma vez por ano. Horário livre muda a cada
     agendamento. Usar o mesmo TTL para os dois desperdiça cota de um
     lado e entrega dado errado do outro.
"""

from datetime import date, timedelta

from limite import Cliente, SemOrcamento

# ---------------------------------------------------------------
# Roteamento
# ---------------------------------------------------------------

DENTISTAS = {
    "lucas": "Dr. Lucas",
    "marina": "Dra. Marina",
    "rafael": "Dr. Rafael",
    "beatriz": "Dra. Beatriz",
}

PROCEDIMENTOS = {
    "limpeza": ["lucas", "marina"],
    "clareamento": ["lucas", "marina"],
    "restauracao": ["marina", "lucas"],
    "lentes": ["lucas"],
    "protese": ["lucas"],
    "implante": ["rafael"],
    "extracao": ["rafael"],
    "cirurgia": ["rafael"],
    "aparelho": ["beatriz"],
    "alinhador": ["beatriz"],
    # 'avaliacao' é o caso caro: sem especificar, teria que perguntar aos 4.
    # Política: avaliação genérica vai para clínica geral, e a recepcionista
    # só amplia se o paciente insistir. Uma escolha de negócio, não técnica.
    "avaliacao": ["lucas", "marina"],
}

# quantos dentistas no máximo consultar numa única pergunta
MAX_DENTISTAS_POR_CONSULTA = 2

# ---------------------------------------------------------------
# TTLs, em segundos — e o porquê de cada um
# ---------------------------------------------------------------

TTL_PROFISSIONAIS = 7 * 24 * 3600   # muda quando entra ou sai dentista
TTL_ESPECIALIDADES = 7 * 24 * 3600
TTL_DIAS_LIVRES = 30 * 60           # granularidade grossa: some um dia só quando lota
TTL_HORARIOS = 5 * 60               # o dado mais volátil do sistema
TTL_PACIENTE = 24 * 3600            # telefone -> id praticamente não muda


class BackendFalso:
    """
    Substitui o Clinicorp enquanto as credenciais não chegam.

    A forma dos dados imita os endpoints reais (inclusive o 'avaliable'
    com o erro de digitação deles, para você não se acostumar errado).
    Quando as credenciais chegarem, só esta classe é trocada.
    """

    def __init__(self, dias=21, semente=7):
        self.chamadas = 0          # o contador que existe só para medir
        self.log = []
        self.agenda = {}
        self.reservas = []
        hoje = date.today()
        for chave in DENTISTAS:
            self.agenda[chave] = {}
            for n in range(1, dias + 1):
                d = hoje + timedelta(days=n)
                if d.weekday() == 6:
                    continue
                fim = 12 if d.weekday() == 5 else 18
                livres = [f"{h:02d}:00" for h in range(8, fim)]
                # ocupa parte dos horários de forma determinística
                ocupados = (n * semente + len(chave)) % 5
                del livres[:ocupados]
                self.agenda[chave][d.isoformat()] = livres

    def _contar(self, endpoint):
        self.chamadas += 1
        self.log.append(endpoint)

    def get_avaliable_days(self, dentista):
        self._contar("get_avaliable_days")
        return sorted(d for d, h in self.agenda[dentista].items() if h)

    def get_avaliable_times_calendar(self, dentista, dia):
        self._contar("get_avaliable_times_calendar")
        return list(self.agenda[dentista].get(dia, []))

    def list_all_professionals(self):
        self._contar("list_all_professionals")
        return [{"id": k, "nome": v} for k, v in DENTISTAS.items()]

    def patient_get(self, telefone):
        self._contar("patient/get")
        return {"patient_id": f"P{abs(hash(telefone)) % 9000 + 1000}",
                "telefone": telefone}

    def create_appointment_by_api(self, dentista, dia, hora, paciente_id, procedimento):
        self._contar("create_appointment_by_api")
        livres = self.agenda[dentista].get(dia, [])
        if hora not in livres:
            # ATENÇÃO: o Clinicorp real pode NÃO fazer essa checagem.
            # Não há 409 documentado. Isso precisa ser testado (ver QUANDO_AS_CREDENCIAIS_CHEGAREM.md).
            return {"ok": False, "motivo": "horário já ocupado"}
        livres.remove(hora)
        protocolo = f"AG{len(self.reservas) + 1:04d}"
        self.reservas.append({"protocolo": protocolo, "dentista": dentista,
                              "data": dia, "hora": hora,
                              "paciente_id": paciente_id,
                              "procedimento": procedimento})
        return {"ok": True, "protocolo": protocolo}


class Agenda:
    """
    A interface que a recepcionista usa. Nada aqui devolve exceção crua:
    ou devolve dado, ou devolve um dicionário com 'erro' que o modelo
    consegue explicar ao paciente.
    """

    def __init__(self, backend, cliente=None):
        self.backend = backend
        self.cliente = cliente or Cliente()

    # ---------- leitura ----------

    def dentistas_para(self, procedimento):
        proc = (procedimento or "").strip().lower()
        for chave, lista in PROCEDIMENTOS.items():
            if chave in proc or proc in chave:
                return lista[:MAX_DENTISTAS_POR_CONSULTA]
        return PROCEDIMENTOS["avaliacao"][:MAX_DENTISTAS_POR_CONSULTA]

    def dias_livres(self, procedimento):
        dentistas = self.dentistas_para(procedimento)
        dias = set()
        parcial = False
        for d in dentistas:
            try:
                valor, origem = self.cliente.ler(
                    chave=f"dias:{d}",
                    ttl=TTL_DIAS_LIVRES,
                    buscar=lambda d=d: self.backend.get_avaliable_days(d),
                )
                dias.update(valor)
                parcial = parcial or origem == "velho"
            except SemOrcamento as e:
                parcial = True
                if not dias:
                    return {"erro": "limite_temporario",
                            "segundos": e.segundos_ate_liberar}
        return {"dias": sorted(dias), "aproximado": parcial}

    def horarios(self, procedimento, dia):
        dentistas = self.dentistas_para(procedimento)
        resultado = {}
        parcial = False
        for d in dentistas:
            try:
                valor, origem = self.cliente.ler(
                    chave=f"horarios:{d}:{dia}",
                    ttl=TTL_HORARIOS,
                    buscar=lambda d=d: self.backend.get_avaliable_times_calendar(d, dia),
                )
                if valor:
                    resultado[d] = valor
                parcial = parcial or origem == "velho"
            except SemOrcamento as e:
                parcial = True
                if not resultado:
                    return {"erro": "limite_temporario",
                            "segundos": e.segundos_ate_liberar}
        return {"data": dia, "por_dentista": resultado, "aproximado": parcial}

    def paciente_por_telefone(self, telefone):
        # LGPD, minimização: guardamos telefone -> id, e NADA mais.
        # Nome, procedimentos e histórico não entram no cache local.
        try:
            valor, _ = self.cliente.ler(
                chave=f"paciente:{telefone}",
                ttl=TTL_PACIENTE,
                buscar=lambda: {"patient_id":
                                self.backend.patient_get(telefone)["patient_id"]},
            )
            return valor
        except SemOrcamento:
            return {"patient_id": None}

    # ---------- escrita ----------

    def agendar(self, dentista, dia, hora, paciente_id, procedimento):
        try:
            resultado = self.cliente.escrever(
                executar=lambda: self.backend.create_appointment_by_api(
                    dentista, dia, hora, paciente_id, procedimento),
            )
        except SemOrcamento as e:
            return {"ok": False, "motivo": "limite_temporario",
                    "segundos": e.segundos_ate_liberar}

        if not resultado.get("ok"):
            # O Clinicorp recusou. Nosso cache estava errado: invalide de
            # verdade, porque agora não sabemos mais o que é verdade ali.
            self.cliente.cache.invalidar(f"horarios:{dentista}:*")
            self.cliente.cache.invalidar(f"dias:{dentista}")
            return resultado

        # Deu certo e sabemos exatamente o que mudou: remenda em vez de jogar fora.
        chave_h = f"horarios:{dentista}:{dia}"
        self.cliente.cache.remendar(
            chave_h, lambda lista: [x for x in lista if x != hora])
        restantes, _ = self.cliente.cache.obter(chave_h)
        if restantes is not None and not restantes:
            self.cliente.cache.remendar(
                f"dias:{dentista}", lambda dias: [d for d in dias if d != dia])
        return resultado
