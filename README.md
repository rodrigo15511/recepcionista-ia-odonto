# Recepcionista de IA para clínica odontológica

Agente conversacional que faz o papel de recepcionista de uma clínica odontológica: responde dúvidas, consulta a agenda e marca consultas. Foi desenhado para rodar no WhatsApp e integrar com o sistema de gestão da clínica (Clinicorp).

> **Status:** protótipo funcional no terminal, com a agenda simulada. A integração com a API real do Clinicorp já foi validada por scripts de teste (criar e cancelar agendamento, disponibilidade, fuso horário). O canal WhatsApp ainda não está ligado.
>
> Os nomes de profissionais e o endereço deste repositório são fictícios.

## O princípio da arquitetura

**A IA decide o que dizer. Regras determinísticas decidem o que acontece.**

O modelo de linguagem nunca é a última coisa antes de uma ação irreversível. Na prática, isso vira quatro camadas:

1. **Ferramentas de escopo mínimo.** O modelo só consegue fazer o que foi declarado como função (`consultar_dias`, `consultar_horarios`, `criar_agendamento`). O que não foi declarado não é só improvável: é impossível.
2. **Validação determinística entre decisão e execução.** Quando o modelo chama `criar_agendamento`, o código revalida tudo contra a fonte (o dentista existe? ele faz esse procedimento? o horário está dentro do expediente?) antes de executar.
3. **Confirmação explícita do paciente** antes de reservar.
4. **Filtro de saída.** Toda resposta passa por `validar()`, que bloqueia preço e linguagem de diagnóstico. A IA é recepcionista, não dentista.

## O problema interessante: 25 requisições por hora

A API do Clinicorp permite só **25 requisições por hora**, e uma conversa de agendamento gasta de 3 a 7. Sem controle, três pacientes simultâneos cegariam o bot pelo resto da hora. O módulo [`limite.py`](limite.py) resolve isso com:

- **Orçamento com prioridade.** Leitura tem teto mais baixo (16) que escrita (22), para sempre sobrar cota para quem já decidiu agendar.
- **Janela deslizante** de 60 minutos, a hipótese conservadora sobre como o provedor conta.
- **Estado persistido em disco.** Se o programa reiniciar, o contador não volta a zero, porque o provedor continua contando.
- **Cache com TTL por volatilidade.** A lista de dentistas vale 7 dias; horários livres, 5 minutos.
- **Degradação honesta.** Sem cota, o bot responde com dado em cache marcado como "aproximado" ou diz que vai confirmar. Nunca inventa horário.
- **Remendo do cache.** Depois de um agendamento bem-sucedido, o horário é retirado do cache sem gastar requisição.

### Resultado da simulação ([`simulacao.py`](simulacao.py))

| conversas/dia | estratégia | requisições | pico/hora | agendados |
|---:|---|---:|---:|---:|
| 40 | ingênua | 130 | **34 (estoura)** | 23 |
| 40 | cache + adiar | 93 | 18 | 23 |
| 90 | ingênua | 223 | **48 (estoura)** | 35 |
| 90 | cache + adiar | 113 | 20 | 32 |

Com cache e orçamento, o pico fica abaixo de 25/h mesmo com 90 conversas por dia.

## Estrutura

| Arquivo | Responsabilidade |
|---|---|
| `recepcionista_v3.py` | Laço do agente, chamadas ao modelo (Gemini), ferramentas, validação e log |
| `agenda.py` | Única camada que conhece o sistema da clínica. Roteamento procedimento → dentista e um `BackendFalso` que imita a API real |
| `limite.py` | Orçamento de requisições, janela deslizante e cache |
| `testes.py` | 10 testes do orçamento, do cache e do roteamento |
| `simulacao.py` | Simulação de carga comparando estratégias |
| `docs/` | Decisões de arquitetura e lições da integração com a API real |

Só a biblioteca padrão do Python, sem dependências externas.

## Como rodar

```bash
# testes (não precisam de chave)
python testes.py

# simulação de carga
python simulacao.py

# conversa no terminal (precisa de uma chave da API Gemini)
export GEMINI_API_KEY="sua-chave"   # no Windows: set GEMINI_API_KEY=sua-chave
python recepcionista_v3.py
```

No terminal, `agenda` mostra as reservas feitas, `cota` mostra o orçamento e `sair` encerra.

## Próximos passos

- Trocar o `BackendFalso` pelo cliente real do Clinicorp (a interface já está pronta para isso)
- Idempotência local antes de `create_appointment_by_api` (a API não tem chave de idempotência)
- SQLite para telefone → paciente, consentimento (LGPD) e histórico
- Canal WhatsApp pela Cloud API oficial da Meta
- Trocar o modelo por um plano pago antes de produção: conversa de paciente é dado sensível pela LGPD

## Como foi construído

Usei IA (Claude) como apoio no desenvolvimento, do jeito que se usa um colega mais experiente: para discutir o desenho antes de codar, revisar e explicar trechos. Construí o projeto passo a passo, uma mudança por vez, entendendo cada parte. Defini as regras de negócio e os limites clínicos, testei a integração com a API real do Clinicorp e investiguei os bugs. O que aprendi nesse processo está em [`docs/licoes-aprendidas.md`](docs/licoes-aprendidas.md).
