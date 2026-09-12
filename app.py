#!/usr/bin/env python3
"""Serveur local pour index.html — accessible depuis l'iMac et depuis l'iPhone via Tailscale.

Ce que le script apporte à la page :
  · il la sert sur toutes les interfaces (donc aussi sur l'adresse Tailscale du Mac) ;
  · il affiche l'adresse exacte à ouvrir depuis l'iPhone ;
  · il relaie les requêtes vers Gemini (/api/gemini), ce qui permet de garder la clé API
    sur le Mac : l'iPhone n'a plus besoin de la connaître ;
  · il n'accepte par défaut que les connexions venant de la machine elle-même ou du
    tailnet (100.64.0.0/10), pour que la clé ne soit pas exposée au Wi-Fi environnant.

Usage :
    python3 app.py            # démarre en arrière-plan et ouvre la page sur le Mac
    python3 app.py --fg       # reste au premier plan (Ctrl+C pour arrêter)
    python3 app.py --lan      # autorise aussi le réseau local, pas seulement Tailscale
    python3 app.py --port 45679   # si jamais 45678 est pris
"""

import atexit
import http.server
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

# ===================== VERSION & HISTORIQUE =====================
VERSION = "2.0.4"
HISTORIQUE = [
    ("2.0.4", "2026-09-12",
     "Le nom MagicDNS (du type imactavernier-2.tail78c299.ts.net), habituel pour joindre une machine "
     "du tailnet, n'apparaissait pas : il n'était cherché que via le binaire tailscale, introuvable "
     "sur ce Mac, et seule l'adresse 100.x était proposée — équivalente, mais inhabituelle. Deux voies "
     "de secours ajoutées : le DNS inverse sur l'adresse 100.x, auquel MagicDNS répond directement par "
     "le nom complet, puis la reconstitution nom de la machine + domaine du tailnet lu dans les "
     "domaines de recherche DNS (scutil). Une constante NOM_TAILSCALE en tête de fichier permet en "
     "dernier ressort de l'écrire à la main."),
    ("2.0.3", "2026-09-12",
     "Port par défaut changé de 8080 à 45678. Le 8080 est le premier port que prennent la plupart des "
     "serveurs de développement : collision quasi assurée avec un autre script, comme cela s'est "
     "produit au lancement. Le 45678 n'est attribué à aucun service connu, reste hors de la plage "
     "éphémère de macOS (49152 et au-delà, où le système pioche pour les connexions sortantes) et se "
     "tape facilement sur l'iPhone. L'option --port reste disponible pour en choisir un autre."),
    ("2.0.2", "2026-09-12",
     "Corrigé : Tailscale était annoncé introuvable alors qu'il tournait, et seule l'adresse du réseau "
     "local était proposée. La détection ne reposait que sur le binaire en ligne de commande, dont "
     "l'emplacement varie selon le mode d'installation. L'adresse 100.x est désormais lue directement "
     "sur les interfaces réseau de la machine (équivalent de `ifconfig | grep \"inet 100.\"`), ce qui "
     "fonctionne quelle que soit l'installation ; le binaire n'est plus utilisé que pour récupérer le "
     "nom MagicDNS, et son absence ne prive plus de l'adresse Tailscale. Chemins de recherche du "
     "binaire élargis (PATH via shutil.which, en plus des emplacements connus)."),
    ("2.0.1", "2026-09-12",
     "Corrigé : le serveur mourait avec le Terminal. Le simple fork hérité de la v1 laissait le "
     "processus dans la session du Terminal, donc tué par le SIGHUP envoyé à la fermeture de la "
     "fenêtre. Vraie mise en arrière-plan désormais : double fork avec setsid (plus de terminal de "
     "contrôle), SIGHUP ignoré, sorties redirigées vers gemini-serveur.log. Le serveur survit à la "
     "fermeture du Terminal et à la déconnexion, et ne s'arrête qu'avec la mise en veille/l'extinction "
     "du Mac ou un arrêt explicite. Ajout d'un fichier PID et de deux commandes : --stop (arrêt propre) "
     "et --statut (savoir s'il tourne, depuis quand, sur quel port), pour ne plus avoir à chercher le "
     "processus avec lsof."),
    ("2.0.0", "2026-09-12",
     "Serveur utilisable depuis l'iPhone via Tailscale : écoute sur toutes les interfaces, détecte "
     "et affiche l'adresse Tailscale (IP et nom MagicDNS) à ouvrir depuis le téléphone, sert la page "
     "sans cache (pour ne jamais garder une version périmée sur l'iPhone), et gère plusieurs requêtes "
     "en parallèle. Nouvelles routes : /api/statut, /api/cle (clé Gemini enregistrée sur le Mac, en "
     "fichier à droits restreints) et /api/gemini (relais vers l'API Google, la clé ne quitte donc "
     "plus le Mac). Les connexions sont limitées à la machine elle-même et au tailnet, sauf --lan. "
     "Options --fg (premier plan) et --port."),
    ("1.0.0", "",
     "Version d'origine : petit serveur de fichiers statiques sur le port 8080, détaché par fork, "
     "ouvrant index.html dans le navigateur du Mac."),
]

# ===================== CONFIGURATION =====================
PORT = 45678   # port peu banal, volontairement à l'écart des 8080/8000/3000 et hors plage éphémère
DOSSIER = os.path.dirname(os.path.abspath(__file__))
FICHIER_CLE = os.path.join(DOSSIER, "gemini-cle.json")
FICHIER_PID = os.path.join(DOSSIER, "gemini-serveur.pid")
FICHIER_LOG = os.path.join(DOSSIER, "gemini-serveur.log")
RESEAU_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
MODELE_DEFAUT = "gemini-2.5-flash"

# Nom MagicDNS à utiliser si la détection automatique échoue (ex. "imactavernier-2.tail78c299.ts.net").
# Laisser vide pour laisser le script le trouver tout seul.
NOM_TAILSCALE = ""
DELAI_GEMINI = 180  # secondes

autoriser_lan = False

CHEMINS_TAILSCALE = [
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    os.path.expanduser("~/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "tailscale",
]


# ===================== CLÉ API =====================
def charger_cle():
    try:
        with open(FICHIER_CLE, "r", encoding="utf-8") as f:
            return (json.load(f).get("cle") or "").strip()
    except Exception:
        return ""


def enregistrer_cle(cle):
    with open(FICHIER_CLE, "w", encoding="utf-8") as f:
        json.dump({"cle": cle, "maj": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    try:
        os.chmod(FICHIER_CLE, 0o600)  # lisible par le seul propriétaire
    except Exception:
        pass


def apercu_cle(cle):
    """Représentation non sensible d'une clé, pour l'afficher dans la page."""
    if not cle:
        return ""
    return f"{cle[:6]}…{cle[-4:]}" if len(cle) > 12 else "clé courte"


# ===================== ADRESSES =====================
def binaire_tailscale():
    """Cherche la commande tailscale. Elle n'est PAS indispensable : elle ne sert qu'à obtenir le nom
    MagicDNS, l'adresse 100.x étant lue directement sur les interfaces réseau (voir ip_tailscale)."""
    chemins = list(CHEMINS_TAILSCALE)
    trouve = shutil.which("tailscale")
    if trouve:
        chemins.insert(0, trouve)
    for chemin in chemins:
        try:
            resultat = subprocess.run([chemin, "version"], capture_output=True, timeout=5)
            if resultat.returncode == 0:
                return chemin
        except Exception:
            continue
    return None


def ip_tailscale():
    """Lit l'adresse Tailscale (plage 100.64.0.0/10) directement sur les interfaces de la machine.

    C'est la méthode fiable : elle ne dépend ni de l'emplacement du binaire tailscale, ni de la
    manière dont l'app a été installée (App Store, téléchargement direct, Homebrew…). L'équivalent
    exact de `ifconfig | grep "inet 100."`.
    """
    try:
        res = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=8)
        if res.returncode != 0:
            return None
        for correspondance in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+)", res.stdout):
            try:
                adresse = ipaddress.ip_address(correspondance.group(1))
            except ValueError:
                continue
            if adresse in RESEAU_TAILSCALE:
                return str(adresse)
    except Exception:
        pass
    return None


def nom_par_dns_inverse(ip):
    """Demande au résolveur le nom associé à l'adresse 100.x.

    Quand MagicDNS est actif, c'est le résolveur de Tailscale qui répond et rend directement le nom
    complet du type machine.tailXXXXX.ts.net — sans avoir besoin du binaire tailscale.
    """
    if not ip:
        return None
    try:
        nom = socket.gethostbyaddr(ip)[0].rstrip(".")
        return nom if nom.endswith(".ts.net") else None
    except Exception:
        return None


def nom_par_scutil():
    """Reconstitue le nom MagicDNS : nom local de la machine + domaine du tailnet.

    Le domaine (tailXXXXX.ts.net) figure parmi les domaines de recherche DNS que Tailscale installe
    sur macOS, lisibles avec `scutil --dns`. Dernier recours si les deux méthodes précédentes échouent.
    """
    domaine = None
    try:
        res = subprocess.run(["scutil", "--dns"], capture_output=True, text=True, timeout=8)
        if res.returncode == 0:
            trouve = re.search(r"search domain\[\d+\]\s*:\s*(\S*\.ts\.net)", res.stdout)
            if trouve:
                domaine = trouve.group(1).rstrip(".")
    except Exception:
        pass
    if not domaine:
        return None
    machine = None
    try:
        res = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            machine = res.stdout.strip()
    except Exception:
        pass
    if not machine:
        machine = socket.gethostname().split(".")[0]
    return f"{machine.lower()}.{domaine}" if machine else None


def infos_tailscale():
    """Retourne (ip_v4, nom_magicdns).

    L'adresse vient des interfaces réseau (fiable en toutes circonstances). Le nom est cherché par
    trois voies successives, la première qui répond gagne : le binaire tailscale s'il est installé,
    le DNS inverse via MagicDNS, puis la reconstitution à partir des domaines de recherche DNS.
    NOM_TAILSCALE, s'il est renseigné en tête de fichier, court-circuite tout cela.
    """
    ip = ip_tailscale()
    if NOM_TAILSCALE:
        return ip, NOM_TAILSCALE

    nom = None
    binaire = binaire_tailscale()
    if binaire:
        if not ip:
            try:
                res = subprocess.run([binaire, "ip", "-4"], capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    lignes = [l.strip() for l in res.stdout.splitlines() if l.strip()]
                    if lignes:
                        ip = lignes[0]
            except Exception:
                pass
        try:
            res = subprocess.run([binaire, "status", "--json"], capture_output=True, text=True, timeout=8)
            if res.returncode == 0:
                etat = json.loads(res.stdout)
                brut = (etat.get("Self") or {}).get("DNSName") or ""
                nom = brut.rstrip(".") or None
        except Exception:
            pass

    if not nom:
        nom = nom_par_dns_inverse(ip)
    if not nom:
        nom = nom_par_scutil()
    return ip, nom


def ip_locale():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.255.255", 1))
        adresse = s.getsockname()[0]
        s.close()
        return adresse
    except Exception:
        return None


def adresses_utiles(port):
    """Adresses à proposer, de la plus pertinente à la moins pertinente."""
    ip_ts, nom_ts = infos_tailscale()
    liste = []
    if nom_ts:
        liste.append(f"http://{nom_ts}:{port}/")
    if ip_ts:
        liste.append(f"http://{ip_ts}:{port}/")
    locale = ip_locale()
    if locale:
        liste.append(f"http://{locale}:{port}/")
    liste.append(f"http://localhost:{port}/")
    return liste, (ip_ts is None and nom_ts is None)


# ===================== RELAIS GEMINI =====================
def interroger_gemini(cle, modele, question, image_base64, mime_type):
    parties = []
    if question:
        parties.append({"text": question})
    if image_base64:
        parties.append({"inline_data": {"mime_type": mime_type or "image/jpeg", "data": image_base64}})
    corps = json.dumps({"contents": [{"parts": parties}]}).encode("utf-8")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{modele}:generateContent?key={cle}")
    requete = urllib.request.Request(
        url, data=corps, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(requete, timeout=DELAI_GEMINI) as reponse:
        return json.loads(reponse.read().decode("utf-8"))


def texte_de_reponse(data):
    try:
        parties = data["candidates"][0]["content"]["parts"]
        texte = "".join(p.get("text", "") for p in parties).strip()
        return texte or None
    except Exception:
        return None


# ===================== SERVEUR HTTP =====================
class Handler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DOSSIER, **kwargs)

    def log_message(self, format, *args):
        pass  # terminal gardé propre, comme dans la version d'origine

    # ---- En-têtes : jamais de cache, sinon l'iPhone garde une version périmée de la page ----
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    # ---- Contrôle d'accès ----
    def client_autorise(self):
        if autoriser_lan:
            return True
        brut = self.client_address[0]
        if brut in ("127.0.0.1", "::1"):
            return True
        if brut.startswith("::ffff:"):
            brut = brut[7:]
        try:
            adresse = ipaddress.ip_address(brut)
        except ValueError:
            return False
        return adresse.is_loopback or adresse in RESEAU_TAILSCALE

    def refuser(self):
        message = ("Accès refusé : cette page n'est ouverte qu'à cette machine et au tailnet "
                   "Tailscale. Relance app.py avec --lan pour autoriser le réseau local.")
        self.repondre_json({"ok": False, "erreur": message}, code=403)

    def repondre_json(self, donnees, code=200):
        corps = json.dumps(donnees, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def lire_json(self):
        try:
            taille = int(self.headers.get("Content-Length") or 0)
            if taille <= 0:
                return {}
            return json.loads(self.rfile.read(taille).decode("utf-8"))
        except Exception:
            return None

    # ---- Routes ----
    def do_GET(self):
        if not self.client_autorise():
            self.refuser()
            return
        chemin = self.path.split("?")[0].rstrip("/")
        if chemin == "/api/statut":
            cle = charger_cle()
            adresses, sans_tailscale = adresses_utiles(PORT)
            self.repondre_json({
                "ok": True,
                "version": VERSION,
                "cle_enregistree": bool(cle),
                "cle_apercu": apercu_cle(cle),
                "adresses": adresses,
                "tailscale_absent": sans_tailscale,
                "modele_defaut": MODELE_DEFAUT,
            })
            return
        if chemin == "/api/cle":
            cle = charger_cle()
            self.repondre_json({"ok": True, "cle_enregistree": bool(cle), "cle_apercu": apercu_cle(cle)})
            return
        super().do_GET()

    def do_HEAD(self):
        if not self.client_autorise():
            self.refuser()
            return
        super().do_HEAD()

    def do_POST(self):
        if not self.client_autorise():
            self.refuser()
            return
        chemin = self.path.split("?")[0].rstrip("/")
        donnees = self.lire_json()
        if donnees is None:
            self.repondre_json({"ok": False, "erreur": "Corps de requête illisible (JSON attendu)."}, code=400)
            return

        if chemin == "/api/cle":
            cle = (donnees.get("cle") or "").strip()
            if not cle:
                self.repondre_json({"ok": False, "erreur": "Aucune clé transmise."}, code=400)
                return
            try:
                enregistrer_cle(cle)
            except Exception as e:
                self.repondre_json({"ok": False, "erreur": f"Écriture impossible : {e}"}, code=500)
                return
            self.repondre_json({"ok": True, "cle_apercu": apercu_cle(cle)})
            return

        if chemin == "/api/gemini":
            cle = (donnees.get("cle") or "").strip() or charger_cle()
            if not cle:
                self.repondre_json({
                    "ok": False,
                    "erreur": "Aucune clé API disponible : saisis-la dans la page puis enregistre-la sur le Mac.",
                }, code=400)
                return
            modele = (donnees.get("modele") or MODELE_DEFAUT).strip()
            question = donnees.get("question") or ""
            image = donnees.get("image") or ""
            mime = donnees.get("mime_type") or "image/jpeg"
            if not question and not image:
                self.repondre_json({"ok": False, "erreur": "Ni question ni image dans la requête."}, code=400)
                return
            depart = time.time()
            try:
                brut = interroger_gemini(cle, modele, question, image, mime)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
                except Exception:
                    pass
                self.repondre_json({
                    "ok": False,
                    "erreur": f"Google a répondu HTTP {e.code}" + (f" : {detail}" if detail else ""),
                }, code=502)
                return
            except Exception as e:
                self.repondre_json({"ok": False, "erreur": f"Appel à Gemini impossible : {e}"}, code=502)
                return
            self.repondre_json({
                "ok": True,
                "texte": texte_de_reponse(brut),
                "brut": brut,
                "modele": modele,
                "duree": round(time.time() - depart, 1),
            })
            return

        self.repondre_json({"ok": False, "erreur": "Route inconnue."}, code=404)


class Serveur(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def lancer_serveur():
    os.chdir(DOSSIER)
    with Serveur(("", PORT), Handler) as httpd:
        httpd.serve_forever()


# ===================== ARRIÈRE-PLAN =====================
def detacher():
    """Détache réellement le processus du Terminal.

    Un simple fork() ne suffit pas : l'enfant reste dans la session du Terminal et reçoit donc le
    SIGHUP envoyé à la fermeture de la fenêtre — c'est ce qui faisait mourir le serveur. On enchaîne
    donc fork + setsid (nouvelle session, plus aucun terminal de contrôle) + second fork (le processus
    final n'est plus chef de session, il ne peut donc plus en acquérir un par accident), on ignore
    explicitement SIGHUP, et on redirige les sorties vers un fichier journal puisqu'il n'y a plus de
    terminal où écrire.
    """
    if os.fork() != 0:
        os._exit(0)          # le Terminal reprend la main immédiatement
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    os.chdir(DOSSIER)
    os.umask(0)

    with open(os.devnull, "rb", 0) as entree:
        os.dup2(entree.fileno(), sys.stdin.fileno())
    sortie = open(FICHIER_LOG, "a", buffering=1, encoding="utf-8")
    os.dup2(sortie.fileno(), sys.stdout.fileno())
    os.dup2(sortie.fileno(), sys.stderr.fileno())
    print(f"\n=== Démarrage v{VERSION} le {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"— port {PORT}, PID {os.getpid()} ===")


def ecrire_pid():
    try:
        with open(FICHIER_PID, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": PORT, "version": VERSION,
                       "depuis": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass
    atexit.register(effacer_pid)


def effacer_pid():
    try:
        if lire_pid().get("pid") == os.getpid():
            os.remove(FICHIER_PID)
    except Exception:
        pass


def lire_pid():
    try:
        with open(FICHIER_PID, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def processus_vivant(pid):
    try:
        os.kill(pid, 0)          # signal 0 : teste l'existence sans rien envoyer
        return True
    except Exception:
        return False


def commande_statut():
    infos = lire_pid()
    pid = infos.get("pid")
    if pid and processus_vivant(pid):
        print(f"Serveur en marche — PID {pid}, port {infos.get('port', '?')}, "
              f"v{infos.get('version', '?')}, depuis {infos.get('depuis', '?')}.")
        print(f"Journal : {FICHIER_LOG}")
        print("Arrêt : python3 app.py --stop")
    else:
        print("Aucun serveur en marche (aucun PID enregistré, ou processus disparu).")
        if pid:
            print("Fichier PID périmé, il sera remplacé au prochain démarrage.")


def commande_stop():
    infos = lire_pid()
    pid = infos.get("pid")
    if not pid or not processus_vivant(pid):
        print("Aucun serveur lancé par app.py à arrêter.")
        print("S'il reste malgré tout quelque chose sur le port : lsof -nP -iTCP:%d -sTCP:LISTEN" % PORT)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as e:
        print(f"Arrêt impossible : {e}")
        return
    for _ in range(20):          # laisse jusqu'à 2 secondes pour une sortie propre
        if not processus_vivant(pid):
            print(f"Serveur arrêté (PID {pid}).")
            try:
                os.remove(FICHIER_PID)
            except Exception:
                pass
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    print(f"Serveur arrêté de force (PID {pid}).")
    try:
        os.remove(FICHIER_PID)
    except Exception:
        pass


# ===================== DÉMARRAGE =====================
def main():
    global PORT, autoriser_lan

    premier_plan = "--fg" in sys.argv
    autoriser_lan = "--lan" in sys.argv
    if "--port" in sys.argv:
        try:
            PORT = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            print(f"Option --port mal formée, port {PORT} conservé.")

    if "--stop" in sys.argv:
        commande_stop()
        return
    if "--statut" in sys.argv:
        commande_statut()
        return

    # Vérifie que le port est libre avant de forker : sinon l'échec passerait inaperçu.
    test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    test.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        test.bind(("", PORT))
    except OSError as e:
        print(f"Impossible d'utiliser le port {PORT} : {e}")
        infos = lire_pid()
        if infos.get("pid") and processus_vivant(infos["pid"]):
            print(f"Un serveur app.py tourne déjà (PID {infos['pid']}, depuis {infos.get('depuis', '?')}).")
            print("Arrête-le avec : python3 app.py --stop")
        else:
            print("Un autre programme occupe ce port. Pour savoir lequel :")
            print(f"  lsof -nP -iTCP:{PORT} -sTCP:LISTEN")
            print("Ou choisis un autre port : python3 app.py --port 45679")
        sys.exit(1)
    finally:
        test.close()

    adresses, sans_tailscale = adresses_utiles(PORT)
    cle = charger_cle()

    print(f"\napp.py v{VERSION} — serveur Gemini local")
    print(f"Dossier servi : {DOSSIER}")
    print(f"Clé Gemini enregistrée : {'oui (' + apercu_cle(cle) + ')' if cle else 'non — saisis-la dans la page'}")
    print(f"Accès : {'tout le réseau local (--lan)' if autoriser_lan else 'cette machine et le tailnet Tailscale'}")
    if sans_tailscale:
        print("Tailscale introuvable sur cette machine : seules les adresses locales sont proposées.")
    print("\nAdresses à ouvrir :")
    for adresse in adresses:
        print(f"  {adresse}")
    print("\nDepuis l'iPhone : ouvre la première adresse (Tailscale doit être actif sur les deux appareils).")
    if premier_plan:
        print("Mode premier plan : le serveur s'arrête à la fermeture du Terminal (Ctrl+C).\n")
    else:
        print("Le serveur passe en arrière-plan et survit à la fermeture du Terminal.")
        print(f"Journal : {FICHIER_LOG}")
        print("Arrêt : python3 app.py --stop   ·   État : python3 app.py --statut\n")
    sys.stdout.flush()

    if not premier_plan and hasattr(os, "fork"):
        detacher()

    ecrire_pid()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # --stop obtient une sortie propre

    fil = threading.Thread(target=lancer_serveur, daemon=True)
    fil.start()

    try:
        webbrowser.open(f"http://localhost:{PORT}/index.html")
    except Exception:
        pass

    try:
        fil.join()
    except (KeyboardInterrupt, SystemExit):
        print("Serveur arrêté.")


if __name__ == "__main__":
    main()
