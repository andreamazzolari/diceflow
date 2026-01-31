# Volgio Monitor (locale)

Prototype completo con backend FastAPI per login, monitoraggi reali e pagamenti.

## Avvio locale

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

API disponibile su `http://localhost:8000`.

## Configurazione

Variabili d'ambiente opzionali:

```bash
export JWT_SECRET="una-stringa-segreta"
export DATABASE_URL="sqlite:///./app.db"
export STRIPE_SECRET_KEY="sk_test_..."
export STRIPE_WEBHOOK_SECRET="whsec_..."
export PAYPAL_CLIENT_ID="your-client-id"
export PAYPAL_CLIENT_SECRET="your-client-secret"
export PAYPAL_MODE="sandbox"
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="587"
export SMTP_USER="user@example.com"
export SMTP_PASSWORD="password"
export SMTP_FROM="Volgio Monitor <noreply@example.com>"
export SMTP_USE_TLS="true"
```

## Flussi principali

- `POST /auth/register` per registrare un utente.
- `POST /auth/token` per login (OAuth2 password flow).
- `POST /monitors` per creare un monitoraggio.
- `POST /monitors/{id}/check` per forzare un check immediato.
- `POST /payments/checkout` per aprire una sessione Stripe di abbonamento.
- `POST /payments/paypal/create` per creare un pagamento PayPal (settimanale/mensile/annuale).

Il worker schedulato controlla i monitoraggi ogni minuto e salva le variazioni.
Se SMTP è configurato, invia una notifica email per errori di fetch e cambiamenti rilevati.
