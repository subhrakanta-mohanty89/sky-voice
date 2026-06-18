from app import create_app
from config import Config

app = create_app()

if __name__ == "__main__":
    print(f"🚀 Server running on port {Config.PORT}")
    app.run(host="0.0.0.0", port=Config.PORT, debug=False, use_reloader=False, threaded=True)