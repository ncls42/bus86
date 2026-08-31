import csv
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import redis
from flask import Flask, jsonify


# ============================================================
# CONFIGURATION
# ============================================================

app = Flask(__name__)

API_URL = (
    "https://carte-interactive.tcl.fr/"
    "api/interface/tcl/busPosition/"
    "line%3ASYTNEX%3A86"
)

CSV_FILE = "ligne_86_arrets.csv"

DESTINATION = "Campus Région numérique"

TIMEZONE = ZoneInfo("Europe/Paris")


# ============================================================
# CACHE
# ============================================================

# Si aucun bus n'est trouvé :
# on ne demande pas immédiatement une nouvelle fois à TCL.
INTERVALLE_SANS_BUS = 65

# Le résultat d'un bus reste valable 3 minutes
# après son heure théorique d'arrivée.
MARGE_CACHE_APRES_ARRIVEE = 3


# ------------------------------------------------------------
# Redis
# ------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL")

if not REDIS_URL:
    raise RuntimeError(
        "La variable d'environnement REDIS_URL n'est pas configurée."
    )

redis_client = redis.from_url(
    REDIS_URL,
    decode_responses=True
)


# Clé Redis contenant le résultat calculé
CACHE_KEY = "tcl:ligne86:campus"

# Verrou Redis empêchant plusieurs workers
# d'interroger TCL simultanément.
LOCK_KEY = "tcl:ligne86:lock"

# Durée maximale du verrou.
LOCK_TIMEOUT = 15


# ============================================================
# IDENTIFIANT DE LA PAGE
# ============================================================

PAGE_ID = os.environ.get("PAGE_ID", "mon-id")


# ============================================================
# STATISTIQUES
# ============================================================

# Ce compteur est volontairement en mémoire.
# Il sert uniquement aux statistiques du worker courant.
nombre_appels_api = 0


# ============================================================
# CHARGEMENT DES ARRETS
# ============================================================

def charger_arrets():

    arrets = {}

    with open(
        CSV_FILE,
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as fichier:

        lecteur = csv.DictReader(
            fichier,
            delimiter=";"
        )

        for ligne in lecteur:

            stop_point = ligne["stop_point"].strip()
            nom = ligne["nom"].strip()
            temps = ligne["temps_jusqua_Campus_min"].strip()

            if not stop_point or not temps:
                continue

            try:
                temps = int(temps)
            except ValueError:
                continue

            arrets[stop_point] = {
                "nom": nom,
                "temps": temps
            }

    return arrets


# Chargement une seule fois au démarrage
arrets = charger_arrets()


# ============================================================
# INTERROGATION API TCL
# ============================================================

def recuperer_bus():

    global nombre_appels_api

    nombre_appels_api += 1

    try:

        response = requests.get(
            API_URL,
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        return data.get("data", [])

    except Exception as e:

        print("Erreur API TCL :", e)

        return None


# ============================================================
# RECHERCHE DES BUS
# ============================================================

def rechercher_bus():

    bus_api = recuperer_bus()

    # None = erreur API
    if bus_api is None:
        return None

    resultats = []

    for vehicule in bus_api:

        bus_id = vehicule.get("id", "")
        prev_stop = vehicule.get("prevStop", "")
        direction = vehicule.get("direction", "")

        # ----------------------------------------------------
        # Sens vers Campus
        # ----------------------------------------------------

        if direction != "backward":
            continue

        # ----------------------------------------------------
        # Arrêt connu dans notre CSV
        # ----------------------------------------------------

        if prev_stop not in arrets:
            continue

        # ----------------------------------------------------
        # Numéro du bus
        # ----------------------------------------------------

        morceaux = bus_id.split(":")

        if len(morceaux) >= 2:
            numero = morceaux[-2]
        else:
            numero = "?"

        arret = arrets[prev_stop]

        resultats.append({
            "numero": numero,
            "arret": arret["nom"],
            "stop_point": prev_stop,
            "temps": arret["temps"]
        })

    # Plus proche en premier
    resultats.sort(
        key=lambda bus: bus["temps"]
    )

    return resultats


# ============================================================
# CALCUL DU RESULTAT
# ============================================================

def construire_resultat():

    maintenant = datetime.now(TIMEZONE)

    bus = rechercher_bus()

    # --------------------------------------------------------
    # ERREUR API
    # --------------------------------------------------------

    if bus is None:
        return None

    # --------------------------------------------------------
    # AUCUN BUS
    # --------------------------------------------------------

    if not bus:

        return {
            "etat": "aucun_bus",
            "heure": None,
            "bus": [],
            "expiration": (
                maintenant
                + timedelta(seconds=INTERVALLE_SANS_BUS)
            ).isoformat()
        }

    # --------------------------------------------------------
    # BUS TROUVE
    # --------------------------------------------------------

    premier_bus = bus[0]

    minutes = premier_bus["temps"]

    heure_arrivee = maintenant + timedelta(
        minutes=minutes
    )

    expiration = (
        heure_arrivee
        + timedelta(minutes=MARGE_CACHE_APRES_ARRIVEE)
    )

    return {
        "etat": "bus",
        "heure": heure_arrivee.isoformat(),
        "bus": bus,
        "expiration": expiration.isoformat()
    }


# ============================================================
# RECUPERATION DU CACHE REDIS
# ============================================================

def lire_cache():

    valeur = redis_client.get(CACHE_KEY)

    if not valeur:
        return None

    try:
        import json
        return json.loads(valeur)

    except Exception as e:

        print("Erreur lecture cache Redis :", e)

        return None


# ============================================================
# ECRITURE DU CACHE REDIS
# ============================================================

def ecrire_cache(resultat):

    import json

    maintenant = datetime.now(TIMEZONE)

    expiration = datetime.fromisoformat(
        resultat["expiration"]
    )

    # --------------------------------------------------------
    # TTL Redis
    # --------------------------------------------------------

    ttl = int(
        (expiration - maintenant).total_seconds()
    )

    # Sécurité
    ttl = max(ttl, 1)

    redis_client.setex(
        CACHE_KEY,
        ttl,
        json.dumps(resultat)
    )


# ============================================================
# OBTENIR LE RESULTAT
# ============================================================

def obtenir_resultat():

    # ========================================================
    # 1. CACHE PARTAGE
    # ========================================================

    resultat = lire_cache()

    if resultat is not None:

        return resultat


    # ========================================================
    # 2. CACHE VIDE
    #
    # On essaie de prendre le verrou.
    #
    # Un seul worker pourra obtenir le verrou.
    # ========================================================

    lock_obtenu = redis_client.set(
        LOCK_KEY,
        "1",
        nx=True,
        ex=LOCK_TIMEOUT
    )


    # ========================================================
    # 3. CE WORKER A LE VERROU
    # ========================================================

    if lock_obtenu:

        try:

            # ------------------------------------------------
            # Double vérification du cache
            #
            # Important :
            # un autre worker pouvait avoir rempli Redis
            # juste avant que nous obtenions le verrou.
            # ------------------------------------------------

            resultat = lire_cache()

            if resultat is not None:
                return resultat


            # ------------------------------------------------
            # UNIQUE APPEL TCL
            # ------------------------------------------------

            resultat = construire_resultat()


            # ------------------------------------------------
            # ERREUR TCL
            # ------------------------------------------------

            if resultat is None:

                # On regarde s'il existe éventuellement
                # un ancien résultat conservé.
                ancien = lire_cache()

                if ancien is not None:
                    return ancien

                return {
                    "etat": "erreur",
                    "heure": None,
                    "bus": []
                }


            # ------------------------------------------------
            # STOCKAGE REDIS
            # ------------------------------------------------

            ecrire_cache(resultat)

            return resultat

        finally:

            # ------------------------------------------------
            # LIBERATION DU VERROU
            # ------------------------------------------------

            redis_client.delete(LOCK_KEY)


    # ========================================================
    # 4. UN AUTRE WORKER EST EN TRAIN D'APPELER TCL
    # ========================================================

    # On attend très brièvement que le résultat apparaisse
    # dans Redis plutôt que de faire nous-mêmes un appel TCL.

    for _ in range(20):

        time.sleep(0.1)

        resultat = lire_cache()

        if resultat is not None:
            return resultat


    # ========================================================
    # 5. LE WORKER PRINCIPAL N'A PAS ENCORE FINI
    # ========================================================

    # On évite absolument de déclencher un deuxième appel TCL.
    return {
        "etat": "chargement",
        "heure": None,
        "bus": []
    }


# ============================================================
# ROUTE API
# ============================================================

@app.route("/")
def accueil():

    resultat = obtenir_resultat()

    return jsonify(resultat)


# ============================================================
# PAGE PRINCIPALE
# ============================================================

@app.route(f"/{PAGE_ID}/")
def page():

    resultat = obtenir_resultat()

    return jsonify(resultat)


# ============================================================
# STATISTIQUES
# ============================================================

@app.route("/stats")
def stats():

    return jsonify({
        "nombre_appels_api_worker": nombre_appels_api
    })


# ============================================================
# DEMARRAGE
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
