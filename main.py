import csv
import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import html as html_lib
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
PWA_PUBLIC_URL = "https://unitdevfr.github.io/veille-juridique/"

MAX_PER_SOURCE = 8
MAX_CANDIDATES_PER_RUN = 80
BATCH_SIZE = 25

CATEGORY_THEMES = {
    "PDP": {"label": "Protection des Données (PDP)", "color": "#2563eb", "bg": "#eff6ff", "badge": "#dbeafe"},
    "CYBER": {"label": "Cybersécurité", "color": "#dc2626", "bg": "#fef2f2", "badge": "#fee2e2"},
    "ANTICOR": {"label": "Anticorruption & Conformité", "color": "#b45309", "bg": "#fffbeb", "badge": "#fef3c7"},
    "PI": {"label": "Propriété Intellectuelle", "color": "#059669", "bg": "#ecfdf5", "badge": "#d1fae5"},
    "COSMETIQUE": {"label": "Cosmétique & Réglementaire", "color": "#db2777", "bg": "#fdf2f8", "badge": "#fce7f3"},
    "INSTITUTIONNEL": {"label": "Institutionnel & Affaires", "color": "#4f46e5", "bg": "#eef2ff", "badge": "#e0e7ff"},
    "AUTRE": {"label": "Autres parutions", "color": "#475569", "bg": "#f8fafc", "badge": "#e2e8f0"}
}

OFFICIAL_SOURCES = {
    "Légifrance JORF",
    "Cour de cassation",
    "Sénat - Textes récents",
    "Assemblée Nationale",
    "DAJ Bercy",
    "DGCCRF",
    "EUR-Lex Actualités",
    "Curia - CJUE",
    "EU Law Tracker",
    "Union Européenne - Presse",
    "CNIL",
    "EDPB (Comité Européen Data)",
    "NOYB",
    "ANSSI (CERT-FR)",
    "Cybermalveillance.gouv.fr",
    "AFA (Anticorruption)",
    "INPI",
    "EUIPO"
}

IMPACT_PRIORITY = {"CRITIQUE": 1, "IMPORTANT": 2, "INFORMATIF": 3}

NOISE_KEYWORDS = [
    "webinaire", "webinar", "colloque", "symposium", "replay",
    "offre d'emploi", "recrutement", "petit-dejeuner", "petit dejeuner",
    "salon professionnel", "conference annuelle", "matinale juridique"
]

ACTIVE_GEMINI_ENDPOINT = None

# ==========================================
# 1. SÉLECTION DU MODÈLE GEMINI
# ==========================================

def init_gemini_endpoint():
    """Sélectionne le modèle Gemini disponible pour l'analyse par lot."""
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY absente.", flush=True)
        return None

    priority_models = [
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
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
                print(f"✅ Modèle Gemini opérationnel : {model_name}", flush=True)
                return url
            else:
                print(f"ℹ️ {model_name} non retenu (HTTP {res.status_code})", flush=True)
        except Exception as e:
            print(f"ℹ️ Erreur test sur {model_name} : {e}", flush=True)

    print("❌ Aucun modèle Gemini disponible.", flush=True)
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
# 3. PRÉ-FILTRAGE & CHARGEMENT DES SOURCES
# ==========================================

def normalize_text(text):
    nfkd = unicodedata.normalize("NFKD", text or "")
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", ascii_str).strip()

def is_noise_article(title, summary):
    haystack = normalize_text(f"{title} {summary[:250]}")
    return any(kw in haystack for kw in NOISE_KEYWORDS)

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
# 4. COLLECTE ÉQUILIBRÉE DES CANDIDATS
# ==========================================

def collect_candidates():
    sources = load_sources()
    candidates = []
    seen_in_run = set()
    cutoff_time = time.time() - (7 * 24 * 3600)
    rss_headers = {"User-Agent": "Mozilla/5.0 (compatible; VeilleJuridiqueBot/2.0)"}
    noise_skipped = 0

    for s in sources:
        if len(candidates) >= MAX_CANDIDATES_PER_RUN:
            print(f"⏹️ Plafond global de {MAX_CANDIDATES_PER_RUN} candidats atteint.", flush=True)
            break

        print(f"Scrutage : {s['source']}...", flush=True)
        source_count = 0
        try:
            resp = requests.get(s["url"], headers=rss_headers, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)

            for entry in feed.entries:
                if source_count >= MAX_PER_SOURCE or len(candidates) >= MAX_CANDIDATES_PER_RUN:
                    break

                link = getattr(entry, "link", None)
                title = getattr(entry, "title", "Sans titre").strip()
                summary_raw = re.sub(r"<[^>]+>", " ", getattr(entry, "summary", ""))
                summary_raw = re.sub(r"\s+", " ", summary_raw).strip()
                pub_date_raw = getattr(entry, "published", str(datetime.date.today()))

                if not link or link in seen_in_run or is_article_seen(link):
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

                if is_noise_article(title, summary_raw):
                    mark_article_seen(link, title, pub_date_raw)
                    noise_skipped += 1
                    continue

                seen_in_run.add(link)
                source_count += 1
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
            print(f"⚠️ Erreur sur {s['source']} : {e}", flush=True)

    if noise_skipped > 0:
        print(f"🧹 {noise_skipped} annonce(s) événementielle(s) écartée(s).", flush=True)

    return candidates

# ==========================================
# 5. ANALYSE PAR LOT & REGROUPEMENT SÉMANTIQUE
# ==========================================

def calibrate_impact(cluster_items, raw_impact):
    impact = raw_impact if raw_impact in IMPACT_PRIORITY else "INFORMATIF"
    combined = " ".join(f"{it['title']} {it['link']}" for it in cluster_items).lower()
    if "certfr-avi" in combined and impact == "CRITIQUE":
        norm = normalize_text(combined)
        if "exploite" not in norm and "0-day" not in combined:
            return "IMPORTANT"
    return impact

def choose_primary_item(cluster_items):
    def score(item):
        is_official = 1 if item["source"] in OFFICIAL_SOURCES else 0
        has_summary = len(item.get("summary_raw", ""))
        return (is_official, has_summary)

    sorted_items = sorted(cluster_items, key=score, reverse=True)
    return sorted_items[0], sorted_items[1:]

def analyze_batch_with_gemini(batch):
    """Analyse jusqu'à 25 articles en 1 appel API : dédoublonnage sémantique, impact et action."""
    if not ACTIVE_GEMINI_ENDPOINT:
        return None

    items_block = []
    for idx, item in enumerate(batch):
        items_block.append(
            f"[{idx}] Source: {item['source']} | Catégorie suggérée: {item['default_category']}\n"
            f"Titre: {item['title']}\n"
            f"Extrait: {item['summary_raw'][:350]}"
        )
    catalog_text = "\n\n".join(items_block)

    prompt = f"""Tu es un directeur juridique et RSSI expert. Analyse ce lot de {len(batch)} publications de veille réglementaire et cybersécurité.

TÂCHES À RÉALISER :
1. Regroupement sémantique : si plusieurs entrées numérotées traitent exactement du même événement, texte, décision ou faille, fusionne leurs numéros dans "indices".
2. Filtrage d'applicabilité : mets "pertinent": false pour les nominations individuelles, arrêtés administratifs locaux/sectoriels sans portée générale, contenus promotionnels ou faits divers sans intérêt juridique/conformité/cyber.
3. Qualification stricte pour chaque sujet pertinent :
   - "indices" : liste d'entiers des articles regroupés (ex: [0, 5]).
   - "pertinent" : booléen (true ou false).
   - "categorie" : une valeur parmi PDP, CYBER, ANTICOR, PI, COSMETIQUE, INSTITUTIONNEL, AUTRE.
   - "impact" : CRITIQUE, IMPORTANT ou INFORMATIF.
     * CRITIQUE (strictement réservé aux urgences majeures) : sanction majeure prononcée (CNIL, DGCCRF, AFA...), alerte cyber d'exploitation active (CERTFR-ALE / faille 0-day exploitée), obligation légale immédiate ou arrêt de principe majeur impactant les entreprises. (Les avis techniques de routine CERTFR-AVI ne sont JAMAIS critiques).
     * IMPORTANT : avis technique de sécurité (CERTFR-AVI), recommandation officielle, projet de loi/règlement structurant ou évolution jurisprudentielle notable.
     * INFORMATIF : veille générale, bilan chiffré ou actualité de fond sans urgence.
   - "resume" : synthèse factuelle de 2 à 3 phrases expliquant l'enjeu concret.
   - "action_requise" : 1 phrase courte décrivant ce qu'il faut vérifier ou faire en interne (ou "" si aucune action n'est requise).
   - "echeance" : date limite ou délai de conformité mentionné (ex: "18 octobre 2026", "Sous 72h", ou "" si non applicable).
   - "cible" : fonction concernée en 1 ou 2 mots ("DPO", "RSSI", "Juriste", "Direction", "Conformité", ou "").

Retourne uniquement un objet JSON valide de la forme :
{{"sujets": [ ... ]}}

LISTE DES PUBLICATIONS :
{catalog_text}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 8192,
            "response_mime_type": "application/json"
        }
    }

    headers = {"Content-Type": "application/json"}

    for attempt in (1, 2, 3):
        try:
            res = requests.post(ACTIVE_GEMINI_ENDPOINT, json=payload, headers=headers, timeout=45)
            if res.status_code == 200:
                raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                clean_json = re.sub(r"^```json\s*|^```\s*|```$", "", raw_text, flags=re.MULTILINE).strip()
                parsed = json.loads(clean_json)
                sujets = parsed.get("sujets", [])
                if isinstance(sujets, list):
                    return sujets
                break

            elif res.status_code in (429, 503) and attempt < 3:
                wait_time = attempt * 6
                print(f"   ⚠️ Surcharge Google ({res.status_code}), pause de {wait_time}s (essai {attempt}/3)...", flush=True)
                time.sleep(wait_time)
                continue
            else:
                print(f"❌ [Gemini Batch HTTP {res.status_code}] : {res.text[:160]}", flush=True)
                break

        except requests.exceptions.RequestException as e:
            if attempt < 3:
                wait_time = attempt * 4
                print(f"   ⚠️ Latence réseau sur lot, relance dans {wait_time}s...", flush=True)
                time.sleep(wait_time)
                continue
            print(f"❌ [Exception Batch Gemini] : {e}", flush=True)
        except Exception as e:
            print(f"❌ [Erreur parsing JSON Batch] : {e}", flush=True)
            break

    return None

def process_candidates(candidates):
    final_articles = []

    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        batch_num = (start // BATCH_SIZE) + 1
        print(f"🧠 Analyse sémantique du lot {batch_num} ({len(batch)} articles)...", flush=True)

        sujets = analyze_batch_with_gemini(batch)
        handled_indices = set()

        if sujets is not None:
            for sujet in sujets:
                if not isinstance(sujet, dict):
                    continue
                raw_indices = sujet.get("indices", [])
                valid_indices = [
                    i for i in raw_indices
                    if isinstance(i, int) and 0 <= i < len(batch) and i not in handled_indices
                ]
                if not valid_indices:
                    continue

                for i in valid_indices:
                    handled_indices.add(i)

                if sujet.get("pertinent") is False:
                    print(f"   ↳ [Écarté] {batch[valid_indices[0]]['title'][:55]}...", flush=True)
                    continue

                cluster_items = [batch[i] for i in valid_indices]
                primary, secondaries = choose_primary_item(cluster_items)

                category = str(sujet.get("categorie", primary["default_category"])).strip().upper()
                if category not in CATEGORY_THEMES:
                    category = primary["default_category"] if primary["default_category"] in CATEGORY_THEMES else "AUTRE"

                raw_impact = str(sujet.get("impact", "INFORMATIF")).strip().upper()
                impact = calibrate_impact(cluster_items, raw_impact)

                art_obj = {
                    "title": primary["title"],
                    "link": primary["link"],
                    "source": primary["source"],
                    "category": category,
                    "impact": impact,
                    "summary": str(sujet.get("resume", "")).strip(),
                    "action_requise": str(sujet.get("action_requise", "")).strip(),
                    "echeance": str(sujet.get("echeance", "")).strip(),
                    "cible": str(sujet.get("cible", "")).strip(),
                    "pub_date": primary["pub_date_iso"]
                }

                if secondaries:
                    art_obj["related_sources"] = [
                        {"source": s["source"], "title": s["title"], "link": s["link"]}
                        for s in secondaries
                    ]

                multi_tag = f" [+{len(secondaries)} src]" if secondaries else ""
                print(f"   ↳ [{impact}]{multi_tag} {primary['title'][:55]}...", flush=True)
                final_articles.append(art_obj)

        for idx, item in enumerate(batch):
            if idx not in handled_indices:
                fallback_cat = item["default_category"] if item["default_category"] in CATEGORY_THEMES else "AUTRE"
                final_articles.append({
                    "title": item["title"],
                    "link": item["link"],
                    "source": item["source"],
                    "category": fallback_cat,
                    "impact": "INFORMATIF",
                    "summary": item["summary_raw"][:280],
                    "action_requise": "",
                    "echeance": "",
                    "cible": "",
                    "pub_date": item["pub_date_iso"]
                })

        for item in batch:
            mark_article_seen(item["link"], item["title"], item["pub_date_raw"])

        if start + BATCH_SIZE < len(candidates):
            time.sleep(5)

    final_articles.sort(key=lambda x: IMPACT_PRIORITY.get(x.get("impact", "INFORMATIF"), 3))
    return final_articles

# ==========================================
# 6. EXPORT JSON COMPLET POUR L'APPLICATION PWA
# ==========================================

def export_to_pwa_json(new_articles, json_path=PWA_JSON_PATH, max_items=200):
    """Envoie tous les sujets pertinents (Critique, Important, Informatif) vers l'app mobile."""
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
    print(f"Flux PWA mis à jour ({len(merged[:max_items])} sujets dans {json_path}).", flush=True)

# ==========================================
# 7. GABARIT HTML DE L'EMAIL (UNIQUEMENT CRITIQUES)
# ==========================================

def build_html_digest(critical_articles, total_pwa_count):
    date_str = datetime.datetime.now().strftime("%d/%m/%Y")
    grouped = {}
    for art in critical_articles:
        grouped.setdefault(art["category"], []).append(art)

    other_count = max(0, total_pwa_count - len(critical_articles))
    other_note = (
        f" ainsi que <strong>{other_count} autre{'s' if other_count > 1 else ''} publication{'s' if other_count > 1 else ''}</strong> (Important / Informatif)"
        if other_count > 0 else ""
    )

    html = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Alerte Veille Juridique</title>
    </head>
    <body style="margin: 0; padding: 24px 12px; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
      <div style="max-width: 640px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); border: 1px solid #e2e8f0;">
        
        <div style="padding: 26px 28px 20px 28px; background-color: #0f172a; color: #ffffff;">
          <div style="font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.2px; color: #f87171; margin-bottom: 6px;">
            Alerte Prioritaire
          </div>
          <h1 style="margin: 0; font-size: 21px; font-weight: 700; letter-spacing: -0.3px; color: #ffffff;">
            Veille Juridique & Cybersécurité
          </h1>
          <div style="margin-top: 10px; font-size: 13px; color: #cbd5e1;">
            <span>📅 {date_str}</span>
            <span style="margin: 0 8px; color: #475569;">•</span>
            <span style="background-color: #dc2626; color: #ffffff; padding: 2px 9px; border-radius: 9999px; font-weight: 700; font-size: 12px;">
              {len(critical_articles)} sujet{'s' if len(critical_articles) > 1 else ''} critique{'s' if len(critical_articles) > 1 else ''}
            </span>
          </div>
        </div>

        <div style="padding: 16px 24px 24px 24px;">
    """

    for cat_key, theme in CATEGORY_THEMES.items():
        if cat_key in grouped:
            arts = grouped[cat_key]
            html += f"""
            <div style="margin-top: 20px; margin-bottom: 10px;">
              <div style="display: inline-block; background-color: {theme['bg']}; color: {theme['color']}; border: 1px solid {theme['badge']}; font-weight: 700; font-size: 12px; letter-spacing: 0.4px; text-transform: uppercase; padding: 4px 10px; border-radius: 6px; margin-bottom: 8px;">
                {theme['label']} ({len(arts)})
              </div>

              <div style="border-top: 2px solid {theme['color']}; padding-top: 4px;">
            """

            for art in arts:
                safe_source = html_lib.escape(art.get("source", ""))
                safe_title = html_lib.escape(art.get("title", ""))
                safe_link = html_lib.escape(art.get("link", ""))
                safe_summary = html_lib.escape(art.get("summary", ""))
                safe_action = html_lib.escape(art.get("action_requise", ""))
                safe_due = html_lib.escape(art.get("echeance", ""))
                safe_role = html_lib.escape(art.get("cible", ""))

                role_badge = (
                    f'<span style="background-color: #e2e8f0; color: #334155; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">{safe_role}</span>'
                    if safe_role else ""
                )
                due_badge = (
                    f'<span style="background-color: #fef3c7; color: #92400e; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">Échéance : {safe_due}</span>'
                    if safe_due else ""
                )

                rel_sources = art.get("related_sources", [])
                rel_badge = (
                    f'<span style="background-color: #e0f2fe; color: #0369a1; font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">+{len(rel_sources)} source{"s" if len(rel_sources) > 1 else ""}</span>'
                    if rel_sources else ""
                )

                summary_html = ""
                if safe_summary:
                    summary_html = f"""
                    <div style="margin-top: 10px; background-color: #f8fafc; border-left: 3px solid {theme['color']}; border-radius: 0 6px 6px 0; padding: 10px 12px;">
                      <div style="font-size: 13px; line-height: 1.55; color: #334155;">
                        {safe_summary}
                      </div>
                    </div>
                    """

                action_html = ""
                if safe_action:
                    action_html = f"""
                    <div style="margin-top: 8px; background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 8px 11px; font-size: 12.5px; color: #166534;">
                      <strong>Action requise :</strong> {safe_action}
                    </div>
                    """

                rel_links_html = ""
                if rel_sources:
                    links = " • ".join(
                        f'<a href="{html_lib.escape(r["link"])}" target="_blank" style="color: #475569; text-decoration: underline;">{html_lib.escape(r["source"])}</a>'
                        for r in rel_sources
                    )
                    rel_links_html = f'<div style="margin-top: 6px; font-size: 11.5px; color: #64748b;">Également couvert par : {links}</div>'

                html += f"""
                <div style="padding: 16px 0; border-bottom: 1px solid #f1f5f9;">
                  <div style="margin-bottom: 6px;">
                    <span style="background-color: #f1f5f9; color: #475569; font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 4px;">
                      {safe_source}
                    </span>
                    <span style="background-color: #dc2626; color: #ffffff; font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; margin-left: 6px; text-transform: uppercase;">Critique</span>
                    {role_badge}
                    {due_badge}
                    {rel_badge}
                  </div>

                  <div>
                    <a href="{safe_link}" target="_blank" style="color: #0f172a; text-decoration: none; font-size: 15px; font-weight: 600; line-height: 1.35; display: inline-block;">
                      {safe_title}
                    </a>
                  </div>

                  {summary_html}
                  {action_html}
                  {rel_links_html}

                  <div style="margin-top: 8px;">
                    <a href="{safe_link}" target="_blank" style="color: {theme['color']}; text-decoration: none; font-size: 12px; font-weight: 600;">
                      Consulter la publication →
                    </a>
                  </div>
                </div>
                """

            html += "</div></div>"

    html += f"""
        </div>
        <div style="padding: 18px 28px; background-color: #f8fafc; border-top: 1px solid #e2e8f0; text-align: center;">
          <p style="margin: 0 0 10px 0; font-size: 12.5px; color: #475569; line-height: 1.5;">
            Retrouvez ces alertes{other_note} sur votre application mobile :
          </p>
          <a href="{PWA_PUBLIC_URL}" target="_blank" style="display: inline-block; background-color: #0f172a; color: #38bdf8; text-decoration: none; font-size: 12.5px; font-weight: 600; padding: 8px 16px; border-radius: 8px;">
            Ouvrir l'application Veille Juridique →
          </a>
        </div>

      </div>
    </body>
    </html>
    """
    return html

# ==========================================
# 8. EXPÉDITION SMTP MULTI-DESTINATAIRES
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

    candidates = collect_candidates()
    if not candidates:
        print("Aucun nouvel article à analyser aujourd'hui. Fin.", flush=True)
        return

    print(f"📦 {len(candidates)} candidat(s) collecté(s). Analyse par lot...", flush=True)
    articles = process_candidates(candidates)
    if not articles:
        print("Aucun sujet pertinent retenu après filtrage IA. Fin.", flush=True)
        return

    # 1. L'application mobile reçoit l'intégralité des sujets pertinents
    export_to_pwa_json(articles)

    # 2. L'email est envoyé uniquement s'il existe au moins un sujet CRITIQUE
    critical_articles = [a for a in articles if a.get("impact") == "CRITIQUE"]
    if not critical_articles:
        print(f"✅ {len(articles)} sujet(s) ajouté(s) à l'app mobile, aucun sujet CRITIQUE aujourd'hui : pas d'email envoyé.", flush=True)
        return

    print(f"🚨 {len(critical_articles)} sujet(s) CRITIQUE(S) détecté(s) sur {len(articles)}. Envoi de l'email...", flush=True)
    html_report = build_html_digest(critical_articles, total_pwa_count=len(articles))
    subject = f"Veille Juridique [CRITIQUE] - {datetime.datetime.now().strftime('%d/%m/%Y')} ({len(critical_articles)} sujet{'s' if len(critical_articles) > 1 else ''})"
    send_email(subject, html_report)
    print("Email critique envoyé avec succès.", flush=True)

if __name__ == "__main__":
    main()
