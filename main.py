import os
from flask import Flask, jsonify
import firebase_admin
from firebase_admin import credentials, auth, firestore
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# Inicialización automática con la cuenta de servicio predeterminada de Cloud Run
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()

@app.route('/cron/abandoned-reminders', methods=['GET'])
def send_abandoned_reminders():
    """
    Busca usuarios registrados hace 24-48h que no tienen suscripción
    y les envía un correo de recordatorio (carrito abandonado).
    """
    now = datetime.now(timezone.utc)
    # Ventana de tiempo profesional: registrados ayer pero hoy siguen sin pagar
    twenty_four_hours_ago = now - timedelta(hours=24)
    forty_eight_hours_ago = now - timedelta(hours=48)
    
    emails_processed = []

    try:
        # 1. Listar todos los usuarios de Firebase Auth
        page = auth.list_users()
        while page:
            for user in page.users:
                # Convertir el timestamp de Firebase (ms) a objeto datetime
                created_at = datetime.fromtimestamp(user.user_metadata.creation_timestamp / 1000, timezone.utc)
                
                # 2. Filtrar: Solo los que están en la ventana de 24h a 48h
                if forty_eight_hours_ago < created_at < twenty_four_hours_ago:
                    
                    # 3. Verificar si el usuario tiene suscripción en Firestore
                    # Estructura de la extensión Stripe: customers/{uid}/subscriptions/...
                    subscriptions = db.collection('customers').document(user.uid)\
                        .collection('subscriptions')\
                        .where('status', 'in', ['active', 'trialing']).get()

                    # Si la lista de suscripciones está vacía, es un "carrito abandonado"
                    if len(subscriptions) == 0:
                        # 4. DISPARAR CORREO: Insertar en la colección 'mail' para la extensión
                        db.collection('mail').add({
                            'to': user.email,
                            'template': {
                                'name': 'reminder-abandoned-reg', # Nombre del documento en Firestore
                                'data': {
                                    'displayName': user.display_name or 'Investigador'
                                }
                            }
                        })
                        emails_processed.append(user.email)
            
            page = page.get_next_page()

        return jsonify({
            "status": "success", 
            "count": len(emails_processed), 
            "processed": emails_processed
        }), 200

    except Exception as e:
        print(f"Error fatal en el cron: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    # Cloud Run usa la variable de entorno PORT
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
