import os
import stripe
import json
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
import firebase_admin
from firebase_admin import credentials, auth, firestore

# 1. INICIALIZACIÓN
app = Flask(__name__)

# Inicializar Firebase Admin (Usa la cuenta de servicio predeterminada de Cloud Run)
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()

# Configurar Stripe: Se lee de las Variables de Entorno por seguridad
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

# Configuración de Administración PIDA
ADMIN_EMAIL = "contacto@pida-ai.com"

# IDs de Precios Mensuales (Sincronizados con tu Dashboard de Stripe)
PRICE_BASIC_USD = "price_1ScEcgGgaloBN5L8BQVnYeFl"
PRICE_BASIC_MXN = "price_1ScnlrGgaloBN5L8fWzCvIFp"

# --- RUTA 1: CRON DE RECORDATORIOS (EMPUJE DE VENTAS) ---
@app.route('/cron/abandoned-reminders', methods=['GET', 'POST'])
def send_abandoned_reminders():
    try:
        now = datetime.now()
        # Límite de 24 horas: usuarios registrados antes de ayer a esta hora
        threshold = now - timedelta(hours=24)
        processed_count = 0

        # Listar usuarios de Firebase Auth
        users_page = auth.list_users()
        while users_page:
            for user in users_page.users:
                # Convertir timestamp de Firebase a objeto datetime
                created_at = datetime.fromtimestamp(user.user_metadata.creation_timestamp / 1000)
                
                # FILTRO: Creado hace más de 24h
                if created_at < threshold:
                    
                    # OBTENER PERFIL DEL USUARIO (Colección 'users')
                    user_ref = db.collection('users').document(user.uid)
                    user_doc_snap = user_ref.get()
                    user_data = user_doc_snap.to_dict() or {}

                    # VALIDACIÓN DE PRIVACIDAD: Si el usuario se dio de baja, lo saltamos
                    if user_data.get('marketing_opt_out') == True:
                        continue

                    # VERIFICAR SUSCRIPCIÓN ACTIVA (Colección 'customers' de la extensión)
                    cust_ref = db.collection('customers').document(user.uid)
                    sub_ref = cust_ref.collection('subscriptions')
                    active_subs = sub_ref.where('status', 'in', ['active', 'trialing']).get()

                    if not active_subs:
                        # VERIFICAR SI YA SE LE ENVIÓ EL RECORDATORIO (Prevención de Spam)
                        log_ref = user_ref.collection('pida_logs').document('abandoned_reminder')
                        if not log_ref.get().exists:
                            
                            # DETERMINAR MONEDA Y GENERAR SESIÓN DE STRIPE
                            cust_data = cust_ref.get().to_dict() or {}
                            stripe_customer_id = cust_data.get('stripeId')
                            
                            country = user_data.get('country', 'US')
                            target_price = PRICE_BASIC_MXN if country == 'MX' else PRICE_BASIC_USD

                            try:
                                # Crear sesión de Checkout dinámica
                                checkout_session = stripe.checkout.Session.create(
                                    customer=stripe_customer_id,
                                    customer_email=user.email if not stripe_customer_id else None,
                                    payment_method_types=['card'],
                                    line_items=[{'price': target_price, 'quantity': 1}],
                                    mode='subscription',
                                    subscription_data={'trial_period_days': 5},
                                    success_url='https://pida-ai.com/?payment_status=success',
                                    cancel_url='https://pida-ai.com/?payment_status=canceled'
                                )
                                checkout_url = checkout_session.url
                            except Exception as e:
                                print(f"Error en Stripe para {user.email}: {e}")
                                checkout_url = "https://pida-ai.com/#planes"

                            # ENVIAR CORREO (Escribir en colección 'mail')
                            db.collection('mail').add({
                                'to': user.email,
                                'template': {
                                    'name': 'reminder-abandoned-reg',
                                    'data': {
                                        'displayName': user.display_name or 'Investigador',
                                        'email': user.email,
                                        'checkoutUrl': checkout_url
                                    }
                                }
                            })

                            # REGISTRAR ENVÍO
                            log_ref.set({
                                'sent_at': firestore.SERVER_TIMESTAMP,
                                'email': user.email,
                                'plan_offered': target_price
                            })
                            processed_count += 1

            users_page = users_page.get_next_page()
        return jsonify({"status": "success", "processed": processed_count}), 200

    except Exception as e:
        print(f"Error crítico en el cron: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- RUTA 2: WEBHOOK PARA NOTIFICACIONES DE SUSCRIPCIÓN (NUEVO) ---
@app.route('/webhooks/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    # Escuchar cuando se crea una suscripción (incluye periodo de prueba)
    if event['type'] == 'customer.subscription.created':
        subscription = event['data']['object']
        customer_id = subscription['customer']
        
        # 1. Buscar UID en 'customers' por stripeId
        cust_query = db.collection('customers').where('stripeId', '==', customer_id).limit(1).get()
        
        if not cust_query.empty:
            uid = cust_query[0].id
            # 2. Obtener datos del perfil en 'users'
            user_doc = db.collection('users').document(uid).get().to_dict() or {}
            user_email = user_doc.get('email', subscription.get('customer_email'))

            # 3. DISPARAR BIENVENIDA AL INVESTIGADOR (Usa plantilla welcome-trial)
            db.collection('mail').add({
                'to': user_email,
                'template': {
                    'name': 'welcome-trial',
                    'data': { 
                        'displayName': user_doc.get('firstName', 'Investigador')
                    }
                }
            })

            # 4. NOTIFICAR AL ADMINISTRADOR DE PIDA (Usa plantilla admin-notification)
            db.collection('mail').add({
                'to': ADMIN_EMAIL,
                'template': {
                    'name': 'admin-notification',
                    'data': {
                        'customerName': f"{user_doc.get('firstName', '')} {user_doc.get('lastName', '')}",
                        'customerEmail': user_email,
                        'planName': "Plan Mensual (5 días de prueba)",
                        'date': datetime.now().strftime("%d/%m/%Y %H:%M")
                    }
                }
            })

    return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
