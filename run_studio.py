"""Arranca o Velocity Studio (interface web local) e abre o browser automaticamente.

Uso:
    venv\\Scripts\\python.exe run_studio.py
Depois abre (ou mantém aberto) http://127.0.0.1:8811 no browser.
Para parar: fecha esta janela do terminal ou Ctrl+C.
"""
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOST, PORT = "127.0.0.1", 8811


def _open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=_open_browser, daemon=True).start()
    print(f"Velocity Studio em http://{HOST}:{PORT}  (Ctrl+C para parar)")
    uvicorn.run("studio.server:app", host=HOST, port=PORT, log_level="warning")
