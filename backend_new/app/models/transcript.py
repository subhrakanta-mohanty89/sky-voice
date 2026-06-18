"""
Transcript Model
================
SQLAlchemy model for storing conversation transcripts.
"""

import enum
from datetime import datetime
from typing import Dict, Any, Optional

from app.extensions import db


class MessageDirection(enum.Enum):
    """Direction of a message in a call."""
    INBOUND = "inbound"  # From customer
    OUTBOUND = "outbound"  # From operator/AI


class SentimentType(enum.Enum):
    """Sentiment analysis results."""
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    FRUSTRATED = "frustrated"
    SATISFIED = "satisfied"


class Transcript(db.Model):
    """Model for storing conversation transcripts."""
    __tablename__ = 'transcripts'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    call_id = db.Column(db.Integer, db.ForeignKey('calls.id'), nullable=False, index=True)
    
    # Message content
    original_text = db.Column(db.Text, nullable=False)  # Original transcribed text
    translated_text = db.Column(db.Text, nullable=True)  # Translated text
    
    # Language info
    source_language = db.Column(db.String(10), nullable=False)
    target_language = db.Column(db.String(10), nullable=True)
    
    # Direction and speaker
    direction = db.Column(db.Enum(MessageDirection), nullable=False)
    speaker = db.Column(db.String(50), nullable=True)  # 'customer', 'operator', 'ai'
    
    # Timing
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    audio_start_time = db.Column(db.Float, nullable=True)  # Start time in the call (seconds)
    audio_end_time = db.Column(db.Float, nullable=True)  # End time in the call (seconds)
    
    # Confidence and analysis
    transcription_confidence = db.Column(db.Float, nullable=True)
    sentiment = db.Column(db.Enum(SentimentType), nullable=True)
    sentiment_score = db.Column(db.Float, nullable=True)  # -1.0 to 1.0
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    call = db.relationship("Call", back_populates="transcripts")
    
    # Indexes
    __table_args__ = (
        db.Index('idx_transcript_timestamp', 'timestamp'),
        db.Index('idx_transcript_direction', 'direction'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert transcript to dictionary."""
        return {
            'id': self.id,
            'call_id': self.call_id,
            'original_text': self.original_text,
            'translated_text': self.translated_text,
            'source_language': self.source_language,
            'target_language': self.target_language,
            'direction': self.direction.value if self.direction else None,
            'speaker': self.speaker,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'sentiment': self.sentiment.value if self.sentiment else None,
            'sentiment_score': self.sentiment_score,
            'transcription_confidence': self.transcription_confidence,
        }
    
    def __repr__(self):
        return f'<Transcript {self.id} for call {self.call_id}>'


# ===========================================
# REPOSITORY FUNCTIONS
# ===========================================

def add_transcript(
    call_uuid: str,
    original_text: str,
    source_language: str,
    direction: MessageDirection,
    translated_text: Optional[str] = None,
    target_language: Optional[str] = None,
    speaker: Optional[str] = None,
    confidence: Optional[float] = None,
    sentiment: Optional[SentimentType] = None,
    sentiment_score: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    """Add a transcript entry for a call."""
    from app.models.call import Call
    
    call = Call.query.filter_by(call_uuid=call_uuid).first()
    if not call:
        return None
    
    transcript = Transcript(
        call_id=call.id,
        original_text=original_text,
        translated_text=translated_text,
        source_language=source_language,
        target_language=target_language,
        direction=direction,
        speaker=speaker,
        transcription_confidence=confidence,
        sentiment=sentiment,
        sentiment_score=sentiment_score
    )
    db.session.add(transcript)
    db.session.commit()
    return transcript.to_dict()


def get_call_transcripts(call_uuid: str) -> list:
    """Get all transcripts for a call."""
    from app.models.call import Call
    
    call = Call.query.filter_by(call_uuid=call_uuid).first()
    if not call:
        return []
    return [t.to_dict() for t in call.transcripts]
