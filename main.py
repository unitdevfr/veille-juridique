import csv
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os
import smtplib
import sqlite3
import feedparser
import requests

# Secrets injectés par GitHub Actions
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT")

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

def load_sources():
    sources = []
    if not os.path.exists("sources.csv"):
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

def classify_with_gemini(title, summary=""):
    if not GEMINI_API_KEY:
        return "AUTRE"

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    prompt = f"""Tu es un assistant de veille juridique. Classe ce titre dans UNE SEULE de ces catégories strictes :
PDP, CYBER, ANTICOR, PI, COSMETIQUE, INSTITUTIONNEL, AUTRE.

Titre : {title}
Extrait : {summary[:200]}

Réponds uniquement avec le code de la catégorie en majuscules (ex: PDP). Rien d'autre."""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 10}
    }

    try:
        res = requests.post(endpoint, json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            result = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            for cat in CATEGORY_LABELS.keys():
                if cat in result.upper():
                    return cat
        return "AUTRE"
    except Exception as e:
        print(f"Erreur classification Gemini : {e}")
        return "AUTRE"

def fetch_new_articles():
    sources = load_sources()
    new_articles = []
    
    for s in sources:
        print(f"Scrutage : {s['source']}...")
        try:
            feed = feedparser.parse(s["url"])
            for entry in feed.entries:
                link = getattr(entry, "link", None)
                title = getattr(entry, "title", "Sans titre").strip()
                summary = getattr(entry, "summary", "")
                pub_date = getattr(entry, "published", str(datetime.date.today()))

                if not link or is_article_seen(link):
                    continue

                category = classify_with_gemini(title, summary) if s["type"] == "AUTO" else s["type"]

                new_articles.append({
                    "title": title,
                    "link": link,
                    "source": s["source"],
                    "category": category,
                    "pub_date": pub_date
                })
                mark_article_seen(link, title, pub_date)
        except Exception as e:
            print(f"Erreur sur {s['source']} : {e}")

    return new_articles

def build_html_digest(articles):
    date_str = datetime.datetime.now().strftime("%d/%m/%Y")
    grouped = {}
    for art in articles:
        grouped.setdefault(art["category"], []).append(art)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: Arial, sans-serif; background-color: #f7fafc; padding: 20px; color: #2d3748;">
      <div style="max-width: 650px; margin: auto; background: #fff; padding: 25px; border-radius: 8px; border: 1px solid #e2e8f0;">
        <h2 style="color: #1a365d; border-bottom: 2px solid #3182ce; padding-bottom: 8px; margin-top:0;">Veille Juridique Quotidienne</h2>
        <p style="font-size: 13px; color: #718096;">{date_str} — <strong>{len(articles)}</strong> nouvelle(s) publication(s)</p>
    """

    for cat_key, cat_label in CATEGORY_LABELS.items():
        if cat_key in grouped:
            arts = grouped[cat_key]
            html += f"""
            <div style="margin-top: 20px;">
              <div style="background-color: #ebf8ff; color: #2b6cb0; font-weight: bold; font-size: 14px; padding: 6px 10px; border-left: 4px solid #3182ce;">
                {cat_label} ({len(arts)})
              </div>
              <ul style="padding-left: 20px; margin: 10px 0;">
            """
            for art in arts:
                html += f"""
                <li style="margin-bottom: 8px;">
                  <a href="{art['link']}" target="_blank" style="color: #2b6cb0; text-decoration: none; font-weight: bold;">{art['title']}</a>
                  <span style="font-size: 12px; color: #a0aec0;"> — {art['source']}</span>
                </li>
                """
            html += "</ul></div>"

    html += """
        <hr style="border: none; border-top: 1px solid #edf2f7; margin-top: 25px;" />
        <p style="font-size: 11px; color: #a0aec0; text-align: center;">Système automatisé de veille - GitHub Actions</p>
      </div>
    </body></html>
    """
    return html

def send_email(subject, html_content):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())

def main():
    init_db()
    articles = fetch_new_articles()
    if not articles:
        print("Aucun nouvel article. Fin.")
        return

    print(f"{len(articles)} article(s) trouvé(s). Envoi de l'email...")
    html_report = build_html_digest(articles)
    subject = f"Veille Juridique - {datetime.datetime.now().strftime('%d/%m/%Y')} ({len(articles)} articles)"
    send_email(subject, html_report)
    print("Email envoyé.")

if __name__ == "__main__":
    main()
