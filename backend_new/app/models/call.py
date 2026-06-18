"""
Call Model
==========
SQLAlchemy model for storing call records.
"""

import enum
from datetime import datetime
from typing import Dict, Any

from app.extensions import db


class CallStatus(enum.Enum):
    """Enumeration of possible call statuses."""
    INITIATED = "initiated"
    RINGING = "ringing"
    ANSWERED = "answered"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    CANCELED = "canceled"


class Call(db.Model):
    """Model for storing call records."""
    __tablename__ = 'calls'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    call_uuid = db.Column(db.String(100), unique=True, nullable=False, index=True)
    plivo_call_id = db.Column(db.String(100), nullable=True)
    
    # Call participants
    from_number = db.Column(db.String(20), nullable=False)
    to_number = db.Column(db.String(20), nullable=False)
    
    # Language settings
    customer_language = db.Column(db.String(10), nullable=True, default='hi')
    operator_language = db.Column(db.String(10), nullable=True, default='en')
    
    # Call timing
    initiated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    answered_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    duration_seconds = db.Column(db.Float, nullable=True)
    
    # Call status and metadata
    status = db.Column(db.Enum(CallStatus), default=CallStatus.INITIATED, nullable=False)
    direction = db.Column(db.String(20), default='outbound')  # inbound/outbound
    
    # Recording
    recording_url = db.Column(db.Text, nullable=True)
    recording_duration = db.Column(db.Float, nullable=True)
    
    # Summary and analysis
    call_summary = db.Column(db.Text, nullable=True)
    overall_sentiment = db.Column(db.String(20), nullable=True)
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    transcripts = db.relationship("Transcript", back_populates="call", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        db.Index('idx_call_status', 'status'),
        db.Index('idx_call_initiated_at', 'initiated_at'),
        db.Index('idx_call_from_number', 'from_number'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert call to dictionary."""
        return {
            'id': self.id,
            'call_uuid': self.call_uuid,
            'plivo_call_id': self.plivo_call_id,
            'from_number': self.from_number,
            'to_number': self.to_number,
            'customer_language': self.customer_language,
            'operator_language': self.operator_language,
            'initiated_at': self.initiated_at.isoformat() if self.initiated_at else None,
            'answered_at': self.answered_at.isoformat() if self.answered_at else None,
            'ended_at': self.ended_at.isoformat() if self.ended_at else None,
            'duration_seconds': self.duration_seconds,
            'status': self.status.value if self.status else None,
            'direction': self.direction,
            'recording_url': self.recording_url,
            'call_summary': self.call_summary,
            'overall_sentiment': self.overall_sentiment,
        }
    
    def __repr__(self):
        return f'<Call {self.call_uuid}>'


# ===========================================
# REPOSITORY FUNCTIONS
# ===========================================

def create_call(
    call_uuid: str,
    from_number: str,
    to_number: str,
    customer_language: str = 'hi',
    operator_language: str = 'en',
    direction: str = 'outbound',
    plivo_call_id: str = None
) -> Dict[str, Any]:
    """Create a new call record."""
    call = Call(
        call_uuid=call_uuid,
        plivo_call_id=plivo_call_id,
        from_number=from_number,
        to_number=to_number,
        customer_language=customer_language,
        operator_language=operator_language,
        direction=direction,
        status=CallStatus.INITIATED
    )
    db.session.add(call)
    db.session.commit()
    return call.to_dict()


def update_call_status(call_uuid: str, status: CallStatus, **kwargs) -> Dict[str, Any]:
    """Update call status and other fields."""
    call = Call.query.filter_by(call_uuid=call_uuid).first()
    if call:
        call.status = status
        for key, value in kwargs.items():
            if hasattr(call, key):
                setattr(call, key, value)
        db.session.commit()
        return call.to_dict()
    return None


def get_call(call_uuid: str) -> Dict[str, Any]:
    """Get a call by UUID."""
    call = Call.query.filter_by(call_uuid=call_uuid).first()
    return call.to_dict() if call else None


def get_call_history(limit: int = 50, offset: int = 0) -> list:
    """Get call history with pagination."""
    calls = Call.query.order_by(Call.initiated_at.desc()).offset(offset).limit(limit).all()
    return [call.to_dict() for call in calls]
