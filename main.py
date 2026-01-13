# main.py (Versión optimizada para múltiples ejecuciones diarias)
import os
from flask import Flask, jsonify
import firebase_admin
from firebase_admin import credentials, auth, firestore
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()

@app.route('/cron/abandoned-reminders', methods=['GET'])
def send_abandoned_reminders():
    now = datetime.now(timezone.utc)
    
    # AJUSTE PRO: Buscamos usuarios que se registraron hace EXACTAMENTE entre 24 y 30 horas.
    # Al ser una ventana de solo 6 horas, si el cron corre a las 12, 16 y 18, 
    # cada usuario solo entrará en el rango una sola vez.
    start_window = now - timedelta(hours=30)
    end_window = now - timedelta(hours=24)
    
    emails_processed = []

    try:
        page = auth.list_users()
        while page:
            for user in page.users:
                created_at = datetime.fromtimestamp(user.user_metadata.creation_timestamp / 1000, timezone.utc)
                
                # Verificar si cae en la ventana de 6 horas (hace un día)
                if start_window < created_at < end_window:
                    
                    # Verificar suscripción activa
                    subscriptions = db.collection('customers').document(user.uid)\
                        .collection('subscriptions')\
                        .where('status', 'in', ['active', 'trialing']).get()

                    if len(subscriptions) == 0:
                        # Evitar duplicidad extra: Verificar si ya le enviamos este recordatorio específico
                        # (Opcional: puedes añadir un campo 'last_reminder' en el perfil del usuario)
                        
                        db.collection('mail').add({
                            'to': user.email,
                            'template': {
                                'name': 'reminder-abandoned-reg',
                                'data': { 'displayName': user.display_name or 'Investigador/a' }
                            }
                        })
                        emails_processed.append(user.email)
            
            page = page.get_next_page()

        return jsonify({"status": "success", "count": len(emails_processed), "processed": emails_processed}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
