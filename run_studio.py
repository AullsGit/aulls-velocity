"""Arranca o AwlsVelocity localmente (modo desenvolvimento / operador) e abre o browser.

Sem SUPABASE_URL definido: contas e projetos ficam em memória (perdem-se ao fechar), os
ficheiros em studio/data/, e os vídeos são gerados nesta máquina. Cada conta nova recebe
créditos de teste (DEV_SIGNUP_CREDITS) para se conseguir percorrer o fluxo completo.

Uso:
    venv\\Scripts\\python.exe run_studio.py
"""
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOST, PORT = "127.0.0.1", 8811

if __name__ == "__main__":
    os.environ.setdefault("DEV_SIGNUP_CREDITS", "50")
    os.environ.setdefault("VELOCITY_NO_TEXT", "1")
    os.environ.setdefault("DEV_PERSIST", "1")  # contas e projetos sobrevivem a reinícios (studio/data/)
    import uvicorn

    threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(f"http://{HOST}:{PORT}")), daemon=True).start()
    print(f"AwlsVelocity em http://{HOST}:{PORT}  (Ctrl+C para parar)")
    uvicorn.run("studio.server:app", host=HOST, port=PORT, log_level="info")
