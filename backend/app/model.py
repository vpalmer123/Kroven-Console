"""
Wraps your LSTM proof-of-concept as a loadable, reusable service object
instead of a script you re-run by hand.

Key idea: load the trained model ONCE at process startup (expensive),
then call .predict() many times (cheap). This is what "model serving"
means — right now your LSTM has no equivalent of this at all.

Drop your actual trained model file in backend/models/lstm_model.h5
(or .keras) and adjust FEATURE_COLUMNS / SEQUENCE_LENGTH to match what
you trained on.
"""

import os
import numpy as np

MODEL_PATH = os.environ.get("LSTM_MODEL_PATH", "app/models/lstm_model.keras")
SEQUENCE_LENGTH = 24  # e.g. last 24 hourly readings -> predict next hour
FEATURE_COLUMNS = ["kwh_consumed", "kwh_solar_generated", "battery_soc_pct"]

_model = None


def _load_model():
    global _model
    if _model is not None:
        return _model
    try:
        import tensorflow as tf
        _model = tf.keras.models.load_model(MODEL_PATH)
    except Exception as e:
        # Model file not present yet — this is expected until you export
        # your trained weights from the notebook into backend/app/models/.
        print(f"[kroven] LSTM model not loaded ({e}). Using naive fallback.")
        _model = "fallback"
    return _model


def predict_next(readings: list[dict]) -> dict:
    """
    readings: list of recent energy_readings rows, most-recent-last,
    already ordered chronologically.

    Returns predicted_kwh plus a rough confidence band, so the frontend
    can show a range instead of a fake-precise single number.
    """
    model = _load_model()

    values = [r.get("kwh_consumed") or 0 for r in readings[-SEQUENCE_LENGTH:]]

    if model == "fallback" or len(values) < SEQUENCE_LENGTH:
        # Naive baseline: mean of recent readings. This is what your
        # 13.5%-better LSTM should be beating — useful as a sanity check
        # and as a safe default when there isn't enough history yet.
        pred = float(np.mean(values)) if values else 0.0
        spread = pred * 0.20
        return {
            "predicted_kwh": round(pred, 3),
            "lower_bound": round(max(pred - spread, 0), 3),
            "upper_bound": round(pred + spread, 3),
            "model_version": "naive-fallback",
        }

    x = np.array(values).reshape(1, SEQUENCE_LENGTH, 1)
    pred = float(model.predict(x, verbose=0)[0][0])
    # TODO: replace this fixed +/-15% with your model's actual validation MAPE
    spread = pred * 0.15
    return {
        "predicted_kwh": round(pred, 3),
        "lower_bound": round(max(pred - spread, 0), 3),
        "upper_bound": round(pred + spread, 3),
        "model_version": "lstm-v1",
    }
