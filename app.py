import requests
import time
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify

# ============================================================
# CONFIGURACION
# ============================================================
URL_API_JAHA = "https://www.jaha.com.py/api/posicionColectivos"
URL_OSRM = "http://router.project-osrm.org/route/v1/driving"

LAT_PARADA = -25.393250
LON_PARADA = -57.468139

LINEA_FILTRO = "187"

UMBRAL_MINUTOS = 30
INTERVALO_SEGUNDOS = 30

# ============================================================
# ESTADO EN MEMORIA
# ============================================================
HISTORIAL_POSICIONES = {}
ALERTAS_ENVIADAS = set()
alertas_visibles = []
buses_monitor = []
last_update = None
errores = []

app = Flask(__name__)


# ============================================================
# LOGICA DE MONITOREO
# ============================================================
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def detectar_sentido(unidad, recorrido, lat, lon):
    """
    Determina sentido usando vector de movimiento real (prioridad).
    Fallback al campo recorrido solo si no hay historial suficiente.
    """
    historial = HISTORIAL_POSICIONES.get(unidad, [])

    # Si tenemos al menos 2 posiciones anteriores, calcular tendencia
    if len(historial) >= 2:
        ultimas_lons = [h["lon"] for h in historial[-3:]]
        ultimas_lons.append(lon)

        # Contar cuantas veces la longitud disminuyo (movimiento hacia Oeste = IDA)
        disminuciones = sum(1 for i in range(1, len(ultimas_lons)) if ultimas_lons[i] < ultimas_lons[i-1])
        aumentos = sum(1 for i in range(1, len(ultimas_lons)) if ultimas_lons[i] > ultimas_lons[i-1])

        # Si la mayoria de los movimientos son hacia el Oeste -> IDA
        if disminuciones > aumentos:
            return "IDA"
        # Si la mayoria son hacia el Este -> VUELTA
        elif aumentos > disminuciones:
            return "VUELTA"

    # Fallback: usar el campo recorrido si no hay tendencia clara
    if recorrido and "(I)" in recorrido:
        return "IDA"
    if recorrido and "(V)" in recorrido:
        return "VUELTA"

    return "DESCONOCIDO"


def calcular_eta(lat_bus, lon_bus):
    """Calcula tiempo estimado en minutos via OSRM."""
    url = (
        f"{URL_OSRM}/{lon_bus},{lat_bus};"
        f"{LON_PARADA},{LAT_PARADA}?overview=false"
    )
    resp = requests.get(url, timeout=15)
    data = resp.json()
    segundos = data["routes"][0]["duration"]
    return round(segundos / 60, 1)


def registrar_alerta(unidad, minutos, aire):
    """Registra una alerta visible en la web."""
    ahora = datetime.now().strftime("%H:%M:%S")
    aire_txt = "SI" if aire else "NO"
    alerta = {
        "hora": ahora,
        "unidad": unidad,
        "minutos": minutos,
        "aire": aire_txt,
        "mensaje": f"Unidad #{unidad} llega en {minutos} min (A/C: {aire_txt})",
    }
    alertas_visibles.append(alerta)
    # Mantener solo las ultimas 20 alertas
    if len(alertas_visibles) > 20:
        alertas_visibles.pop(0)
    log(f"[ALERTA] {alerta['mensaje']}")


def ciclo_monitoreo():
    """Un ciclo completo de monitoreo."""
    global buses_monitor, last_update

    try:
        log("Consultando API Jaha...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
        }
        payload = {"linea": LINEA_FILTRO}
        resp = requests.post(URL_API_JAHA, headers=headers, json=payload, timeout=20)
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:100]}")
        buses = resp.json()
        log(f"Recibidos {len(buses)} buses de la API")

    except Exception as e:
        msg = f"Error API Jaha: {e}"
        log(msg)
        errores.append(f"{datetime.now().strftime('%H:%M:%S')} - {msg}")
        return

    buses_filtrados = []
    ahora = datetime.now().strftime("%H:%M:%S")

    for bus in buses:
        unidad = bus.get("unidad")
        lat_str = bus.get("lat", "0")
        lon_str = bus.get("lon", "0")
        recorrido = bus.get("recorrido", "")
        estado = bus.get("estado", "")
        aire = bus.get("aire", False)

        try:
            lat = float(lat_str)
            lon = float(lon_str)
        except (ValueError, TypeError):
            continue

        # Actualizar historial ANTES del filtro (todos los buses acumulan posiciones)
        if unidad not in HISTORIAL_POSICIONES:
            HISTORIAL_POSICIONES[unidad] = []
        HISTORIAL_POSICIONES[unidad].append({"lat": lat, "lon": lon})
        if len(HISTORIAL_POSICIONES[unidad]) > 3:
            HISTORIAL_POSICIONES[unidad].pop(0)

        # La API ya filtra por linea 187, no hace falta filtrar aqui

        # Detectar sentido
        sentido = detectar_sentido(unidad, recorrido, lat, lon)
        metodo = "VECTOR" if len(HISTORIAL_POSICIONES.get(unidad, [])) >= 2 else "RECORRIDO"
        log(f"  Unidad {unidad}: sentido={sentido} (por {metodo}) lon={lon:.5f}")
        if sentido != "IDA":
            continue

        # Barrera geografica: solo buses que aun no pasaron la parada
        if lon < LON_PARADA:
            continue

        # Calcular ETA
        try:
            minutos = calcular_eta(lat, lon)
        except Exception as e:
            log(f"Error OSRM unidad {unidad}: {e}")
            minutos = None

        info = {
            "unidad": unidad,
            "lat": lat,
            "lon": lon,
            "recorrido": recorrido,
            "estado": estado,
            "aire": aire,
            "sentido": sentido,
            "minutos": minutos,
            "ultima_actualizacion": ahora,
        }
        buses_filtrados.append(info)

        # Alerta visible en web si esta a <= 10 min y no se alerto antes
        clave_alerta = f"{unidad}_{ahora[:5]}"
        if minutos is not None and minutos <= UMBRAL_MINUTOS:
            if clave_alerta not in ALERTAS_ENVIADAS:
                registrar_alerta(unidad, minutos, aire)
                ALERTAS_ENVIADAS.add(clave_alerta)

    buses_monitor = buses_filtrados
    last_update = ahora

    log(f"Buses linea {LINEA_FILTRO} (IDA) encontrados: {len(buses_filtrados)}")
    for b in buses_filtrados:
        aire_icon = "[A/C]" if b["aire"] else ""
        eta = f"{b['minutos']} min" if b["minutos"] is not None else "?"
        log(
            f"  Unidad {b['unidad']} | "
            f"{eta} | {aire_icon} | "
            f"lon={b['lon']:.5f}"
        )


def bucle_monitoreo():
    """Hilo principal que corre indefinidamente."""
    log("=== MONITOR COLECTIVO INICIADO ===")
    log(f"Parada: ({LAT_PARADA}, {LON_PARADA})")
    log(f"Linea: {LINEA_FILTRO} | Sentido: IDA")
    log(f"Alertas web: <= {UMBRAL_MINUTOS} min")
    log(f"Intervalo: {INTERVALO_SEGUNDOS}s")
    log("")

    while True:
        try:
            ciclo_monitoreo()
        except Exception as e:
            log(f"Error en ciclo: {e}")
            errores.append(f"{datetime.now().strftime('%H:%M:%S')} - {e}")

        time.sleep(INTERVALO_SEGUNDOS)


# ============================================================
# RUTAS WEB
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/buses")
def api_buses():
    return jsonify({
        "buses": buses_monitor,
        "alertas": alertas_visibles,
        "last_update": last_update,
        "parada": {"lat": LAT_PARADA, "lon": LON_PARADA},
        "errores": errores[-10:],
    })


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    hilo = threading.Thread(target=bucle_monitoreo, daemon=True)
    hilo.start()
    time.sleep(2)
    app.run(host="0.0.0.0", port=5000, debug=False)
