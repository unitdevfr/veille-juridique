import csv
import datetime
import json
import os
import re
import sqlite3
import time
import unicodedata
import feedparser
import requests

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

DB_PATH = "veille_history_v2.db"
PWA_JSON_PATH = "docs/v2/articles.json"
MAX_CANDIDATES_PER_RUN = 60
BATCH_SIZE = 25

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

VALID_CATEGORIES = {
    "PDP", "CYBER", "ANTICOR", "PI", "COSMETIQUE", "INSTITUTIONNEL", "AUTRE"
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
    """Sélectionne le meilleur modèle Gemini disponible pour l'analyse par lot."""
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
                print(f"✅ [V2] Modèle Gemini opérationnel : {model_name}", flush=True)
                return url
            else:
                print(f"ℹ️ {model_name} non retenu (HTTP {res.status_code})", flush=True)
        except Exception as e:
            print(f"ℹ️ Erreur test sur {model_name} : {e}", flush=True)

    print("❌ Aucun modèle Gemini disponible.", flush=True)
    return None

# ==========================================
# 2. BASE DE MÉMOIRE INDÉPENDANTE (SQLITE V2)
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
# 3. PRÉ-FILTRAGE & CHARGEMENT SOURCES
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
# 4. COLLECTE DES CANDIDATS (7 JOURS)
# ==========================================

def collect_candidates():
    sources = load_sources()
    candidates = []
    seen_in_run = set()
    cutoff_time = time.time() - (7 * 24 * 3600)
    rss_headers = {"User-Agent": "Mozilla/5.0 (compatible; VeilleJuridiqueV2/1.0)"}
    noise_skipped = 0

    for s in sources:
        if len(candidates) >= MAX_CANDIDATES_PER_RUN:
            print(f"⏹️ Plafond de {MAX_CANDIDATES_PER_RUN} candidats atteint pour cette passe.", flush=True)
            break

        print(f"Scrutage : {s['source']}...", flush=True)
        try:
            resp = requests.get(s["url"], headers=rss_headers, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)

            for entry in feed.entries:
                if len(candidates) >= MAX_CANDIDATES_PER_RUN:
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
# 5. ANALYSE GLOBALE PAR LOT & REGROUPEMENT SÉMANTIQUE
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
    """Privilégie une source institutionnelle officielle comme lien principal."""
    def score(item):
        is_official = 1 if item["source"] in OFFICIAL_SOURCES else 0
        has_summary = len(item.get("summary_raw", ""))
        return (is_official, has_summary)

    sorted_items = sorted(cluster_items, key=score, reverse=True)
    return sorted_items[0], sorted_items[1:]

def analyze_batch_with_gemini(batch):
    """Analyse jusqu'à 25 articles en 1 seul appel API : dédoublonnage sémantique, impact, action et échéance."""
    if not ACTIVE_GEMINI_ENDPOINT:
        return []

    items_block = []
    for idx, item in enumerate(batch):
        items_block.append(
            f"[{idx}] Source: {item['source']} | Catégorie suggérée: {item['default_category']}\n"
            f"Titre: {item['title']}\n"
            f"Extrait: {item['summary_raw'][:350]}"
        )
    catalog_text = "\n\n".join(items_block)

    prompt = f"""Tu es un directeur juridique et RSSI expert. Analyse ce lot de {len(batch)} publications de veille réglementaire et cybersécurité.

TÂCHES À RÉALISER EN UN SEUL PASSAGE :
1. Regroupement sémantique : si plusieurs entrées numérotées traitent exactement du même événement, de la même décision, du même texte ou de la même faille (par exemple une source officielle + un article de presse ou cabinet d'avocats), fusionne leurs numéros dans "indices".
2. Filtrage d'applicabilité : mets "pertinent": false uniquement pour les contenus purement promotionnels, nominations individuelles sans portée normative, ou faits divers sans intérêt juridique/conformité/cyber.
3. Qualification et extraction métier pour chaque sujet pertinent :
   - "indices" : liste d'entiers des articles regroupés (ex: [0, 5]).
   - "pertinent" : booléen (true ou false).
   - "categorie" : une valeur parmi PDP, CYBER, ANTICOR, PI, COSMETIQUE, INSTITUTIONNEL, AUTRE.
   - "impact" : CRITIQUE, IMPORTANT ou INFORMATIF.
     * CRITIQUE : sanction majeure prononcée, alerte cyber d'exploitation active (CERTFR-ALE / 0-day), obligation légale immédiate ou arrêt de principe majeur. (Les avis techniques de routine CERTFR-AVI sont IMPORTANT, pas CRITIQUE).
     * IMPORTANT : avis technique de sécurité (CERTFR-AVI), recommandation officielle, projet de loi/règlement structurant ou évolution jurisprudentielle notable.
     * INFORMATIF : veille générale, bilan chiffré ou actualité de fond sans urgence.
   - "resume" : synthèse factuelle de 2 à 3 phrases expliquant l'enjeu concret.
   - "action_requise" : 1 phrase courte et concrète décrivant ce qu'il faut vérifier ou faire en interne (ou chaîne vide "" si aucune action n'est requise).
   - "echeance" : date limite, délai de conformité ou date d'entrée en vigueur mentionnée (ex: "18 octobre 2026", "Sous 72h", ou "" si non applicable).
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
    """Découpe les candidats en lots et construit les objets finaux."""
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
                    print(f"   ↳ [Écarté par IA] {batch[valid_indices[0]]['title'][:55]}...", flush=True)
                    continue

                cluster_items = [batch[i] for i in valid_indices]
                primary, secondaries = choose_primary_item(cluster_items)

                category = str(sujet.get("categorie", primary["default_category"])).strip().upper()
                if category not in VALID_CATEGORIES:
                    category = primary["default_category"] if primary["default_category"] in VALID_CATEGORIES else "AUTRE"

                raw_impact = str(sujet.get("impact", "INFORMATIF")).strip().upper()
                impact = calibrate_impact(cluster_items, raw_impact)

                summary = str(sujet.get("resume", "")).strip()
                action = str(sujet.get("action_requise", "")).strip()
                echeance = str(sujet.get("echeance", "")).strip()
                cible = str(sujet.get("cible", "")).strip()

                art_obj = {
                    "title": primary["title"],
                    "link": primary["link"],
                    "source": primary["source"],
                    "category": category,
                    "impact": impact,
                    "summary": summary,
                    "action_requise": action,
                    "echeance": echeance,
                    "cible": cible,
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

        # Filet de sécurité pour tout article non couvert en cas d'erreur sur le lot
        for idx, item in enumerate(batch):
            if idx not in handled_indices:
                fallback_cat = item["default_category"] if item["default_category"] in VALID_CATEGORIES else "AUTRE"
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

        # Enregistrement dans la base SQLite V2
        for item in batch:
            mark_article_seen(item["link"], item["title"], item["pub_date_raw"])

        if start + BATCH_SIZE < len(candidates):
            time.sleep(5)

    final_articles.sort(key=lambda x: IMPACT_PRIORITY.get(x.get("impact", "INFORMATIF"), 3))
    return final_articles

# ==========================================
# 6. EXPORT JSON POUR LE SITE V2
# ==========================================

def export_to_pwa_json(new_articles, json_path=PWA_JSON_PATH, max_items=200):
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
    print(f"✅ Flux V2 mis à jour ({len(merged[:max_items])} sujets dans {json_path}).", flush=True)

# ==========================================
# POINT D'ENTRÉE V2
# ==========================================

def main():
    global ACTIVE_GEMINI_ENDPOINT
    init_db()
    ACTIVE_GEMINI_ENDPOINT = init_gemini_endpoint()

    candidates = collect_candidates()
    if not candidates:
        print("Aucun nouveau candidat à analyser pour la V2.", flush=True)
        return

    print(f"📦 {len(candidates)} candidat(s) collecté(s). Lancement de l'analyse par lot...", flush=True)
    articles = process_candidates(candidates)

    if not articles:
        print("Aucun sujet pertinent retenu après filtrage IA.", flush=True)
        return

    export_to_pwa_json(articles)
    print(f"🎯 Terminé : {len(articles)} sujet(s) publiés sur la V2.", flush=True)

if __name__ == "__main__":
    main()
