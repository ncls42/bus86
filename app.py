import csv
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, abort


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

# Fuseau horaire français
TIMEZONE = ZoneInfo("Europe/Paris")

# Si aucun bus n'est trouvé, on attend 60 secondes
# avant de refaire une requête API.
INTERVALLE_SANS_BUS = 65

# Une fois un bus trouvé, son résultat reste valable
# jusqu'à 3 minutes après son heure d'arrivée théorique.
MARGE_CACHE_APRES_ARRIVEE = 3


# ============================================================
# IDENTIFIANT DE LA PAGE
# ============================================================

# Dans Render, on pourra mettre :
#
# PAGE_ID = mon-id
#
# L'adresse sera alors :
#
# https://ton-projet.onrender.com/mon-id/
#
PAGE_ID = os.environ.get("PAGE_ID", "mon-id")


# ============================================================
# CACHE
# ============================================================

cache = {
    "heure": None,
    "bus": None,
    "expiration": None,
    "prochaine_requete": None,
}

cache_lock = threading.Lock()
# Nombre total d'appels réels à l'API TCL
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

    # None signifie que l'API a rencontré une erreur.
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
        # L'arrêt doit être dans notre CSV
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
# MISE A JOUR DU CACHE
# ============================================================

def mettre_a_jour_cache(maintenant):

    bus = rechercher_bus()

    # --------------------------------------------------------
    # ERREUR API
    # --------------------------------------------------------

    if bus is None:

        # On ne détruit pas forcément un résultat précédent
        # en cas d'erreur temporaire de l'API.
        #
        # Si un ancien résultat existe encore, on le conserve.
        if (
            cache["heure"] is not None
            and cache["expiration"] is not None
            and maintenant < cache["expiration"]
        ):
            return cache

        # Sinon, nouvelle tentative dans 60 secondes.
        cache["prochaine_requete"] = (
            maintenant
            + timedelta(seconds=INTERVALLE_SANS_BUS)
        )

        return cache

    # --------------------------------------------------------
    # AUCUN BUS
    # --------------------------------------------------------

    if not bus:

        cache["heure"] = None
        cache["bus"] = []
        cache["expiration"] = None

        cache["prochaine_requete"] = (
            maintenant
            + timedelta(seconds=INTERVALLE_SANS_BUS)
        )

        return cache

    # --------------------------------------------------------
    # BUS TROUVE
    # --------------------------------------------------------

    premier_bus = bus[0]

    minutes = premier_bus["temps"]

    # Heure théorique d'arrivée
    heure_arrivee = maintenant + timedelta(
        minutes=minutes
    )

    # On garde le résultat jusqu'à 3 minutes
    # après l'heure théorique.
    expiration = (
        heure_arrivee
        + timedelta(minutes=MARGE_CACHE_APRES_ARRIVEE)
    )

    cache["heure"] = heure_arrivee
    cache["bus"] = bus
    cache["expiration"] = expiration

    # Pas de nouvelle requête programmée :
    # le cache est valable jusqu'à expiration.
    cache["prochaine_requete"] = None

    return cache


# ============================================================
# OBTENIR LE RESULTAT
# ============================================================

def obtenir_resultat():

    maintenant = datetime.now(TIMEZONE)

    with cache_lock:

        # ----------------------------------------------------
        # CACHE D'UN BUS
        # ----------------------------------------------------

        if (
            cache["heure"] is not None
            and cache["expiration"] is not None
        ):

            if maintenant < cache["expiration"]:

                return {
                    "etat": "bus",
                    "heure": cache["heure"],
                    "bus": cache["bus"],
                }

            # Le cache est expiré.
            cache["heure"] = None
            cache["bus"] = None
            cache["expiration"] = None

        # ----------------------------------------------------
        # CACHE "PAS DE BUS"
        # ----------------------------------------------------

        if (
            cache["prochaine_requete"] is not None
            and maintenant < cache["prochaine_requete"]
        ):

            return {
                "etat": "aucun_bus",
                "heure": None,
                "bus": [],
            }

        # ----------------------------------------------------
        # NOUVELLE INTERROGATION TCL
        # ----------------------------------------------------

        mettre_a_jour_cache(maintenant)

        # ----------------------------------------------------
        # RESULTAT
        # ----------------------------------------------------

        if cache["heure"] is not None:

            return {
                "etat": "bus",
                "heure": cache["heure"],
                "bus": cache["bus"],
            }

        return {
            "etat": "aucun_bus",
            "heure": None,
            "bus": [],
        }


# ============================================================
# PAGE HTML
# ============================================================

def generer_page(resultat):

    if resultat["etat"] == "bus":

        heure = resultat["heure"].strftime("%Hh%M")

        contenu = f"""
        <div class="heure">
            {heure}
        </div>
        """

    else:

        contenu = """
        <div class="aucun-bus">
            Pas de bus détecté
        </div>
        """

    return f"""
<!DOCTYPE html>

<html lang="fr">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Ligne 86</title>

<style>

    * {{
        box-sizing: border-box;
    }}

    html,
    body {{
        margin: 0;
        padding: 0;
        width: 100%;
        height: 100%;
    }}

    body {{
        background: #202124;
        color: white;
        font-family: Arial, sans-serif;

        display: flex;
        align-items: center;
        justify-content: center;

        overflow: hidden;
    }}

    .conteneur {{
        text-align: center;
    }}

    .ligne {{
        font-size: 20px;
        color: #ffffff;
        margin-bottom: 8px;
        font-weight: bold;
    }}

    .destination {{
        font-size: 14px;
        color: #999999;
        margin-bottom: 20px;
    }}

    .heure {{
        font-size: 72px;
        line-height: 1;
        font-weight: bold;
        color: #55dd88;
    }}

    .aucun-bus {{
        font-size: 28px;
        color: #888888;
        font-weight: bold;
    }}

</style>

</head>

<body>

<div class="conteneur">

    <div class="ligne">
        🚌 Ligne 86
    </div>

    <div class="destination">
        {DESTINATION}
    </div>

    {contenu}

</div>

<script>

    // Actualisation de la page toutes les 60 secondes.
    //
    // Attention :
    // cela ne signifie PAS que l'API TCL est appelée
    // toutes les 60 secondes.
    //
    // C'est le cache Python qui décide s'il faut
    // réellement interroger TCL.

    setTimeout(function() {{
        location.reload();
    }}, 60000);

</script>

</body>

</html>
"""


# ============================================================
# ROUTE PRINCIPALE
# ============================================================

@app.route("/")
def racine():

    return f"""
    <html>
    <head>
        <meta http-equiv="refresh" content="0; url=/{PAGE_ID}/">
    </head>
    <body>
        <a href="/{PAGE_ID}/">
            Accéder à la ligne 86
        </a>
    </body>
    </html>
    """


# ============================================================
# ROUTE /mon-id/
# ============================================================

@app.route("/<page_id>/")
def page_bus(page_id):

    # On n'autorise que l'identifiant configuré.
    if page_id != PAGE_ID:
        abort(404)

    resultat = obtenir_resultat()

    return generer_page(resultat)


# ============================================================
# LANCEMENT LOCAL
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )

@app.route("/callapi")
def callapi():

    return f"""
    <!DOCTYPE html>

    <html lang="fr">

    <head>
        <meta charset="UTF-8">

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1.0"
        >

        <title>API TCL - Statistiques</title>

        <style>

            * {{
                box-sizing: border-box;
            }}

            html,
            body {{
                margin: 0;
                padding: 0;
                width: 100%;
                height: 100%;
            }}

            body {{
                background: #202124;
                color: white;
                font-family: Arial, sans-serif;

                display: flex;
                align-items: center;
                justify-content: center;

                text-align: center;
            }}

            .conteneur {{
                padding: 30px;
            }}

            .titre {{
                font-size: 22px;
                color: #999999;
                margin-bottom: 20px;
            }}

            .nombre {{
                font-size: 80px;
                line-height: 1;
                font-weight: bold;
                color: #55dd88;
            }}

            .texte {{
                font-size: 18px;
                margin-top: 15px;
                color: #ffffff;
            }}

        </style>

    </head>

    <body>

        <div class="conteneur">

            <div class="titre">
                API TCL
            </div>

            <div class="nombre">
                {nombre_appels_api}
            </div>

            <div class="texte">
                appels effectués
            </div>

        </div>

    </body>

    </html>
    """
