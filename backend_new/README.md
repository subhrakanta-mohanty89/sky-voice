# QuickHyr Voice Backend

Real-time AI Voice Calling System with multi-language support.

## Features

- 🎤 **Real-time Speech-to-Text** (Deepgram Nova-3)
- 🗣️ **Text-to-Speech** (Google Cloud Neural voices)
- 🌐 **Multi-language Translation** (7+ languages)
- 📞 **Plivo Voice Integration** (Inbound/Outbound calls)
- 💬 **WebSocket Operator Chat** (Real-time bidirectional)
- 🔄 **Audio Streaming** (Plivo Media Streams)

## Project Structure

```
backend_new/
├── main.py                    # Entry point (CLI commands)
├── config.py                  # Configuration class
├── requirements.txt           # Dependencies
├── .env.example              # Environment template
├── .gitignore                # Git ignore rules
└── app/
    ├── __init__.py           # Application factory
    ├── extensions.py         # Flask extensions (db, sock, cors)
    ├── models/
    │   ├── __init__.py       # Model exports
    │   ├── call.py           # Call model + CallStatus enum
    │   ├── transcript.py     # Transcript model + enums
    │   ├── cache_models.py   # TranslationCache, TTSCache
    │   └── metrics.py        # CallMetrics model
    ├── views/
    │   ├── __init__.py       # Blueprint exports
    │   ├── plivo_routes.py   # Voice webhooks
    │   ├── call_management.py # Call CRUD
    │   ├── operator_chat.py  # WebSocket chat
    │   └── media_streams.py  # Audio streaming
    ├── services/
    │   ├── __init__.py       # Service exports
    │   ├── translation.py    # Google Translation
    │   ├── tts.py            # Google Cloud TTS
    │   ├── stt.py            # Deepgram STT
    │   └── ai_client.py      # OpenAI client
    └── utils/
        ├── __init__.py       # Utils exports
        ├── helpers.py        # Shutdown, decorators
        ├── security.py       # Rate limiting, sanitization
        ├── cache.py          # LRU cache with TTL
        ├── metrics.py        # Metrics collection
        └── exceptions.py     # Custom exceptions
```

## Installation

1. **Clone and setup:**
   ```bash
   cd backend_new
   python -m venv venv
   venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   copy .env.example .env
   # Edit .env with your API keys
   ```

3. **Initialize database:**
   ```bash
   python main.py init-db
   ```

4. **Run development server:**
   ```bash
   python main.py run
   ```

## API Endpoints

### Calls
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/make-call` | Initiate outbound call |
| GET | `/active-calls` | List active calls |
| POST | `/answer-call/<uuid>` | Answer inbound call |
| POST | `/hold-call/<uuid>` | Put call on hold |
| POST | `/unhold-call/<uuid>` | Resume call |
| POST | `/end-call/<uuid>` | End call |
| GET | `/call-history` | Get call history |

### WebSocket
| Endpoint | Description |
|----------|-------------|
| `/operator-ws/<uuid>` | Operator chat WebSocket |
| `/media-stream/<uuid>` | Plivo audio stream |

### Plivo Webhooks
| Endpoint | Description |
|----------|-------------|
| `/voice` | Answer URL (language selection) |
| `/language-selection` | DTMF language input |
| `/call-status` | Status callbacks |

## Supported Languages

| # | Language | Code | Native |
|---|----------|------|--------|
| 1 | Hindi | hi | हिंदी |
| 2 | English | en | English |
| 3 | Kannada | kn | ಕನ್ನಡ |
| 4 | Marathi | mr | मराठी |
| 5 | Tamil | ta | தமிழ் |
| 6 | Telugu | te | తెలుగు |
| 7 | Urdu | ur | اردو |

## Production Deployment

```bash
# Using Gunicorn with Gevent
gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
    -w 4 -b 0.0.0.0:5000 "app:create_app()"
```

## Environment Variables

See [.env.example](.env.example) for all configuration options.

**Required:**
- `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_PHONE_NUMBER`
- `DEEPGRAM_API_KEY`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `BASE_URL` (public ngrok/server URL)

## License

Proprietary - QuickHyr
