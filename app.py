import http.server
import os
import socketserver
import sys
import threading
import webbrowser

PORT = 8080


class Handler(http.server.SimpleHTTPRequestHandler):

  def log_message(self, format, *args):
    # Désactive les logs dans le terminal pour garder la propreté
    pass


def run_server():
  os.chdir(os.path.dirname(os.path.abspath(__file__)))
  with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.serve_forever()


if __name__ == "__main__":
  # Détachement du processus sur macOS
  if os.fork() != 0:
    sys.exit(0)

  # Démarrage du serveur dans un thread en arrière-plan
  server_thread = threading.Thread(target=run_server, daemon=True)
  server_thread.start()

  # Ouverture automatique de la page web
  webbrowser.open(f"http://localhost:{PORT}/index.html")

  # Maintien du processus actif
  server_thread.join()