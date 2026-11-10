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
# 1. SÉLECTION ET VALIDATION DU MODÈLE
# ==========================================

def init_gemini_endpoint():
    """Valide en priorité le modèle flash-lite le plus stable et disponible."""
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY absente.")
        return None

    # Ordre de priorité : modèles lite ultra-rapides d'abord
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
# 2. DÉDOUBLONNAGE (SQLITE)
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
# 3. CHARGEMENT DU CSV
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
# 4. ANALYSE ET SYNTHÈSE (JSON STRICT NATIF)
# ==========================================

def analyze_with_gemini(title, summary_raw="", default_category="AUTO"):
    """Génère la catégorie et la synthèse opérationnelle avec gestion des quotas."""
    if not ACTIVE_GEMINI_ENDPOINT:
        return (default_category if default_category != "AUTO" else "AUTRE"), ""

    headers = {"Content-Type": "application/json"}
    prompt = f"""Tu es un analyste juridique expert. Pour l'article suivant :
Titre : {title}
Extrait : {summary_raw[:500]}

Génère un objet JSON avec exactement deux champs :
- "categorie" : une seule valeur parmi PDP, CYBER, ANTICOR, PI, COSMETIQUE, INSTITUTIONNEL, AUTRE
- "resume" : une synthèse claire de 2 à 3 phrases expliquant l'enjeu juridique, l'impact direct ou l'action requise."""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 350,
            "response_mime_type": "application/json"
        }
    }

    # 2 tentatives avec backoff si quota ou surcharge temporaire
    for attempt in (1, 2):
        try:
            res = requests.post(ACTIVE_GEMINI_ENDPOINT, json=payload, headers=headers, timeout=20)
            if res.status_code == 200:
                raw_json = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                data = json.loads(raw_json)
                
                category = data.get("categorie", default_category).strip().upper()
                if category not in CATEGORY_LABELS:
                    category = default_category if default_category != "AUTO" else "AUTRE"
                
                ai_summary = data.get("resume", "").strip()
                if ai_summary:
                    print(f"   ↳ Résumé généré ({len(ai_summary)} car.)")
                    return category, ai_summary
                return category, ""

            elif res.status_code in (429, 503) and attempt == 1:
                print(f"   ⚠️ Rate-limit / Surcharge Google ({res.status_code}), pause de 6s avant relance...")
                time.sleep(6)
                continue
            else:
                print(f"❌ [Gemini HTTP {res.status_code}] : {res.text[:140]}")
                break

        except requests.exceptions.RequestException as e:
            if attempt == 1:
                print("   ⚠️ Timeout réseau, relance dans 3s...")
                time.sleep(3)
                continue
            print(f"❌ [Exception Gemini] : {e}")

    return (default_category if default_category != "AUTO" else "AUTRE"), ""

# ==========================================
# 5. SCRUTAGE DES FLUX & RESPECT DES QUOTAS
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

                # Pause de 4.2 secondes pour garantir <= 14 requêtes par minute (limite gratuite : 15 RPM)
                time.sleep(4.2)

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

CATEGORY_THEMES = {
    "PDP": {"label": "Protection des Données (PDP)", "color": "#2563eb", "bg": "#eff6ff", "badge": "#dbeafe"},
    "CYBER": {"label": "Cybersécurité", "color": "#dc2626", "bg": "#fef2f2", "badge": "#fee2e2"},
    "ANTICOR": {"label": "Anticorruption & Conformité", "color": "#b45309", "bg": "#fffbeb", "badge": "#fef3c7"},
    "PI": {"label": "Propriété Intellectuelle", "color": "#059669", "bg": "#ecfdf5", "badge": "#d1fae5"},
    "COSMETIQUE": {"label": "Cosmétique & Réglementaire", "color": "#db2777", "bg": "#fdf2f8", "badge": "#fce7f3"},
    "INSTITUTIONNEL": {"label": "Institutionnel & Affaires", "color": "#4f46e5", "bg": "#eef2ff", "badge": "#e0e7ff"},
    "AUTRE": {"label": "Autres parutions", "color": "#475569", "bg": "#f8fafc", "badge": "#e2e8f0"}
}

def build_html_digest(articles):
    date_str = datetime.datetime.now().strftime("%d %B %Y").capitalize()
    grouped = {}
    for art in articles:
        grouped.setdefault(art["category"], []).append(art)

    # En-tête global
    html = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Veille Juridique</title>
    </head>
    <body style="margin: 0; padding: 24px 12px; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
      <div style="max-width: 640px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05); border: 1px solid #e2e8f0;">
        
        <!-- Header -->
        <div style="padding: 28px 28px 20px 28px; background-color: #0f172a; color: #ffffff;">
          <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.2px; color: #94a3b8; margin-bottom: 6px;">
            Brief Quotidien
          </div>
          <h1 style="margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.3px; color: #ffffff;">
            Veille Juridique & Réglementaire
          </h1>
          <div style="margin-top: 10px; font-size: 13px; color: #cbd5e1; display: flex; align-items: center;">
            <span>📅 {date_str}</span>
            <span style="margin: 0 8px; color: #475569;">•</span>
            <span style="background-color: #1e293b; color: #38bdf8; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 12px;">
              {len(articles)} parution{'s' if len(articles) > 1 else ''}
            </span>
          </div>
        </div>

        <!-- Contenu -->
        <div style="padding: 20px 24px;">
    """

    for cat_key, theme in CATEGORY_THEMES.items():
        if cat_key in grouped:
            arts = grouped[cat_key]
            html += f"""
            <div style="margin-top: 24px; margin-bottom: 12px;">
              <!-- En-tête Catégorie -->
              <div style="display: inline-block; background-color: {theme['bg']}; color: {theme['color']}; border: 1px solid {theme['badge']}; font-weight: 700; font-size: 12px; letter-spacing: 0.4px; text-transform: uppercase; padding: 4px 10px; border-radius: 6px; margin-bottom: 8px;">
                {theme['label']} ({len(arts)})
              </div>

              <!-- Liste des cartes articles -->
              <div style="border-top: 2px solid {theme['color']}; padding-top: 4px;">
            """

            for art in arts:
                summary_html = ""
                if art.get("summary"):
                    summary_html = f"""
                    <div style="margin-top: 10px; background-color: #f8fafc; border-left: 3px solid {theme['color']}; border-radius: 0 6px 6px 0; padding: 10px 12px;">
                      <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #64748b; margin-bottom: 4px;">
                        Analyse synthétique
                      </div>
                      <div style="font-size: 13px; line-height: 1.55; color: #334155;">
                        {art['summary']}
                      </div>
                    </div>
                    """

                html += f"""
                <div style="padding: 16px 0; border-bottom: 1px solid #f1f5f9;">
                  <!-- Meta Source -->
                  <div style="margin-bottom: 6px;">
                    <span style="background-color: #f1f5f9; color: #475569; font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 4px;">
                      {art['source']}
                    </span>
                  </div>

                  <!-- Titre et Lien -->
                  <div>
                    <a href="{art['link']}" target="_blank" style="color: #0f172a; text-decoration: none; font-size: 15px; font-weight: 600; line-height: 1.35; display: inline-block;">
                      {art['title']}
                    </a>
                  </div>

                  <!-- Bloc Synthèse IA -->
                  {summary_html}

                  <!-- Lien direct -->
                  <div style="margin-top: 8px;">
                    <a href="{art['link']}" target="_blank" style="color: {theme['color']}; text-decoration: none; font-size: 12px; font-weight: 600;">
                      Consulter la publication &rarr;
                    </a>
                  </div>
                </div>
                """

            html += "</div></div>"

    # Pied de page
    html += """
        </div>
        <div style="padding: 20px 28px; background-color: #f8fafc; border-top: 1px solid #e2e8f0; text-align: center;">
          <p style="margin: 0; font-size: 12px; color: #64748b; line-height: 1.5;">
            Rapport automatisé généré quotidiennement via <strong>GitHub Actions</strong> & <strong>Google Gemini</strong>.
          </p>
        </div>

      </div>
    </body>
    </html>
    """
    return html

# ==========================================
# 7. ENVOI MULTI-DESTINATAIRES
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
# POINT D'ENTRÉE
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
