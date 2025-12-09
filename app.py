import os
import time
import json
import hmac
import base64
import hashlib
import uuid

import requests
from flask import Flask, request, jsonify

# -----------------------------------------------------------------------------
# Configuration from environment
# -----------------------------------------------------------------------------
BLOFIN_API_KEY = os.environ.get("BLOFIN_API_KEY")
BLOFIN_API_SECRET = os.environ.get("BLOFIN_API_SECRET")
BLOFIN_API_PASSPHRASE = os.environ.get("BLOFIN_API_PASSPHRASE")

TRADINGVIEW_WEBHOOK_SECRET = os.environ.get("TRADINGVIEW_WEBHOOK_SECRET", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

# BloFin REST base URL (mainnet)
BLOFIN_BASE_URL = "https://openapi.blofin.com"

# Flask app
app = Flask(__name__)


# -----------------------------------------------------------------------------
# Slack helper
# -----------------------------------------------------------------------------
def send_slack_message(text, extra=None):
    """
    Send a simple message to Slack via incoming webhook.
    This should never crash the app – failures are swallowed.
    """
    if not SLACK_WEBHOOK_URL:
        # No Slack configured; just log and return
        print("Slack webhook URL not configured.")
        return

    payload = {"text": text}

    if extra is not None:
        try:
            pretty = json.dumps(extra, indent=2, default=str)
            payload["text"] += f"\n```{pretty}```"
        except Exception:
            # If extra can't be serialized, just ignore
            pass

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
        if not resp.ok:
            print("Slack returned non-200:", resp.status_code, resp.text)
    except Exception as e:
        print("Error sending Slack message:", e)


# -----------------------------------------------------------------------------
# BloFin signing & order helpers
# -----------------------------------------------------------------------------
def sign_request(secret, method, path, body=None):
    """
    Generate BloFin API request signature + timestamp + nonce.

    prehash string format (according to BloFin docs):
        prehash = path + method + timestamp + nonce + (body_json or "")
    signature = Base64( HMAC_SHA256(secret, prehash).hexdigest().bytes() )
    """
    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())

    method = method.upper()
    message = f"{path}{method}{timestamp}{nonce}"

    if body:
        body_str = json.dumps(body, separators=(",", ":"))
        message += body_str

    hex_sig = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest().encode("utf-8")

    signature = base64.b64encode(hex_sig).decode("utf-8")

    return signature, timestamp, nonce


def place_blofin_order(inst_id, side, size,
                       order_type="market", margin_mode="isolated"):
    """
    Place a simple futures order on BloFin.

    NOTE: if BLOFIN_* env vars are not set, this will raise a RuntimeError.
    """
    if not (BLOFIN_API_KEY and BLOFIN_API_SECRET and BLOFIN_API_PASSPHRASE):
        raise RuntimeError(
            "BloFin API credentials are not set in environment variables."
        )

    path = "/api/v1/trade/order"
    url = BLOFIN_BASE_URL + path

    body = {
        "instId": inst_id,          # e.g. "BTC-USDT-SWAP"
        "marginMode": margin_mode,  # "isolated" or "cross"
        "side": side,               # "buy" or "sell"
        "orderType": order_type,    # "market" etc.
        "size": str(size),          # size as string
        # price can be omitted for pure market orders
    }

    signature, timestamp, nonce = sign_request(
        BLOFIN_API_SECRET, "POST", path, body=body
    )

    headers = {
        "ACCESS-KEY": BLOFIN_API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": BLOFIN_API_PASSPHRASE,
        "Content-Type": "application/json",
    }

    resp = requests.post(url, headers=headers, json=body, timeout=10)

    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text}

    if not resp.ok:
        raise RuntimeError(f"BloFin order failed: {resp.status_code} {data}")

    return data


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Trading webhook is running"})


@app.route("/webhook", methods=["POST"])
def tradingview_webhook():
    """
    Endpoint for TradingView alerts.

    Example TradingView JSON message:

    {
      "secret": "YOUR_TV_SECRET",
      "instId": "BTC-USDT-SWAP",
      "side": "buy",
      "size": "1"
    }
    """
    payload = request.get_json(silent=True)
    if payload is None:
        send_slack_message(
            "❌ TradingView webhook received invalid JSON",
            extra={"raw_body": request.data.decode(errors="ignore")}
        )
        return jsonify({"ok": False, "error": "Invalid or missing JSON"}), 400

    # 1) Secret validation (if configured)
    if TRADINGVIEW_WEBHOOK_SECRET:
        incoming_secret = str(payload.get("secret", ""))
        if incoming_secret != TRADINGVIEW_WEBHOOK_SECRET:
            send_slack_message(
                "❌ Unauthorized TradingView webhook (secret mismatch)",
                extra={"payload": payload}
            )
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

    # 2) Extract trading info
    inst_id = payload.get("instId") or payload.get("symbol")
    side = payload.get("side")
    size = payload.get("size")

    if not inst_id or not side or not size:
        send_slack_message(
            "❌ TradingView webhook missing required fields",
            extra={"payload": payload}
        )
        return jsonify({
            "ok": False,
            "error": "Missing instId/symbol, side, or size in payload",
            "received": payload,
        }), 400

    # 3) Notify Slack that we got a valid webhook
    send_slack_message(
        "📩 TradingView webhook received",
        extra={"instId": inst_id, "side": side, "size": size, "payload": payload}
    )

    # 4) Try to place BloFin order
    try:
        order_response = place_blofin_order(
            inst_id=inst_id,
            side=side,
            size=str(size),
        )
    except Exception as e:
        # Report error to Slack but still respond to TradingView
        send_slack_message(
            "💥 Error placing BloFin order",
            extra={
                "error": str(e),
                "instId": inst_id,
                "side": side,
                "size": size,
            },
        )
        return jsonify({"ok": False, "error": str(e)}), 500

    # 5) Success → notify Slack
    send_slack_message(
        "✅ BloFin order placed successfully",
        extra={
            "instId": inst_id,
            "side": side,
            "size": size,
            "blofin_response": order_response,
        },
    )

    return jsonify({"ok": True, "blofin_response": order_response}), 200


# -----------------------------------------------------------------------------
# Local dev entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # For local testing only. Render will use `gunicorn app:app`.
    app.run(host="0.0.0.0", port=5000, debug=True)
