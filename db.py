from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
import os

DATABASE_URL = os.getenv("DB_URL")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

Base = declarative_base()

class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    room = Column(String)
    category = Column(String)
    status = Column(String)
    priority = Column(String)
    assigned_to = Column(String)
    escalation_level = Column(Integer)
    quantity = Column(Integer)
    created_at = Column(DateTime)

Base.metadata.create_all(engine)