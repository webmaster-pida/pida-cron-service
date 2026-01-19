import os
import stripe
import json
from datetime import datetime
from flask import Flask, jsonify, request
import firebase_admin
from firebase_admin import credentials, firestore

# 1. INICIALIZACIÓN
app = Flask(__name__)

# Inicializar Firebase Admin
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()

# Configurar Stripe desde Variables de Entorno
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

# Email del Administrador para notificaciones
ADMIN_EMAIL = "contacto@pida-ai.com"

# --- RUTA ÚNICA: WEBHOOK PARA NOTIFICACIONES ---
@app.route('/webhooks/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        # Verificar firma de seguridad de Stripe
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # Lógica cuando se crea una suscripción nueva
    if event['type'] == 'customer.subscription.created':
        subscription = event['data']['object']
        customer_id = subscription['customer']
        
        # 1. Buscar al usuario en Firestore usando el ID de cliente de Stripe
        # .get() devuelve una lista, por lo que quitamos .empty
        cust_query = db.collection('customers').where('stripeId', '==', customer_id).limit(1).get()
        
        # CORRECCIÓN AQUÍ: Usamos "if cust_query" para ver si la lista tiene datos
        if cust_query:
            uid = cust_query[0].id
            
            # 2. Obtener datos del usuario
            user_doc = db.collection('users').document(uid).get().to_dict() or {}
            user_email = user_doc.get('email', subscription.get('customer_email'))
            user_name = user_doc.get('firstName', 'Investigador')

            # 3. ENVIAR CORREO DE BIENVENIDA (Al Usuario)
            db.collection('mail').add({
                'to': user_email,
                'template': {
                    'name': 'welcome-trial',
                    'data': { 
                        'displayName': user_name
                    }
                }
            })

            # 4. ENVIAR NOTIFICACIÓN (Al Admin)
            db.collection('mail').add({
                'to': ADMIN_EMAIL,
                'template': {
                    'name': 'admin-notification',
                    'data': {
                        'customerName': f"{user_name} {user_doc.get('lastName', '')}",
                        'customerEmail': user_email,
                        'planName': "Nueva Suscripción",
                        'date': datetime.now().strftime("%d/%m/%Y %H:%M")
                    }
                }
            })
            print(f"✅ Notificaciones enviadas para: {user_email}")
        else:
             print(f"⚠️ No se encontró usuario para stripeId: {customer_id}")

    return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))