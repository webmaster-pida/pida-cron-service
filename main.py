import os
import stripe
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
import firebase_admin
from firebase_admin import credentials, firestore, auth as firebase_auth

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

# =================================================================
# RUTA 1: WEBHOOK PARA STRIPE (Suscripciones y Fallos)
# =================================================================
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

    # -------------------------------------------------------------
    # A. Lógica cuando se crea una suscripción nueva (ÉXITO)
    # -------------------------------------------------------------
    if event['type'] == 'customer.subscription.created':
        subscription = event['data']['object']
        customer_id = subscription['customer']
        
        # Buscar al usuario en Firestore
        cust_query = db.collection('customers').where('stripeId', '==', customer_id).limit(1).get()
        
        if cust_query:
            customer_snapshot = cust_query[0]
            uid = customer_snapshot.id
            customer_data = customer_snapshot.to_dict()
            
            user_doc = db.collection('users').document(uid).get().to_dict() or {}
            
            stripe_name = None
            try:
                stripe_customer = stripe.Customer.retrieve(customer_id)
                stripe_name = stripe_customer.name
            except Exception: pass

            user_name = stripe_name or user_doc.get('displayName') or user_doc.get('firstName') or 'Investigador'
            email_from_stripe = subscription.get('customer_email')
            final_email = user_doc.get('email') or customer_data.get('email') or email_from_stripe

            if final_email:
                # ENVIAR CORREO DE BIENVENIDA
                db.collection('mail').add({
                    'to': final_email,
                    'template': {
                        'name': 'welcome-trial',
                        'data': { 
                            'displayName': user_name
                        }
                    }
                })

                # ENVIAR NOTIFICACIÓN AL ADMIN
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

    # -------------------------------------------------------------
    # B. Lógica cuando un pago falla (CARRITO ABANDONADO POR RECHAZO)
    # -------------------------------------------------------------
    elif event['type'] == 'payment_intent.payment_failed':
        intent = event['data']['object']
        customer_id = intent.get('customer')
        error_message = intent.get('last_payment_error', {}).get('message', 'Problema con la tarjeta de crédito')

        if customer_id:
            cust_query = db.collection('customers').where('stripeId', '==', customer_id).limit(1).get()
            
            if cust_query:
                customer_snapshot = cust_query[0]
                uid = customer_snapshot.id
                user_doc = db.collection('users').document(uid).get().to_dict() or {}
                
                final_email = user_doc.get('email') or intent.get('receipt_email')
                user_name = user_doc.get('displayName') or user_doc.get('firstName') or 'Investigador'

                if final_email:
                    db.collection('mail').add({
                        'to': final_email,
                        'template': {
                            'name': 'reminder-abandoned-reg',
                            'data': { 
                                'displayName': user_name,
                                'checkoutUrl': "https://pida-ai.com/"  # <--- ¡CORREGIDO AQUÍ!
                            }
                        }
                    })
                    print(f"🛒 Correo de RECUPERACIÓN enviado a: {final_email} (Fallo Stripe: {error_message})")

    return jsonify({"status": "success"}), 200


# =================================================================
# RUTA 2: CRON JOB PARA CARRITOS ABANDONADOS TOTALES (Firebase)
# =================================================================
@app.route('/cron/recover-carts', methods=['GET', 'POST'])
def recover_carts():
    # 🛡️ Seguridad: Token configurable en variables de entorno o usa este por defecto
    token = request.headers.get('X-Cron-Secret')
    if token != os.getenv("CRON_SECRET", "pida_recovery_secret_2026"):
        return jsonify({"error": "No autorizado"}), 401

    now = datetime.now(timezone.utc)
    # Buscamos usuarios que se registraron entre 2 y 3 horas atrás
    start_time = now - timedelta(hours=3)
    end_time = now - timedelta(hours=2)

    recovered_count = 0

    try:
        # Iterar sobre todos los usuarios de Auth en Firebase
        for user_record in firebase_auth.list_users().iterate_all():
            creation_time = datetime.fromtimestamp(user_record.user_metadata.creation_timestamp / 1000, tz=timezone.utc)
            
            # Filtramos los que caen en nuestra ventana de tiempo
            if start_time <= creation_time <= end_time:
                uid = user_record.uid
                
                # Verificamos si este usuario logró pagar o activar su prueba
                cust_doc = db.collection('customers').document(uid).get()
                is_active = False
                
                if cust_doc.exists:
                    status = cust_doc.to_dict().get('status')
                    if status in ['active', 'trialing']:
                        is_active = True
                
                # Si NO está activo, preparamos el rescate
                if not is_active:
                    # Verificamos que no le hayamos enviado un correo antes (Anti-Spam)
                    log_ref = db.collection('recovery_logs').document(uid).get()
                    
                    if not log_ref.exists:
                        user_name = user_record.display_name or "Investigador"
                        final_email = user_record.email
                        
                        if final_email:
                            # Enviamos la plantilla reminder-abandoned-reg
                            db.collection('mail').add({
                                'to': final_email,
                                'template': {
                                    'name': 'reminder-abandoned-reg',
                                    'data': { 
                                        'displayName': user_name,
                                        'checkoutUrl': "https://pida-ai.com/"  # <--- ¡CORREGIDO AQUÍ!
                                    }
                                }
                            })
                            
                            # Registramos en la BD que ya fue notificado
                            db.collection('recovery_logs').document(uid).set({
                                'email': final_email,
                                'sent_at': firestore.SERVER_TIMESTAMP
                            })
                            
                            recovered_count += 1
                            print(f"🛒 Rescate Total - Correo enviado a: {final_email} (Sin suscripción en BD)")

        return jsonify({"status": "success", "emails_sent": recovered_count}), 200

    except Exception as e:
        print(f"Error en Cron Job: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
