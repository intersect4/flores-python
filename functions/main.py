import firebase_functions as functions
from app import app as flask_app

@functions.https_fn.on_request()
def app(req: functions.https_fn.Request) -> functions.https_fn.Response:
    return functions.https_fn.Response.from_flask(flask_app, req) 