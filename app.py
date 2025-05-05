from flask import Flask, render_template, jsonify, request
import firebase_admin
from firebase_admin import credentials, firestore
import plotly.graph_objects as go
from plotly.utils import PlotlyJSONEncoder
import json
from datetime import datetime, timedelta
import pytz
import numpy as np
from sklearn.linear_model import LinearRegression
from dateutil.relativedelta import relativedelta
import os
from functools import lru_cache

app = Flask(__name__)

# Zona horaria local (UTC-5)
LOCAL_TZ = pytz.timezone('America/Bogota')

# Verificar que el archivo de credenciales existe
cred_path = 'ensayos-rack-firebase-adminsdk-jkauk-cfa68835d5.json'
if not os.path.exists(cred_path):
    raise FileNotFoundError(f"Archivo de credenciales no encontrado: {cred_path}")

# Inicializar Firebase
try:
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    print(f"Error al inicializar Firebase: {e}")
    raise

def convert_to_local_time(utc_dt):
    """Convierte tiempo UTC a tiempo local (UTC-5)"""
    if utc_dt.tzinfo is None:
        utc_dt = pytz.UTC.localize(utc_dt)
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

# Caché de sensores (válido por 1 hora)
@lru_cache(maxsize=1)
def get_cached_sensors():
    """Obtiene y almacena en caché la lista de sensores"""
    timestamp = datetime.now().replace(minute=0, second=0, microsecond=0)  # Actualiza cada hora
    return get_unique_sensors_from_db(), timestamp

def get_unique_sensors():
    """Obtiene la lista de deviceId únicos con caché"""
    cached_data, _ = get_cached_sensors()
    return cached_data

def get_unique_sensors_from_db():
    """Obtiene la lista de deviceId únicos de Firestore de manera optimizada."""
    try:
        # Mejor enfoque: crear una colección de sensores o usar un documento de metadatos
        # Solución temporal: usar una consulta agregada para reducir datos transferidos
        sensors_ref = db.collection('lecturasSensores').select(['deviceId']).limit(500).stream()
        unique_sensors = set()
        for sensor in sensors_ref:
            unique_sensors.add(sensor.to_dict()['deviceId'])
        return sorted(list(unique_sensors))
    except Exception as e:
        print(f"Error al obtener lista de sensores: {e}")
        return []

def get_sensor_data(device_id, start_date_req=None, end_date_req=None):
    """
    Obtiene datos del sensor de manera optimizada.
    """
    try:
        # 1. Determinar el rango de fechas para la visualización
        query_base = db.collection('lecturasSensores').where('deviceId', '==', device_id)
        
        if start_date_req and end_date_req:
            # Rango específico del usuario
            start_date_vis = start_date_req
            end_date_vis = end_date_req
        elif start_date_req:
            # Solo fecha de inicio (mostrar ese día)
            start_date_vis = start_date_req
            end_date_vis = start_date_req.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif end_date_req:
             # Solo fecha de fin (mostrar ese día)
            start_date_vis = end_date_req.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date_vis = end_date_req
        else:
            # Por defecto: último día registrado
            try:
                # Optimización: limitar a sólo un registro para el último timestamp
                latest_doc = query_base.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(1).stream()
                last_timestamp_utc = next(latest_doc).to_dict()['timestamp']
                last_timestamp_local = convert_to_local_time(last_timestamp_utc)
                start_date_vis = last_timestamp_local.replace(hour=0, minute=0, second=0, microsecond=0)
                end_date_vis = last_timestamp_local.replace(hour=23, minute=59, second=59, microsecond=999999)
            except StopIteration:
                 # No hay datos para este sensor
                return [], [], [], [], None, None, None, None

        # Convertir fechas de visualización a UTC para la consulta
        start_utc_vis = start_date_vis.astimezone(pytz.UTC)
        end_utc_vis = end_date_vis.astimezone(pytz.UTC)

        # 2. Obtener datos para visualización (rango determinado)
        # OPTIMIZACIÓN: Limitar a máximo 1000 lecturas para visualización
        docs_vis = query_base.where('timestamp', '>=', start_utc_vis)\
                             .where('timestamp', '<=', end_utc_vis)\
                             .order_by('timestamp')\
                             .limit(1000)\
                             .stream()
        
        data_vis = []
        for doc in docs_vis:
            data = doc.to_dict()
            ts_local = convert_to_local_time(data['timestamp'])
            data_vis.append((ts_local, data['temperatura'], data['luz'], data['humedad']))
        
        # No hay datos para visualizar
        if not data_vis:
            return [], [], [], [], None, None, None, None
        
        # 3. OPTIMIZACIÓN: Reducir datos para análisis histórico
        # En lugar de siempre traer 100 puntos, usar una muestra más pequeña
        # y solo si realmente se necesita para predicción
        datos_hist_necesarios = 30  # Reducir de 100 a 30
        data_hist = []
        
        # Solo traer históricos si hay suficientes puntos de luz > 100 para predicción
        luz_vis = [d[2] for d in data_vis]
        if sum(1 for l in luz_vis if l > 100) >= 5:  # Solo si hay al menos 5 puntos útiles
            oldest_ts = min(d[0] for d in data_vis)
            oldest_utc = oldest_ts.astimezone(pytz.UTC)
            
            # Traer puntos históricos antes del rango de visualización
            docs_hist = query_base.where('timestamp', '<', start_utc_vis)\
                                  .order_by('timestamp', direction=firestore.Query.DESCENDING)\
                                  .limit(datos_hist_necesarios)\
                                  .stream()
            
            for doc in docs_hist:
                data = doc.to_dict()
                ts_local = convert_to_local_time(data['timestamp'])
                data_hist.append((ts_local, data['temperatura'], data['luz'], data['humedad']))

        # 4. Combinar datos, eliminando duplicados
        all_data = data_vis + data_hist
        timestamps_all = [d[0] for d in all_data]
        temperaturas_all = [d[1] for d in all_data]
        luz_all = [d[2] for d in all_data]
        humedad_all = [d[3] for d in all_data]

        # 5. Analizar depreciación solo si hay suficientes datos
        fechas_pred, luz_pred, fecha_80, max_luz = None, None, None, None
        if len(all_data) >= 10:  # Solo analizar si hay suficientes puntos
            fechas_pred, luz_pred, fecha_80, max_luz = analizar_depreciacion_luz(timestamps_all, luz_all)

        # 6. Preparar datos de VISUALIZACIÓN para retornar
        timestamps_vis = [d[0] for d in data_vis]
        temperaturas_vis = [d[1] for d in data_vis]
        luz_vis = [d[2] for d in data_vis]
        humedad_vis = [d[3] for d in data_vis]

        return timestamps_vis, temperaturas_vis, luz_vis, humedad_vis, \
               fechas_pred, luz_pred, fecha_80, max_luz

    except Exception as e:
        print(f"Error al obtener datos: {e}")
        import traceback
        traceback.print_exc() # Imprimir traceback completo
        return [], [], [], [], None, None, None, None

@app.route('/')
def index():
    try:
        all_sensors = get_unique_sensors()
        default_sensor = all_sensors[0] if all_sensors else None # Manejar caso sin sensores
        device_id = request.args.get('device_id', default_sensor)
        
        # Asegurar que el device_id seleccionado es válido o usar el default
        if not device_id or (all_sensors and device_id not in all_sensors):
             device_id = default_sensor

        if not device_id:
            # No hay sensores disponibles o seleccionados
            return render_template('index.html', error="No hay sensores disponibles o seleccionados.")

        # Procesar fechas del formulario
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')
        start_date, end_date = None, None
        try:
            if start_date_str:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
                start_date = LOCAL_TZ.localize(start_date)
        except ValueError:
            start_date = None # Ignorar fecha inválida
        try:
             if end_date_str:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
                # Para incluir el día completo, ajustar a fin del día
                end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
                end_date = LOCAL_TZ.localize(end_date)
        except ValueError:
            end_date = None # Ignorar fecha inválida

        # Obtener datos: La función ahora maneja la lógica de fechas por defecto/rango
        # y devuelve solo los datos para graficar en las primeras 4 variables
        timestamps, temperaturas, luz, humedad, \
        fechas_pred, luz_pred, fecha_80, max_luz = get_sensor_data(device_id, start_date, end_date)
        
        # Configuración común para las gráficas
        common_layout = dict(
            margin=dict(t=30, r=20, b=40, l=50),
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(family='Arial, sans-serif'),
            xaxis=dict(
                gridcolor='rgba(0,0,0,0.1)',
                showgrid=True,
                zeroline=False
            ),
            yaxis=dict(
                gridcolor='rgba(0,0,0,0.1)',
                showgrid=True,
                zeroline=False
            )
        )
        
        # Gráfica de temperatura
        fig_temp = go.Figure()
        fig_temp.add_trace(go.Scatter(
            x=timestamps,
            y=temperaturas,
            mode='lines',
            name='Temperatura',
            line=dict(color='#2563eb', width=2)
        ))
        fig_temp.update_layout(
            title='Temperatura vs Tiempo',
            yaxis_title='Temperatura (°C)',
            **common_layout
        )
        
        # Gráfica de luz con predicción
        fig_luz = go.Figure()
        if luz:
            # Mantener los valores en lux
            fig_luz.add_trace(go.Scatter(
                x=timestamps,
                y=luz,
                mode='lines',
                name='Luz Actual',
                line=dict(color='#ca8a04', width=2)
            ))
            
            # Línea de predicción si existe
            if fechas_pred is not None and luz_pred is not None:
                # Convertir predicción de porcentaje a lux
                luz_pred_lux = [l * max_luz / 100 for l in luz_pred]
                fig_luz.add_trace(go.Scatter(
                    x=fechas_pred,
                    y=luz_pred_lux,
                    mode='lines',
                    name='Predicción',
                    line=dict(color='#dc2626', width=2, dash='dash')
                ))
                
                # Línea del 80% en lux
                fig_luz.add_hline(y=max_luz * 0.8, line_dash="dash", line_color="red",
                                annotation_text="Límite 80%")
        
        fig_luz.update_layout(
            title='Depreciación de Luz vs Tiempo',
            yaxis_title='Nivel de Luz (lux)',
            **common_layout
        )
        
        # Gráfica de humedad
        fig_hum = go.Figure()
        fig_hum.add_trace(go.Scatter(
            x=timestamps,
            y=humedad,
            mode='lines',
            name='Humedad',
            line=dict(color='#16a34a', width=2)
        ))
        fig_hum.update_layout(
            title='Humedad vs Tiempo',
            yaxis_title='Humedad (%)',
            **common_layout
        )
        
        return render_template('index.html',
                            plot_temp=fig_temp.to_json(),
                            plot_luz=fig_luz.to_json(),
                            plot_hum=fig_hum.to_json(),
                            fecha_80=fecha_80,
                            sensors=all_sensors, 
                            selected_sensor=device_id,
                            start_date=request.args.get('start_date', ''),
                            end_date=request.args.get('end_date', ''))
    except Exception as e:
        print(f"Error en la ruta index: {e}")
        return f"Error: {str(e)}", 500

@app.route('/sensors')
def sensors_endpoint(): # Renombrado para evitar conflicto con la variable
    try:
        unique_sensors = get_unique_sensors()
        return jsonify(unique_sensors)
    except Exception as e:
        print(f"Error en la ruta /sensors: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)
