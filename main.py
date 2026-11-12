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
import unicodedata
import feedparser
import requests

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
EMAIL_SENDER = (os.getenv("EMAIL_SENDER") or "").strip()
EMAIL_PASSWORD = (os.getenv("EMAIL_PASSWORD") or "").strip()
EMAIL_RECIPIENT = (os.getenv("EMAIL_RECIPIENT") or "").strip()

DB_PATH = "veille_history.db"
PWA_JSON_PATH = "docs/articles.json"
MAX_CLUSTERS_PER_RUN = 40

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

# Mots-clés événementiels écartés avant appel à Gemini pour préserver le quota
NOISE_KEYWORDS = [
    "webinaire", "webinar", "colloque", "symposium", "replay",
    "offre d'emploi", "recrutement", "petit-dejeuner", "petit dejeuner",
    "salon professionnel", "conference annuelle", "matinale juridique"
]

STOPWORDS_FR = {
    "dans", "pour", "avec", "sans", "sous", "vers", "chez", "entre", "sur",
    "les", "des", "une", "aux", "ces", "ses", "leur", "leurs", "plus",
    "tout", "tous", "toute", "toutes", "cette", "quel", "quelle", "quels",
    "quelles", "comment", "pourquoi", "quand", "mise", "jour", "nouveau",
    "nouvelle", "nouveaux", "nouvelles", "projet", "article", "publie",
    "france", "europe", "europeen", "europeenne", "avis", "relative", "relatif"
}

ACTIVE_GEMINI_ENDPOINT = None

# ==========================================
# 1. SÉLECTION ET VALIDATION DU MODÈLE GEMINI
# ==========================================

def init_gemini_endpoint():
    """Valide en priorité le modèle flash-lite le plus stable et disponible."""
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY absente.", flush=True)
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
                print(f"✅ Modèle Gemini validé et opérationnel : {model_name}", flush=True)
                return url
            else:
                print(f"ℹ️ {model_name} non retenu (HTTP {res.status_code})", flush=True)
        except Exception as e:
            print(f"ℹ️ Erreur test sur {model_name} : {e}", flush=True)

    print("❌ Aucun modèle prioritaire n'a validé le test d'envoi.", flush=True)
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
# 3. PRÉ-FILTRAGE & REGROUPEMENT MULTI-SOURCES
# ==========================================

def normalize_text(text):
    nfkd = unicodedata.normalize("NFKD", text or "")
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", ascii_str).strip()

def is_noise_article(title, summary):
    haystack = normalize_text(f"{title} {summary[:250]}")
    return any(kw in haystack for kw in NOISE_KEYWORDS)

def extract_tokens(title):
    clean = re.sub(r"[^a-z0-9\s]", " ", normalize_text(title))
    return {w for w in clean.split() if len(w) >= 4 and w not in STOPWORDS_FR}

def are_titles_related(tokens_a, tokens_b):
    if not tokens_a or not tokens_b:
        return False
    common = tokens_a & tokens_b
    if len(common) < 3:
        return False
    union = tokens_a | tokens_b
    similarity = len(common) / len(union)
    return similarity >= 0.42

def cluster_candidates(candidates):
    """Regroupe les articles de sources différentes couvrant le même événement."""
    clusters = []
    for cand in candidates:
        cand_tokens = extract_tokens(cand["title"])
        matched_cluster = None

        for cluster in clusters:
            existing_sources = {c["source"] for c in cluster}
            if cand["source"] in existing_sources:
                continue
            if are_titles_related(cand_tokens, cluster[0]["_tokens"]):
                matched_cluster = cluster
                break

        cand["_tokens"] = cand_tokens
        if matched_cluster:
            matched_cluster.append(cand)
        else:
            clusters.append([cand])

    return clusters

# ==========================================
# 4. CHARGEMENT DES SOURCES CSV
# ==========================================

def load_sources():
    sources = []
    if not os.path.exists("sources.csv"):
        print("Fichier sources.csv introuvable.", flush=True)
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
# 5. ANALYSE, IMPACT ET SYNTHÈSE (JSON NATIF)
# ==========================================

def calibrate_impact(title, link, source, raw_impact):
    """Recalibre les bulletins techniques ANSSI/CERT-FR courants en IMPORTANT."""
    impact = raw_impact if raw_impact in IMPACT_PRIORITY else "INFORMATIF"
    ref = f"{title} {link}".lower()
    if "certfr-avi" in ref and impact == "CRITIQUE":
        if "exploite" not in normalize_text(title) and "0-day" not in ref:
            return "IMPORTANT"
    return impact

def analyze_with_gemini(title, link="", source="", summary_raw="", extra_sources=None, default_category="AUTO"):
    """Catégorise, évalue le niveau d'impact et synthétise l'article ou le cluster."""
    fallback_cat = default_category if default_category != "AUTO" else "AUTRE"
    if not ACTIVE_GEMINI_ENDPOINT:
        return fallback_cat, "INFORMATIF", ""

    multi_context = ""
    if extra_sources:
        others = ", ".join(f"{s['source']} ({s['title']})" for s in extra_sources[:3])
        multi_context = f"\nAutres sources couvrant le sujet : {others}"

    headers = {"Content-Type": "application/json"}
    prompt = f"""Tu es un analyste juridique et cybersécurité expert. Pour l'actualité suivante :
Source principale : {source}
Titre : {title}
Extrait : {summary_raw[:500]}{multi_context}

Génère un objet JSON avec exactement trois champs :
- "categorie" : une seule valeur parmi PDP, CYBER, ANTICOR, PI, COSMETIQUE, INSTITUTIONNEL, AUTRE
- "impact" : une seule valeur parmi CRITIQUE, IMPORTANT, INFORMATIF
  * CRITIQUE : sanction majeure prononcée, alerte cybersécurité d'exploitation active (CERTFR-ALE / faille critique exploitée), obligation légale immédiate ou arrêt de principe majeur. (Les avis techniques de routine type CERTFR-AVI ne sont PAS critiques).
  * IMPORTANT : avis technique de sécurité (CERTFR-AVI), nouvelle recommandation officielle, projet de texte structurant ou mise à jour réglementaire à anticiper.
  * INFORMATIF : actualité générale, bilan statistique, étude sectorielle ou veille de fond sans action urgente.
- "resume" : une synthèse claire et factuelle de 2 à 3 phrases expliquant l'enjeu juridique, l'impact concret ou l'action requise."""

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

                raw_impact = data.get("impact", "INFORMATIF").strip().upper()
                impact = calibrate_impact(title, link, source, raw_impact)

                ai_summary = data.get("resume", "").strip()
                if ai_summary:
                    multi_tag = f" [+{len(extra_sources)} src]" if extra_sources else ""
                    print(f"   ↳ [{impact}]{multi_tag} {title[:55]}...", flush=True)
                return category, impact, ai_summary

            elif res.status_code in (429, 503) and attempt < 3:
                wait_time = attempt * 4
                print(f"   ⚠️ Surcharge Google ({res.status_code}), pause de {wait_time}s (essai {attempt}/3)...", flush=True)
                time.sleep(wait_time)
                continue
            else:
                print(f"❌ [Gemini HTTP {res.status_code}] : {res.text[:140]}", flush=True)
                break

        except requests.exceptions.RequestException as e:
            if attempt < 3:
                wait_time = attempt * 3
                print(f"   ⚠️ Latence réseau, relance dans {wait_time}s (essai {attempt}/3)...", flush=True)
                time.sleep(wait_time)
                continue
            print(f"❌ [Exception Gemini] : {e}", flush=True)
        except Exception as e:
            print(f"❌ [Erreur parsing Gemini] : {e}", flush=True)
            break

    return fallback_cat, "INFORMATIF", ""

# ==========================================
# 6. COLLECTE, REGROUPEMENT & ANALYSE
# ==========================================

def fetch_new_articles():
    sources = load_sources()
    candidates = []
    cutoff_time = time.time() - (7 * 24 * 3600)
    rss_headers = {"User-Agent": "Mozilla/5.0 (compatible; VeilleJuridiqueBot/1.0)"}
    filtered_noise_count = 0

    for s in sources:
        print(f"Scrutage : {s['source']}...", flush=True)
        try:
            resp = requests.get(s["url"], headers=rss_headers, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)

            for entry in feed.entries:
                link = getattr(entry, "link", None)
                title = getattr(entry, "title", "Sans titre").strip()
                summary_raw = getattr(entry, "summary", "")
                pub_date_raw = getattr(entry, "published", str(datetime.date.today()))

                if not link or is_article_seen(link):
                    continue

                parsed_time = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
                pub_date_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

                if parsed_time:
                    try:
                        entry_timestamp = time.mktime(parsed_time)
                        if entry_timestamp < cutoff_time:
                            mark_article_seen(link, title, pub_date_raw)
                            continue
                        pub_date_iso = datetime.datetime.fromtimestamp(
                            entry_timestamp, tz=datetime.timezone.utc
                        ).isoformat()
                    except Exception:
                        pass

                # Pré-filtrage des événements/webinaires
                if is_noise_article(title, summary_raw):
                    mark_article_seen(link, title, pub_date_raw)
                    filtered_noise_count += 1
                    continue

                candidates.append({
                    "title": title,
                    "link": link,
                    "source": s["source"],
                    "default_category": s["type"],
                    "summary_raw": summary_raw,
                    "pub_date_raw": pub_date_raw,
                    "pub_date_iso": pub_date_iso
                })

        except Exception as e:
            print(f"⚠️ Erreur ou timeout sur {s['source']} : {e}", flush=True)

    if filtered_noise_count > 0:
        print(f"🧹 {filtered_noise_count} annonce(s) événementielle(s) écartée(s) avant analyse.", flush=True)

    if not candidates:
        return []

    clusters = cluster_candidates(candidates)
    print(f"📦 {len(candidates)} article(s) regroupé(s) en {len(clusters)} sujet(s) unique(s).", flush=True)

    new_articles = []
    for cluster in clusters[:MAX_CLUSTERS_PER_RUN]:
        # Le candidat avec le résumé brut le plus riche devient la source principale
        cluster.sort(key=lambda x: len(x.get("summary_raw", "")), reverse=True)
        primary = cluster[0]
        secondaries = [
            {"source": item["source"], "title": item["title"], "link": item["link"]}
            for item in cluster[1:]
        ]

        category, impact, ai_summary = analyze_with_gemini(
            title=primary["title"],
            link=primary["link"],
            source=primary["source"],
            summary_raw=primary["summary_raw"],
            extra_sources=secondaries,
            default_category=primary["default_category"]
        )

        # Pause de 4.2s pour rester sous le plafond gratuit de 15 requêtes/minute
        time.sleep(4.2)

        article_obj = {
            "title": primary["title"],
            "link": primary["link"],
            "source": primary["source"],
            "category": category,
            "impact": impact,
            "summary": ai_summary,
            "pub_date": primary["pub_date_iso"]
        }
        if secondaries:
            article_obj["related_sources"] = secondaries

        new_articles.append(article_obj)

        for item in cluster:
            mark_article_seen(item["link"], item["title"], item["pub_date_raw"])

    new_articles.sort(key=lambda x: IMPACT_PRIORITY.get(x.get("impact", "INFORMATIF"), 3))
    return new_articles

# ==========================================
# 7. EXPORT JSON POUR L'APPLICATION PWA
# ==========================================

def export_to_pwa_json(new_articles, json_path=PWA_JSON_PATH, max_items=200):
    """Met à jour le flux JSON alimentant l'interface mobile PWA sur GitHub Pages."""
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    existing = []
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    seen_links = {art["link"] for art in new_articles}
    for art in new_articles:
        for rel in art.get("related_sources", []):
            seen_links.add(rel["link"])

    merged = new_articles + [art for art in existing if art.get("link") not in seen_links]

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(merged[:max_items], f, ensure_ascii=False, indent=2)
    print(f"Flux PWA mis à jour ({len(merged[:max_items])} articles dans {json_path}).", flush=True)

# ==========================================
# 8. GABARIT HTML DE L'EMAIL
# ==========================================

def build_html_digest(articles):
    date_str = datetime.datetime.now().strftime("%d/%m/%Y")
    grouped = {}
    for art in articles:
        grouped.setdefault(art["category"], []).append(art)

    crit_count = sum(1 for a in articles if a.get("impact") == "CRITIQUE")
    crit_pill = f'<span style="background-color: #dc2626; color: #ffffff; padding: 2px 8px; border-radius: 9999px; font-weight: 700; font-size: 11px; margin-left: 6px;">{crit_count} critique{"s" if crit_count > 1 else ""}</span>' if crit_count > 0 else ""

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
        
        <div style="padding: 28px 28px 20px 28px; background-color: #0f172a; color: #ffffff;">
          <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.2px; color: #94a3b8; margin-bottom: 6px;">
            Brief Quotidien
          </div>
          <h1 style="margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.3px; color: #ffffff;">
            Veille Juridique & Réglementaire
          </h1>
          <div style="margin-top: 10px; font-size: 13px; color: #cbd5e1;">
            <span>📅 {date_str}</span>
            <span style="margin: 0 8px; color: #475569;">•</span>
            <span style="background-color: #1e293b; color: #38bdf8; padding: 2px 8px; border-radius: 9999px; font-weight: 600; font-size: 12px;">
              {len(articles)} sujet{'s' if len(articles) > 1 else ''}
            </span>
            {crit_pill}
          </div>
        </div>

        <div style="padding: 20px 24px;">
    """

    for cat_key, theme in CATEGORY_THEMES.items():
        if cat_key in grouped:
            arts = grouped[cat_key]
            html += f"""
            <div style="margin-top: 24px; margin-bottom: 12px;">
              <div style="display: inline-block; background-color: {theme['bg']}; color: {theme['color']}; border: 1px solid {theme['badge']}; font-weight: 700; font-size: 12px; letter-spacing: 0.4px; text-transform: uppercase; padding: 4px 10px; border-radius: 6px; margin-bottom: 8px;">
                {theme['label']} ({len(arts)})
              </div>

              <div style="border-top: 2px solid {theme['color']}; padding-top: 4px;">
            """

            for art in arts:
                impact_badge = IMPACT_BADGES_HTML.get(art.get("impact", "INFORMATIF"), "")
                rel_sources = art.get("related_sources", [])
                rel_badge = f'<span style="background-color: #e0f2fe; color: #0369a1; font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">+{len(rel_sources)} source{"s" if len(rel_sources) > 1 else ""}</span>' if rel_sources else ""

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

                rel_links_html = ""
                if rel_sources:
                    links = " • ".join(
                        f'<a href="{r["link"]}" target="_blank" style="color: #475569; text-decoration: underline;">{r["source"]}</a>'
                        for r in rel_sources
                    )
                    rel_links_html = f'<div style="margin-top: 6px; font-size: 11.5px; color: #64748b;">Également couvert par : {links}</div>'

                html += f"""
                <div style="padding: 16px 0; border-bottom: 1px solid #f1f5f9;">
                  <div style="margin-bottom: 6px;">
                    <span style="background-color: #f1f5f9; color: #475569; font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 4px;">
                      {art['source']}
                    </span>
                    {impact_badge}
                    {rel_badge}
                  </div>

                  <div>
                    <a href="{art['link']}" target="_blank" style="color: #0f172a; text-decoration: none; font-size: 15px; font-weight: 600; line-height: 1.35; display: inline-block;">
                      {art['title']}
                    </a>
                  </div>

                  {summary_html}
                  {rel_links_html}

                  <div style="margin-top: 8px;">
                    <a href="{art['link']}" target="_blank" style="color: {theme['color']}; text-decoration: none; font-size: 12px; font-weight: 600;">
                      Consulter la publication →
                    </a>
                  </div>
                </div>
                """

            html += "</div></div>"

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
# 9. EXPÉDITION SMTP MULTI-DESTINATAIRES
# ==========================================

def send_email(subject, html_content):
    if not EMAIL_RECIPIENT:
        print("Aucun destinataire configuré.", flush=True)
        return

    raw_recipients = EMAIL_RECIPIENT.replace("\r", "").replace("\n", "").split(",")
    recipients = [email.strip() for email in raw_recipients if email.strip()]

    if not recipients:
        print("Liste de destinataires vide après nettoyage.", flush=True)
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
        print("Aucun nouvel article pertinent aujourd'hui. Fin.", flush=True)
        return

    export_to_pwa_json(articles)

    print(f"{len(articles)} sujet(s) retenu(s). Envoi de l'email...", flush=True)
    html_report = build_html_digest(articles)
    subject = f"Veille Juridique - {datetime.datetime.now().strftime('%d/%m/%Y')} ({len(articles)} sujets)"
    send_email(subject, html_report)
    print("Email envoyé avec succès.", flush=True)

if __name__ == "__main__":
    main()
