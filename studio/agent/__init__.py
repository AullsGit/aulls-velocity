"""O agente de edição: conversa, decide, usa as ferramentas e verifica o próprio trabalho.

  * `prompts`: quem ele é e como trabalha;
  * `tools`: o que ele pode fazer (cada ferramenta valida a entrada e só toca neste projeto);
  * `loop`: o ciclo com o modelo (streaming, histórico só por acréscimo, erros, cancelamento);
  * `service`: conversas, turnos em segundo plano, retoma depois de um reinício.
"""
