import csv
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import os
import re
import smtplib
import sqlite3
import time
import feedparser
import requests

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
EMAIL_SENDER = (os.getenv("EMAIL_SENDER") or "").strip()
EMAIL_PASSWORD = (os.getenv("EMAIL_PASSWORD") or "").strip()
EMAIL_RECIPIENT = (os.getenv("EMAIL_RECIPIENT") or "").strip()

DB_PATH = "veille_history.db"
PWA_JSON_PATH = "docs/articles.json"

CATEGORY_THEMES = {
    "PDP": {"label": "Protection des Données (PDP)", "color": "#2563eb", "bg": "#eff6ff", "badge": "#dbeafe"},
    "CYBER": {"label": "Cybersécurité", "color": "#dc2626", "bg": "#fef2f2", "badge": "#fee2e2"},
    "ANTICOR": {"label": "Anticorruption & Conformité", "color": "#b45309", "bg": "#fffbeb", "badge": "#fef3c7"},
    "PI": {"label": "Propriété Intellectuelle", "color": "#059669", "bg": "#ecfdf5", "badge": "#d1fae5"},
    "COSMETIQUE": {"label": "Cosmétique & Réglementaire", "color": "#db2777", "bg": "#fdf2f8", "badge": "#fce7f3"},
    "INSTITUTIONNEL": {"label": "Institutionnel & Affaires", "color": "#4f46e5", "bg": "#eef2ff", "badge": "#e0e7ff"},
    "AUTRE": {"label": "Autres parutions", "color": "#475569", "bg": "#f8fafc", "badge": "#e2e8f0"}
}

ACTIVE_GEMINI_ENDPOINT = None

# ==========================================
# 1. SÉLECTION ET VALIDATION DU MODÈLE GEMINI
# ==========================================

def init_gemini_endpoint():
    """Valide en priorité le modèle flash-lite le plus stable et disponible."""
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY absente.")
        return None

    priority_models = [
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-flash-latest"
    ]

    test_payload = {
        "contents": [{"parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 5}
    }

    for model_name in priority_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(url, json=test_payload, headers={"Content-Type": "application/json"}, timeout=10)
            if res.status_code == 200:
                print(f"✅ Modèle Gemini validé et opérationnel : {model_name}")
                return url
            else:
                print(f"ℹ️
