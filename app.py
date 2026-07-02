import logging

from flask import Flask

from finance.blueprints import register_blueprints
from finance.data_service import refresh_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__)
register_blueprints(app)

# Load data on startup (runs the idempotent file->SQLite migration first)
with app.app_context():
    refresh_data()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
