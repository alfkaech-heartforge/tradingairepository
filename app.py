import os
import time
import json
import hmac
import base64
import hashlib
import uuid

import requests
from flask import Flask, request, jsonify

# ------------------------
# Config from environment
# ------------------------
BLOFIN_API_KEY = os.environ.get("BLOFIN_API_KEY")
BLOFIN_API_SECRET = os.environ.get("BLOFIN_API_SECRET")
BLOFIN_API_PASSPHRASE = os.environ.get("BLOFIN_API_PASSPHRASE")
TRADINGVIEW_WEBHOOK_SECRET = os.environ.get("TRADINGVIEW_WEBHOOK_SECRET", "")

# BloFin REST base URL (mainnet)
BLOFIN_BASE_URL = "https://openapi.blofin.com"  #  [oai_citation:0‡BloFin](https://docs.blofin.com/index.html)

# Flask app
app = Flask(__name__)


# ------------------------
# BloFin signing helper
# ------------------------
def sign_request(secret: str, method: str, path: str, body: dict | None = None):
    """
    Generate BloFin API request signature + timestamp + nonce.

    BloFin signature format (from docs):
      prehash = path + method + timestamp + nonce + (body_json or "")
      signature = Base64( HMAC_SHA256(secret, prehash).hexdigest().bytes() )
     [oai_citation:1‡BloFin](https://docs.blofin.com/index.html)
    """
    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())

    method = method.upper()
    msg = f"{path}{method}{timestamp}{nonce}"

    body_str = ""
    if body:
        # Use compact JSON: no extra spaces (BloFin is picky about this)  [oai_citation:2‡BloFin](https://docs.blofin.com/index.html)
        body_str = json.dumps(body, separators=(",", ":"))
        msg += body_str

    hex_signature = hmac.new(
        secret.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest().encode()

    signature = base64.b64encode(hex_signature).decode()

    return signature, timestamp, nonce


# ------------------------
# BloFin order helper
# ------------------------
def place_blofin_order(
    inst_id: str,
    side: str,
    size: str,
    order_type: str = "market",
    margin_mode: str = "isolated",
):
    """
    Place a simple futures order on BloFin.

    Docs example uses:
      path = "/api/v1/trade/order"
      body = { "instId", "marginMode", "side", "orderType", "price", "size" }  [oai_citation:3‡BloFin](https://docs.blofin.com/index.html)
    """
    if not (BLOFIN_API_KEY and BLOFIN_API_SECRET and BLOFIN_API_PASSPHRASE):
        raise RuntimeError("BloFin API credentials are not set in environment variables")

    path = "/api/v1/trade/order"
    url = BLOFIN_BASE_URL + path

    body: dict[str, str] = {
        "instId": inst_id,          # e.g. "BTC-USDT-SWAP" or "SOL-USDT-SWAP"
        "marginMode": margin_mode,  # "isolated" or "cross"
        "side": side,               # "buy" or "sell"
        "orderType": order_type,    # "market" for simplicity
        "size": str(size),          # contract size as string
    }

    # For pure market orders, BloFin lets you omit "price" (check your instrument rules)

    signature, timestamp, nonce = sign_request(
        BLOFIN_API_SECRET,
        "POST",
        path,
        body=body,
    )

    headers = {
        "ACCESS-KEY": BLOFIN_API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": BLOFIN_API_PASSPHRASE,
        "Content-Type": "application/json",
    }  #  [oai_citation:4‡BloFin](https://docs.blofin.com/index.html)

    resp = requests.post(url, headers=headers, json=body, timeout=10)

    # Raise for HTTP errors but also return JSON for logging
    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text}

    if not resp.ok:
        raise RuntimeError(f"BloFin order failed: {resp.status_code} {data}")

    return data


# ------------------------
# TradingView webhook route
# ------------------------
@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    """
    Minimal endpoint for TradingView -> BloFin.
    Expected JSON from TradingView alert (example):

    {
      "secret": "YOUR_WEBHOOK_SECRET",
      "instId": "BTC-USDT-SWAP",
      "side": "buy",
      "size": "1"
    }

    In TradingView, set the alert message to custom JSON like above.
    """

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"ok": False, "error": "Invalid or missing JSON"}), 400

    # 1) Verify webhook secret
    if TRADINGVIEW_WEBHOOK_SECRET:
        incoming_secret = str(payload.get("secret", ""))
        if incoming_secret != TRADINGVIEW_WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

    # 2) Extract trading info (adapt this to your TradingView JSON)
    inst_id = payload.get("instId") or payload.get("symbol")
    side = payload.get("side")      # "buy" or "sell"
    size = payload.get("size")      # e.g. "1"

    if not inst_id or not side or not size:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Missing instId/symbol, side, or size in payload",
                    "received": payload,
                }
            ),
            400,
        )

    try:
        order_response = place_blofin_order(
            inst_id=inst_id,
            side=side,
            size=str(size),
        )
    except Exception as e:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(e),
                }
            ),
            500,
        )

    return jsonify({"ok": True, "blofin_response": order_response}), 200


# ------------------------
# Simple health check
# ------------------------
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Trading webhook is running"})


# For local testing: python app.py
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
