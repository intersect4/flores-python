import os

# Configuración de Firebase para autenticación
# Valores predeterminados que se pueden sobrescribir con variables de entorno
firebaseConfig = {
    "apiKey": os.environ.get("FIREBASE_API_KEY", "AIzaSyBumADL3SiUUo5sOZ5aM1XMQG9VJcETyAQ"),
    "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", "ensayos-rack.firebaseapp.com"),
    "databaseURL": os.environ.get("FIREBASE_DATABASE_URL", "https://ensayos-rack-default-rtdb.firebaseio.com"),
    "projectId": os.environ.get("FIREBASE_PROJECT_ID", "ensayos-rack"),
    "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET", "ensayos-rack.firebasestorage.app"),
    "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "1023723482815"),
    "appId": os.environ.get("FIREBASE_APP_ID", "1:1023723482815:web:eac82f7f734570dbfc600a"),
    "measurementId": os.environ.get("FIREBASE_MEASUREMENT_ID", "G-2HJ62W38J4")
}