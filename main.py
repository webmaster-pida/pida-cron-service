import os
import stripe
from datetime import datetime, timedelta
from flask import Flask, jsonify
import firebase_admin
from firebase_admin import credentials, auth, firestore

# 1. INICIALIZACIÓN
app = Flask(__name__)

# Inicializar Firebase Admin (Usa la cuenta de servicio predeterminada de Cloud Run)
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()

# Configurar Stripe (La llave secreta se lee de las Variables de Entorno)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# IDs de Precios Mensuales (Extraídos de tu main.js)
PRICE_BASIC_USD = "price_1ScEcgGgaloBN5L8BQVnYeFl"
PRICE_BASIC_MXN = "price_1ScnlrGgaloBN5L8fWzCvIFp"

@app.route('/cron/abandoned-reminders', methods=['GET', 'POST'])
def send_abandoned_reminders():
    try:
        now = datetime.now()
        # Límite de 24 horas: solo usuarios registrados antes de ayer a esta hora
        threshold = now - timedelta(hours=24)
        processed_count = 0

        # 2. LISTAR USUARIOS DE FIREBASE AUTH
        users_page = auth.list_users()
        while users_page:
            for user in users_page.users:
                # Convertir timestamp de Firebase a objeto datetime
                created_at = datetime.fromtimestamp(user.user_metadata.creation_timestamp / 1000)
                
                # FILTRO: Creado hace más de 24h
                if created_at < threshold:
                    
                    # 3. OBTENER DOCUMENTO DEL USUARIO Y VERIFICAR UNUBSCRIBE
                    user_ref = db.collection('customers').document(user.uid)
                    user_doc_snap = user_ref.get()
                    user_data = user_doc_snap.to_dict() or {}

                    # VALIDACIÓN CRÍTICA: Si el usuario se dio de baja, lo saltamos inmediatamente
                    if user_data.get('marketing_opt_out') == True:
                        continue

                    # 4. VERIFICAR SUSCRIPCIÓN EN FIRESTORE (Colección de la extensión de Stripe)
                    sub_ref = user_ref.collection('subscriptions')
                    active_subs = sub_ref.where('status', 'in', ['active', 'trialing']).get()

                    if not active_subs:
                        # 5. VERIFICAR SI YA SE LE ENVIÓ EL RECORDATORIO (Prevención de Spam)
                        log_ref = user_ref.collection('pida_logs').document('abandoned_reminder')
                        if not log_ref.get().exists:
                            
                            # 6. DETERMINAR MONEDA Y GENERAR SESIÓN DE STRIPE
                            stripe_customer_id = user_data.get('stripeId') # ID de Stripe si ya existe
                            
                            # Lógica de moneda: Si no hay país en el perfil, por defecto USD
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
                                    subscription_data={'trial_period_days': 5}, # Los 5 días de prueba
                                    success_url='https://pida-ai.com/?payment_status=success',
                                    cancel_url='https://pida-ai.com/?payment_status=canceled'
                                )
                                checkout_url = checkout_session.url
                            except Exception as e:
                                print(f"Error en Stripe para {user.email}: {e}")
                                checkout_url = "https://pida-ai.com/#planes"

                            # 7. ENVIAR CORREO (Escribir en colección 'mail' para la extensión)
                            db.collection('mail').add({
                                'to': user.email,
                                'template': {
                                    'name': 'reminder-abandoned-reg',
                                    'data': {
                                        'displayName': user.display_name or 'Investigador',
                                        'email': user.email, # IMPORTANTE: Para que el link de baja funcione
                                        'checkoutUrl': checkout_url
                                    }
                                }
                            })

                            # 8. MARCAR COMO ENVIADO EN LOS LOGS DEL USUARIO
                            log_ref.set({
                                'sent_at': firestore.SERVER_TIMESTAMP,
                                'email': user.email,
                                'plan_offered': target_price
                            })
                            processed_count += 1

            # Siguiente página de usuarios
            users_page = users_page.get_next_page()

        return jsonify({"status": "success", "processed": processed_count}), 200

    except Exception as e:
        print(f"Error crítico en el cron: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
