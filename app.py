from flask import Flask, render_template, jsonify, request, redirect, url_for, session, flash
import firebase_admin
from firebase_admin import credentials, db, auth as firebase_auth
import plotly.graph_objects as go
from plotly.utils import PlotlyJSONEncoder
import json
from datetime import datetime, timedelta
import pytz
import numpy as np
from sklearn.linear_model import LinearRegression
from dateutil.relativedelta import relativedelta
import os
from functools import lru_cache, wraps
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit
import pyrebase
from firebase_config import firebaseConfig
import base64

# Configurar logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Clave secreta para sesiones

# Inicializar Pyrebase para autenticación
firebase = pyrebase.initialize_app(firebaseConfig)
auth = firebase.auth()

# Zona horaria local (UTC-5)
LOCAL_TZ = pytz.timezone('America/Bogota')

# Verificar que el archivo de credenciales existe
cred_path = 'ensayos-rack-firebase-adminsdk-jkauk-cfa68835d5.json'
if not os.path.exists(cred_path):
    raise FileNotFoundError(f"Archivo de credenciales no encontrado: {cred_path}")

# Inicializar Firebase con RTDB
try:
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://ensayos-rack-default-rtdb.firebaseio.com'
    })
except Exception as e:
    print(f"Error al inicializar Firebase: {e}")
    raise

# Decorador para verificar autenticación
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def convert_to_local_time(timestamp_ms):
    """Convierte timestamp en milisegundos a tiempo local (UTC-5)"""
    utc_dt = datetime.fromtimestamp(timestamp_ms/1000, pytz.UTC)
    return utc_dt.astimezone(LOCAL_TZ)

def analizar_depreciacion_luz(timestamps, valores_luz):
    # Filtrar datos cuando la luz está encendida (mayor a 100 lux)
    datos_filtrados = [(ts, luz) for ts, luz in zip(timestamps, valores_luz) if luz > 100]
    if not datos_filtrados:
        return None, None, None, None
    
    timestamps_filtrados, luz_filtrada = zip(*datos_filtrados)
    
    # Convertir timestamps a números (días desde el inicio)
    t0 = min(timestamps_filtrados)
    dias = [(ts - t0).total_seconds() / (24*3600) for ts in timestamps_filtrados]
    
    # Encontrar valor máximo de luz (100%)
    max_luz = max(luz_filtrada)
    luz_normalizada = [l/max_luz * 100 for l in luz_filtrada]
    
    # Preparar datos para regresión
    X = np.array(dias).reshape(-1, 1)
    y = np.array(luz_normalizada)
    
    # Ajustar regresión lineal
    modelo = LinearRegression()
    modelo.fit(X, y)
    
    # Predecir cuando llegará al 80%
    if modelo.coef_[0] >= 0:  # Si no hay depreciación
        return None, None, None, None
    
    dias_hasta_80 = (80 - modelo.intercept_) / modelo.coef_[0]
    fecha_80 = t0 + timedelta(days=dias_hasta_80)
    
    # Generar línea de predicción
    dias_pred = np.linspace(0, max(dias_hasta_80 * 1.2, max(dias)), 100)
    luz_pred = modelo.predict(dias_pred.reshape(-1, 1))
    fechas_pred = [t0 + timedelta(days=d) for d in dias_pred]
    
    return fechas_pred, luz_pred, fecha_80, max_luz

def get_sensors_list():
    """Obtiene la lista de sensores desde RTDB"""
    try:
        sensors_ref = db.reference('sensores')
        sensors_data = sensors_ref.get()
        if not sensors_data:
            return []
        
        # Devolver las claves como strings, sin convertir a enteros
        sensors = sorted(list(sensors_data.keys()))
        
        # Imprimir para depuración
        logging.info(f"Sensores encontrados: {sensors}")
        return sensors
    except Exception as e:
        print(f"Error al obtener lista de sensores: {e}")
        return []

def get_sensor_data(sensor_id, start_date=None, end_date=None, view_all=False):
    """
    Obtiene datos del sensor desde RTDB.
    """
    logging.info(f"Buscando datos para sensor: {sensor_id}, Rango: {start_date} a {end_date}, Ver todo: {view_all}")
    try:
        # Referencia al sensor específico
        sensor_ref = db.reference(f'sensores/{sensor_id}')
        sensor_data = sensor_ref.get()
        
        if not sensor_data:
            logging.warning(f"No se encontraron datos crudos para el sensor {sensor_id}")
            return [], [], [], None, None, None, None
        
        # Filtrar solo si el usuario proporcionó start_date o end_date
        effective_start_date = start_date
        effective_end_date = end_date

        # Procesar datos: sensor_id -> month_key -> iso_ts_key -> reading_dict
        all_readings = []
        processed_count = 0
        filtered_out_count = 0
        parse_error_count = 0
        
        for month_key, month_data in sensor_data.items(): 
            if isinstance(month_data, dict):
                for iso_ts_key, reading_dict in month_data.items():
                    if isinstance(reading_dict, dict):
                        try:
                            # La clave ES el timestamp en formato ISO 8601 (con Z para UTC)
                            # Necesitamos parsearlo
                            # Python < 3.11 no maneja bien la 'Z' directamente con %z
                            # Reemplazar 'Z' con '+00:00' o parsear e indicar UTC
                            if iso_ts_key.endswith('Z'):
                                iso_ts_key_adjusted = iso_ts_key[:-1] + "+00:00"
                                ts_utc = datetime.fromisoformat(iso_ts_key_adjusted)
                            else:
                                # Intentar parsear directamente si no termina en Z (puede fallar)
                                ts_utc = datetime.fromisoformat(iso_ts_key)
                                if ts_utc.tzinfo is None:
                                     ts_utc = pytz.UTC.localize(ts_utc) # Asumir UTC si no hay timezone

                            # Convertir a tiempo local
                            ts = ts_utc.astimezone(LOCAL_TZ)
                            
                            # Filtrar por fecha si el usuario especificó rangos
                            if (effective_start_date and ts < effective_start_date) or (effective_end_date and ts > effective_end_date):
                                filtered_out_count += 1
                                continue
                                
                            # Extraer datos directamente del reading_dict
                            temperatura = reading_dict.get('temperatura', 0)
                            luz = reading_dict.get('luz', 0)
                            
                            all_readings.append((ts, temperatura, luz))
                            processed_count += 1
                        except ValueError as ve:
                            parse_error_count += 1
                            logging.warning(f"Error al parsear clave de timestamp ISO '{iso_ts_key}' para {sensor_id}: {ve}")
                        except Exception as e:
                             logging.error(f"Error procesando lectura con clave {iso_ts_key} para {sensor_id}: {e} - Datos: {reading_dict}")
                    # else: (ignorar si el valor no es un diccionario)
            # else: (ignorar si el valor del mes no es un diccionario)

        logging.info(f"Procesados: {processed_count}, Filtrados: {filtered_out_count}, Errores Parseo TS: {parse_error_count} para {sensor_id}")
        
        # Ordenar lecturas por timestamp
        all_readings.sort(key=lambda x: x[0])
        
        # Si no hay datos después de filtrar, retornar listas vacías
        if not all_readings:
            logging.warning(f"No quedaron datos para {sensor_id} después del filtrado.")
            return [], [], [], None, None, None, None
            
        # Separar datos para gráficas
        timestamps = [r[0] for r in all_readings]
        temperaturas = [r[1] for r in all_readings]
        luz = [r[2] for r in all_readings]
        
        # Análisis de depreciación de luz
        fechas_pred, luz_pred, fecha_80, max_luz = None, None, None, None
        if len(all_readings) >= 10:  # Solo analizar si hay suficientes puntos
            fechas_pred, luz_pred, fecha_80, max_luz = analizar_depreciacion_luz(timestamps, luz)
            
        return timestamps, temperaturas, luz, fechas_pred, luz_pred, fecha_80, max_luz

    except Exception as e:
        logging.error(f"Error general al obtener datos del sensor {sensor_id}: {e}")
        import traceback
        traceback.print_exc()
        return [], [], [], None, None, None, None

def get_display_name(sensor_id):
    """Devuelve un nombre corto para mostrar al usuario"""
    prefix = 'ESP_RACK_FLOWER_'
    if sensor_id.startswith(prefix):
        return sensor_id[len(prefix):]
    return sensor_id

def get_led_state():
    """Obtiene el estado actual del LED desde RTDB"""
    try:
        led_ref = db.reference(LED_CONTROL_PATH)
        state = led_ref.get()
        return state if state is not None else False
    except Exception as e:
        logging.error(f"Error al obtener estado del LED: {e}")
        return False

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html', config=firebaseConfig)
    
    if request.method == 'POST':
        try:
            data = request.get_json()
            id_token = data.get('idToken')
            
            # Verificar el token con Firebase Admin
            decoded_token = firebase_auth.verify_id_token(id_token)
            uid = decoded_token['uid']
            
            # Crear sesión de usuario
            user_info = firebase_auth.get_user(uid)
            session['user'] = {
                'uid': uid,
                'email': user_info.email,
                'display_name': user_info.display_name or user_info.email
            }
            
            return jsonify({'success': True}), 200
        except Exception as e:
            logging.error(f"Error en login: {e}")
            return jsonify({'error': str(e)}), 401

@app.route('/check-auth', methods=['POST'])
def check_auth():
    try:
        data = request.get_json()
        id_token = data.get('idToken')
        
        # Verificar el token con Firebase Admin
        decoded_token = firebase_auth.verify_id_token(id_token)
        uid = decoded_token['uid']
        
        # Crear sesión de usuario si no existe
        if 'user' not in session:
            user_info = firebase_auth.get_user(uid)
            session['user'] = {
                'uid': uid,
                'email': user_info.email,
                'display_name': user_info.display_name or user_info.email
            }
        
        return jsonify({'success': True}), 200
    except Exception as e:
        logging.error(f"Error en check-auth: {e}")
        return jsonify({'error': str(e)}), 401

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    """Página principal con las 4 tarjetas de sensores"""
    try:
        sensors = get_sensors_list()
        # Construir lista con id y nombre corto
        sensors_display = []
        for sid in sensors:
            name = get_display_name(sid)
            folder_num = name.split('_')[-1][-2:]
            folder_num = int(folder_num.lstrip('0')) if folder_num.lstrip('0') else 1
            img_folder = os.path.join('static', 'public', str(folder_num))
            images = []
            if os.path.isdir(img_folder):
                for fname in sorted(os.listdir(img_folder)):
                    if fname.lower().endswith('.jpeg'):
                        path = os.path.join(img_folder, fname)
                        with open(path, 'rb') as f:
                            data = base64.b64encode(f.read()).decode('utf-8')
                            images.append(f'data:image/jpeg;base64,{data}')
            sensors_display.append({'id': sid, 'name': name, 'images': images})
        # Obtener fecha actual y estado del LED
        current_date = datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M:%S')
        led_state = get_led_state()
        return render_template(
            'index.html', 
            sensors=sensors_display,
            current_date=current_date,
            led_state=led_state,
            user=session.get('user')
        )
    except Exception as e:
        print(f"Error en la página principal: {e}")
        return render_template('index.html', error=str(e), user=session.get('user'))

@app.route('/sensor/<sensor_id>')
@login_required
def sensor_detail(sensor_id):
    """Página de detalle para un sensor específico"""
    display_name = get_display_name(sensor_id)
    try:
        # Obtener parámetros
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')
        view_all = request.args.get('view_all', 'false').lower() == 'true'

        # Parsear fechas
        start_date, end_date = None, None
        if start_date_str:
            sd = datetime.strptime(start_date_str, '%Y-%m-%d')
            start_date = LOCAL_TZ.localize(sd)
        if end_date_str:
            ed = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_date = LOCAL_TZ.localize(ed)
            # Ajustar al final del día
            end_date = end_date.replace(hour=23, minute=59, second=59)
            
        # Si no se proporciona fecha, usar último día
        if not start_date and not end_date and not view_all:
            end_date = datetime.now(LOCAL_TZ)
            start_date = end_date - timedelta(days=1)
            
        # Obtener datos del sensor
        timestamps, temperaturas, luz, fechas_pred, luz_pred, fecha_80, max_luz = get_sensor_data(
            sensor_id, start_date, end_date, view_all
        )
        
        # Si no hay datos, mostrar mensaje
        if not timestamps:
            return render_template(
                'sensor_detail.html', 
                error=f"No hay datos disponibles para el sensor {display_name} en el período seleccionado.",
                sensor_id=sensor_id,
                display_name=display_name,
                start_date=start_date_str,
                end_date=end_date_str,
                current_date=datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M:%S'),
                led_state=get_led_state(),
                user=session.get('user')
            )
            
        # Crear gráficas con Plotly
        # Gráfica de temperatura
        fig_temp = go.Figure()
        fig_temp.add_trace(go.Scatter(
            x=timestamps, 
            y=temperaturas,
            mode='lines+markers',
            name='Temperatura',
            line=dict(color='#3b82f6', width=2),
            marker=dict(size=4)
        ))
        fig_temp.update_layout(
            title='Temperatura vs. Tiempo',
            xaxis_title='Fecha',
            yaxis_title='Temperatura (°C)',
            hovermode='x unified',
            height=400,
            template='plotly_white',
            margin=dict(l=20, r=20, t=40, b=20)
        )
        
        # Gráfica de luz en foot-candles
        # Inicializar parámetros de conversión
        DEFAULT_FOOT_CANDLES = float(os.environ.get('DEFAULT_FOOT_CANDLES', 12))
        threshold_fc = float(os.environ.get('THRESHOLD_FOOT_CANDLES', 7))
        # Calcular valores en fc basados en max_luz
        fc_values = [l / max_luz * DEFAULT_FOOT_CANDLES for l in luz] if (luz and max_luz) else []
        # Calcular tendencia en fc
        fc_pred = [p / 100 * DEFAULT_FOOT_CANDLES for p in luz_pred] if (luz_pred and max_luz) else []
        
        fig_luz = go.Figure()
        # Trazar nivel de luz en fc
        fig_luz.add_trace(go.Scatter(
            x=timestamps,
            y=fc_values,
            mode='lines+markers',
            name='Nivel de Luz (fc)',
            line=dict(color='#f59e0b', width=2),
            marker=dict(size=4)
        ))
        
        # Si hay datos de predicción, agregarlos
        if fechas_pred is not None and fc_pred:
            fig_luz.add_trace(go.Scatter(
                x=fechas_pred,
                y=fc_pred,
                mode='lines',
                name='Tendencia FC',
                line=dict(color='#ef4444', width=2, dash='dash')
            ))
            # Umbral fijo en fc
            fig_luz.add_trace(go.Scatter(
                x=[min(timestamps), max(fechas_pred)],
                y=[threshold_fc, threshold_fc],
                mode='lines',
                name=f'Umbral {threshold_fc} fc',
                line=dict(color='#10b981', width=1.5, dash='dot')
            ))
        
        fig_luz.update_layout(
            title='Nivel de Luz vs. Tiempo',
            xaxis_title='Fecha',
            yaxis_title='Nivel de Luz (fc)',
            hovermode='x unified',
            height=400,
            template='plotly_white',
            margin=dict(l=20, r=20, t=40, b=20)
        )
        
        # Convertir figuras a JSON para pasar a la plantilla
        plot_temp = json.dumps(fig_temp, cls=PlotlyJSONEncoder)
        plot_luz = json.dumps(fig_luz, cls=PlotlyJSONEncoder)
        
        # Obtener último valor para mostrar en tiempo real
        current_temp = temperaturas[-1] if temperaturas else None
        current_fc = fc_values[-1] if fc_values else None
        
        # Obtener fecha actual para la plantilla
        current_date = datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M:%S')
        
        return render_template(
            'sensor_detail.html',
            sensor_id=sensor_id,
            display_name=display_name,
            plot_temp=plot_temp,
            plot_luz=plot_luz,
            current_temp=current_temp,
            current_fc=current_fc,
            start_date=start_date_str,
            end_date=end_date_str,
            fecha_80=fecha_80,
            current_date=current_date,
            led_state=get_led_state(),
            user=session.get('user'),
            threshold_fc=threshold_fc
        )
        
    except Exception as e:
        print(f"Error en la página de detalle: {e}")
        import traceback
        traceback.print_exc()
        return render_template(
            'sensor_detail.html', 
            error=str(e),
            sensor_id=sensor_id,
            display_name=display_name,
            current_date=datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M:%S'),
            led_state=get_led_state(),
            user=session.get('user')
        )

# --- Lógica del ciclo de luces ---
LED_CONTROL_PATH = 'control/led'
CYCLE_START_HOUR_LOCAL = int(os.environ.get('CYCLE_START_HOUR_LOCAL', 20))  # Por defecto 20 (8pm)
ACTIVE_PHASE_HOURS = 7
ACTIVE_PHASE_MINUTES = ACTIVE_PHASE_HOURS * 60 # 420 minutos

# Generar patrón para la fase activa: 10 x (20min ON, 20min OFF) + 1 x (20min ON)
CYCLE_PATTERN_MINUTES = []
minutes_covered = 0
on_duration = 20
off_duration = 20
while minutes_covered < ACTIVE_PHASE_MINUTES:
    # Añadir periodo ON
    actual_on = min(on_duration, ACTIVE_PHASE_MINUTES - minutes_covered)
    CYCLE_PATTERN_MINUTES.append((actual_on, 0)) # Temporalmente 0 OFF
    minutes_covered += actual_on
    if minutes_covered >= ACTIVE_PHASE_MINUTES:
        break
    
    # Añadir periodo OFF
    actual_off = min(off_duration, ACTIVE_PHASE_MINUTES - minutes_covered)
    # Añadir la duración OFF al último periodo ON añadido
    last_on, _ = CYCLE_PATTERN_MINUTES.pop()
    CYCLE_PATTERN_MINUTES.append((last_on, actual_off))
    minutes_covered += actual_off

# La duración total de la fase activa es fija (7 horas)
total_active_phase_duration_minutes = ACTIVE_PHASE_MINUTES

scheduler = BackgroundScheduler(daemon=True, timezone=LOCAL_TZ)

def update_led_state(state: bool):
    """Actualiza el estado del LED en Firebase RTDB."""
    try:
        led_ref = db.reference(LED_CONTROL_PATH)
        led_ref.set(state)
        logging.info(f"Estado del LED actualizado a: {state}")
    except Exception as e:
        logging.error(f"Error al actualizar estado del LED: {e}")

def schedule_next_led_change():
    """Calcula el próximo cambio de estado y lo programa."""
    now = datetime.now(LOCAL_TZ)
    start_of_cycle_today = now.replace(hour=CYCLE_START_HOUR_LOCAL, minute=0, second=0, microsecond=0)
    end_of_active_phase_today = start_of_cycle_today + timedelta(minutes=total_active_phase_duration_minutes)
    start_of_next_cycle = start_of_cycle_today + timedelta(days=1)

    current_state = False # Por defecto OFF
    next_change_time = None

    if start_of_cycle_today <= now < end_of_active_phase_today:
        # Estamos en la FASE ACTIVA de 7 horas
        logging.info("Evaluando estado dentro de la fase activa (7h).")
        minutes_since_start = (now - start_of_cycle_today).total_seconds() / 60
        current_minutes_in_pattern = 0
        in_active_phase = True

        for on_duration, off_duration in CYCLE_PATTERN_MINUTES:
            # Intervalo ON
            start_on = current_minutes_in_pattern
            end_on = start_on + on_duration
            if start_on <= minutes_since_start < end_on:
                current_state = True
                next_change_time = start_of_cycle_today + timedelta(minutes=end_on)
                logging.info(f"Fase activa: ON. Próximo cambio (a OFF) en: {next_change_time.strftime('%H:%M:%S')}")
                break
            current_minutes_in_pattern = end_on

            # Intervalo OFF (si existe para este par)
            if off_duration > 0:
                start_off = current_minutes_in_pattern
                end_off = start_off + off_duration
                if start_off <= minutes_since_start < end_off:
                    current_state = False
                    next_change_time = start_of_cycle_today + timedelta(minutes=end_off)
                    logging.info(f"Fase activa: OFF. Próximo cambio (a ON) en: {next_change_time.strftime('%H:%M:%S')}")
                    break
                current_minutes_in_pattern = end_off
        else:
            # Esto no debería ocurrir si la lógica del patrón es correcta
            logging.error("Se alcanzó el final del patrón durante la fase activa. Revisar lógica.")
            current_state = False # Apagar por seguridad
            next_change_time = end_of_active_phase_today # Siguiente evento es el fin de la fase activa
    
    elif now >= end_of_active_phase_today:
         # Estamos en la FASE INACTIVA (después de las 7h activas de hoy)
         logging.info("Fase inactiva (después de 7h). Estado: OFF.")
         current_state = False
         next_change_time = start_of_next_cycle # El próximo evento es el inicio del ciclo mañana
         logging.info(f"Próximo cambio (inicio de ciclo) programado para mañana a las {next_change_time.strftime('%Y-%m-%d %H:%M:%S')}")

    else: # now < start_of_cycle_today
        # Estamos en la FASE INACTIVA (antes de las 12 PM de hoy)
        logging.info("Fase inactiva (antes de las 12 PM). Estado: OFF.")
        current_state = False
        next_change_time = start_of_cycle_today # El próximo evento es el inicio del ciclo hoy a las 12 PM
        logging.info(f"Próximo cambio (inicio de ciclo) programado para hoy a las {next_change_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Actualizar estado inmediatamente
    update_led_state(current_state)

    # Programar la próxima ejecución de esta función
    if next_change_time:
         job_id = 'led_cycle_job'
         existing_job = scheduler.get_job(job_id)
         
         # Redondear next_change_time a segundos para comparación fiable
         next_change_time_sec = next_change_time.replace(microsecond=0)
         existing_run_time_sec = existing_job.next_run_time.replace(microsecond=0) if existing_job else None

         if existing_job and existing_run_time_sec == next_change_time_sec:
             logging.info(f"Job '{job_id}' ya programado para {next_change_time_sec}, no se reprograma.")
         else:
             scheduler.add_job(
                 schedule_next_led_change,
                 trigger='date',
                 run_date=next_change_time,
                 id=job_id, 
                 replace_existing=True
             )
             logging.info(f"Próximo chequeo del ciclo programado para: {next_change_time.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        logging.error("No se pudo determinar el próximo tiempo de cambio. No se programó job.")

@app.route('/api/sensors')
def api_sensors():
    """API para obtener la lista de sensores"""
    if 'user' not in session:
        return jsonify({"error": "No autenticado"}), 401
    return jsonify(get_sensors_list())

@app.route('/api/sensor/<sensor_id>')
def api_sensor_data(sensor_id):
    """API para obtener datos de un sensor específico"""
    if 'user' not in session:
        return jsonify({"error": "No autenticado"}), 401
    try:
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')
        view_all = request.args.get('view_all', 'false').lower() == 'true'
        
        # Parsear fechas
        start_date, end_date = None, None
        if start_date_str:
            sd = datetime.strptime(start_date_str, '%Y-%m-%d')
            start_date = LOCAL_TZ.localize(sd)
        if end_date_str:
            ed = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_date = LOCAL_TZ.localize(ed)
        
        timestamps, temperaturas, luz, fechas_pred, luz_pred, fecha_80, max_luz = get_sensor_data(
            sensor_id, start_date, end_date, view_all
        )
        
        return jsonify({
            'timestamps': [ts.isoformat() for ts in timestamps],
            'temperaturas': temperaturas,
            'luz': luz
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/led_state')
def api_led_state():
    """API para obtener el estado actual del LED"""
    if 'user' not in session:
        return jsonify({"error": "No autenticado"}), 401
    return jsonify({"state": get_led_state()})

# --- Inicialización del Scheduler ---
if __name__ == '__main__':
    # Programar el primer chequeo al iniciar la app (o poco después)
    # Y también asegurarse de que se ejecute cerca de las 12 PM cada día
    scheduler.add_job(
        schedule_next_led_change,
        trigger='cron',
        hour=CYCLE_START_HOUR_LOCAL -1, # Una hora antes para asegurar
        minute=59,
        id='daily_cycle_start_check',
        replace_existing=True
        )
    scheduler.add_job(schedule_next_led_change, trigger='date', run_date=datetime.now(LOCAL_TZ) + timedelta(seconds=5), id='initial_run')
    scheduler.start()
    logging.info("Scheduler iniciado.")

    # Asegurarse de que el scheduler se apague correctamente al salir
    atexit.register(lambda: scheduler.shutdown())

    # Ejecutar Flask con puerto diferente
    app.run(debug=True, use_reloader=False, port=5000)
