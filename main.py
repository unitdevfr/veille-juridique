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

IMPACT_PRIORITY = {"CRITIQUE": 1, "IMPORTANT": 2, "INFORMATIF": 3}

IMPACT_BADGES_HTML = {
    "CRITIQUE": '<span style="background-color: #dc2626; color: #ffffff; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; margin-left: 6px; text-transform: uppercase;">Critique</span>',
    "IMPORTANT": '<span style="background-color: #fef3c7; color: #b45309; border: 1px solid #fcd34d; font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 4px; margin-left: 6px; text-transform: uppercase;">Important</span>',
    "INFORMATIF": ""
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
                print(f"ℹ️ {model_name} non retenu (HTTP {res.status_code})")
        except Exception as e:
            print(f"ℹ️ Erreur test sur {model_name} : {e}")

    print("❌ Aucun modèle prioritaire n'a validé le test d'envoi.")
    return None

# ==========================================
# 2. DÉDOUBLONNAGE PERSISTANT (SQLITE)
# ==========================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_articles (
            link TEXT PRIMARY KEY,
            title TEXT,
            published_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_article_seen(link):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_articles WHERE link = ?", (link,))
    seen = cursor.fetchone() is not None
    conn.close()
    return seen

def mark_article_seen(link, title, published_date):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO seen_articles (link, title, published_date) VALUES (?, ?, ?)",
        (link, title, published_date)
    )
    conn.commit()
    conn.close()

# ==========================================
# 3. CHARGEMENT DES SOURCES CSV
# ==========================================

def load_sources():
    sources = []
    if not os.path.exists("sources.csv"):
        print("Fichier sources.csv introuvable.")
        return sources
    with open("sources.csv", mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("url") and row["url"].strip():
                sources.append({
                    "source": row["source"].strip(),
                    "url": row["url"].strip(),
                    "type": row.get("type", "AUTRE").strip()
                })
    return sources

# ==========================================
# 4. ANALYSE, IMPACT ET SYNTHÈSE (JSON NATIF)
# ==========================================

def analyze_with_gemini(title, summary_raw="", default_category="AUTO"):
    """Catégorise, évalue le niveau d'impact et synthétise l'article."""
    fallback_cat = default_category if default_category != "AUTO" else "AUTRE"
    if not ACTIVE_GEMINI_ENDPOINT:
        return fallback_cat, "INFORMATIF", ""

    headers = {"Content-Type": "application/json"}
    prompt = f"""Tu es un analyste juridique et cybersécurité expert. Pour l'article suivant :
Titre : {title}
Extrait : {summary_raw[:500]}

Génère un objet JSON avec exactement trois champs :
- "categorie" : une seule valeur parmi PDP, CYBER, ANTICOR, PI, COSMETIQUE, INSTITUTIONNEL, AUTRE
- "impact" : une seule valeur parmi CRITIQUE, IMPORTANT, INFORMATIF
  * CRITIQUE : sanction majeure, faille de sécurité exploitée, obligation légale avec échéance proche ou arrêt de principe impactant directement l'entreprise.
  * IMPORTANT : nouvelle recommandation officielle, projet de texte structurant, mise à jour réglementaire à anticiper.
  * INFORMATIF : actualité générale, nomination, bilan statistique ou veille de fond sans action urgente.
- "resume" : une synthèse claire de 2 à 3 phrases expliquant l'enjeu juridique, l'impact direct ou l'action requise."""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 380,
            "response_mime_type": "application/json"
        }
    }

    for attempt in (1, 2, 3):
        try:
            res = requests.post(ACTIVE_GEMINI_ENDPOINT, json=payload, headers=headers, timeout=20)
            if res.status_code == 200:
                raw_json = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                clean_json = re.sub(r"^```json\s*|^```\s*|```$", "", raw_json, flags=re.MULTILINE).strip()
                data = json.loads(clean_json)

                category = data.get("categorie", default_category).strip().upper()
                if category not in CATEGORY_THEMES:
                    category = fallback_cat

                impact = data.get("impact", "INFORMATIF").strip().upper()
                if impact not in IMPACT_PRIORITY:
                    impact = "INFORMATIF"

                ai_summary = data.get("resume", "").strip()
                if ai_summary:
                    print(f"   ↳ [{impact}] Résumé généré ({len(ai_summary)} car.)")
                return category, impact, ai_summary

            elif res.status_code in (429, 503) and attempt < 3:
                wait_time = attempt * 4
                print(f"   ⚠️ Surcharge Google ({res.status_code}), pause de {wait_time}s (essai {attempt}/3)...")
                time.sleep(wait_time)
                continue
            else:
                print(f"❌ [Gemini HTTP {res.status_code}] : {res.text[:140]}")
                break

        except requests.exceptions.RequestException as e:
            if attempt < 3:
                wait_time = attempt * 3
                print(f"   ⚠️ Latence réseau, relance dans {wait_time}s (essai {attempt}/3)...")
                time.sleep(wait_time)
                continue
            print(f"❌ [Exception Gemini] : {e}")
        except Exception as e:
            print(f"❌ [Erreur parsing Gemini] : {e}")
            break

    return fallback_cat, "INFORMATIF", ""

# ==========================================
# 5. SCRUTAGE DES FLUX (7 JOURS) & QUOTAS
# ==========================================

def fetch_new_articles():
    sources = load_sources()
    new_articles = []
    # Fenêtre de récupération élargie aux 7 derniers jours (168 heures)
    cutoff_time = time.time() - (7 * 24 * 3600)

    for s in sources:
        print(f"Scrutage : {s['source']}...")
        try:
            feed = feedparser.parse(s["url"])

            for entry in feed.entries:
                link = getattr(entry, "link", None)
                title = getattr(entry, "title", "Sans titre").strip()
                summary_raw
