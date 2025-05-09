from flask import Flask, render_template, jsonify, request
import firebase_admin
from firebase_admin import credentials, db
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

# Inicializar Firebase con RTDB
try:
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://ensayos-rack-default-rtdb.firebaseio.com'
    })
except Exception as e:
    print(f"Error al inicializar Firebase: {e}")
    raise

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
        return sensors
    except Exception as e:
        print(f"Error al obtener lista de sensores: {e}")
        return []

def get_sensor_data(sensor_id, start_date=None, end_date=None):
    """
    Obtiene datos del sensor desde RTDB.
    """
    try:
        # Referencia al sensor específico
        sensor_ref = db.reference(f'sensores/{sensor_id}')
        sensor_data = sensor_ref.get()
        
        if not sensor_data:
            return [], [], [], None, None, None, None

        # Procesar datos por mes
        all_readings = []
        for month, readings in sensor_data.items():
            if isinstance(readings, dict):  # Asegurarse de que es un diccionario
                for date_ts, reading in readings.items():
                    # Convertir timestamp a datetime
                    if 'fecha' in reading:
                        ts = convert_to_local_time(reading['fecha'])
                        
                        # Filtrar por fecha si se proporcionaron rangos
                        if (start_date and ts < start_date) or (end_date and ts > end_date):
                            continue
                            
                        temperatura = reading.get('temperatura', 0)
                        luz = reading.get('luz', 0)
                        
                        all_readings.append((ts, temperatura, luz))
        
        # Ordenar lecturas por timestamp
        all_readings.sort(key=lambda x: x[0])
        
        # Si no hay datos, retornar listas vacías
        if not all_readings:
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
        print(f"Error al obtener datos del sensor {sensor_id}: {e}")
        import traceback
        traceback.print_exc()
        return [], [], [], None, None, None, None

@app.route('/')
def index():
    """Página principal con las 4 tarjetas de sensores"""
    try:
        sensors = get_sensors_list()
        return render_template('index.html', sensors=sensors)
    except Exception as e:
        print(f"Error en la página principal: {e}")
        return render_template('index.html', error=str(e))

@app.route('/sensor/<sensor_id>')
def sensor_detail(sensor_id):
    """Página de detalle para un sensor específico"""
    try:
        # No convertir sensor_id a entero
        
        # Procesar fechas del formulario
        start_date_str = request.args.get('start_date', '')
        end_date_str = request.args.get('end_date', '')
        start_date, end_date = None, None
        
        try:
            if start_date_str:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
                start_date = LOCAL_TZ.localize(start_date)
        except ValueError:
            start_date = None
            
        try:
            if end_date_str:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
                end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
                end_date = LOCAL_TZ.localize(end_date)
        except ValueError:
            end_date = None

        # Obtener datos del sensor
        timestamps, temperaturas, luz, fechas_pred, luz_pred, fecha_80, max_luz = get_sensor_data(
            sensor_id, start_date, end_date
        )
        
        # Configuración común para las gráficas
        common_layout = dict(
            margin=dict(t=30, r=20, b=40, l=50),
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(family='Arial, sans-serif'),
            xaxis=dict(
                gridcolor='rgba(0,0,0,0.1)',
                zerolinecolor='rgba(0,0,0,0.1)'
            ),
            yaxis=dict(
                gridcolor='rgba(0,0,0,0.1)',
                zerolinecolor='rgba(0,0,0,0.1)'
            )
        )
        
        # Crear gráficos solo si hay datos
        if timestamps:
            # Gráfico de temperatura
            fig_temp = go.Figure()
            fig_temp.add_trace(go.Scatter(
                x=timestamps, 
                y=temperaturas,
                mode='lines',
                name='Temperatura',
                line=dict(color='#2563eb', width=2)
            ))
            fig_temp.update_layout(
                title='Temperatura a lo largo del tiempo',
                yaxis_title='Temperatura (°C)',
                **common_layout
            )
            
            # Gráfico de luz
            fig_luz = go.Figure()
            fig_luz.add_trace(go.Scatter(
                x=timestamps, 
                y=luz,
                mode='lines',
                name='Nivel de Luz',
                line=dict(color='#d97706', width=2)
            ))
            
            # Añadir línea de predicción si está disponible
            if fechas_pred and luz_pred:
                fig_luz.add_trace(go.Scatter(
                    x=fechas_pred,
                    y=luz_pred,
                    mode='lines',
                    name='Predicción',
                    line=dict(color='red', width=1, dash='dash')
                ))
                
                # Línea al 80%
                if max_luz:
                    fig_luz.add_shape(
                        type="line",
                        x0=min(timestamps),
                        y0=max_luz * 0.8,
                        x1=max(fechas_pred),
                        y1=max_luz * 0.8,
                        line=dict(color="red", width=1, dash="dot")
                    )
            
            fig_luz.update_layout(
                title='Nivel de Luz a lo largo del tiempo',
                yaxis_title='Luz (lux)',
                **common_layout
            )
            
            # Convertir figuras a JSON para enviar al template
            plot_temp = json.dumps(fig_temp, cls=PlotlyJSONEncoder)
            plot_luz = json.dumps(fig_luz, cls=PlotlyJSONEncoder)
            
            return render_template(
                'sensor_detail.html',
                sensor_id=sensor_id,
                plot_temp=plot_temp,
                plot_luz=plot_luz,
                start_date=start_date_str,
                end_date=end_date_str,
                fecha_80=fecha_80,
                current_temp=temperaturas[-1] if temperaturas else None,
                current_luz=luz[-1] if luz else None
            )
        else:
            return render_template(
                'sensor_detail.html',
                sensor_id=sensor_id,
                error="No hay datos disponibles para este sensor en el rango de fechas seleccionado."
            )
            
    except Exception as e:
        print(f"Error en detalle del sensor: {e}")
        import traceback
        traceback.print_exc()
        return render_template('sensor_detail.html', sensor_id=sensor_id, error=str(e))

@app.route('/api/sensors')
def api_sensors():
    """API para obtener la lista de sensores"""
    return jsonify(get_sensors_list())

@app.route('/api/sensor/<sensor_id>')
def api_sensor_data(sensor_id):
    """API para obtener datos de un sensor específico"""
    try:
        # No convertir sensor_id a entero
        timestamps, temperaturas, luz, _, _, _, _ = get_sensor_data(sensor_id)
        
        # Convertir timestamps a strings para JSON
        timestamps_str = [ts.strftime('%Y-%m-%d %H:%M:%S') for ts in timestamps]
        
        return jsonify({
            'timestamps': timestamps_str,
            'temperatura': temperaturas,
            'luz': luz
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
