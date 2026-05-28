import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

PASSWORD_ITERATIONS = 260_000
SESSION_DAYS = 30


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(512), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    max_projects = Column(Integer, nullable=True)
    disk_limit_mb = Column(Integer, nullable=True)
    memory_limit_mb = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    projects = relationship("Project", back_populates="owner", cascade="all, delete-orphan")
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(128), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", back_populates="sessions")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("name", name="uq_projects_name"),)

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), nullable=False, index=True)
    container_id = Column(String(80), nullable=True, index=True)
    disk_used_mb = Column(Integer, default=0, nullable=False)
    memory_limit_mb = Column(Integer, nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    owner = relationship("User", back_populates="projects")


class AdminSettings(Base):
    __tablename__ = "admin_settings"

    id = Column(Integer, primary_key=True)
    default_max_projects = Column(Integer, default=3, nullable=False)
    default_disk_limit_mb = Column(Integer, default=100, nullable=False)
    default_memory_limit_mb = Column(Integer, default=128, nullable=False)


def init_db():
    Base.metadata.create_all(bind=engine)
    ensure_schema_columns()
    ensure_admin_settings()
    ensure_admin_user()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return secrets.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def make_session_token(db, user: User) -> str:
    token = secrets.token_urlsafe(48)
    db.add(
        UserSession(
            token_hash=hash_session_token(token),
            user_id=user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
        )
    )
    db.commit()
    return token


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_schema_columns():
    inspector = inspect(engine)
    if "users" in inspector.get_table_names():
        ensure_columns(
            "users",
            {
                "max_projects": "INTEGER",
                "disk_limit_mb": "INTEGER",
                "memory_limit_mb": "INTEGER",
            },
        )

    if "projects" in inspector.get_table_names():
        ensure_columns(
            "projects",
            {
                "disk_used_mb": "INTEGER DEFAULT 0 NOT NULL",
                "memory_limit_mb": "INTEGER",
            },
        )


def ensure_columns(table_name: str, columns: dict[str, str]):
    inspector = inspect(engine)
    existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
    with engine.begin() as connection:
        for column_name, definition in columns.items():
            if column_name not in existing_columns:
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"))


def ensure_admin_settings():
    db = SessionLocal()
    try:
        settings = db.query(AdminSettings).filter(AdminSettings.id == 1).first()
        if not settings:
            db.add(AdminSettings(id=1))
            db.commit()
    finally:
        db.close()


def ensure_admin_user():
    admin_email = normalize_email(os.getenv("ADMIN_EMAIL", "admin@example.com"))
    admin_password = os.getenv("ADMIN_PASSWORD", "admin")

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == admin_email).first()
        if admin:
            if not admin.is_admin:
                admin.is_admin = True
                db.commit()
            return

        db.add(
            User(
                email=admin_email,
                password_hash=hash_password(admin_password),
                is_admin=True,
            )
        )
        db.commit()
    finally:
        db.close()
