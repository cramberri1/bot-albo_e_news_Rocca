"""
Albo Pretorio Bot - Comune di Roccabascerana
Sistema Halley EG / EGHOMEPAGE.HBL + EGSCHTST6.HBL (News)

Comandi Telegram:
  /start            — benvenuto
  /abbonati         — iscriviti a entrambe le notifiche (albo + news)
  /disabbonati      — cancella entrambe le iscrizioni
  /abbonati_albo    — iscriviti solo alle notifiche albo pretorio
  /disabbonati_albo — cancella iscrizione albo pretorio
  /abbonati_news    — iscriviti solo alle notifiche news
  /disabbonati_news — cancella iscrizione news
  /atti             — mostra gli atti attuali con PDF allegato
  /news             — mostra le ultime 10 news del sito
  /controlla        — forza un controllo immediato (albo + news)
  /status           — statistiche bot (solo admin)
"""

import asyncio
import json
import logging
import os
import re
import sys
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
def load_config():
    cfg = {}
    if os.environ.get("BOT_TOKEN"):
        cfg["BOT_TOKEN"]        = os.environ["BOT_TOKEN"]
        cfg["ADMIN_IDS"]        = [int(x.strip()) for x in os.environ.get("CHAT_IDS", "").split(",") if x.strip()]
        cfg["ALBO_URL"]         = os.environ.get("ALBO_URL", "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL")
        cfg["NEWS_URL"]         = os.environ.get("NEWS_URL", "https://www.comune.roccabascerana.av.it/EG0/EGSCHTST6.HBL")
        cfg["INTERVAL_MINUTES"] = int(os.environ.get("INTERVAL_MINUTES", "180"))
    else:
        try:
            import config
            cfg["BOT_TOKEN"]        = config.BOT_TOKEN
            cfg["ADMIN_IDS"]        = config.CHAT_IDS
            cfg["ALBO_URL"]         = getattr(config, "ALBO_URL", "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL")
            cfg["NEWS_URL"]         = getattr(config, "NEWS_URL", "https://www.comune.roccabascerana.av.it/EG0/EGSCHTST6.HBL")
            cfg["INTERVAL_MINUTES"] = getattr(config, "INTERVAL_MINUTES", 180)
        except ImportError:
            print("ERRORE: config.py non trovato e variabili d'ambiente mancanti.")
            sys.exit(1)
    return cfg

CONFIG = load_config()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

def validate_config():
    """Verifica subito i secret/config essenziali, con errore chiaro.

    Meglio fallire all'avvio in modo esplicito che far partire un runner
    GitHub Actions apparentemente "verde" ma incapace di inviare notifiche.
    """
    if not CONFIG.get("BOT_TOKEN"):
        raise RuntimeError("BOT_TOKEN mancante: imposta il secret GitHub Actions o config.py.")
    if not CONFIG.get("ADMIN_IDS"):
        raise RuntimeError("CHAT_IDS mancante o vuoto: serve almeno un admin Telegram.")
    if CONFIG.get("INTERVAL_MINUTES", 0) < 1:
        raise RuntimeError("INTERVAL_MINUTES deve essere almeno 1.")

validate_config()

# ---------------------------------------------------------------------------
# Cifratura dei file contenenti dati personali (chat_id Telegram iscritti)
# ---------------------------------------------------------------------------
# subscribers.json, subscribers_news.json e user_seen.json contengono
# chat_id — dato personale indiretto. Dato che il repository è pubblico
# (necessario per i minuti GitHub Actions gratuiti illimitati), questi tre
# file vengono cifrati prima di ogni commit con una chiave simmetrica letta
# SOLO da un secret GHA (STATE_ENCRYPTION_KEY), mai presente nel repository.
# Gli altri file di stato (seen_items.json, seen_news.json, last_check.txt)
# non contengono dati personali e restano in chiaro.
from cryptography.fernet import Fernet, InvalidToken

STATE_ENCRYPTION_KEY = os.environ.get("STATE_ENCRYPTION_KEY", "")
try:
    _fernet = Fernet(STATE_ENCRYPTION_KEY.encode()) if STATE_ENCRYPTION_KEY else None
except Exception as e:
    raise RuntimeError(
        "STATE_ENCRYPTION_KEY non valida: rigenerala con "
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    ) from e

if _fernet is None and os.environ.get("GITHUB_ACTIONS"):
    log.warning(
        "STATE_ENCRYPTION_KEY non impostata su GitHub Actions — i file con "
        "chat_id degli iscritti verranno salvati IN CHIARO. Imposta il secret "
        "STATE_ENCRYPTION_KEY per proteggerli."
    )

def _encrypt_json(data) -> bytes:
    """Serializza in JSON e cifra. Senza chiave (es. sviluppo locale) salva
    in chiaro — comodo per il dev, ma va sempre impostata la chiave su GHA."""
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if _fernet is None:
        return raw
    return _fernet.encrypt(raw)

def _decrypt_json(raw: bytes):
    """Decifra e deserializza. Se il contenuto non è cifrato (file scritto
    prima della migrazione alla cifratura, o dev locale senza chiave) prova
    a leggerlo come JSON in chiaro — così il primo avvio dopo aver aggiunto
    la chiave migra automaticamente i file esistenti al primo salvataggio,
    senza bisogno di uno script di migrazione separato."""
    if _fernet is not None:
        try:
            raw = _fernet.decrypt(raw)
        except InvalidToken:
            log.warning("File non cifrato o chiave errata — lo leggo come JSON in chiaro (verrà cifrato al prossimo salvataggio).")
    return json.loads(raw.decode("utf-8"))

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------
BASE_URL = CONFIG["ALBO_URL"]

# ---------------------------------------------------------------------------
# Cartella dati: tutti i file di stato generati dal bot vivono qui, per
# tenere la root del repository pulita (solo codice: bot.py, README.md,
# requirements.txt, .github/). Creata automaticamente se assente — utile
# al primo checkout/deploy, dove la cartella potrebbe non esistere ancora.
# ---------------------------------------------------------------------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
NEWS_URL = CONFIG["NEWS_URL"]
ORIGIN   = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(BASE_URL))
ENTE     = "e1396"

# Soglie per la visualizzazione atti
RECENT_EXPIRED_DAYS = 30          # atti scaduti da <= 30 giorni: mostra + allegati
ARCHIVE_CUTOFF_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)  # prima di questa data: nascondi

# Soglie per la visualizzazione news
NEWS_LIST_LIMIT = 10              # quante news mostrare con /news
NEWS_NOTIFY_MAX_AGE_DAYS = 60     # non notificare push news più vecchie di N giorni
                                   # (es. dopo un downtime prolungo del bot) — vengono
                                   # comunque segnate come viste e restano visibili con /news
NEWS_CATEGORY_EMOJI = {
    "avviso":     "📢",
    "comunicati": "📣",
    "notizia":    "📰",
}
NEWS_CATEGORY_DEFAULT_EMOJI = "🗞"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
    "Content-Type":    "text/plain;charset=UTF-8; boundary=AZazAZ",
    "Referer":         BASE_URL,
    "Origin":          ORIGIN,
}

# Tentativi massimi per le richieste HTTP
MAX_RETRIES = 3
RETRY_DELAY = 5  # secondi tra un tentativo e l'altro

MENU_TEXT = (
    "\n\n─────────────────\n"
    "*Comandi disponibili:*\n"
    "/abbonati — iscriviti a tutte le notifiche\n"
    "/disabbonati — cancella tutte le iscrizioni\n"
    "/abbonati\\_albo — solo notifiche albo pretorio\n"
    "/abbonati\\_news — solo notifiche news\n"
    "/atti — mostra gli atti attuali\n"
    "/news — mostra le ultime news\n"
    "/controlla — forza un controllo\n"
    "/status — statistiche bot (solo admin)\n"
    "/start — messaggio di benvenuto"
)

# ---------------------------------------------------------------------------
# Gestione iscritti
# ---------------------------------------------------------------------------
# subscribers.json  -> iscritti Albo Pretorio (nome storico, retrocompatibile)
# subscribers_news.json -> iscritti News
SUBSCRIBERS_PATH      = DATA_DIR / "subscribers.json"
SUBSCRIBERS_NEWS_PATH = DATA_DIR / "subscribers_news.json"

def _load_set(path: Path) -> set:
    if path.exists():
        return set(_decrypt_json(path.read_bytes()))
    return set()

def _save_set(path: Path, subs: set):
    path.write_bytes(_encrypt_json(sorted(subs)))

def load_subscribers() -> set:
    """Iscritti Albo Pretorio."""
    subs = _load_set(SUBSCRIBERS_PATH)
    return subs if subs or SUBSCRIBERS_PATH.exists() else set(CONFIG["ADMIN_IDS"])

def save_subscribers(subs: set):
    _save_set(SUBSCRIBERS_PATH, subs)
    git_commit_and_push([str(SUBSCRIBERS_PATH)], message="aggiornamento iscritti albo [skip ci]")

def load_subscribers_news() -> set:
    """Iscritti News."""
    return _load_set(SUBSCRIBERS_NEWS_PATH)

def save_subscribers_news(subs: set):
    _save_set(SUBSCRIBERS_NEWS_PATH, subs)
    git_commit_and_push([str(SUBSCRIBERS_NEWS_PATH)], message="aggiornamento iscritti news [skip ci]")

def get_all_recipients() -> set:
    """Destinatari notifiche Albo Pretorio."""
    return load_subscribers() | set(CONFIG["ADMIN_IDS"])

def get_all_news_recipients() -> set:
    """Destinatari notifiche News."""
    return load_subscribers_news() | set(CONFIG["ADMIN_IDS"])

def is_admin(chat_id: int) -> bool:
    return chat_id in CONFIG["ADMIN_IDS"]

# ---------------------------------------------------------------------------
# Database atti visti
# ---------------------------------------------------------------------------
DB_PATH         = DATA_DIR / "seen_items.json"
LAST_CHECK_PATH = DATA_DIR / "last_check.txt"

def _normalize_item_record(value: dict | list | str | None, *, default_notified: bool = True) -> dict:
    """Normalizza una voce di seen_items.json.

    Compatibilità: i dati scritti dalle versioni precedenti non avevano il
    campo `notified`. Per non rispedire notifiche storiche, quelle voci sono
    considerate già notificate. Le nuove voci create solo come cache tecnica,
    invece, nascono con `notified=False`.
    """
    if isinstance(value, dict):
        rec = dict(value)
    else:
        rec = {}
    rec.setdefault("notified", default_notified)
    return rec

def load_db() -> dict:
    """
    Carica il database atti. Formato attuale:
    {
      "hash16": {
        "notified": true|false,   # True = già notificato/baseline globale
        "date": "DD-MM-YYYY",
        "date_end": "DD-MM-YYYY",
        "expired": bool
      }
    }

    Compatibile con:
    - vecchio formato lista di hash: tutti considerati già notificati;
    - vecchio formato dict senza `notified`: voci considerate già notificate
      per evitare reinvii massivi dopo l'upgrade.
    """
    if not DB_PATH.exists():
        return {}
    raw = json.loads(DB_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        log.info(f"Migrazione seen_items.json: {len(raw)} hash -> formato con notified/cache")
        migrated = {h: {"notified": True} for h in raw}
        DB_PATH.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
        return migrated
    if isinstance(raw, dict):
        changed = False
        normalized = {}
        for h, value in raw.items():
            rec = _normalize_item_record(value, default_notified=True)
            if rec != value:
                changed = True
            normalized[h] = rec
        if changed:
            DB_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return normalized
    log.warning("seen_items.json ha un formato inatteso: riparto da database vuoto.")
    return {}

def save_db(db: dict, *, push: bool = True, message: str = "aggiornamento database [skip ci]"):
    DB_PATH.write_text(
        json.dumps(db, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    if push:
        git_commit_and_push([str(DB_PATH)], message=message)

def git_commit_and_push(paths: list | None = None,
                          message: str = "aggiornamento database [skip ci]",
                          allow_delete: bool = False,
                          max_retries: int = 3) -> bool:
    """
    Committa e pusha file di stato direttamente dal bot, indipendentemente
    da quanto dura il job GHA. Necessario perché il bot resta in esecuzione
    per ore e lo step "Salva database" del workflow parte solo alla fine.

    Se `paths` non è specificato, usa l'elenco predefinito dei file di stato
    principali (seen_items.json, subscribers.json, ecc). Passare `paths`
    esplicitamente per casi mirati specifici, se mai servisse in futuro.

    Gestisce il caso di push concorrenti (in teoria non dovrebbe più
    verificarsi, dato che `concurrency` nel workflow GHA impedisce a due
    run dello stesso gruppo di essere attivi insieme — ma il retry resta
    come rete di sicurezza aggiuntiva): se il push viene rifiutato con
    "fetch first"/non-fast-forward, fa un
    `git pull --rebase` per riallinearsi alla history remota e riprova,
    fino a max_retries volte. Senza questo, il fallimento veniva ignorato
    silenziosamente (check=False) e lo stato si perdeva: il file restava
    aggiornato solo nel filesystem effimero del runner, mai arrivato su
    GitHub, quindi invisibile al prossimo run.

    Restituisce True se il push è andato a buon fine (o se non c'era nulla
    da committare), False se ha fallito dopo tutti i tentativi. Resta
    non-bloccante per il chiamante: logga sempre, non solleva eccezioni.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return True  # solo su GHA, non in locale/dev

    if paths is None:
        paths = [
            str(p) for p in [
                DB_PATH, SUBSCRIBERS_PATH, LAST_CHECK_PATH, USER_SEEN_PATH,
                NEWS_DB_PATH, SUBSCRIBERS_NEWS_PATH,
            ]
            if p.exists()
        ]
        if not paths:
            return True

    try:
        import subprocess
        subprocess.run(["git", "config", "user.name", "AlboBot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@alborocca"], check=False)
        if allow_delete:
            subprocess.run(["git", "add", "-A"] + paths, check=False)
        else:
            subprocess.run(["git", "add"] + paths, check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
        if diff.returncode == 0:
            return True  # nessuna modifica da committare, non è un fallimento

        subprocess.run(["git", "commit", "-m", message], check=False)

        for attempt in range(1, max_retries + 1):
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                log.info(f"Committato e pushato su GitHub ({len(paths)} file: {', '.join(paths)}).")
                return True

            log.warning(
                f"git push fallito (tentativo {attempt}/{max_retries}): "
                f"{push.stderr.strip()[:300]}"
            )
            if attempt == max_retries:
                break

            # Probabile push concorrente da un altro run: riallineo con
            # rebase e riprovo. autostash preserva eventuali modifiche
            # non committate (non dovrebbero essercene qui, ma per sicurezza).
            pull = subprocess.run(
                ["git", "pull", "--rebase", "--autostash"],
                capture_output=True, text=True
            )
            if pull.returncode != 0:
                log.warning(
                    f"git pull --rebase fallito (tentativo {attempt}/{max_retries}): "
                    f"{pull.stderr.strip()[:300]}"
                )
                break  # rebase rotto, inutile riprovare il push

        log.error(
            f"git_commit_and_push: push fallito definitivamente dopo {max_retries} tentativi "
            f"(file: {', '.join(paths)}). Stato locale NON sincronizzato con il repository remoto."
        )
        return False
    except Exception as e:
        log.warning(f"git_commit_and_push fallito (non bloccante): {e}")
        return False

# Compatibilità: load_seen e save_seen usati nel codice esistente.
# Importante: "seen" ora significa notificato/baseline globale, NON semplice
# presenza nella cache tecnica. Così /atti può arricchire date e allegati senza
# impedire al check automatico di notificare davvero un nuovo atto.
def load_seen() -> set:
    db = load_db()
    return {h for h, rec in db.items() if rec.get("notified", True)}

def save_seen(seen: set):
    db = load_db()
    for h in seen:
        rec = db.setdefault(h, {})
        rec["notified"] = True
    save_db(db)

def update_item_cache(item: dict, *, push: bool = False) -> bool:
    """Salva date/stato di un atto senza marcarlo come notificato.

    Restituisce True se il database è cambiato. Di default scrive il file ma
    non pusha subito, così enrich_with_pdf può fare un solo commit finale.
    """
    h  = item_id(item)
    db = load_db()
    rec = db.setdefault(h, {"notified": False})
    old = dict(rec)
    rec.setdefault("notified", False)
    rec["date"]     = item.get("date", "")
    rec["date_end"] = item.get("date_end", "")
    rec["expired"]  = item.get("expired", True)
    changed = rec != old
    if changed:
        save_db(db, push=push, message="aggiornamento cache albo [skip ci]")
    return changed

def enrich_from_cache(item: dict) -> bool:
    """
    Carica date e expired dalla cache se disponibili.
    Restituisce True se i dati erano in cache (skip MC02), False altrimenti.
    """
    h  = item_id(item)
    db = load_db()
    if h in db and db[h].get("date"):
        item["date"]     = db[h].get("date", "")
        item["date_end"] = db[h].get("date_end", "")
        item["expired"]  = db[h].get("expired", True)
        return True
    return False

# ---------------------------------------------------------------------------
# Database news viste
# ---------------------------------------------------------------------------
NEWS_DB_PATH = DATA_DIR / "seen_news.json"

def load_news_db() -> dict:
    """
    Carica il database news. Formato:
    { "id": {"title": ..., "category": ..., "date": "DD-MM-YYYY", "url": ...} }
    """
    if not NEWS_DB_PATH.exists():
        return {}
    return json.loads(NEWS_DB_PATH.read_text(encoding="utf-8"))

def save_news_db(db: dict):
    NEWS_DB_PATH.write_text(
        json.dumps(db, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    git_commit_and_push([str(NEWS_DB_PATH)], message="aggiornamento archivio news [skip ci]")

def load_seen_news() -> set:
    return set(load_news_db().keys())

def save_seen_news(seen_ids: set, items_by_id: dict):
    """
    Salva gli id visti, conservando i metadati (title/category/date/url)
    per ogni news passata in items_by_id.
    """
    db = load_news_db()
    for nid in seen_ids:
        if nid not in db:
            item = items_by_id.get(nid, {})
            db[nid] = {
                "title":    item.get("title", ""),
                "category": item.get("category", ""),
                "date":     item.get("date", ""),
                "url":      item.get("url", ""),
            }
    for nid in list(db.keys()):
        if nid not in seen_ids:
            del db[nid]
    save_news_db(db)

# ---------------------------------------------------------------------------
# Cache "atti visti" per singolo utente (diversa dalla cache globale atti)
# ---------------------------------------------------------------------------
USER_SEEN_PATH = DATA_DIR / "user_seen.json"

# Store temporaneo in memoria per gli hash da rimandare (bypassa il limite
# di 64 byte di callback_data — il bottone passa solo un ID corto)
_pending_resend: dict[str, list[str]] = {}

def store_pending_resend(chat_id: int, hashes: list[str]) -> str:
    """Salva gli hash in memoria e restituisce un riferimento corto."""
    ref = f"{chat_id}_{int(datetime.now(timezone.utc).timestamp())}"
    _pending_resend[ref] = hashes
    return ref

def pop_pending_resend(ref: str) -> list[str] | None:
    return _pending_resend.pop(ref, None)


def load_user_seen() -> dict:
    """Formato: { "chat_id_str": ["hash1", "hash2", ...] }"""
    if not USER_SEEN_PATH.exists():
        return {}
    return _decrypt_json(USER_SEEN_PATH.read_bytes())

def save_user_seen(data: dict):
    USER_SEEN_PATH.write_bytes(_encrypt_json(data))
    git_commit_and_push([str(USER_SEEN_PATH)], message="aggiornamento cronologia utenti [skip ci]")

def get_user_seen_hashes(chat_id: int) -> set:
    data = load_user_seen()
    return set(data.get(str(chat_id), []))

def mark_user_seen(chat_id: int, hashes: list):
    """Aggiunge una lista di hash atto come 'visti' da questo utente."""
    data = load_user_seen()
    key  = str(chat_id)
    current = set(data.get(key, []))
    current.update(hashes)
    data[key] = sorted(current)
    save_user_seen(data)

def touch_last_check():
    """Aggiorna il timestamp locale dell'ultimo controllo.

    Su GitHub persiste il file al massimo una volta al giorno: il comando
    /status continua a leggere l'orario aggiornato dal filesystem del runner,
    ma si evitano circa 96 commit al giorno quando il polling è ogni 15 minuti.
    Il commit giornaliero è sufficiente anche a mantenere attivo il workflow
    nei repository pubblici inattivi.
    """
    now = datetime.now(timezone.utc)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    previous = ""
    if LAST_CHECK_PATH.exists():
        previous = LAST_CHECK_PATH.read_text(encoding="utf-8").strip()

    LAST_CHECK_PATH.write_text(now_text, encoding="utf-8")

    previous_day = previous[:10] if len(previous) >= 10 else ""
    current_day = now_text[:10]
    if previous_day != current_day:
        git_commit_and_push(
            [str(LAST_CHECK_PATH)],
            message="heartbeat giornaliero [skip ci]",
        )

# ---------------------------------------------------------------------------
# Heartbeat — inviato solo se il run è quello delle 9:00 UTC (ora italiana 10/11)
# ---------------------------------------------------------------------------
async def send_heartbeat(bot: Bot, seen: set, seen_news: set | None = None):
    now = datetime.now(timezone.utc)
    # Il bot gira ogni INTERVAL_MINUTES: per evitare invii multipli nella
    # stessa ora (un ciclo per ogni passaggio dentro l'ora 9), la finestra
    # utile è ampia esattamente quanto un intervallo di polling — così,
    # qualunque sia INTERVAL_MINUTES, al massimo un solo ciclo ci ricade
    # dentro, e l'eventuale spostamento dell'orario di avvio di GHA non
    # rompe il guard.
    if not (now.hour == 9 and now.minute < CONFIG["INTERVAL_MINUTES"]):
        return
    subs      = load_subscribers()
    subs_news = load_subscribers_news()
    news_line = f"🗞 News in archivio: *{len(seen_news)}*\n👥 Iscritti news: *{len(subs_news)}*\n" if seen_news is not None else ""
    text = (
        "✅ *Albo Pretorio Bot – report giornaliero*\n\n"
        f"🗂 Atti in archivio: *{len(seen)}*\n"
        f"👥 Iscritti albo: *{len(subs)}*\n"
        f"{news_line}"
        f"🕐 {now.strftime('%d/%m/%Y %H:%M')} UTC\n\n"
        "_Tutto funziona correttamente._"
    )
    for chat_id in CONFIG["ADMIN_IDS"]:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
            log.info(f"💓 Heartbeat inviato a {chat_id}")
        except Exception as e:
            log.error(f"Errore heartbeat: {e}")

# ---------------------------------------------------------------------------
# Sessione Halley EG — con retry
# ---------------------------------------------------------------------------
async def open_session(client: httpx.AsyncClient) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = await client.post(
                BASE_URL,
                content=f"ss=1&F=MC09&en={ENTE}&freeze=1".encode(),
                headers=HEADERS
            )
            r.raise_for_status()
            soup   = BeautifulSoup(r.text, "html.parser")
            jb_tag = soup.find("meta", {"name": "jb"})
            jb     = jb_tag["content"] if jb_tag and jb_tag.get("content") else ""
            if not jb:
                m  = re.search(r'name="jb"\s+content="([^"]+)"', r.text)
                jb = m.group(1) if m else ""
            if jb:
                return ORIGIN + "/" + jb.lstrip("/")
            log.warning(f"Token jb non trovato (tentativo {attempt}/{MAX_RETRIES})")
        except httpx.HTTPError as e:
            log.warning(f"Errore sessione tentativo {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
    log.error("Impossibile aprire sessione Halley dopo tutti i tentativi.")
    return None

# ---------------------------------------------------------------------------
# Fetch lista atti con paginazione completa (senza PDF)
# ---------------------------------------------------------------------------
def has_next_page(html: str) -> bool:
    """Controlla se esiste il bottone pagina successiva."""
    soup = BeautifulSoup(html, "html.parser")
    nxt  = soup.find("li", id="btSucc")
    return bool(nxt)

async def fetch_all_pages(client: httpx.AsyncClient, session_url: str, on_progress=None) -> list:
    """
    Scarica tutte le pagine dell'albo nella sessione corrente.
    Pagina 1: POST &F=MC01
    Pagina N: POST &F=PMC02&1=N
    Termina quando non c'è più il bottone "pagina successiva".

    on_progress: callback opzionale async, chiamata dopo ogni pagina
    come on_progress(numero_pagina, totale_atti_finora) — usata da
    /atti per mostrare un messaggio di avanzamento, senza che la logica
    di paginazione debba essere duplicata altrove.
    """
    all_items  = []
    seen_righe = set()

    # Pagina 1
    r = await client.post(
        session_url,
        content=f"&F=MC01&en={ENTE}".encode(),
        headers=HEADERS
    )
    r.raise_for_status()
    page_items = parse_albo_html(r.text)
    for item in page_items:
        if item["num_riga"] not in seen_righe:
            seen_righe.add(item["num_riga"])
            all_items.append(item)
    log.info(f"Pagina 1: {len(page_items)} atti")
    if on_progress:
        await on_progress(1, len(all_items))

    # Pagine successive
    page = 2
    while has_next_page(r.text):
        await asyncio.sleep(0.5)
        r = await client.post(
            session_url,
            content=f"&F=PMC02&1={page}&en={ENTE}".encode(),
            headers=HEADERS
        )
        r.raise_for_status()
        page_items = parse_albo_html(r.text)
        if not page_items:
            break
        new_count = 0
        for item in page_items:
            if item["num_riga"] not in seen_righe:
                seen_righe.add(item["num_riga"])
                all_items.append(item)
                new_count += 1
        log.info(f"Pagina {page}: {len(page_items)} atti ({new_count} nuovi)")
        if on_progress:
            await on_progress(page, len(all_items))
        if new_count == 0:
            break  # sicurezza anti-loop infinito
        page += 1

    log.info(f"Totale atti recuperati da tutte le pagine: {len(all_items)}")
    return all_items

async def fetch_albo_html() -> list | None:
    """Restituisce lista atti (tutte le pagine), senza PDF."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                session_url = await open_session(client)
                if not session_url:
                    return None
                return await fetch_all_pages(client, session_url)
        except httpx.HTTPError as e:
            log.warning(f"Errore fetch_albo tentativo {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
    log.error("Fetch albo fallito dopo tutti i tentativi.")
    return None

# ---------------------------------------------------------------------------
# Fetch e parsing News (EGSCHTST6.HBL) — pubblica, no PDF/sessione MC02
# ---------------------------------------------------------------------------
def parse_news_html(html: str) -> list:
    """
    Estrae le card 'news' dalla pagina lista. A differenza dell'albo,
    data e descrizione sono già presenti nella card stessa: non serve
    un secondo step di dettaglio (niente equivalente di MC02).
    """
    soup  = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.card-wrapper")
    items = []
    for card in cards:
        link = card.select_one("h3.card-title a")
        if not link:
            continue
        title = link.get_text(strip=True)
        url   = link.get("href", "")
        if not title or not url:
            continue

        # ID stabile dal numero progressivo nell'URL (novita_166.html -> "166")
        m   = re.search(r"novita_(\d+)\.html", url)
        nid = m.group(1) if m else hashlib.sha256(url.encode()).hexdigest()[:12]

        cat_div  = card.select_one("div.category-top")
        category = ""
        date     = ""
        if cat_div:
            date_span = cat_div.select_one("span.data")
            date = date_span.get_text(strip=True) if date_span else ""
            # Il testo della categoria è quello che precede lo span data
            category = cat_div.get_text(strip=True)
            if date:
                category = category.replace(date, "").strip()

        desc_tag    = card.select_one("p.card-text")
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        items.append({
            "id":          nid,
            "title":       title,
            "url":         url,
            "category":    category,
            "date":        date,
            "description": description,
        })
    return items

def has_next_news_page(html: str) -> bool:
    """Stesso pattern dell'albo: controlla presenza bottone pagina successiva."""
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.find("li", id="btSucc"))

async def fetch_all_news_pages(client: httpx.AsyncClient, max_pages: int = 30, stop_at_known: set | None = None) -> list:
    """
    Scarica le pagine di News in ordine, fermandosi quando:
    - non c'è più il bottone 'pagina successiva', oppure
    - le news cominciano a ripetersi (sicurezza anti-loop, come per l'albo), oppure
    - se `stop_at_known` è fornito (gli ID già noti in cache): la pagina corrente
      non contiene NESSUN ID nuovo rispetto a quelli già visti. Dato che il sito
      elenca le news dalla più recente alla più vecchia, una volta raggiunta
      una pagina "tutta nota" tutto il resto è storico immutabile — non ha senso
      continuare a scaricare pagine vecchie ad ogni check periodico.

    Nota: a differenza dell'albo qui non serve apertura sessione MC09/jb
    — la pagina è pubblica. La navigazione LEGGE() lato client è una
    semplice GET con querystring (confermato via tab Network), non un
    POST come per l'albo: stessa forma '?en=...&MESSA=PAGSUCC=N'.
    """
    all_items = []
    seen_ids  = set()  # dedup locale tra pagine (diverso da stop_at_known)

    def page_has_new(page_items: list) -> bool:
        if stop_at_known is None:
            return True  # nessun confronto richiesto: non fermarsi mai per questo
        return any(i["id"] not in stop_at_known for i in page_items)

    r = await client.get(f"{NEWS_URL}?en={ENTE}&MESSA=PUBBLICA", headers=HEADERS)
    r.raise_for_status()
    page_items = parse_news_html(r.text)
    for item in page_items:
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            all_items.append(item)
    log.info(f"[news] Pagina 1: {len(page_items)} news")

    if not page_has_new(page_items):
        log.info("[news] Pagina 1 già tutta nota — stop anticipato (nessuna pagina successiva scaricata).")
        return all_items

    page = 2
    while has_next_news_page(r.text) and page <= max_pages:
        await asyncio.sleep(0.5)
        r = await client.get(
            f"{NEWS_URL}?en={ENTE}&MESSA=PAGSUCC={page}",
            headers=HEADERS
        )
        r.raise_for_status()
        page_items = parse_news_html(r.text)
        if not page_items:
            break
        new_count = 0
        for item in page_items:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                all_items.append(item)
                new_count += 1
        log.info(f"[news] Pagina {page}: {len(page_items)} news ({new_count} nuove)")
        if new_count == 0:
            break  # sicurezza anti-loop infinito
        if not page_has_new(page_items):
            log.info(f"[news] Pagina {page} già tutta nota — stop anticipato (cache già coperta).")
            break
        page += 1

    log.info(f"[news] Totale news recuperate: {len(all_items)}")
    return all_items

async def fetch_news_html(max_pages: int = 30, stop_at_known: set | None = None) -> list | None:
    """Restituisce lista news (paginazione completa, o parziale se stop_at_known è dato)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                return await fetch_all_news_pages(client, max_pages=max_pages, stop_at_known=stop_at_known)
        except httpx.HTTPError as e:
            log.warning(f"Errore fetch_news tentativo {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
    log.error("Fetch news fallito dopo tutti i tentativi.")
    return None

def news_id(item: dict) -> str:
    return item["id"]

# ---------------------------------------------------------------------------
# Fetch allegati per una lista di atti (dentro sessione già aperta)
# ---------------------------------------------------------------------------
async def enrich_with_pdf(client: httpx.AsyncClient, session_url: str, items: list) -> list:
    cache_updates = 0
    # Rientra nella sessione MC01 prima di richiedere i dettagli
    await client.post(
        session_url,
        content=f"&F=MC01&en={ENTE}".encode(),
        headers=HEADERS
    )
    for item in items:
        num_riga = item.get("num_riga")
        if not num_riga:
            continue
        try:
            # Cache hit su atto scaduto: skip totale di MC02
            if enrich_from_cache(item) and item.get("expired"):
                log.debug(f"Cache hit (scaduto, skip MC02): {item['title'][:40]}")
                continue

            r2 = await client.post(
                session_url,
                content=f"&F=MC02&NUMRIGA={num_riga}&en={ENTE}".encode(),
                headers=HEADERS
            )
            soup2 = BeautifulSoup(r2.text, "html.parser")

            # Se i dati sono in cache (attivo), salta solo il parsing date
            if enrich_from_cache(item):
                log.debug(f"Cache hit (attivo): {item['title'][:40]}")
            else:
                cal_dates = soup2.select("div.calendar-date-day span strong")
                date_vals = [d.get_text(strip=True) for d in cal_dates if d.get_text(strip=True)]
                if len(date_vals) >= 1:
                    item["date"] = date_vals[0]
                if len(date_vals) >= 2:
                    item["date_end"] = date_vals[1]
                    try:
                        d = datetime.strptime(date_vals[1], "%d-%m-%Y").replace(tzinfo=timezone.utc)
                        item["expired"] = d.date() < datetime.now(timezone.utc).date()
                    except ValueError:
                        item["expired"] = True
                else:
                    item["expired"] = True
                # Salva in cache tecnica, senza marcarlo come notificato.
                if update_item_cache(item, push=False):
                    cache_updates += 1

            allegati = []
            for func in ["MC96", "MC97", "MC98", "MC99"]:
                pattern = re.compile(rf"{func}\(")
                tags    = soup2.find_all("a", attrs={"onclick": pattern})
                for tag in tags:
                    m = re.search(rf"{func}\(['\"]?(\d+)['\"]?\)", tag.get("onclick", ""))
                    if not m:
                        continue
                    num_alleg = m.group(1)
                    filename  = tag.get_text(strip=True) or f"allegato_{num_alleg}.pdf"
                    # Assicura estensione .pdf
                    if not filename.lower().endswith(".pdf"):
                        filename += ".pdf"
                    r3 = await client.post(
                        session_url,
                        content=f"&F={func}&NUMRIG={num_alleg}&en={ENTE}".encode(),
                        headers=HEADERS
                    )
                    data = r3.json()
                    if data.get("K") == "000" and data.get("PATH"):
                        allegati.append({"url": data["PATH"], "filename": filename})
                    await asyncio.sleep(0.2)
            if allegati:
                item["allegati"] = allegati
                log.info(f"'{item['title'][:40]}': {len(allegati)} allegato/i trovati")
            else:
                # Nessuna eccezione, ma nessun link MC96-99 trovato nella pagina:
                # può essere un atto genuinamente senza allegati, o un codice
                # funzione diverso da quelli che riconosciamo — non lo sappiamo
                # con certezza, ma almeno ora è visibile nei log invece di
                # essere indistinguibile da un fallimento silenzioso.
                log.info(f"'{item['title'][:40]}' (riga {num_riga}): nessun allegato rilevato "
                          "(nessun link MC96-99 trovato — verifica manuale consigliata se inatteso).")
            await asyncio.sleep(0.3)
        except Exception as e:
            log.warning(
                f"Errore durante il recupero allegati per '{item.get('title', '?')[:50]}' "
                f"(riga {num_riga}): {type(e).__name__}: {e}"
            )
    if cache_updates:
        git_commit_and_push([str(DB_PATH)], message="aggiornamento cache albo [skip ci]")
    return items

# ---------------------------------------------------------------------------
# Fetch lista atti + PDF in sessione unica
# ---------------------------------------------------------------------------
async def fetch_atti_with_pdf() -> list | None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        session_url = await open_session(client)
        if not session_url:
            return None
        try:
            items = await fetch_all_pages(client, session_url)
        except httpx.HTTPError as e:
            log.error(f"Errore fetch_atti_with_pdf: {e}")
            return None
        if not items:
            return []
        await enrich_with_pdf(client, session_url, items)
    return items

# ---------------------------------------------------------------------------
# Parsing lista atti
# ---------------------------------------------------------------------------
def item_id(item: dict) -> str:
    """
    ID stabile basato su titolo + numero pubblicazione + tipo.
    NON usa num_riga: è la posizione dell'atto nella sessione Halley
    corrente, non un ID permanente — può cambiare tra una sessione e
    l'altra anche per lo stesso identico atto, causando falsi positivi
    di "atto nuovo" e rinvii di notifiche per atti già visti da anni.
    """
    raw = (item.get("title", "") + "|" + item.get("num_pub", "") + "|" + item.get("tipo", "")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]

def item_status(item: dict) -> str:
    """
    Restituisce lo stato dell'atto:
    - 'active'    — non scaduto
    - 'recent'    — scaduto da <= RECENT_EXPIRED_DAYS giorni
    - 'expired'   — scaduto da > RECENT_EXPIRED_DAYS, pubblicato >= ARCHIVE_CUTOFF_DATE
    - 'archived'  — pubblicato prima di ARCHIVE_CUTOFF_DATE
    """
    # Controlla data pubblicazione
    date_str = item.get("date", "")
    if date_str:
        try:
            pub = datetime.strptime(date_str, "%d-%m-%Y").replace(tzinfo=timezone.utc)
            if pub < ARCHIVE_CUTOFF_DATE:
                return "archived"
        except ValueError:
            pass

    if not item.get("expired"):
        return "active"

    # Scaduto: controlla da quanto
    date_end_str = item.get("date_end", "")
    if date_end_str:
        try:
            end = datetime.strptime(date_end_str, "%d-%m-%Y").date()
            days_since_end = (datetime.now(timezone.utc).date() - end).days
            if 0 <= days_since_end <= RECENT_EXPIRED_DAYS:
                return "recent"
        except ValueError:
            pass
    return "expired"

def parse_albo_html(html: str) -> list:
    soup  = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.cmp-card")
    log.info(f"Card trovate nel DOM: {len(cards)}")
    items     = []
    seen_keys = set()
    for card in cards:
        link = card.find("a", attrs={"onclick": re.compile(r"MC02\(")})
        if not link:
            continue
        h5    = link.find("h5")
        title = h5.get_text(strip=True) if h5 else link.get_text(strip=True)
        if not title:
            continue

        # Estrai num_riga dall'onclick
        onclick  = link.get("onclick", "")
        nm       = re.search(r"MC02\(['\"]?(\d+)['\"]?\)", onclick)
        num_riga = nm.group(1) if nm else ""

        # Deduplicazione robusta su (num_riga, title)
        dedup_key = f"{num_riga}|{title}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        num_span   = card.find("span", class_="fw-semibold")
        num_pub    = num_span.get_text(strip=True) if num_span else ""
        card_text   = card.get_text(separator=" ", strip=True)
        date_match  = re.search(r"Pubblicazione dal[\s\S]{0,10}?(\d{2}-\d{2}-\d{4})[\s\S]{0,10}?al[\s\S]{0,10}?(\d{2}-\d{2}-\d{4})", card_text)
        date_start  = date_match.group(1) if date_match else ""
        date_end    = date_match.group(2) if date_match else ""
        tipo_match  = re.search(r"Tipo:\s+([A-ZÀÈÉÌÒÙA-Z ]+)", card_text)
        tipo        = tipo_match.group(1).strip() if tipo_match else ""

        # Calcola se l'atto è scaduto
        # Se manca la data di scadenza, assume scaduto (atti vecchi senza data)
        expired = True
        if date_end:
            try:
                d = datetime.strptime(date_end, "%d-%m-%Y").replace(tzinfo=timezone.utc)
                expired = d.date() < datetime.now(timezone.utc).date()
            except ValueError:
                pass

        items.append({
            "title":      title,
            "num_riga":   num_riga,
            "date":       date_start,
            "date_end":   date_end,
            "expired":    expired,
            "tipo":       tipo,
            "num_pub":    num_pub,
            "allegati":   [],
        })
    log.info(f"Atti unici estratti: {len(items)}")
    return items

# ---------------------------------------------------------------------------
# Formattazione e invio messaggi
# ---------------------------------------------------------------------------
def format_caption(item: dict) -> str:
    """
    Usa ParseMode.HTML invece di Markdown, per lo stesso motivo di
    format_news_message: title/tipo sono testo arbitrario scrapato dal
    portale e possono contenere _, *, [, ` in qualsiasi combinazione — in
    Markdown legacy questi non si possono escapare DENTRO un'entità già
    aperta (*titolo_con_underscore* causa sempre 'Can't parse entities'),
    mentre in HTML il problema non esiste.
    """
    tipo     = f" ({escape_html(item['tipo'])})" if item.get("tipo") else ""
    allegati = item.get("allegati", [])
    lines    = ["🏛 <b>Nuovo atto in Albo Pretorio</b>\n"]
    lines.append(f"📄 <b>{escape_html(item['title'])}</b>{tipo}")
    if item.get("num_pub"):
        lines.append(f"🔢 N° {escape_html(item['num_pub'])}")
    if item.get("date") and item.get("date_end"):
        lines.append(f"📅 Dal {escape_html(item['date'])} al {escape_html(item['date_end'])}")
    elif item.get("date"):
        lines.append(f"📅 Dal {escape_html(item['date'])}")
    if len(allegati) > 1:
        lines.append(f"📎 {len(allegati)} documenti allegati — seguono in sequenza")
    elif len(allegati) == 1:
        lines.append("📎 1 documento allegato")
    else:
        lines.append("📎 Nessun documento allegato")
    return "\n".join(lines)

async def _download_and_send_docs(bot_or_update, chat_id: int | None, item: dict, is_reply: bool = False) -> bool:
    """
    Invia caption + allegati a un chat_id oppure come reply a un update.
    Restituisce True se almeno il messaggio principale è stato inviato.
    Gli allegati possono fallire singolarmente senza far perdere lo stato del
    messaggio principale: vengono loggati e ritentati al prossimo /atti.
    """
    caption  = format_caption(item)
    allegati = item.get("allegati", [])

    async def send_text(text):
        if is_reply:
            await bot_or_update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await bot_or_update.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

    async def send_doc(content, filename):
        if is_reply:
            await bot_or_update.message.reply_document(document=content, filename=filename)
        else:
            await bot_or_update.send_document(chat_id=chat_id, document=content, filename=filename)

    try:
        await send_text(caption)
        async with httpx.AsyncClient(follow_redirects=True, timeout=45) as dl:
            for alleg in allegati:
                await asyncio.sleep(0.3)
                resp = None
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        resp = await dl.get(alleg["url"])
                        resp.raise_for_status()
                        break
                    except Exception as e:
                        log.warning(f"Tentativo {attempt}/{MAX_RETRIES} fallito per '{alleg['filename']}': {e}")
                        resp = None
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(RETRY_DELAY)
                if resp is not None:
                    try:
                        await send_doc(resp.content, alleg["filename"])
                    except Exception as e:
                        log.warning(f"Impossibile inviare allegato '{alleg['filename']}': {e}")
                else:
                    log.error(f"Allegato '{alleg['filename']}' non scaricato dopo {MAX_RETRIES} tentativi.")
        target = chat_id if not is_reply else "reply"
        log.info(f"✓ Inviato a {target}: {item['title'][:60]}")
        return True
    except Exception as e:
        log.error(f"Errore invio atto: {e}")
        return False

async def send_item_to_chat(bot: Bot, chat_id: int, item: dict) -> bool:
    return await _download_and_send_docs(bot, chat_id, item, is_reply=False)

async def reply_item(update: Update, item: dict) -> bool:
    return await _download_and_send_docs(update, None, item, is_reply=True)

async def notify(bot: Bot, item: dict) -> int:
    """Notifica l'atto a tutti i destinatari e restituisce quanti invii sono riusciti."""
    h = item_id(item)
    sent = 0
    for chat_id in get_all_recipients():
        ok = await send_item_to_chat(bot, chat_id, item)
        if ok:
            sent += 1
            mark_user_seen(chat_id, [h])
        await asyncio.sleep(1.0)  # evita flood verso Telegram
    return sent

# ---------------------------------------------------------------------------
# Formattazione e invio messaggi — News
# ---------------------------------------------------------------------------
def escape_html(text: str) -> str:
    """
    Escape per ParseMode.HTML di Telegram: solo 3 caratteri riservati
    (& < >), e — a differenza di Markdown — l'escape funziona anche
    DENTRO i tag di formattazione (<b>...&amp;...</b> è valido), quindi
    niente rischio di 'entity sbilanciata' per titoli con caratteri
    speciali arbitrari (_, *, [, ecc. non significano nulla in HTML).
    """
    if not text:
        return text
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

def format_news_message(item: dict) -> str:
    """
    Usa ParseMode.HTML invece di Markdown: i titoli/descrizioni sono
    testo arbitrario scrapato dal sito e possono contenere _, *, [, `
    in qualsiasi combinazione. In Markdown legacy questi caratteri non
    si possono escapare DENTRO un'entità già aperta (es. *titolo_con_underscore*
    causa sempre 'Can't parse entities'); in HTML il problema non esiste.
    """
    cat      = (item.get("category") or "").strip()
    emoji    = NEWS_CATEGORY_EMOJI.get(cat.lower(), NEWS_CATEGORY_DEFAULT_EMOJI)
    lines    = [f"{emoji} <b>Nuova news pubblicata sul sito</b>\n"]
    cat_line = f" <i>{escape_html(cat)}</i>" if cat else ""
    lines.append(f"📌 <b>{escape_html(item['title'])}</b>{cat_line}")
    if item.get("date"):
        lines.append(f"🗓 {escape_html(item['date'])}")
    if item.get("description"):
        lines.append(f"\n{escape_html(item['description'])}")
    if item.get("url"):
        lines.append(f"\n🔗 {escape_html(item['url'])}")
    return "\n".join(lines)

async def send_news_to_chat(bot: Bot, chat_id: int, item: dict) -> bool:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=format_news_message(item),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        log.info(f"✓ News inviata a {chat_id}: {item['title'][:60]}")
        return True
    except Exception as e:
        log.error(f"Errore invio news a {chat_id}: {e}")
        return False

async def notify_news(bot: Bot, item: dict) -> int:
    sent = 0
    for chat_id in get_all_news_recipients():
        if await send_news_to_chat(bot, chat_id, item):
            sent += 1
        await asyncio.sleep(1.0)  # evita flood verso Telegram
    return sent

# ---------------------------------------------------------------------------
# Comandi Telegram
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs       = load_subscribers()
    subs_news  = load_subscribers_news()
    chat_id    = update.effective_chat.id
    in_albo    = chat_id in subs
    in_news    = chat_id in subs_news

    if in_albo and in_news:
        iscritto = "✅ Sei già iscritto a tutte le notifiche."
    elif in_albo:
        iscritto = "✅ Sei iscritto all'Albo Pretorio. ℹ️ Usa /abbonati\\_news per ricevere anche le news."
    elif in_news:
        iscritto = "✅ Sei iscritto alle News. ℹ️ Usa /abbonati\\_albo per ricevere anche l'albo pretorio."
    else:
        iscritto = "ℹ️ Non sei ancora iscritto. Usa /abbonati per ricevere tutte le notifiche."

    await update.message.reply_text(
        "🏛 *Albo Pretorio Bot*\n"
        "Comune di Roccabascerana\n\n"
        "Ti notifica ogni nuovo atto pubblicato in albo, "
        "con il documento allegato direttamente in chat, "
        "e ogni nuova news pubblicata sul sito del Comune.\n\n"
        f"{iscritto}" + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra solo l'elenco comandi, senza il messaggio di benvenuto di /start."""
    await update.message.reply_text(
        "📋 *Elenco comandi*" + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_abbonati(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Iscrive a entrambe le notifiche: Albo Pretorio + News."""
    chat_id    = update.effective_chat.id
    subs_albo  = load_subscribers()
    subs_news  = load_subscribers_news()
    already_albo = chat_id in subs_albo
    already_news = chat_id in subs_news

    if not already_albo:
        subs_albo.add(chat_id)
        save_subscribers(subs_albo)
    if not already_news:
        subs_news.add(chat_id)
        save_subscribers_news(subs_news)

    log.info(f"Iscrizione completa: {chat_id} (albo: {len(subs_albo)}, news: {len(subs_news)})")

    if already_albo and already_news:
        await update.message.reply_text("✅ Sei già iscritto a tutte le notifiche." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(
        "✅ *Iscrizione completata!*\n\n"
        "Riceverai notifiche sia per i nuovi atti dell'Albo Pretorio "
        "(con documento allegato) sia per le nuove news pubblicate sul sito "
        "del Comune di Roccabascerana."
        + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_disabbonati(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancella entrambe le iscrizioni: Albo Pretorio + News."""
    chat_id = update.effective_chat.id
    if chat_id in CONFIG["ADMIN_IDS"]:
        await update.message.reply_text("ℹ️ Gli amministratori ricevono sempre le notifiche." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return

    subs_albo = load_subscribers()
    subs_news = load_subscribers_news()
    was_albo  = chat_id in subs_albo
    was_news  = chat_id in subs_news

    subs_albo.discard(chat_id)
    subs_news.discard(chat_id)
    save_subscribers(subs_albo)
    save_subscribers_news(subs_news)

    if not was_albo and not was_news:
        await update.message.reply_text("ℹ️ Non eri iscritto a nessuna notifica." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return

    log.info(f"Disiscritto da tutto: {chat_id} (albo: {len(subs_albo)}, news: {len(subs_news)})")
    await update.message.reply_text(
        "✅ *Iscrizioni cancellate.*\n\n"
        "Non riceverai più notifiche né dall'Albo Pretorio né dalle News.\n"
        "Puoi reiscriverti in qualsiasi momento con /abbonati."
        + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_abbonati_albo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs    = load_subscribers()
    if chat_id in subs:
        await update.message.reply_text("✅ Sei già iscritto alle notifiche dell'Albo Pretorio." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    subs.add(chat_id)
    save_subscribers(subs)
    log.info(f"Nuovo iscritto albo: {chat_id} (totale: {len(subs)})")
    await update.message.reply_text(
        "✅ *Iscrizione Albo Pretorio completata!*\n\n"
        "Riceverai una notifica con il documento allegato ogni volta che "
        "viene pubblicato un nuovo atto."
        + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_disabbonati_albo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs    = load_subscribers()
    if chat_id in CONFIG["ADMIN_IDS"]:
        await update.message.reply_text("ℹ️ Gli amministratori ricevono sempre le notifiche." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    if chat_id not in subs:
        await update.message.reply_text("ℹ️ Non eri iscritto alle notifiche dell'Albo Pretorio." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    subs.discard(chat_id)
    save_subscribers(subs)
    log.info(f"Disiscritto albo: {chat_id} (totale: {len(subs)})")
    await update.message.reply_text(
        "✅ Iscrizione Albo Pretorio cancellata." + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_abbonati_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs    = load_subscribers_news()
    if chat_id in subs:
        await update.message.reply_text("✅ Sei già iscritto alle notifiche delle News." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    subs.add(chat_id)
    save_subscribers_news(subs)
    log.info(f"Nuovo iscritto news: {chat_id} (totale: {len(subs)})")
    await update.message.reply_text(
        "✅ *Iscrizione News completata!*\n\n"
        "Riceverai una notifica ogni volta che viene pubblicata una nuova "
        "news sul sito del Comune."
        + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_disabbonati_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs    = load_subscribers_news()
    if chat_id in CONFIG["ADMIN_IDS"]:
        await update.message.reply_text("ℹ️ Gli amministratori ricevono sempre le notifiche." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    if chat_id not in subs:
        await update.message.reply_text("ℹ️ Non eri iscritto alle notifiche delle News." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    subs.discard(chat_id)
    save_subscribers_news(subs)
    log.info(f"Disiscritto news: {chat_id} (totale: {len(subs)})")
    await update.message.reply_text(
        "✅ Iscrizione News cancellata." + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_atti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = await update.message.reply_text("⏳ Connessione al portale...")
    # fetch_all_pages con feedback progressivo
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        session_url = await open_session(client)
        if not session_url:
            await msg.edit_text("❌ Impossibile connettersi al portale. Riprova tra poco." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
            return

        async def update_progress(page, count):
            if page > 1:  # niente edit per la pagina 1, il messaggio iniziale basta
                await msg.edit_text(f"⏳ Scaricando pagina {page}... ({count} atti finora)")

        items = await fetch_all_pages(client, session_url, on_progress=update_progress)
    if not items:
        await msg.edit_text("Nessun atto trovato al momento." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return

    # Recupera date e allegati per tutti gli atti PRIMA di mostrare l'elenco
    # (altrimenti i badge attivo/scaduto sarebbero provvisori e sbagliati)
    msg_allegati = await update.message.reply_text("⏳ Recupero dettagli e allegati...")
    async with httpx.AsyncClient(follow_redirects=True, timeout=180) as client:
        session_url = await open_session(client)
        if session_url:
            await enrich_with_pdf(client, session_url, items)
    await msg_allegati.delete()

    # Elimina il messaggio di stato e manda l'elenco
    # Splitting dinamico: mai superare 3800 caratteri per messaggio
    import html as html_lib
    await msg.delete()
    current_lines = []
    current_len   = 0
    MAX_LEN       = 3800

    async def flush(lines):
        if lines:
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.3)

    for idx, item in enumerate(items, start=1):
        tipo     = f" <i>{html_lib.escape(item.get('tipo', ''))}</i>" if item.get("tipo") else ""
        date     = f" · {item['date']}" if item.get("date") else ""
        date_end = f" → {item['date_end']}" if item.get("date_end") else ""
        num      = f"<b>{html_lib.escape(item.get('num_pub', ''))}</b> — " if item.get("num_pub") else ""
        status   = item_status(item)
        if status == "archived":
            continue  # nascondi atti archiviati
        badge    = {"active": "🟢 ", "recent": "🟡 ", "expired": "🔴 "}.get(status, "🔴 ")
        title    = html_lib.escape(item["title"])
        albo_url = "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL"
        scaduto  = f' — <a href="{albo_url}">consulta l\'albo</a>' if status in ("expired", "recent") else ""
        date_str = f"\n    📅 Dal {item['date']}" if item.get("date") else ""
        date_str += f" al {item['date_end']}" if item.get("date_end") else ""
        line     = f"{idx}. {badge}{num}{title}{tipo}{date_str}{scaduto}"
        if current_len + len(line) + 1 > MAX_LEN:
            await flush(current_lines)
            current_lines = []
            current_len   = 0
        current_lines.append(line)
        current_len += len(line) + 1

    await flush(current_lines)

    # Ora le date sono note: filtra per status
    visibili = [i for i in items if item_status(i) != "archived"]
    attivi_recenti = [i for i in visibili if item_status(i) in ("active", "recent")]
    statuses = [item_status(i) for i in visibili]
    totale_msg = (
        f"📋 *{len(visibili)} atti* in albo\n"
        f"🟢 Attivi: *{statuses.count('active')}* · "
        f"🟡 Scaduti recenti: *{statuses.count('recent')}* · "
        f"🔴 Scaduti: *{statuses.count('expired')}*"
    )
    await update.message.reply_text(totale_msg, parse_mode=ParseMode.MARKDOWN)

    # Personalizzazione per utente: manda solo i PDF mai visti da QUESTO utente
    chat_id      = update.effective_chat.id
    user_seen    = get_user_seen_hashes(chat_id)
    da_inviare   = [i for i in attivi_recenti if i.get("allegati") and item_id(i) not in user_seen]
    gia_visti    = [i for i in attivi_recenti if i.get("allegati") and item_id(i) in user_seen]

    sent_hashes = []
    for item in da_inviare:
        await asyncio.sleep(0.5)
        if await reply_item(update, item):
            sent_hashes.append(item_id(item))

    if sent_hashes:
        mark_user_seen(chat_id, sent_hashes)

    if gia_visti:
        ref = store_pending_resend(chat_id, [item_id(i) for i in gia_visti])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📎 Sì, rimandameli", callback_data=f"resend:{ref}"),
            InlineKeyboardButton("No grazie", callback_data="resend:no"),
        ]])
        await update.message.reply_text(
            f"📨 Hai già ricevuto i documenti di *{len(gia_visti)}* atti attivi. "
            "Vuoi che te li rimandi?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text("✅ Fine." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_atti_resend_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce la risposta al bottone 'Vuoi che te li rimandi?'."""
    query = update.callback_query
    await query.answer()
    data = query.data  # "resend:no" oppure "resend:<ref>"

    if data == "resend:no":
        await query.edit_message_text("👍 Ok, nessun documento rinviato." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return

    ref    = data.split(":", 1)[1]
    hashes = pop_pending_resend(ref)
    if hashes is None:
        await query.edit_message_text(
            "⏰ Richiesta scaduta (il bot potrebbe essere stato riavviato). "
            "Usa di nuovo /atti per riprovare." + MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    chat_id = query.message.chat_id

    # Ricostruisce gli item dalla cache globale atti
    items = await fetch_albo_html()
    if items is None:
        await query.edit_message_text("❌ Impossibile recuperare gli atti. Riprova tra poco.")
        return

    targets = [i for i in items if item_id(i) in hashes]
    if not targets:
        await query.edit_message_text("⚠️ Atti non trovati (potrebbero essere scaduti nel frattempo).")
        return

    await query.edit_message_text(f"⏳ Recupero {len(targets)} documenti...")
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        session_url = await open_session(client)
        if session_url:
            await enrich_with_pdf(client, session_url, targets)

    for item in targets:
        if item.get("allegati"):
            await send_item_to_chat(context.bot, chat_id, item)
            await asyncio.sleep(0.5)

    await context.bot.send_message(chat_id=chat_id, text="✅ Fine." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra le ultime news pubblicate sul sito del Comune."""
    msg   = await update.message.reply_text("⏳ Recupero ultime news...")
    items = await fetch_news_html(max_pages=2)  # bastano le prime pagine per il top N
    if items is None:
        await msg.edit_text("❌ Impossibile recuperare le news. Riprova tra poco." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return
    if not items:
        await msg.edit_text("Nessuna news trovata al momento." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return

    await msg.delete()
    for item in items[:NEWS_LIST_LIMIT]:
        await update.message.reply_text(
            format_news_message(item),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        await asyncio.sleep(0.3)

    await update.message.reply_text("✅ Fine." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_controlla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forza un controllo immediato usando la stessa logica produttiva del polling."""
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Comando riservato agli amministratori.")
        return

    await update.message.reply_text("🔍 Controllo in corso...")

    result_albo = await run_check(context.bot)
    result_news = await run_check_news(context.bot)

    ok_albo = "✅" if result_albo.get("ok") else "❌"
    ok_news = "✅" if result_news.get("ok") else "❌"
    await update.message.reply_text(
        f"{ok_albo} Albo: {result_albo.get('new', 0)} nuovi atti · archivio {result_albo.get('total', 0)}\n"
        f"{ok_news} News: {result_news.get('new', 0)} nuove news · archivio {result_news.get('total', 0)}"
        + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Statistiche bot — solo admin."""
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("⛔ Comando riservato agli amministratori.")
        return
    seen       = load_seen()
    subs       = load_subscribers()
    seen_news  = load_seen_news()
    subs_news  = load_subscribers_news()
    now   = datetime.now(timezone.utc)
    last  = LAST_CHECK_PATH.read_text().strip() if LAST_CHECK_PATH.exists() else "N/D"
    await update.message.reply_text(
        "📊 *Status Albo Pretorio Bot*\n\n"
        f"🗂 Atti in archivio: *{len(seen)}*\n"
        f"👥 Iscritti albo: *{len(subs)}*\n"
        f"🗞 News in archivio: *{len(seen_news)}*\n"
        f"👥 Iscritti news: *{len(subs_news)}*\n"
        f"🕐 Ora UTC: {now.strftime('%d/%m/%Y %H:%M')}\n"
        f"🔄 Ultimo check: {last}\n\n"
        f"⚙️ Intervallo polling: {CONFIG['INTERVAL_MINUTES']} min",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Non riconosco questo messaggio." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Check principale — richiamato sia da GHA (run singolo) che dal loop
# ---------------------------------------------------------------------------
async def run_check(bot: Bot) -> dict:
    """
    Esegue un singolo ciclo di controllo.
    Restituisce un dict con i risultati per il logging/reporting.
    """
    seen  = load_seen()
    items = await fetch_albo_html()
    touch_last_check()  # aggiorna sempre, anche se fetch fallisce

    if items is None:
        log.error("Fetch albo fallito — skip ciclo.")
        return {"ok": False, "new": 0, "total": len(seen)}

    new_items = [i for i in items if item_id(i) not in seen]

    if new_items:
        log.info(f"🆕 {len(new_items)} nuovi atti — recupero PDF...")
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
            session_url = await open_session(client)
            if not session_url:
                log.error("Impossibile aprire sessione dettagli: non marco i nuovi atti come visti "
                           "(verranno ritentati al prossimo ciclo).")
                return {"ok": False, "new": 0, "total": len(seen)}
            await enrich_with_pdf(client, session_url, new_items)

        new_seen = set(seen)
        notified_count = 0
        failed_count = 0
        for item in new_items:
            sent = await notify(bot, item)
            if sent > 0:
                new_seen.add(item_id(item))
                notified_count += 1
            else:
                failed_count += 1
                log.warning(f"Atto non marcato come visto perché nessun invio è riuscito: {item.get('title', '?')[:80]}")
            await asyncio.sleep(1.5)  # margine extra tra un atto e il successivo

        save_seen(new_seen)
        if failed_count:
            log.warning(f"⚠️ {failed_count} atti non salvati come notificati: verranno ritentati.")
        log.info(f"✅ {notified_count} nuovi atti notificati e salvati.")
        return {"ok": failed_count == 0, "new": notified_count, "total": len(new_seen), "failed": failed_count}
    else:
        log.info(f"Nessun nuovo atto. (archivio: {len(seen)}, albo ora: {len(items)})")
        return {"ok": True, "new": 0, "total": len(seen)}

def news_is_recent(item: dict, max_age_days: int = NEWS_NOTIFY_MAX_AGE_DAYS) -> bool:
    """
    True se la news è entro la soglia di età per essere notificata via push.
    Se la data non è parsabile, la considera comunque recente (fail-open:
    meglio una notifica in più che perderne una per un errore di parsing).
    """
    date_str = item.get("date", "")
    if not date_str:
        return True
    try:
        pub = datetime.strptime(date_str, "%d-%m-%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - pub) <= timedelta(days=max_age_days)

async def run_check_news(bot: Bot) -> dict:
    """
    Esegue un singolo ciclo di controllo News.
    Stesso schema di run_check, ma senza PDF/sessione MC02.

    Usa stop_at_known per fermare la paginazione alla prima pagina
    interamente già vista: le news più vecchie di quelle in cache non
    cambiano mai, quindi non ha senso ri-scaricare tutto l'archivio
    (25+ pagine) ad ogni ciclo di 6h.

    Le news "nuove" più vecchie di NEWS_NOTIFY_MAX_AGE_DAYS (es. dopo
    un downtime prolungato del bot) vengono segnate come viste ma SENZA
    notifica push — restano comunque visibili con /news.
    """
    seen_news  = load_seen_news()
    news_items = await fetch_news_html(stop_at_known=seen_news)

    if news_items is None:
        log.error("Fetch news fallito — skip ciclo.")
        return {"ok": False, "new": 0, "total": len(seen_news)}

    new_news = [i for i in news_items if news_id(i) not in seen_news]

    if new_news:
        to_notify = [i for i in new_news if news_is_recent(i)]
        too_old   = [i for i in new_news if not news_is_recent(i)]

        if too_old:
            log.info(f"🕓 {len(too_old)} news più vecchie di {NEWS_NOTIFY_MAX_AGE_DAYS}gg — "
                      "segnate come viste senza notifica push.")

        log.info(f"🆕 {len(to_notify)} nuove news da notificare...")
        notified_ids = set()
        failed_notify = 0
        for item in to_notify:
            sent = await notify_news(bot, item)
            if sent > 0:
                notified_ids.add(news_id(item))
            else:
                failed_notify += 1
                log.warning(f"News non marcata come vista perché nessun invio è riuscito: {item.get('title', '?')[:80]}")
            await asyncio.sleep(0.5)

        # Le news troppo vecchie vengono volutamente segnate come viste senza push.
        silent_ids = {news_id(i) for i in too_old}
        ids_to_save = notified_ids | silent_ids
        items_by_id = {news_id(i): i for i in new_news}
        save_seen_news(seen_news | ids_to_save, items_by_id)
        log.info(f"✅ {len(ids_to_save)} nuove news salvate ({len(notified_ids)} notificate, {len(silent_ids)} silenziose, {failed_notify} fallite).")
        return {"ok": failed_notify == 0, "new": len(notified_ids), "total": len(seen_news | ids_to_save), "failed": failed_notify}
    else:
        log.info(f"Nessuna nuova news. (archivio: {len(seen_news)}, online ora: {len(news_items)})")
        return {"ok": True, "new": 0, "total": len(seen_news)}

# ---------------------------------------------------------------------------
# Loop principale — check immediato all'avvio, poi ogni INTERVAL_MINUTES
# ---------------------------------------------------------------------------
async def polling_loop(app: Application):
    bot = app.bot

    seen      = load_seen()
    seen_news = load_seen_news()

    # Prima esecuzione: baseline senza notifiche (albo)
    if not seen:
        log.info("Prima esecuzione: costruisco baseline albo senza notifiche...")
        items = await fetch_albo_html()
        if items:
            seen  = {item_id(i) for i in items}
            save_seen(seen)
            touch_last_check()
            log.info(f"Baseline albo: {len(seen)} atti salvati.")

    # Prima esecuzione: baseline senza notifiche (news)
    if not seen_news:
        log.info("Prima esecuzione: costruisco baseline news senza notifiche...")
        news_items = await fetch_news_html()
        if news_items:
            seen_news   = {news_id(i) for i in news_items}
            items_by_id = {news_id(i): i for i in news_items}
            save_seen_news(seen_news, items_by_id)
            log.info(f"Baseline news: {len(seen_news)} news salvate.")

    # Check immediato all'avvio (ogni run GHA controlla subito senza aspettare 3h)
    log.info("Check immediato all'avvio...")
    try:
        result = await run_check(bot)
        seen   = load_seen()
        result_news = await run_check_news(bot)
        seen_news   = load_seen_news()
        await send_heartbeat(bot, seen, seen_news)
        log.info(f"Check avvio: albo nuovi={result['new']}, totale={result['total']} · "
                 f"news nuove={result_news['new']}, totale={result_news['total']}")
    except Exception as e:
        log.error(f"Errore check avvio: {e}", exc_info=True)

    # Loop continuo (su GHA viene chiuso dal timeout dopo ~357 min —
    # la concorrenza tra run è gestita nativamente da GitHub Actions tramite
    # il blocco `concurrency` nel workflow YAML, non serve più gestirla qui).
    log.info(f"Loop polling ogni {CONFIG['INTERVAL_MINUTES']} min.")
    while True:
        await asyncio.sleep(CONFIG["INTERVAL_MINUTES"] * 60)
        try:
            result = await run_check(bot)
            seen   = load_seen()
            result_news = await run_check_news(bot)
            seen_news   = load_seen_news()
            await send_heartbeat(bot, seen, seen_news)
        except Exception as e:
            log.error(f"Errore loop: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    log.info("=== Albo Pretorio Bot avviato ===")
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=90.0,
        write_timeout=90.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(CONFIG["BOT_TOKEN"]).request(request).build()
    app.add_handler(CommandHandler("start",             cmd_start))
    app.add_handler(CommandHandler("help",              cmd_help))
    app.add_handler(CommandHandler("abbonati",          cmd_abbonati))
    app.add_handler(CommandHandler("disabbonati",       cmd_disabbonati))
    app.add_handler(CommandHandler("abbonati_albo",     cmd_abbonati_albo))
    app.add_handler(CommandHandler("disabbonati_albo",  cmd_disabbonati_albo))
    app.add_handler(CommandHandler("abbonati_news",     cmd_abbonati_news))
    app.add_handler(CommandHandler("disabbonati_news",  cmd_disabbonati_news))
    app.add_handler(CommandHandler("atti",              cmd_atti))
    app.add_handler(CommandHandler("news",              cmd_news))
    app.add_handler(CommandHandler("controlla",         cmd_controlla))
    app.add_handler(CommandHandler("status",            cmd_status))
    app.add_handler(CallbackQueryHandler(cmd_atti_resend_callback, pattern=r"^resend:"))
    app.add_handler(MessageHandler(filters.ALL,         cmd_unknown))

    async with app:
        await app.start()
        await app.updater.start_polling()
        log.info("Bot in ascolto comandi Telegram + polling automatico...")
        await polling_loop(app)

if __name__ == "__main__":
    asyncio.run(main())
