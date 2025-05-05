import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import pytz
import os

# --- Configuración ---
CRED_PATH = 'ensayos-rack-firebase-adminsdk-jkauk-cfa68835d5.json'
COLLECTION_NAME = 'lecturasSensores'
TIMESTAMP_FIELD = 'timestamp'
DEVICE_ID_FIELD = 'deviceId' # Nombre del campo para deviceId
TARGET_DEVICE_ID = 'ESP_RACK_FLOWER_CJ15_03' # <--- Revisa que este sea el deviceId exacto
CUTOFF_DATE_STR = '2024-04-11' # Fecha límite (exclusiva)
BATCH_SIZE = 500 # Número de documentos a eliminar por lote
# ---------------------

def delete_old_documents():
    """Elimina documentos antiguos de un deviceId específico en Firestore en lotes."""
    
    # Verificar credenciales
    if not os.path.exists(CRED_PATH):
        print(f"Error: Archivo de credenciales no encontrado en {CRED_PATH}")
        return

    # Validar que TARGET_DEVICE_ID no esté vacío
    if not TARGET_DEVICE_ID:
        print("Error: Debes especificar un TARGET_DEVICE_ID en la configuración.")
        return

    # Inicializar Firebase
    try:
        cred = credentials.Certificate(CRED_PATH)
        if not firebase_admin._apps:
             firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firebase inicializado correctamente.")
    except Exception as e:
        print(f"Error al inicializar Firebase: {e}")
        return

    # Definir fecha límite en UTC
    try:
        cutoff_date_naive = datetime.strptime(CUTOFF_DATE_STR, '%Y-%m-%d')
        cutoff_date_utc = pytz.UTC.localize(cutoff_date_naive)
        print(f"Fecha límite (UTC): {cutoff_date_utc}")
    except ValueError:
        print(f"Error: Formato de fecha inválido '{CUTOFF_DATE_STR}'. Usar YYYY-MM-DD.")
        return

    # Consultar documentos antiguos para el deviceId específico (Sintaxis actualizada)
    print(f"Buscando documentos para {DEVICE_ID_FIELD} = '{TARGET_DEVICE_ID}' y {TIMESTAMP_FIELD} < {cutoff_date_utc}")
    collection_ref = db.collection(COLLECTION_NAME)
    query = collection_ref.where(filter=firestore.FieldFilter(DEVICE_ID_FIELD, '==', TARGET_DEVICE_ID))\
                          .where(filter=firestore.FieldFilter(TIMESTAMP_FIELD, '<', cutoff_date_utc))

    # --- Verificación inicial ---
    try:
        docs_exist_check = query.limit(1).stream()
        if not list(docs_exist_check):
            print("\nVERIFICACIÓN: No se encontraron documentos que cumplan ambos criterios.")
            print("Verifica el TARGET_DEVICE_ID y la fecha, y comprueba los datos en Firestore.")
            return # Salir si no hay nada que hacer
        else:
            print("\nVERIFICACIÓN: Se encontraron documentos que cumplen los criterios. Procediendo a eliminar...")
    except Exception as e:
         print(f"\nError durante la verificación inicial de documentos: {e}")
         print("Esto podría indicar un problema con la consulta o un índice faltante.")
         # Sugerencia: Verifica si Firestore requiere un índice compuesto para esta consulta.
         return
    # ---------------------------

    deleted_count = 0
    while True:
        # Nota: Firestore podría requerir un índice compuesto en (deviceId, timestamp)
        docs_to_delete = query.limit(BATCH_SIZE).stream()
        batch = db.batch()
        batch_count = 0

        for doc in docs_to_delete:
            doc_data = doc.to_dict()
            print(f"Marcando para eliminar: {doc.id} (Device: {doc_data.get(DEVICE_ID_FIELD)}, Timestamp: {doc_data.get(TIMESTAMP_FIELD)})")
            batch.delete(doc.reference)
            batch_count += 1
        
        if batch_count == 0:
            # Este punto no debería alcanzarse si la verificación inicial funciona, 
            # pero se deja como salvaguarda.
            print("No hay más documentos que cumplan los criterios para eliminar en este lote.")
            break 

        try:
            print(f"\nEjecutando lote para eliminar {batch_count} documentos...")
            batch.commit()
            deleted_count += batch_count
            print(f"Lote completado. {deleted_count} documentos eliminados en total.")
        except Exception as e:
            print(f"Error al ejecutar el lote: {e}")
            print("Error en el lote. Deteniendo el script para evitar problemas mayores.")
            break 

    print(f"\nProceso finalizado. Total de documentos eliminados para {TARGET_DEVICE_ID}: {deleted_count}")

if __name__ == '__main__':
    print("--- Iniciando script para eliminar datos antiguos por Device ID ---")
    # Advertencia importante
    print("\n*** ADVERTENCIA ***")
    print("Este script eliminará permanentemente datos de Firestore.")
    print(f"Colección:      {COLLECTION_NAME}")
    print(f"Device ID:      {TARGET_DEVICE_ID}") # Asegúrate que este valor es correcto
    print(f"Condición Fecha: {TIMESTAMP_FIELD} < {CUTOFF_DATE_STR} (UTC)")
    
    if not TARGET_DEVICE_ID:
         print("\nError: Debes configurar el TARGET_DEVICE_ID en el script antes de continuar.")
    else:
        confirm = input("¿Estás seguro de que deseas continuar? (escribe 'si' para confirmar): ")
        if confirm.lower() == 'si':
            delete_old_documents()
        else:
            print("Operación cancelada por el usuario.")
            
    print("--- Script finalizado ---") 