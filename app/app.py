from flask import Flask, jsonify
import os
import psycopg2

app = Flask(__name__)


@app.route("/")
def home():
    return jsonify({
        "application": "Platform Engineer Demo",
        "status": "running"
    })


@app.route("/health")
def health():
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            port=os.getenv("DB_PORT", "5432")
        )

        conn.close()

        return jsonify({
            "status": "healthy",
            "database": "connected"
        }), 200

    except Exception:
        return jsonify({
            "status": "unhealthy",
            "database": "unavailable"
        }), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)