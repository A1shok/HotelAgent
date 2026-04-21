from sqlalchemy import create_engine, Column, String, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

DATABASE_URL = os.getenv("DB_URL")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    room = Column(String)

    category = Column(String)
    item = Column(String, nullable=True)

    # -----------------------
    # STATUS FLOW
    # -----------------------
    # assigned → active → completed_unverified → completed
    # or → cancelled
    status = Column(String)

    # -----------------------
    # STAFF MAPPING
    # -----------------------
    assigned_to = Column(String, nullable=True)
    department = Column(String)

    # -----------------------
    # PRIORITY
    # -----------------------
    priority = Column(String, default="normal")  # normal | escalated

    # -----------------------
    # CONFIRMATION FLOW
    # -----------------------
    confirmation_required = Column(Boolean, default=False)

    # -----------------------
    # TIMESTAMPS
    # -----------------------
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
