from __future__ import annotations

import hashlib
import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Generator

import httpx
import paypalrestsdk
import stripe
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    plan: Mapped[str] = mapped_column(String(50), default="free")

    monitors: Mapped[list[Monitor]] = relationship("Monitor", back_populates="owner")


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    url: Mapped[str] = mapped_column(String(1024))
    interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    owner: Mapped[User] = relationship("User", back_populates="monitors")
    changes: Mapped[list[Change]] = relationship("Change", back_populates="monitor")


class Change(Base):
    __tablename__ = "changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id"))
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    summary: Mapped[str] = mapped_column(String(255))
    diff: Mapped[str] = mapped_column(Text)

    monitor: Mapped[Monitor] = relationship("Monitor", back_populates="changes")


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class Token(BaseModel):
    access_token: str
    token_type: str


class MonitorCreate(BaseModel):
    url: str
    interval_minutes: int = Field(default=30, ge=5, le=1440)


class MonitorOut(BaseModel):
    id: int
    url: str
    interval_minutes: int
    is_active: bool
    last_checked_at: datetime | None

    class Config:
        from_attributes = True


class ChangeOut(BaseModel):
    detected_at: datetime
    summary: str
    diff: str

    class Config:
        from_attributes = True


class CheckoutRequest(BaseModel):
    price_id: str


class PaypalCheckoutRequest(BaseModel):
    interval: str = Field(pattern="^(weekly|monthly|annual)$")


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

app = FastAPI(title="Volgio Monitor API")
scheduler = BackgroundScheduler()
logger = logging.getLogger("volgio")

PAYPAL_PRICES = {
    "weekly": 12.0,
    "monthly": 29.0,
    "annual": 290.0,
}


def get_db() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(email: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": email, "exp": expire}
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenziali non valide",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email: str | None = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError as exc:
        raise credentials_exception from exc
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
    return user


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(engine)
    scheduler.add_job(check_all_monitors, "interval", minutes=1)
    scheduler.start()
    if STRIPE_SECRET_KEY:
        stripe.api_key = STRIPE_SECRET_KEY
    if PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET:
        paypalrestsdk.configure(
            {
                "mode": PAYPAL_MODE,
                "client_id": PAYPAL_CLIENT_ID,
                "client_secret": PAYPAL_CLIENT_SECRET,
            }
        )


@app.on_event("shutdown")
def on_shutdown() -> None:
    scheduler.shutdown(wait=False)


@app.post("/auth/register", response_model=Token)
def register(user: UserCreate, db: Session = Depends(get_db)) -> Token:
    if db.query(User).filter(User.email == user.email).first():
        raise HTTPException(status_code=400, detail="Email già registrata")
    db_user = User(email=user.email, hashed_password=hash_password(user.password))
    db.add(db_user)
    db.commit()
    token = create_access_token(user.email)
    return Token(access_token=token, token_type="bearer")


@app.post("/auth/token", response_model=Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> Token:
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Credenziali non valide")
    token = create_access_token(user.email)
    return Token(access_token=token, token_type="bearer")


@app.get("/me")
def me(current_user: User = Depends(get_current_user)) -> dict[str, str]:
    return {"email": current_user.email, "plan": current_user.plan}


@app.post("/monitors", response_model=MonitorOut)
def create_monitor(
    monitor: MonitorCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MonitorOut:
    db_monitor = Monitor(
        url=monitor.url,
        interval_minutes=monitor.interval_minutes,
        owner=current_user,
    )
    db.add(db_monitor)
    db.commit()
    db.refresh(db_monitor)
    return MonitorOut.model_validate(db_monitor)


@app.get("/monitors", response_model=list[MonitorOut])
def list_monitors(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[MonitorOut]:
    monitors = db.query(Monitor).filter(Monitor.user_id == current_user.id).all()
    return [MonitorOut.model_validate(monitor) for monitor in monitors]


@app.delete("/monitors/{monitor_id}")
def delete_monitor(
    monitor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    monitor = (
        db.query(Monitor)
        .filter(Monitor.id == monitor_id, Monitor.user_id == current_user.id)
        .first()
    )
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitoraggio non trovato")
    db.delete(monitor)
    db.commit()
    return {"status": "deleted"}


@app.post("/monitors/{monitor_id}/check", response_model=list[ChangeOut])
def check_monitor_now(
    monitor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ChangeOut]:
    monitor = (
        db.query(Monitor)
        .filter(Monitor.id == monitor_id, Monitor.user_id == current_user.id)
        .first()
    )
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitoraggio non trovato")
    changes = check_single_monitor(db, monitor)
    return [ChangeOut.model_validate(change) for change in changes]


@app.get("/monitors/{monitor_id}/changes", response_model=list[ChangeOut])
def list_changes(
    monitor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ChangeOut]:
    monitor = (
        db.query(Monitor)
        .filter(Monitor.id == monitor_id, Monitor.user_id == current_user.id)
        .first()
    )
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitoraggio non trovato")
    return [ChangeOut.model_validate(change) for change in monitor.changes]


@app.post("/payments/checkout")
def create_checkout_session(
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe non configurato")
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": payload.price_id, "quantity": 1}],
        success_url="http://localhost:8000/pricing.html?success=true",
        cancel_url="http://localhost:8000/pricing.html?canceled=true",
        customer_email=current_user.email,
    )
    return {"checkout_url": session.url}


@app.post("/payments/paypal/create")
def create_paypal_payment(
    payload: PaypalCheckoutRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="PayPal non configurato")

    price = PAYPAL_PRICES[payload.interval]
    payment = paypalrestsdk.Payment(
        {
            "intent": "sale",
            "payer": {"payment_method": "paypal"},
            "redirect_urls": {
                "return_url": "http://localhost:8000/pricing.html?paypal=success",
                "cancel_url": "http://localhost:8000/pricing.html?paypal=canceled",
            },
            "transactions": [
                {
                    "amount": {"total": f"{price:.2f}", "currency": "EUR"},
                    "description": f"Volgio Monitor piano {payload.interval}",
                }
            ],
        }
    )

    if not payment.create():
        raise HTTPException(status_code=400, detail="Impossibile creare pagamento PayPal")

    approval_url = next(
        (link.href for link in payment.links if link.rel == "approval_url"),
        None,
    )
    if not approval_url:
        raise HTTPException(status_code=400, detail="URL di approvazione mancante")
    return {"approval_url": approval_url}


@app.post("/payments/paypal/execute")
def execute_paypal_payment(
    payment_id: str,
    payer_id: str,
    interval: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="PayPal non configurato")
    if interval not in PAYPAL_PRICES:
        raise HTTPException(status_code=400, detail="Intervallo non valido")

    payment = paypalrestsdk.Payment.find(payment_id)
    if not payment.execute({"payer_id": payer_id}):
        raise HTTPException(status_code=400, detail="Pagamento PayPal fallito")

    current_user.plan = f"pro-{interval}"
    db.commit()
    return {"status": "ok"}


@app.post("/payments/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=400, detail="Webhook non configurato")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Payload non valido") from exc
    except stripe.error.SignatureVerificationError as exc:
        raise HTTPException(status_code=400, detail="Firma non valida") from exc

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = session.get("customer_email")
        if email:
            user = db.query(User).filter(User.email == email).first()
            if user:
                user.plan = "pro"
                db.commit()
    return {"status": "ok"}


def check_all_monitors() -> None:
    with Session(engine) as db:
        monitors = db.query(Monitor).filter(Monitor.is_active == True).all()
        for monitor in monitors:
            if should_check(monitor):
                check_single_monitor(db, monitor)
        db.commit()


def should_check(monitor: Monitor) -> bool:
    if not monitor.last_checked_at:
        return True
    return datetime.utcnow() - monitor.last_checked_at >= timedelta(
        minutes=monitor.interval_minutes
    )


def check_single_monitor(db: Session, monitor: Monitor) -> list[Change]:
    changes: list[Change] = []
    try:
        response = httpx.get(monitor.url, timeout=20)
        response.raise_for_status()
        content = response.text
    except httpx.HTTPError as exc:
        summary = f"Errore fetch: {exc}"
        change = Change(monitor=monitor, summary=summary, diff="")
        db.add(change)
        changes.append(change)
        monitor.last_checked_at = datetime.utcnow()
        notify_change(monitor, change)
        return changes

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if monitor.last_hash and monitor.last_hash != content_hash:
        diff = generate_diff(monitor.last_content or "", content)
        change = Change(
            monitor=monitor,
            summary="Pagina aggiornata",
            diff=diff,
        )
        db.add(change)
        changes.append(change)
        notify_change(monitor, change)

    monitor.last_checked_at = datetime.utcnow()
    monitor.last_hash = content_hash
    monitor.last_content = content
    return changes


def generate_diff(old: str, new: str) -> str:
    import difflib

    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        lineterm="",
    )
    return "\n".join(diff)


def notify_change(monitor: Monitor, change: Change) -> None:
    if not SMTP_HOST or not SMTP_FROM:
        return
    if not monitor.owner or not monitor.owner.email:
        return
    subject = f"Aggiornamento monitoraggio: {monitor.url}"
    diff_preview = change.diff[:2000] if change.diff else "Nessun diff disponibile."
    body = (
        f"Rilevato un cambiamento per {monitor.url}.\n\n"
        f"Riepilogo: {change.summary}\n\n"
        f"Diff:\n{diff_preview}"
    )
    send_email(monitor.owner.email, subject, body)


def send_email(to_address: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = SMTP_FROM
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(message)
    except smtplib.SMTPException as exc:
        logger.warning("Errore invio email: %s", exc)
