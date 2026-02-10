import logging

from flask import Flask

from finance.routes import refresh_data, register_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__)
register_routes(app)

# Load data on startup
with app.app_context():
    refresh_data()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
