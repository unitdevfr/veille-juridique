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

CATEGORY_LABELS = {
    "PDP": "🛡️ Protection des Données (PDP)",
    "CYBER": "🔒 Cybersécurité",
    "ANTICOR": "⚖️ Anticorruption & Conformité",
    "PI": "💡 Propriété Intellectuelle",
    "COSMETIQUE": "💄 Cosmétique & Réglementaire",
    "INSTITUTIONNEL": "🏛️ Institutionnel & Affaires",
    "AUTRE": "📌 Autres sujets"
}

ACTIVE_GEMINI_ENDPOINT = None

# ==========================================
# 1. VALIDATION ET SÉLECTION DU MODÈLE GEMINI
# ==========================================

def init_gemini_endpoint():
    """Valide et active en priorité gemini-3.5-flash avec test réel de génération."""
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY absente.")
        return None

    # Ordre de priorité basé sur les modèles actifs de votre compte
    priority_models = [
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
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
# 2. GESTION DU DÉDOUBLONNAGE (SQLITE)
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
# 4. ANALYSE ET RÉSUMÉ EN JSON STRICT
# ==========================================

def analyze_with_gemini(title, summary_raw="", default_category="AUTO"):
    """Catégorise et synthétise l'article en 2-3 phrases."""
    if not ACTIVE_GEMINI_ENDPOINT:
        return (default_category if default_category != "AUTO" else "AUTRE"), ""

    headers = {"Content-Type": "application/json"}
    prompt = f"""Tu es un analyste juridique expert. Pour l'article suivant :
Titre : {title}
Extrait : {summary_raw[:500]}

Génère une réponse au format JSON strict avec exactement ces deux clés :
- "categorie" : une seule valeur parmi PDP, CYBER, ANTICOR, PI, COSMETIQUE, INSTITUTIONNEL, AUTRE
- "resume" : une synthèse claire de 2 à 3 phrases expliquant l'enjeu juridique, l'impact concret ou l'action requise.

Réponds UNIQUEMENT le bloc JSON brut, sans balises Markdown."""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 350
        }
    }

    try:
        res = requests.post(ACTIVE_GEMINI_ENDPOINT, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            clean_json = re.sub(r"^```json\s*|^```\s*|```$", "", raw_text, flags=re.MULTILINE).strip()
            
            try:
                data = json.loads(clean_json)
                category = data.get("categorie", default_category).strip().upper()
                if category not in CATEGORY_LABELS:
                    category = default_category if default_category != "AUTO" else "AUTRE"
                
                ai_summary = data.get("resume", "").strip()
                if ai_summary:
                    print(f"   ↳ Résumé généré ({len(ai_summary)} car.)")
                    return category, ai_summary
            except Exception:
                print("   ⚠️ Repli d'extraction brute")
                return (default_category if default_category != "AUTO" else "AUTRE"), clean_json[:300]
        else:
            print(f"❌ [Gemini HTTP {res.status_code}] : {res.text[:140]}")
    except Exception as e:
        print(f"❌ [Exception Gemini] : {e}")

    return (default_category if default_category != "AUTO" else "AUTRE"), ""

# ==========================================
# 5. SCRUTAGE DES FLUX
# ==========================================

def fetch_new_articles():
    sources = load_sources()
    new_articles = []
    cutoff_time = time.time() - (48 * 3600)
    
    for s in sources:
        print(f"Scrutage : {s['source']}...")
        try:
            feed = feedparser.parse(s["url"])
            
            for entry in feed.entries:
                link = getattr(entry, "link", None)
                title = getattr(entry, "title", "Sans titre").strip()
                summary_raw = getattr(entry, "summary", "")
                pub_date = getattr(entry, "published", str(datetime.date.today()))

                if not link or is_article_seen(link):
                    continue

                parsed_time = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                if parsed_time:
                    try:
                        entry_timestamp = time.mktime(parsed_time)
                        if entry_timestamp < cutoff_time:
                            mark_article_seen(link, title, pub_date)
                            continue
                    except Exception:
                        pass

                category, ai_summary = analyze_with_gemini(
                    title=title, 
                    summary_raw=summary_raw, 
                    default_category=s["type"]
                )

                time.sleep(1.2)

                new_articles.append({
                    "title": title,
                    "link": link,
                    "source": s["source"],
                    "category": category,
                    "summary": ai_summary,
                    "pub_date": pub_date
                })
                mark_article_seen(link, title, pub_date)

        except Exception as e:
            print(f"Erreur sur {s['source']} : {e}")

    return new_articles

# ==========================================
# 6. GABARIT HTML DE L'EMAIL
# ==========================================

def build_html_digest(articles):
    date_str = datetime.datetime.now().strftime("%d/%m/%Y")
    grouped = {}
    for art in articles:
        grouped.setdefault(art["category"], []).append(art)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; padding: 24px; color: #1e293b; margin: 0;">
      <div style="max-width: 680px; margin: auto; background: #ffffff; padding: 28px; border-radius: 8px; border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        
        <h2 style="color: #0f172a; margin: 0 0 6px 0; font-size: 20px;">Veille Réglementaire & Juridique</h2>
        <p style="font-size: 13px; color: #64748b; margin: 0 0 24px 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 12px;">
          {date_str} — <strong>{len(articles)}</strong> publication(s) retenue(s)
        </p>
    """

    for cat_key, cat_label in CATEGORY_LABELS.items():
        if cat_key in grouped:
            arts = grouped[cat_key]
            html += f"""
            <div style="margin-top: 26px;">
              <div style="background-color: #f1f5f9; color: #0f172a; font-weight: 700; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; padding: 6px 12px; border-left: 4px solid #2563eb; border-radius: 2px;">
                {cat_label} ({len(arts)})
              </div>
              <div style="margin-top: 10px;">
            """
            for art in arts:
                summary_block = ""
                if art.get("summary"):
                    summary_block = f"""
                    <div style="margin-top: 8px; font-size: 13px; color: #334155; line-height: 1.5; background-color: #f1f5f9; padding: 10px 12px; border-radius: 4px; border-left: 3px solid #3b82f6;">
                      {art['summary']}
                    </div>
                    """

                html += f"""
                <div style="padding: 14px 0; border-bottom: 1px solid #f1f5f9;">
                  <div>
                    <a href="{art['link']}" target="_blank" style="color: #1d4ed8; text-decoration: none; font-weight: 600; font-size: 14px; line-height: 1.3;">
                      {art['title']}
                    </a>
                    <span style="font-size: 11px; color: #94a3b8; margin-left: 6px;">({art['source']})</span>
                  </div>
                  {summary_block}
                </div>
                """
            html += "</div></div>"

    html += """
        <p style="font-size: 11px; color: #94a3b8; text-align: center; margin-top: 30px; border-top: 1px solid #f1f5f9; padding-top: 14px;">
          Pipeline de veille automatisé • GitHub Actions & Gemini
        </p>
      </div>
    </body></html>
    """
    return html

# ==========================================
# 7. EXPÉDITION MULTI-DESTINATAIRES
# ==========================================

def send_email(subject, html_content):
    if not EMAIL_RECIPIENT:
        print("Aucun destinataire configuré.")
        return

    raw_recipients = EMAIL_RECIPIENT.replace("\r", "").replace("\n", "").split(",")
    recipients = [email.strip() for email in raw_recipients if email.strip()]

    if not recipients:
        print("Liste de destinataires vide après nettoyage.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())

# ==========================================
# POINT D'ENTRÉE PRINCIPAL
# ==========================================

def main():
    global ACTIVE_GEMINI_ENDPOINT
    init_db()
    ACTIVE_GEMINI_ENDPOINT = init_gemini_endpoint()
    
    articles = fetch_new_articles()
    if not articles:
        print("Aucun nouvel article pertinent aujourd'hui. Fin.")
        return

    print(f"{len(articles)} article(s) retenu(s). Envoi de l'email...")
    html_report = build_html_digest(articles)
    subject = f"Veille Juridique - {datetime.datetime.now().strftime('%d/%m/%Y')} ({len(articles)} articles)"
    send_email(subject, html_report)
    print("Email envoyé avec succès.")

if __name__ == "__main__":
    main()
