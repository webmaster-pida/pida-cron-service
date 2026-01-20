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

    # ... (resto del código igual) ...

    # Lógica cuando se crea una suscripción nueva
    if event['type'] == 'customer.subscription.created':
        subscription = event['data']['object']
        customer_id = subscription['customer']
        
        # 1. Buscar al usuario en Firestore
        cust_query = db.collection('customers').where('stripeId', '==', customer_id).limit(1).get()
        
        if cust_query:
            # Obtenemos el documento y sus datos
            customer_snapshot = cust_query[0]
            uid = customer_snapshot.id
            customer_data = customer_snapshot.to_dict()
            
            # Obtener perfil de usuario (Firestore)
            user_doc = db.collection('users').document(uid).get().to_dict() or {}
            
            # --- CORRECCIÓN NOMBRE: Obtener nombre real desde Stripe ---
            stripe_name = None
            try:
                # CORRECCIÓN: Usamos 'customer_id' (variable existente) en lugar de 'new_customer_id'
                stripe_customer = stripe.Customer.retrieve(customer_id)
                stripe_name = stripe_customer.name
            except Exception: pass

            # Prioridad: 1. Nombre en Stripe, 2. Nombre en Firebase, 3. Default
            user_name = stripe_name or user_doc.get('displayName') or user_doc.get('firstName') or 'Investigador'

            # Definir email final
            email_from_stripe = subscription.get('customer_email')
            final_email = user_doc.get('email') or customer_data.get('email') or email_from_stripe

            # CORRECCIÓN: Usamos 'final_email' para el IF y el envío
            if final_email:
                # 3. ENVIAR CORREO DE BIENVENIDA (Al Usuario)
                db.collection('mail').add({
                    'to': final_email,
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
                            'customerEmail': final_email,
                            'planName': "Nueva Suscripción",
                            'date': datetime.now().strftime("%d/%m/%Y %H:%M")
                        }
                    }
                })
                print(f"✅ Notificaciones enviadas para: {final_email}")
            else:
                print(f"⚠️ Error: Se encontró el usuario {uid} pero NO tiene email registrado en ninguna colección.")
                
            else:
                print(f"⚠️ No se encontró usuario para stripeId: {customer_id}")

    return jsonify({"status": "success"}), 200