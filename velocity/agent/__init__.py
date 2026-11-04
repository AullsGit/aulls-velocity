"""As mãos do agente de edição: operações sobre o Plano, relatórios para o modelo, construção do
plano a partir da referência, verificação do resultado.

Duas metades, porque a API de produção é leve (sem numpy/OpenCV/ffmpeg):
  * `planops`, `report`: Python puro, correm na API (o ciclo do agente vive lá);
  * `builder`, `sheets`, `verify`, `compute`: precisam do motor, correm no worker (job de computação).
"""
