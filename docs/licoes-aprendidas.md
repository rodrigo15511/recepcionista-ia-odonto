# Lições aprendidas

Coisas que descobri construindo o agente e integrando com a API real do Clinicorp. Cada uma virou regra para o código novo.

## Sobre o agente de IA

- **O que a API te dá, você devolve como veio.** Não reconstrua a resposta do modelo campo a campo. Descobri isso ao descartar sem querer o `thoughtSignature` do Gemini, o que quebrou o *function calling*. Hoje o histórico guarda as `parts` originais.
- **Nunca mova validação para dentro do prompt.** O prompt reduz a frequência de erro, mas não elimina. Regra que importa fica no código.
- **Trava anti-loop.** O laço do agente tem no máximo 5 voltas (`MAX_VOLTAS`). Se passar disso, o paciente recebe uma resposta segura e o caso vai para o log.

## Sobre chamadas de rede

- **4xx é erro meu; 5xx e 429 são passageiros.** Só os passageiros justificam retry, com backoff exponencial.
- **Retry só é seguro na camada sem efeito colateral.** O retry mora na chamada HTTP, nunca em volta do laço que executa ferramentas.
- **Falha parcial é o pior caso.** Já aconteceu de a ferramenta executar e a resposta ao modelo falhar: agendamento criado e paciente informado de erro.
- **Log que não diz o suficiente é quase tão ruim quanto não ter log.** Registre o corpo do erro, não só o tipo.

## Sobre a API do Clinicorp (confirmado por teste real)

- **O Swagger nem sempre bate com a realidade.** A resposta de disponibilidade vem como lista plana (`From`, `To`, `ProfessionalId`), não aninhada como no exemplo da documentação.
- **Lista vazia não é "sem horário".** Um profissional não habilitado no Agendamento Online faz a API devolver `200` com lista vazia, sem erro nenhum.
- **Não existe idempotência no servidor.** Não há `Idempotency-Key` nem `409`. Por isso a criação de agendamento **nunca** tem retry automático: se der timeout, o pedido pode ter chegado. A defesa precisa ser uma tabela local checada *antes* da chamada.
- **Agendamento por cadeira ou por profissional são modos exclusivos.** Mandar os dois ao mesmo tempo dá `400`.
- **Fuso horário:** `fromTime`/`toTime` são hora local pura, sem conversão. O campo `AtomicDate` (AAAAMMDD) é a fonte confiável do dia gravado.
- **Erro de digitação faz parte do contrato.** O endpoint é `get_avaliable_times_calendar`, com "avaliable". Escrever "available" dá 404.

## Sobre webhook e segurança

O webhook do Clinicorp não exige HTTPS e não tem assinatura. Regras que decorrem disso:

- A URL do webhook é o próprio segredo: caminho longo e aleatório, nunca `/webhook`.
- Webhook é um aviso para verificar, não fonte de verdade. Ao receber "cancelado", consulte a API antes de agir.
- Responda `200` rápido e processe em segundo plano.

## Limites clínicos (inegociáveis)

A IA é recepcionista, não profissional de saúde. Ela nunca diagnostica, nunca interpreta sintoma, nunca indica tratamento e nunca informa preço fechado. Isso é garantido em camadas: instrução no prompt, validação determinística do texto de saída e uma rota de escape ("isso precisa de avaliação do dentista, quer que eu veja um horário?").

## LGPD

- Credenciais ficam fora do código (variável de ambiente ou arquivo ignorado pelo git).
- O cache local guarda só telefone → id do paciente, nada mais.
- Um modelo em plano gratuito nunca vai para produção, porque os prompts podem ser usados para treino.
