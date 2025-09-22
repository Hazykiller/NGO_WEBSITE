# backend_prod_ready.py
"""
Flask backend that supports:
- POST /create_order  -> create order (real razorpay if configured, otherwise DUMMY)
- POST /verify_payment -> verify payment, then generate PDF certificate and send email
- GET  /certificate/<filename> -> serve certificate PDF

Environment variables:
- USE_RAZORPAY = '1' to enable real razorpay use (requires RAZORPAY_KEY_ID & RAZORPAY_KEY_SECRET)
- RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET (optional)
- SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_EMAIL  (to send emails)
- FRONTEND_ORIGIN (optional)
"""

import os
import time
import hmac
import json
import uuid
import smtplib
import traceback
from pathlib import Path
from datetime import datetime
from email.utils import formataddr
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email import encoders

from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from dotenv import load_dotenv

load_dotenv()

# Flags / keys
USE_RAZORPAY = os.getenv("USE_RAZORPAY", "0") == "1"
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_dummy")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER or "no-reply@example.com")

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN")

# Try to set up razorpay client if enabled
if USE_RAZORPAY:
    try:
        import razorpay
        razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    except Exception as e:
        print("Failed to import razorpay library:", e)
        USE_RAZORPAY = False

# Flask app
app = Flask(__name__)
CORS(app, resources={r"*": {"origins": "*"}})  # dev: allow all origins; change in production

BASE_DIR = Path(__file__).parent
CERT_DIR = BASE_DIR / "certificates"
CERT_DIR.mkdir(exist_ok=True)

@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "mode": "razorpay" if USE_RAZORPAY else "dummy"})

def _fake_order(amount_inr):
    order_id = f"order_fake_{uuid.uuid4().hex[:12]}"
    return {"id": order_id, "amount": amount_inr * 100, "currency": "INR", "key": RAZORPAY_KEY_ID, "mode": "dummy"}

@app.route("/create_order", methods=["POST"])
def create_order():
    data = request.get_json() or {}
    # We accept name/email/phone but they are not required to create the order in dummy mode
    try:
        amount = int(data.get("amount", 0))
    except Exception:
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400

    if USE_RAZORPAY:
        try:
            order_data = {
                "amount": amount * 100,
                "currency": "INR",
                "receipt": f"rcpt_{int(time.time())}",
                "payment_capture": 1,
            }
            order = razorpay_client.order.create(order_data)
            return jsonify({
                "id": order["id"],
                "amount": order["amount"],
                "currency": order["currency"],
                "key": RAZORPAY_KEY_ID,
                "mode": "razorpay"
            })
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": "Razorpay order creation failed", "details": str(e)}), 500
    else:
        # Dummy order
        return jsonify(_fake_order(amount))

def verify_signature(payload_dict):
    """
    For Razorpay mode, verify signature with Razorpay SDK.
    For Dummy mode, we accept if:
      - payload contains simulate: true, OR
      - razorpay_signature equals "sim_signature".
    We still require razorpay_order_id, razorpay_payment_id, razorpay_signature fields to exist.
    """
    try:
        razorpay_order_id = payload_dict.get("razorpay_order_id")
        razorpay_payment_id = payload_dict.get("razorpay_payment_id")
        razorpay_signature = payload_dict.get("razorpay_signature")
        if not (razorpay_order_id and razorpay_payment_id and razorpay_signature):
            return False, "Missing fields"

        if USE_RAZORPAY:
            try:
                razorpay_client.utility.verify_payment_signature({
                    "razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": razorpay_payment_id,
                    "razorpay_signature": razorpay_signature
                })
                return True, None
            except Exception as e:
                return False, str(e)
        else:
            # Dummy mode
            if payload_dict.get("simulate") is True:
                return True, None
            if razorpay_signature == "sim_signature":
                return True, None
            return False, "Dummy mode requires simulate:true or sim_signature"
    except Exception as e:
        return False, str(e)

@app.route("/verify_payment", methods=["POST"])
def verify_payment():
    payload = request.get_json() or {}
    ok, err = verify_signature(payload)
    if not ok:
        return jsonify({"error": "Signature verification failed", "details": err}), 400

    # If verified, generate certificate and email it.
    try:
        donor_name = payload.get("name", "Donor")
        donor_email = payload.get("email")
        amount = int(payload.get("amount", 0))
        order_id = payload.get("razorpay_order_id", payload.get("order_id", f"order_{int(time.time())}"))

        # Generate PDF certificate
        fname = generate_certificate(donor_name, amount, order_id)
        cert_url = f"/certificate/{fname}"

        # Email with attachment
        email_sent = False
        email_err = None
        if donor_email and SMTP_HOST and SMTP_USER and SMTP_PASS:
            try:
                subject = "Thank you for your donation — Pratibha Charitable Trust"
                body = (
                    f"Dear {donor_name},\n\n"
                    f"Thank you for your generous donation of INR {amount}.\n"
                    f"Please find your donation certificate attached.\n\n"
                    f"Order ID: {order_id}\n"
                    f"Warm regards,\nPratibha Charitable Trust"
                )
                send_email_with_attachment(
                    to_email=donor_email,
                    subject=subject,
                    body=body,
                    attachment_path=str(CERT_DIR / fname)
                )
                email_sent = True
            except Exception as e:
                traceback.print_exc()
                email_err = str(e)

        result = {
            "status": "success",
            "message": "Payment verified" if USE_RAZORPAY else "Dummy payment accepted",
            "payment": {"order_id": order_id, "amount": amount},
            "certificate_url": cert_url,
            "email_sent": email_sent,
            "email_error": email_err
        }
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Internal error generating certificate", "details": str(e)}), 500

@app.route("/certificate/<path:filename>", methods=["GET"])
def serve_certificate(filename):
    full = CERT_DIR / filename
    if not full.exists():
        return abort(404)
    return send_from_directory(str(CERT_DIR), filename, as_attachment=True)

# --- PDF generator ---
def generate_certificate(name, amount, order_id) -> str:
    """
    Creates a clean A4 certificate PDF and returns the filename.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d")
    safe_order = order_id.replace("/", "_")
    filename = f"certificate_{safe_order}_{int(time.time())}.pdf"
    path = CERT_DIR / filename

    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4

    # border
    c.setStrokeColor(colors.HexColor("#6C63FF"))
    c.setLineWidth(6)
    c.rect(1.0*cm, 1.0*cm, w-2.0*cm, h-2.0*cm)

    # heading
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(w/2, h-3.5*cm, "Certificate of Appreciation")

    # NGO name
    c.setFont("Helvetica-Bold", 16)
    c.setFillColor(colors.HexColor("#333333"))
    c.drawCentredString(w/2, h-5.0*cm, "Pratibha Charitable Trust")

    # text
    c.setFont("Helvetica", 12.5)
    text = (
        f"This certificate is proudly presented to\n\n"
        f"{name}\n\n"
        f"for supporting our mission through a donation of INR {amount}.\n"
        f"Order ID: {order_id}    Date: {now}"
    )
    textobj = c.beginText(w/2 - 7.5*cm, h-8.0*cm)
    for line in text.split("\n"):
        textobj.textLine(line)
    c.drawText(textobj)

    # sign
    c.setFont("Helvetica-Oblique", 11)
    c.drawString(2.5*cm, 3.0*cm, "Signature")
    c.line(2.5*cm, 2.9*cm, 7.5*cm, 2.9*cm)

    c.showPage()
    c.save()
    return filename

# --- Email helper ---
def send_email_with_attachment(to_email, subject, body, attachment_path):
    msg = MIMEMultipart()
    # FROM_EMAIL can be "Name <email@domain>"
    if "<" in FROM_EMAIL and ">" in FROM_EMAIL:
        display_name = FROM_EMAIL.split("<")[0].strip()
        sender_email = FROM_EMAIL.split("<")[1].split(">")[0].strip()
        msg["From"] = formataddr((display_name, sender_email))
    else:
        msg["From"] = FROM_EMAIL

    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with open(attachment_path, "rb") as f:
        part = MIMEBase('application', "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename="{Path(attachment_path).name}"')
    msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

if __name__ == "__main__":
    # Don’t expose in prod; use gunicorn/uvicorn and proper CORS
    app.run(host="0.0.0.0", port=5000, debug=True)
