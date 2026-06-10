from flask import Flask, jsonify, send_file, render_template_string
import time
import subprocess
import board
import adafruit_dht
import digitalio
import smbus2

app = Flask(__name__)

# --- CONFIGURARE HARDWARE ---
URMARESTE_HUB_USB = "3"  # Schimbă cu hub-ul tău (1 sau 3)

# Relee/MOSFET-uri Active-Low (True = OPRIT, False = PORNIT)
heater = digitalio.DigitalInOut(board.D17)
heater.direction = digitalio.Direction.OUTPUT
heater.value = True  

peltier = digitalio.DigitalInOut(board.D27)
peltier.direction = digitalio.Direction.OUTPUT
peltier.value = True  

# Starea pompei salvată în memorie (implicit oprită)
pump_state = False

# Senzori
dhtDevice = adafruit_dht.DHT22(board.D24)
bus = smbus2.SMBus(1)
PCF8591_ADDRESS = 0x48

def read_light():
    try:
        bus.write_byte(PCF8591_ADDRESS, 0x40)
        bus.read_byte(PCF8591_ADDRESS)
        return bus.read_byte(PCF8591_ADDRESS)
    except:
        return 127

# --- RUTE API (BACKEND) ---

@app.route('/api/data')
def get_data():
    """Returnează datele de la senzori în format JSON"""
    try:
        temp = dhtDevice.temperature
        hum = dhtDevice.humidity
    except RuntimeError:
        # DHT22 mai dă rateuri de citire, returnăm valori simulate sau ultimele cunoscute ca să nu crăpe
        temp, hum = 24.5, 55.0 
        
    light = read_light()
    
    return jsonify({
        "temperatura": temp,
        "umiditate": hum,
        "lumina": light,
        "pompa": pump_state,
        "incalzire": not heater.value,  # Inversat pentru UI (True = funcționează)
        "racire": not peltier.value     # Inversat pentru UI
    })

@app.route('/api/camera')
def get_image():
    """Face o poză în timp real și o trimite direct către browser"""
    img_path = "/tmp/local_plant.jpg"
    # Captură rapidă
    subprocess.run(["rpicam-jpeg", "-o", img_path, "-t", "500", "--width", "640", "--height", "480", "--nopreview"], stdout=subprocess.DEVNULL)
    return send_file(img_path, mimetype='image/jpeg')

@app.route('/api/control/<string:actuator>/<string:state>')
def control_actuator(actuator, state):
    """Controlează componentele hardware (state poate fi 'on' sau 'off')"""
    global pump_state
    is_on = state == "on"
    
    if actuator == "pompa":
        pump_state = is_on
        action = "1" if is_on else "0"
        subprocess.run(["sudo", "uhubctl", "-l", URMARESTE_HUB_USB, "-a", action], stdout=subprocess.DEVNULL)
        
    elif actuator == "incalzire":
        heater.value = not is_on  # Active-Low: False pornește curentul
        
    elif actuator == "racire":
        peltier.value = not is_on   # Active-Low: False pornește curentul
        
    return jsonify({"status": "succes", "actuator": actuator, "stare": state})

# --- INTERFAȚA WEB (FRONTEND ÎNCORPORAT) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sera Localhost</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-gray-100 font-sans min-h-screen p-4 flex flex-col items-center justify-center">

    <div class="w-full max-w-xl bg-gray-800 rounded-3xl p-6 shadow-2xl border border-gray-700 space-y-6">
        <h1 class="text-2xl font-black text-center text-emerald-400">🌱 SERA LOCALHOST DASHBOARD</h1>
        
        <div class="grid grid-cols-3 gap-3 text-center">
            <div class="bg-gray-700/50 p-3 rounded-2xl">
                <p class="text-xs text-gray-400 font-semibold">TEMP</p>
                <p id="txt-temp" class="text-2xl font-bold text-orange-400">-- °C</p>
            </div>
            <div class="bg-gray-700/50 p-3 rounded-2xl">
                <p class="text-xs text-gray-400 font-semibold">UMIDITATE</p>
                <p id="txt-hum" class="text-2xl font-bold text-blue-400">-- %</p>
            </div>
            <div class="bg-gray-700/50 p-3 rounded-2xl">
                <p class="text-xs text-gray-400 font-semibold">LUMINĂ</p>
                <p id="txt-light" class="text-2xl font-bold text-amber-400">--</p>
            </div>
        </div>

        <div class="bg-gray-950 rounded-2xl overflow-hidden aspect-video border border-gray-700 flex items-center justify-center relative">
            <img id="webcam" src="/api/camera" class="w-full h-full object-cover" alt="Plantă">
            <button onclick="refreshImage()" class="absolute bottom-2 right-2 bg-gray-800/80 hover:bg-gray-700 text-xs px-2 py-1 rounded-md border border-gray-600">🔄 Refresh Foto</button>
        </div>

        <div class="space-y-3">
            <h3 class="text-xs font-bold text-gray-400 uppercase tracking-wider">Control Actuatoare</h3>
            <div class="grid grid-cols-3 gap-3">
                <button id="btn-pompa" onclick="toggle('pompa')" class="bg-gray-700 p-4 rounded-xl font-bold transition-all text-sm">💧 Pompă</button>
                <button id="btn-incalzire" onclick="toggle('incalzire')" class="bg-gray-700 p-4 rounded-xl font-bold transition-all text-sm">🔥 Căldură</button>
                <button id="btn-racire" onclick="toggle('racire')" class="bg-gray-700 p-4 rounded-xl font-bold transition-all text-sm">❄️ Răcire</button>
            </div>
        </div>
    </div>

    <script>
        let states = { pompa: false, incalzire: false, racire: false };

        function updateUI() {
            fetch('/api/data')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('txt-temp').innerText = data.temperatura.toFixed(1) + " °C";
                    document.getElementById('txt-hum').innerText = data.umiditate.toFixed(1) + " %";
                    document.getElementById('txt-light').innerText = data.lumina;
                    
                    states.pompa = data.pompa;
                    states.incalzire = data.incalzire;
                    states.racire = data.racire;

                    styleButton('pompa', states.pompa, 'bg-blue-600', 'bg-gray-700');
                    styleButton('incalzire', states.incalzire, 'bg-orange-600', 'bg-gray-700');
                    styleButton('racire', states.racire, 'bg-cyan-600', 'bg-gray-700');
                });
        }

        function styleButton(id, active, activeClass, inactiveClass) {
            const btn = document.getElementById('btn-' + id);
            if(active) {
                btn.className = activeClass + " p-4 rounded-xl font-bold transition-all text-sm shadow-lg scale-105";
            } else {
                btn.className = inactiveClass + " p-4 rounded-xl font-bold transition-all text-sm";
            }
        }

        function toggle(actuator) {
            const nextState = states[actuator] ? 'off' : 'on';
            fetch(`/api/control/${actuator}/${nextState}`)
                .then(r => r.json())
                .then(() => {
                    updateUI();
                    if(actuator === 'pompa' || actuator === 'incalzire' || actuator === 'racire') {
                        // Forțăm și un refresh la imagine după o acțiune
                        setTimeout(refreshImage, 1000);
                    }
                });
        }

        function refreshImage() {
            document.getElementById('webcam').src = '/api/camera?t=' + Date.now();
        }

        // Loop de actualizare date la fiecare 3 secunde
        setInterval(updateUI, 3000);
        updateUI();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

if __name__ == '__main__':
    # Rulăm serverul pe portul 5000, vizibil în toată rețeaua locală
    app.run(host='0.0.0.0', port=5000, debug=False)
