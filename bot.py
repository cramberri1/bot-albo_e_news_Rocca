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
import mimetypes
import os
import re
import secrets
import sys
import hashlib
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

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


def _safe_error(error: BaseException | str) -> str:
    """Restituisce un errore utile senza URL, token o identificativi Telegram.

    I log dei job possono essere pubblici. Alcune eccezioni HTTP includono
    l'intero URL Halley (con token di sessione), mentre quelle Telegram possono
    includere il chat ID: nei log conserviamo il tipo e un messaggio redatto.
    """
    if isinstance(error, BaseException):
        label = type(error).__name__
        message = str(error)
    else:
        label = "Errore"
        message = str(error)
    message = re.sub(r"https?://\S+", "[URL redatto]", message, flags=re.IGNORECASE)
    message = re.sub(
        r"(?i)\b(token|authorization|secret|key|jb)\s*[:=]\s*[^\s,;]+",
        r"\1=[redatto]",
        message,
    )
    message = re.sub(
        r"(?i)\b(chat(?:_id)?|user_id|recipient)\s*[:=]\s*-?\d+",
        r"\1=[id redatto]",
        message,
    )
    message = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{5,}\b", "[bot token redatto]", message)
    message = re.sub(r"\b-?\d{6,}\b", "[id redatto]", message)
    message = re.sub(r"\s+", " ", message).strip()
    return f"{label}: {message[:240]}" if message else label

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
# subscribers.json, subscribers_news.json, user_seen.json e
# user_seen_news.json contengono
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
    raise RuntimeError(
        "STATE_ENCRYPTION_KEY mancante: su GitHub Actions è obbligatoria per "
        "leggere e proteggere i file contenenti chat_id Telegram."
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
            # Compatibilità con file JSON in chiaro creati prima della cifratura.
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise RuntimeError(
                    "Impossibile decifrare un file di stato personale: "
                    "STATE_ENCRYPTION_KEY non corrisponde a quella usata in precedenza."
                ) from e
    else:
        # Un token Fernet inizia normalmente con gAAAAA. Senza chiave non va
        # interpretato come JSON: restituiamo un errore operativo comprensibile.
        if raw.startswith(b"gAAAAA"):
            raise RuntimeError(
                "File di stato cifrato rilevato, ma STATE_ENCRYPTION_KEY non è impostata."
            )
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
# Lo stato operativo del bot può (e in produzione dovrebbe) vivere in un
# repository privato separato, montato dal workflow tramite BOT_STATE_DIR.
DATA_DIR = Path(os.environ.get("BOT_STATE_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
NEWS_URL = CONFIG["NEWS_URL"]
ORIGIN   = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(BASE_URL))
ENTE     = "e1396"

# Soglie per la visualizzazione atti
RECENT_EXPIRED_DAYS = 30          # atti scaduti da <= 30 giorni: mostra + allegati
ARCHIVE_CUTOFF_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)  # prima di questa data: nascondi
# Se Halley non indica la scadenza, /atti invia automaticamente i documenti
# soltanto per pubblicazioni recenti: evita di riversare allegati storici senza
# poter stabilire se l'atto sia ancora efficace. L'atto resta comunque visibile.
NO_EXPIRY_ATTACHMENT_MAX_AGE_DAYS = max(1, int(os.environ.get(
    "NO_EXPIRY_ATTACHMENT_MAX_AGE_DAYS", str(RECENT_EXPIRED_DAYS)
)))
# Gli atti già notificati ma ancora rilevanti vengono ricontrollati periodicamente
# per intercettare rettifiche, nuove scadenze o allegati aggiunti/sostituiti.
REVISION_RECHECK_MINUTES = max(15, int(os.environ.get("REVISION_RECHECK_MINUTES", "60")))
REVISION_RECHECK_MAX_ITEMS = max(1, int(os.environ.get("REVISION_RECHECK_MAX_ITEMS", "20")))

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


class IncompleteAlboSnapshot(RuntimeError):
    """La risposta Halley è HTTP 200 ma la lista non è affidabile/completa."""


class IncompleteNewsSnapshot(RuntimeError):
    """La risposta News è HTTP 200 ma la paginazione è risultata incompleta."""
MAX_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_BYTES", str(45 * 1024 * 1024)))
MAX_ATTACHMENT_BATCH_BYTES = int(os.environ.get("MAX_ATTACHMENT_BATCH_BYTES", str(200 * 1024 * 1024)))
MAX_ATTACHMENT_SPOOL_BYTES = int(os.environ.get("MAX_ATTACHMENT_SPOOL_BYTES", str(512 * 1024 * 1024)))
RESEND_REQUEST_TTL_SECONDS = max(60, int(os.environ.get("RESEND_REQUEST_TTL_SECONDS", "1200")))

if MAX_ATTACHMENT_BYTES < 1:
    raise RuntimeError("MAX_ATTACHMENT_BYTES deve essere maggiore di zero.")
if MAX_ATTACHMENT_BATCH_BYTES < MAX_ATTACHMENT_BYTES:
    raise RuntimeError("MAX_ATTACHMENT_BATCH_BYTES non può essere inferiore a MAX_ATTACHMENT_BYTES.")
if MAX_ATTACHMENT_SPOOL_BYTES < MAX_ATTACHMENT_BYTES:
    raise RuntimeError("MAX_ATTACHMENT_SPOOL_BYTES non può essere inferiore a MAX_ATTACHMENT_BYTES.")

# Gli allegati possono essere numerosi e grandi: conservarli tutti come ``bytes``
# moltiplicava la RAM usata da /atti e dal polling. Ora ogni download viene
# scritto a flusso in una directory temporanea privata, con un tetto globale.
# Il file resta disponibile per tutti i destinatari dell'atto e viene eliminato
# appena terminato l'invio (con un finalizer come rete di sicurezza).
_ATTACHMENT_TEMP_DIR_OWNER = tempfile.TemporaryDirectory(prefix="albo-rocca-")
_ATTACHMENT_TEMP_DIR = Path(_ATTACHMENT_TEMP_DIR_OWNER.name).resolve()
_ATTACHMENT_SPOOL_GUARD = threading.RLock()
_ATTACHMENT_SPOOL_BYTES = 0


def _reserve_attachment_spool(size: int) -> bool:
    global _ATTACHMENT_SPOOL_BYTES
    if size < 0:
        return False
    with _ATTACHMENT_SPOOL_GUARD:
        if _ATTACHMENT_SPOOL_BYTES + size > MAX_ATTACHMENT_SPOOL_BYTES:
            return False
        _ATTACHMENT_SPOOL_BYTES += size
        return True


def _release_attachment_spool(size: int):
    global _ATTACHMENT_SPOOL_BYTES
    with _ATTACHMENT_SPOOL_GUARD:
        _ATTACHMENT_SPOOL_BYTES = max(0, _ATTACHMENT_SPOOL_BYTES - max(0, int(size)))


class _AttachmentSpool:
    """File temporaneo posseduto dal singolo allegato, idempotente nel cleanup."""

    __slots__ = ("path", "size", "sha256", "_closed")

    def __init__(self, path: Path, size: int, sha256: str):
        self.path = path.resolve()
        self.size = int(size)
        self.sha256 = sha256
        self._closed = False

    def open(self):
        if self._closed:
            raise FileNotFoundError("allegato temporaneo già eliminato")
        return self.path.open("rb")

    def verify(self) -> bool:
        """Controlla percorso, dimensione e hash senza caricare il file in RAM."""
        if self._closed:
            return False
        try:
            self.path.relative_to(_ATTACHMENT_TEMP_DIR)
            if not self.path.is_file() or self.path.stat().st_size != self.size:
                return False
            digest = hashlib.sha256()
            with self.path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(256 * 1024), b""):
                    digest.update(chunk)
            return self.size > 0 and digest.hexdigest() == self.sha256
        except (OSError, ValueError):
            return False

    def cleanup(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.path.relative_to(_ATTACHMENT_TEMP_DIR)
            self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        finally:
            _release_attachment_spool(self.size)

    def __del__(self):  # pragma: no cover - rete di sicurezza a fine vita
        try:
            self.cleanup()
        except Exception:
            pass


def cleanup_attachment_files(items_or_item):
    """Elimina gli spool posseduti da uno o più atti; può essere richiamata più volte."""
    if isinstance(items_or_item, dict):
        items = [items_or_item]
    else:
        items = list(items_or_item or [])
    for item in items:
        for attachment in item.get("allegati", []) or []:
            spool = attachment.pop("_spool", None)
            if isinstance(spool, _AttachmentSpool):
                spool.cleanup()

# Halley usa identificatori di riga validi soltanto nella sessione corrente.
# Questo lock impedisce che due flussi lista -> dettaglio -> allegati si
# sovrappongano, mentre i lock per chat mantengono ogni atto e i suoi documenti
# contigui su Telegram anche in presenza del polling automatico.
_ALBO_SESSION_LOCK = asyncio.Lock()
_CHAT_DELIVERY_LOCKS: dict[int, asyncio.Lock] = {}


def _chat_delivery_lock(chat_id: int) -> asyncio.Lock:
    lock = _CHAT_DELIVERY_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHAT_DELIVERY_LOCKS[chat_id] = lock
    return lock

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
    _atomic_write_bytes(path, _encrypt_json(sorted(subs)))

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
         "expired": bool|null,
         "delivery_pending": bool,
         "revision_fingerprint": "sha256...",
         "revision_snapshot": {...},
         "last_revision_check_at": "timestamp ISO"
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
        _atomic_write_text(DB_PATH, json.dumps(migrated, ensure_ascii=False, indent=2) + "\n")
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
            _atomic_write_text(DB_PATH, json.dumps(normalized, ensure_ascii=False, indent=2) + "\n")
        return normalized
    raise RuntimeError(
        "seen_items.json ha un formato inatteso: rifiuto di ripartire da database vuoto "
        "per evitare notifiche duplicate o perdita di stato."
    )

def save_db(db: dict, *, push: bool = True, message: str = "aggiornamento database [skip ci]"):
    _atomic_write_text(DB_PATH, json.dumps(db, ensure_ascii=False, indent=2) + "\n")
    if push:
        git_commit_and_push([str(DB_PATH)], message=message)

def git_commit_and_push(
    paths: list | None = None,
    message: str = "aggiornamento database [skip ci]",
    allow_delete: bool = False,
    max_retries: int = 3,
) -> bool:
    """Committa e sincronizza lo stato su GitHub Actions.

    Migliorie rispetto alla versione precedente:
    - individua il vero root Git con ``git rev-parse`` (BOT_STATE_DIR può essere
      anche una sottocartella del repository);
    - dopo un push fallito, un invocazione successiva prova comunque a spedire
      commit locali già esistenti, anche se non ci sono nuove modifiche staged;
    - non tratta silenziosamente una cartella ``data/`` come se fosse per forza
      essa stessa un repository Git.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return True

    if paths is None:
        paths = [
            str(p)
            for p in (
                DB_PATH,
                SUBSCRIBERS_PATH,
                LAST_CHECK_PATH,
                USER_SEEN_PATH,
                USER_SEEN_NEWS_PATH,
                NEWS_DB_PATH,
                SUBSCRIBERS_NEWS_PATH,
            )
            if p.exists()
        ]
        if not paths:
            return True

    try:
        import subprocess

        state_root = DATA_DIR.resolve()
        grouped: dict[Path, list[Path]] = {}

        for raw_path in paths:
            resolved = Path(raw_path).resolve()
            try:
                resolved.relative_to(state_root)
            except ValueError:
                log.error(f"Percorso di stato non autorizzato: {resolved}")
                return False

            probe_dir = resolved if resolved.is_dir() else resolved.parent
            root_probe = subprocess.run(
                ["git", "-C", str(probe_dir), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if root_probe.returncode != 0:
                log.error(
                    f"Impossibile individuare il repository Git per {resolved}: "
                    f"{_safe_error(root_probe.stderr)}"
                )
                return False

            repo_root = Path(root_probe.stdout.strip()).resolve()
            try:
                relative = resolved.relative_to(repo_root)
            except ValueError:
                log.error(f"File di stato fuori dal root Git rilevato: {resolved}")
                return False
            grouped.setdefault(repo_root, []).append(relative)

        all_ok = True
        for repo_root, relative_paths in grouped.items():
            repo = str(repo_root)
            display_paths = [str(path) for path in relative_paths]

            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "AlboBot"],
                check=False,
                timeout=30,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "bot@alborocca"],
                check=False,
                timeout=30,
            )

            add_args = ["git", "-C", repo, "add"]
            if allow_delete:
                add_args.append("-A")
            add_args.extend(["--", *display_paths])
            add = subprocess.run(
                add_args,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if add.returncode != 0:
                log.error(f"git add fallito in {repo}: {_safe_error(add.stderr)}")
                all_ok = False
                continue

            diff = subprocess.run(
                ["git", "-C", repo, "diff", "--staged", "--quiet"],
                check=False,
                timeout=30,
            )
            if diff.returncode not in (0, 1):
                log.error(f"git diff --staged fallito in {repo}.")
                all_ok = False
                continue

            if diff.returncode == 1:
                commit = subprocess.run(
                    ["git", "-C", repo, "commit", "-m", message],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if commit.returncode != 0:
                    log.error(f"git commit fallito in {repo}: {_safe_error(commit.stderr)}")
                    all_ok = False
                    continue

            # Anche senza nuove modifiche può esserci un commit locale rimasto
            # indietro per un precedente push fallito.
            for attempt in range(1, max_retries + 1):
                push = subprocess.run(
                    ["git", "-C", repo, "push"],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                if push.returncode == 0:
                    log.info(
                        f"Stato Git sincronizzato ({len(relative_paths)} file: "
                        f"{', '.join(display_paths)})."
                    )
                    break

                log.warning(
                    f"git push fallito in {repo} (tentativo {attempt}/{max_retries}): "
                    f"{_safe_error(push.stderr)}"
                )
                if attempt >= max_retries:
                    all_ok = False
                    break

                pull = subprocess.run(
                    ["git", "-C", repo, "pull", "--rebase", "--autostash"],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                if pull.returncode != 0:
                    log.warning(
                        f"git pull --rebase fallito in {repo}: {_safe_error(pull.stderr)}"
                    )
                    all_ok = False
                    break

        return all_ok
    except Exception as e:
        log.warning(f"git_commit_and_push fallito: {_safe_error(e)}")
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
    non pusha subito, così enrich_with_attachments può fare un solo commit finale.
    """
    h  = item_id(item)
    db = load_db()
    rec = db.setdefault(h, {"notified": False})
    old = dict(rec)
    rec.setdefault("notified", False)
    rec["date"]     = item.get("date", "")
    rec["date_end"] = item.get("date_end", "")
    rec["expired"]  = item.get("expired")
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
        item["expired"]  = db[h].get("expired")
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

def save_news_db(db: dict, *, push: bool = True):
    _atomic_write_text(NEWS_DB_PATH, json.dumps(db, ensure_ascii=False, indent=2) + "\n")
    if push:
        git_commit_and_push([str(NEWS_DB_PATH)], message="aggiornamento archivio news [skip ci]")

def load_seen_news() -> set:
    return set(load_news_db().keys())

def save_seen_news(seen_ids: set, items_by_id: dict, *, push: bool = True):
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
                "description": item.get("description", ""),
                "notified": True,
                "delivery_pending": False,
            }
    for nid in list(db.keys()):
        if nid not in seen_ids:
            del db[nid]
    save_news_db(db, push=push)

# ---------------------------------------------------------------------------
# Cache "atti visti" per singolo utente (diversa dalla cache globale atti)
# ---------------------------------------------------------------------------
USER_SEEN_PATH      = DATA_DIR / "user_seen.json"
USER_SEEN_NEWS_PATH = DATA_DIR / "user_seen_news.json"

# Store temporaneo in memoria per gli hash da rimandare (bypassa il limite
# di 64 byte di callback_data — il bottone passa solo un ID corto)
_pending_resend: dict[str, dict] = {}


def _purge_expired_resend_requests(now_ts: float | None = None):
    now_ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    for key, request in list(_pending_resend.items()):
        if float(request.get("expires_at", 0)) <= now_ts:
            _pending_resend.pop(key, None)

def store_pending_resend(chat_id: int, hashes: list[str]) -> str:
    """Salva una richiesta breve, non prevedibile, legata alla chat e con TTL."""
    now_ts = datetime.now(timezone.utc).timestamp()
    _purge_expired_resend_requests(now_ts)
    ref = secrets.token_urlsafe(12)
    _pending_resend[ref] = {
        "chat_id": int(chat_id),
        "hashes": list(dict.fromkeys(hashes)),
        "expires_at": now_ts + RESEND_REQUEST_TTL_SECONDS,
    }
    return ref

def pop_pending_resend(ref: str, chat_id: int) -> list[str] | None:
    """Consuma la richiesta solo dalla chat che l'ha creata e prima della scadenza."""
    now_ts = datetime.now(timezone.utc).timestamp()
    _purge_expired_resend_requests(now_ts)
    request = _pending_resend.get(ref)
    if not request or int(request.get("chat_id", -1)) != int(chat_id):
        return None
    _pending_resend.pop(ref, None)
    return list(request.get("hashes", []))


def cancel_pending_resend(ref: str, chat_id: int) -> bool:
    """Annulla un reinvio solo dalla stessa chat che ha creato il token."""
    now_ts = datetime.now(timezone.utc).timestamp()
    _purge_expired_resend_requests(now_ts)
    request = _pending_resend.get(ref)
    if not request or int(request.get("chat_id", -1)) != int(chat_id):
        return False
    _pending_resend.pop(ref, None)
    return True


def load_user_seen() -> dict:
    """Formato: { "chat_id_str": ["hash1", "hash2", ...] }"""
    if not USER_SEEN_PATH.exists():
        return {}
    return _decrypt_json(USER_SEEN_PATH.read_bytes())

def save_user_seen(data: dict, *, push: bool = True):
    _atomic_write_bytes(USER_SEEN_PATH, _encrypt_json(data))
    if push:
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


def load_user_seen_news() -> dict:
    """Formato: { "chat_id_str": ["news_id_1", "news_id_2", ...] }."""
    if not USER_SEEN_NEWS_PATH.exists():
        return {}
    return _decrypt_json(USER_SEEN_NEWS_PATH.read_bytes())


def save_user_seen_news(data: dict, *, push: bool = True):
    _atomic_write_bytes(USER_SEEN_NEWS_PATH, _encrypt_json(data))
    if push:
        git_commit_and_push(
            [str(USER_SEEN_NEWS_PATH)],
            message="aggiornamento consegne news [skip ci]",
        )

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

    _atomic_write_text(LAST_CHECK_PATH, now_text + "\n")

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
async def send_heartbeat(
    bot: Bot,
    seen: set,
    seen_news: set | None = None,
    *,
    albo_ok: bool = True,
    news_ok: bool = True,
):
    """Report giornaliero: non dichiara mai "tutto ok" se un check è fallito."""
    now = datetime.now(timezone.utc)
    if not (now.hour == 9 and now.minute < min(CONFIG["INTERVAL_MINUTES"], 60)):
        return

    subs = load_subscribers()
    subs_news = load_subscribers_news()
    healthy = bool(albo_ok and news_ok)
    headline = (
        "✅ *Albo Pretorio Bot – report giornaliero*"
        if healthy else
        "⚠️ *Albo Pretorio Bot – attenzione*"
    )
    health_lines = [
        f"Albo: {'✅ check riuscito' if albo_ok else '❌ check fallito'}",
        f"News: {'✅ check riuscito' if news_ok else '❌ check fallito'}",
    ]
    news_line = (
        f"🗞 News in archivio: *{len(seen_news)}*\n"
        f"👥 Iscritti news: *{len(subs_news)}*\n"
        if seen_news is not None else ""
    )
    conclusion = (
        "_Tutto funziona correttamente._"
        if healthy else
        "_Il processo è vivo, ma almeno una sorgente non è stata controllata correttamente._"
    )
    text = (
        f"{headline}\n\n"
        f"🗂 Atti in archivio: *{len(seen)}*\n"
        f"👥 Iscritti albo: *{len(subs)}*\n"
        f"{news_line}"
        + "\n".join(health_lines)
        + f"\n🕐 {now.strftime('%d/%m/%Y %H:%M')} UTC\n\n"
        + conclusion
    )

    for chat_id in CONFIG["ADMIN_IDS"]:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
            log.info("💓 Heartbeat inviato a un amministratore.")
        except Exception as e:
            log.error(f"Errore heartbeat: {_safe_error(e)}")

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
            log.warning(f"Errore sessione tentativo {attempt}/{MAX_RETRIES}: {_safe_error(e)}")
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

    async def report_progress(page_number: int):
        if not on_progress:
            return
        try:
            await on_progress(page_number, len(all_items))
        except Exception as error:
            # Il feedback Telegram è accessorio: un edit duplicato o scaduto
            # non deve interrompere né far ripartire la sessione Halley.
            log.debug(f"Aggiornamento progresso /atti ignorato: {_safe_error(error)}")

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
    await report_progress(1)

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
            raise IncompleteAlboSnapshot(
                f"pagina {page} indicata da Halley ma priva di atti parsabili"
            )
        new_count = 0
        for item in page_items:
            if item["num_riga"] not in seen_righe:
                seen_righe.add(item["num_riga"])
                all_items.append(item)
                new_count += 1
        log.info(f"Pagina {page}: {len(page_items)} atti ({new_count} nuovi)")
        await report_progress(page)
        if new_count == 0:
            break  # sicurezza anti-loop infinito
        page += 1

    log.info(f"Totale atti recuperati da tutte le pagine: {len(all_items)}")
    return all_items

async def fetch_albo_html(
    *,
    detail_selector=None,
    on_progress=None,
    force_detail: bool = False,
    push_cache: bool = True,
    raise_on_failure: bool = False,
) -> list | None:
    """Recupera lista e, se richiesto, dettagli nella *stessa* sessione.

    ``num_riga`` e i percorsi MC96-99 sono temporanei e legati alla sessione
    Halley: separarli tra due ``AsyncClient`` può associare un allegato all'atto
    sbagliato. ``detail_selector`` riceve la lista appena letta e restituisce gli
    item da arricchire prima che client e sessione vengano chiusi.
    """
    last_error: BaseException | None = None
    async with _ALBO_SESSION_LOCK:
        for attempt in range(1, MAX_RETRIES + 1):
            items = None
            try:
                timeout = 180 if detail_selector is not None else 60
                async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
                    session_url = await open_session(client)
                    if not session_url:
                        last_error = ConnectionError("impossibile aprire la sessione Halley")
                        break
                    items = await fetch_all_pages(client, session_url, on_progress=on_progress)
                    if detail_selector is not None and items:
                        selected = list(detail_selector(items) or [])
                        if selected:
                            await enrich_with_attachments(
                                client,
                                session_url,
                                selected,
                                push_cache=push_cache,
                                force_detail=force_detail,
                            )
                    return items
            except (httpx.HTTPError, IncompleteAlboSnapshot) as e:
                last_error = e
                cleanup_attachment_files(items)
                log.warning(
                    f"Errore fetch_albo tentativo {attempt}/{MAX_RETRIES}: {_safe_error(e)}"
                )
            except Exception:
                # Gli errori applicativi dell'arricchimento sono gestiti per
                # singolo atto. Se qualcosa sfugge, non lasciare spool orfani.
                cleanup_attachment_files(items)
                raise
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
    log.error("Fetch albo fallito dopo tutti i tentativi.")
    if raise_on_failure and last_error is not None:
        raise last_error
    return None

# ---------------------------------------------------------------------------
# Fetch e parsing News (EGSCHTST6.HBL) — pubblica, senza allegati/sessione MC02
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
        raw_url = link.get("href", "")
        if not title or not raw_url:
            continue
        url = urljoin(NEWS_URL, raw_url)

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
            raise IncompleteNewsSnapshot(
                f"pagina news {page} indicata dal portale ma priva di elementi parsabili"
            )
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
        except (httpx.HTTPError, IncompleteNewsSnapshot) as e:
            log.warning(f"Errore fetch_news tentativo {attempt}/{MAX_RETRIES}: {_safe_error(e)}")
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY)
    log.error("Fetch news fallito dopo tutti i tentativi.")
    return None

def news_id(item: dict) -> str:
    return item["id"]

# ---------------------------------------------------------------------------
# Fetch allegati per una lista di atti (dentro sessione già aperta)
# ---------------------------------------------------------------------------
_DETAIL_IDENTITY_STOPWORDS = {
    "della", "delle", "degli", "dello", "alla", "alle", "agli", "allo",
    "nella", "nelle", "negli", "nello", "con", "per", "del", "dei", "dal",
    "atto", "pubblicazione", "comune",
}


def detail_identity_matches(item: dict, detail_text: str) -> bool:
    """Difesa conservativa contro lo scambio di riga nella pagina MC02."""
    def normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()

    title = normalize(item.get("title", ""))
    detail = normalize(detail_text)
    if not title or not detail:
        return True
    if len(detail.split()) < 5:
        return True
    if len(title) >= 18 and title in detail:
        return True
    tokens = {
        token for token in title.split()
        if len(token) >= 4 and token not in _DETAIL_IDENTITY_STOPWORDS
    }
    if len(tokens) < 3:
        return True
    overlap = len(tokens.intersection(detail.split())) / len(tokens)
    return overlap >= 0.30


_CONTENT_TYPE_SUFFIXES = {
    "application/pdf": ".pdf",
    "application/pkcs7-mime": ".p7m",
    "application/x-pkcs7-mime": ".p7m",
    "application/pkcs7-signature": ".p7s",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/rtf": ".rtf",
    "application/zip": ".zip",
    "application/x-7z-compressed": ".7z",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/tiff": ".tif",
}


def _safe_attachment_suffix(value: str) -> str:
    suffix = Path(value or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return ""
    if suffix in {".hbl", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}:
        return ""
    return suffix


def _content_disposition_filename(header: str) -> str:
    if not header:
        return ""
    match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", header, re.I)
    if match:
        return Path(unquote(match.group(1).strip().strip('"'))).name
    match = re.search(r'filename\s*=\s*"([^"]+)"', header, re.I)
    if not match:
        match = re.search(r"filename\s*=\s*([^;]+)", header, re.I)
    return Path(match.group(1).strip().strip('"')).name if match else ""


def _resolved_attachment_filename(
    requested_name: str,
    attachment_url: str,
    content_type: str,
    content_disposition: str,
    head: bytes,
) -> str:
    """Mantiene il nome Halley e ricava l'estensione senza inventare `.pdf`."""
    requested = Path(requested_name or "allegato").name[:170] or "allegato"
    if _safe_attachment_suffix(requested):
        return requested[:180]

    disposition_name = _content_disposition_filename(content_disposition)
    suffix = _safe_attachment_suffix(disposition_name)
    if not suffix:
        suffix = _safe_attachment_suffix(urlparse(attachment_url).path)

    clean_type = (content_type or "").split(";", 1)[0].strip().lower()
    if not suffix:
        suffix = _CONTENT_TYPE_SUFFIXES.get(clean_type, "")
    if not suffix and clean_type and clean_type != "application/octet-stream":
        guessed = mimetypes.guess_extension(clean_type, strict=False) or ""
        if re.fullmatch(r"\.[a-z0-9]{1,10}", guessed.lower()):
            suffix = guessed.lower()

    signature = (head or b"").lstrip()
    if not suffix:
        if signature.startswith(b"%PDF-"):
            suffix = ".pdf"
        elif signature.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif signature.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif signature[:6] in (b"GIF87a", b"GIF89a"):
            suffix = ".gif"

    # Meglio `.bin` che attribuire al file un formato sbagliato.
    suffix = suffix or ".bin"
    stem = requested[: max(1, 180 - len(suffix))]
    return f"{stem}{suffix}"


async def _download_attachment_to_spool(
    client: httpx.AsyncClient,
    attachment_url: str,
    filename: str,
    batch_bytes: int,
) -> tuple[_AttachmentSpool, str]:
    """Scarica un allegato a flusso e restituisce spool + nome formato reale."""
    temporary = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix="attachment-",
        suffix=_safe_attachment_suffix(filename) or ".bin",
        dir=_ATTACHMENT_TEMP_DIR,
        delete=False,
    )
    path = Path(temporary.name)
    size = 0
    reserved = 0
    head = bytearray()
    digest = hashlib.sha256()
    completed = False
    content_type = ""
    content_disposition = ""
    try:
        async with client.stream("GET", attachment_url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            content_disposition = response.headers.get("content-disposition", "")
            if "text/html" in content_type or "application/json" in content_type:
                raise ValueError(f"contenuto inatteso: {content_type}")
            async for chunk in response.aiter_bytes(256 * 1024):
                if not chunk:
                    continue
                next_size = size + len(chunk)
                if next_size > MAX_ATTACHMENT_BYTES:
                    raise ValueError(
                        f"allegato oltre il limite di {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB"
                    )
                if batch_bytes + next_size > MAX_ATTACHMENT_BATCH_BYTES:
                    raise ValueError("limite complessivo allegati dell'atto superato")
                if not _reserve_attachment_spool(len(chunk)):
                    raise ValueError("spazio temporaneo allegati esaurito; il bot ritenterà")
                reserved += len(chunk)
                temporary.write(chunk)
                digest.update(chunk)
                if len(head) < 2048:
                    head.extend(chunk[:2048 - len(head)])
                size = next_size

        if not size:
            raise ValueError("allegato vuoto")
        signature = bytes(head).lstrip().lower()
        if signature.startswith((b"<!doctype html", b"<html", b"{")):
            raise ValueError("il percorso allegato ha restituito una pagina di errore")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary.close()
        spool = _AttachmentSpool(path, size, digest.hexdigest())
        resolved_name = _resolved_attachment_filename(
            filename,
            attachment_url,
            content_type,
            content_disposition,
            bytes(head),
        )
        completed = True
        return spool, resolved_name
    finally:
        if not temporary.closed:
            temporary.close()
        if not completed:
            try:
                path.unlink(missing_ok=True)
            finally:
                _release_attachment_spool(reserved)


async def enrich_with_attachments(
    client: httpx.AsyncClient,
    session_url: str,
    items: list,
    *,
    push_cache: bool = True,
    force_detail: bool = False,
) -> list:
    cache_updates = 0
    # Rientra nella sessione MC01 prima di richiedere i dettagli
    entry_response = await client.post(
        session_url,
        content=f"&F=MC01&en={ENTE}".encode(),
        headers=HEADERS
    )
    entry_response.raise_for_status()
    for item in items:
        item["allegati"] = []
        item["_attachment_fetch_ok"] = False
        item["_attachment_expected_count"] = 0
        item["_attachment_errors"] = []
        allegati = []
        batch_bytes = 0
        num_riga = item.get("num_riga")
        if not num_riga:
            item["_attachment_errors"].append("identificatore di riga Halley mancante")
            continue
        try:
            r2 = None
            detail_error = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    r2 = await client.post(
                        session_url,
                        content=f"&F=MC02&NUMRIGA={num_riga}&en={ENTE}".encode(),
                        headers=HEADERS,
                    )
                    r2.raise_for_status()
                    detail_error = None
                    break
                except httpx.HTTPError as error:
                    detail_error = error
                    log.warning(
                        f"Dettaglio MC02 {attempt}/{MAX_RETRIES} fallito per "
                        f"'{item.get('title', '')[:45]}': {_safe_error(error)}"
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY)
            if r2 is None or detail_error is not None:
                raise detail_error or RuntimeError("dettaglio MC02 non disponibile")
            soup2 = BeautifulSoup(r2.text, "html.parser")
            item["_detail_text"] = soup2.get_text(" ", strip=True)
            if not detail_identity_matches(item, item["_detail_text"]):
                raise ValueError(
                    "il dettaglio MC02 non corrisponde al titolo della riga; "
                    "allegati rifiutati per sicurezza"
                )
            item["_detail_captured"] = True
            item["_detail_captured_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            # Se i dati sono in cache (attivo), salta solo il parsing date
            if not force_detail and enrich_from_cache(item):
                log.debug(f"Cache hit (attivo): {item['title'][:40]}")
            else:
                cal_dates = soup2.select("div.calendar-date-day span strong")
                date_vals = [d.get_text(strip=True) for d in cal_dates if d.get_text(strip=True)]
                if len(date_vals) >= 1:
                    item["date"] = date_vals[0]
                if len(date_vals) >= 2:
                    item["date_end"] = date_vals[1]

                # Non distruggere dati validi già estratti dalla card lista se
                # Halley cambia il markup del dettaglio MC02.
                effective_end = item.get("date_end", "")
                if effective_end:
                    try:
                        d = datetime.strptime(effective_end, "%d-%m-%Y").replace(tzinfo=timezone.utc)
                        item["expired"] = d.date() < datetime.now(timezone.utc).date()
                    except ValueError:
                        pass
                # Salva in cache tecnica, senza marcarlo come notificato.
                if update_item_cache(item, push=False):
                    cache_updates += 1

            seen_digests = set()
            seen_attachment_refs = set()
            expected_attachments = 0
            for func in ["MC96", "MC97", "MC98", "MC99"]:
                pattern = re.compile(rf"{func}\(")
                tags    = soup2.find_all("a", attrs={"onclick": pattern})
                for tag in tags:
                    m = re.search(rf"{func}\(['\"]?(\d+)['\"]?\)", tag.get("onclick", ""))
                    if not m:
                        continue
                    num_alleg = m.group(1)
                    attachment_ref = (func, num_alleg)
                    if attachment_ref in seen_attachment_refs:
                        continue
                    seen_attachment_refs.add(attachment_ref)
                    expected_attachments += 1
                    filename = Path(tag.get_text(strip=True) or f"allegato_{num_alleg}").name[:170] or f"allegato_{num_alleg}"

                    downloaded = None
                    last_error = None
                    for attempt in range(1, MAX_RETRIES + 1):
                        try:
                            r3 = await client.post(
                                session_url,
                                content=f"&F={func}&NUMRIG={num_alleg}&en={ENTE}".encode(),
                                headers=HEADERS,
                            )
                            r3.raise_for_status()
                            data = r3.json()
                            if data.get("K") != "000" or not data.get("PATH"):
                                raise ValueError(f"Halley non ha restituito un percorso valido (K={data.get('K')})")

                            # PATH è temporaneo e può essere riutilizzato da
                            # Halley: va scaricato subito, prima di chiedere il
                            # documento successivo, nella stessa sessione.
                            attachment_url = urljoin(ORIGIN + "/", str(data["PATH"]))
                            downloaded, resolved_filename = await _download_attachment_to_spool(
                                client,
                                attachment_url,
                                filename,
                                batch_bytes,
                            )
                            filename = resolved_filename
                            break
                        except Exception as error:
                            last_error = error
                            log.warning(
                                f"Download immediato {attempt}/{MAX_RETRIES} fallito per "
                                f"'{filename}' dell'atto '{item.get('title', '')[:45]}': "
                                f"{_safe_error(error)}"
                            )
                            if attempt < MAX_RETRIES:
                                await asyncio.sleep(RETRY_DELAY)

                    if downloaded is None:
                        item["_attachment_errors"].append(
                            f"{filename}: {type(last_error).__name__ if last_error else 'errore sconosciuto'}"
                        )
                        continue
                    digest = downloaded.sha256
                    if digest in seen_digests:
                        log.warning(
                            f"Contenuto allegato duplicato per '{item.get('title', '')[:45]}': {filename}"
                        )
                    seen_digests.add(digest)
                    batch_bytes += downloaded.size
                    allegati.append({
                        "filename": filename,
                        "_spool": downloaded,
                        "sha256": digest,
                        "size": downloaded.size,
                        "position": expected_attachments,
                    })
                    await asyncio.sleep(0.2)
            item["_attachment_expected_count"] = expected_attachments
            item["_attachment_fetch_ok"] = len(allegati) == expected_attachments
            item["allegati"] = allegati
            if allegati:
                log.info(f"'{item['title'][:40]}': {len(allegati)} allegato/i trovati")
            if item["_attachment_errors"]:
                log.warning(
                    f"'{item['title'][:40]}': {len(item['_attachment_errors'])} allegato/i non acquisiti; "
                    "l'atto resterà in stato da ritentare."
                )
            elif not allegati:
                # Nessuna eccezione, ma nessun link MC96-99 trovato nella pagina:
                # può essere un atto genuinamente senza allegati, o un codice
                # funzione diverso da quelli che riconosciamo — non lo sappiamo
                # con certezza, ma almeno ora è visibile nei log invece di
                # essere indistinguibile da un fallimento silenzioso.
                log.info(f"'{item['title'][:40]}' (riga {num_riga}): nessun allegato rilevato "
                          "(nessun link MC96-99 trovato — verifica manuale consigliata se inatteso).")
            # La prima scrittura conserva le date; questa seconda marca il
            # backfill come concluso soltanto quando tutti i link rilevati sono
            # stati acquisiti. Gli errori parziali verranno quindi ritentati.
            if update_item_cache(item, push=False):
                cache_updates += 1
            await asyncio.sleep(0.3)
        except Exception as e:
            cleanup_attachment_files({"allegati": allegati})
            item["allegati"] = []
            item["_attachment_fetch_ok"] = False
            item["_attachment_errors"].append(f"dettaglio atto: {type(e).__name__}")
            log.warning(
                f"Errore durante il recupero allegati per '{item.get('title', '?')[:50]}' "
                f"(riga presente): {_safe_error(e)}"
            )
    if cache_updates and push_cache:
        git_commit_and_push([str(DB_PATH)], message="aggiornamento cache albo [skip ci]")
    return items

# ---------------------------------------------------------------------------
# Fetch lista atti + allegati in sessione unica
# ---------------------------------------------------------------------------
async def fetch_atti_with_attachments() -> list | None:
    return await fetch_albo_html(detail_selector=lambda items: items)

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

def _parse_albo_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return None


def item_status(item: dict) -> str:
    """Classifica l'atto senza confondere scadenza assente e scadenza superata.

    Stati:
    - ``active``: scadenza nota e non ancora trascorsa;
    - ``recent``: scaduto da non oltre RECENT_EXPIRED_DAYS;
    - ``expired``: scaduto da più tempo;
    - ``no_expiry``: Halley non indica una scadenza valida;
    - ``archived``: pubblicato prima della soglia di archivio.
    """
    pub = _parse_albo_date(item.get("date"))
    if pub and pub < ARCHIVE_CUTOFF_DATE.date():
        return "archived"

    end = _parse_albo_date(item.get("date_end"))
    if end is None:
        return "no_expiry"

    today = datetime.now(timezone.utc).date()
    if end >= today:
        return "active"

    days_since_end = (today - end).days
    if days_since_end <= RECENT_EXPIRED_DAYS:
        return "recent"
    return "expired"


def item_attachment_delivery_eligible(item: dict) -> bool:
    """Decide se /atti deve proporre automaticamente i documenti dell'atto."""
    status = item_status(item)
    if status in {"active", "recent"}:
        return True
    if status != "no_expiry":
        return False

    pub = _parse_albo_date(item.get("date"))
    if pub is None:
        return False
    age = (datetime.now(timezone.utc).date() - pub).days
    return 0 <= age <= NO_EXPIRY_ATTACHMENT_MAX_AGE_DAYS


def item_revision_snapshot(item: dict) -> dict | None:
    """Restituisce una fotografia minimale e stabile della revisione corrente.

    I contenuti degli allegati sono rappresentati dall'hash SHA-256: in questo
    modo viene rilevata anche la sostituzione di un file mantenendo lo stesso
    nome. Non salviamo i file nel DB.
    """
    if not item.get("_detail_captured") or not item.get("_attachment_fetch_ok", False):
        return None

    attachments = item.get("allegati", []) or []
    digests = sorted(
        str(a.get("sha256") or "")
        for a in attachments
        if a.get("sha256")
    )
    name_hashes = sorted(
        hashlib.sha256(Path(str(a.get("filename") or "")).name.casefold().encode("utf-8")).hexdigest()[:16]
        for a in attachments
        if a.get("filename")
    )
    return {
        "date": item.get("date", ""),
        "date_end": item.get("date_end", ""),
        "sender": item.get("sender", ""),
        "act_number": item.get("act_number", ""),
        "register_number": item.get("register_number", ""),
        "attachment_count": int(item.get("_attachment_expected_count", len(attachments))),
        "attachment_sha256": digests,
        "attachment_name_hashes": name_hashes,
    }


def item_revision_fingerprint(snapshot: dict | None) -> str:
    if not snapshot:
        return ""
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def revision_delivery_key(item_hash: str, fingerprint: str) -> str:
    return f"{item_hash}@{fingerprint[:24]}"


def revision_changes(previous_snapshot: dict | None, current_snapshot: dict) -> list[str]:
    if not previous_snapshot:
        return []
    changes = []
    if (
        previous_snapshot.get("date") != current_snapshot.get("date")
        or previous_snapshot.get("date_end") != current_snapshot.get("date_end")
    ):
        changes.append("periodo di pubblicazione")
    if any(
        previous_snapshot.get(key) != current_snapshot.get(key)
        for key in ("sender", "act_number", "register_number")
    ):
        changes.append("dati dell'atto")
    if any(
        previous_snapshot.get(key) != current_snapshot.get(key)
        for key in ("attachment_count", "attachment_sha256", "attachment_name_hashes")
    ):
        changes.append("allegati")
    return changes


def _revision_check_due(record: dict, now: datetime) -> bool:
    raw = str(record.get("last_revision_check_at") or "")
    if not raw:
        return True
    try:
        checked = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now - checked) >= timedelta(minutes=REVISION_RECHECK_MINUTES)


def _revision_trackable(item: dict) -> bool:
    return item_status(item) in {"active", "recent", "no_expiry"}


def parse_albo_html(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.cmp-card")
    log.info(f"Card trovate nel DOM: {len(cards)}")
    items = []
    seen_keys = set()
    invalid_candidates = 0
    mc02_candidates = 0

    for card in cards:
        link = card.find("a", attrs={"onclick": re.compile(r"\bMC02\s*\(", re.I)})
        if not link:
            continue
        mc02_candidates += 1

        h5 = link.find("h5")
        title = h5.get_text(strip=True) if h5 else link.get_text(strip=True)
        onclick = link.get("onclick", "")
        nm = re.search(r"\bMC02\s*\(\s*['\"]?(\d+)['\"]?", onclick, re.I)
        num_riga = nm.group(1) if nm else ""

        # Fail-soft sul singolo elemento: una card Halley anomala non deve
        # rendere inutilizzabile l'intero Albo. La saltiamo e continuiamo.
        # Se invece una pagina contiene candidati MC02 ma NESSUNO è parsabile,
        # la pagina nel suo complesso non è affidabile e viene ritentata.
        if not title or not num_riga:
            invalid_candidates += 1
            log.warning(
                "Card Albo ignorata perché incompleta "
                f"(titolo={'presente' if title else 'mancante'}, "
                f"NUMRIGA={'presente' if num_riga else 'mancante'})."
            )
            continue

        dedup_key = f"{num_riga}|{title}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        num_span = card.find("span", class_="fw-semibold")
        num_pub = num_span.get_text(strip=True) if num_span else ""
        card_text = card.get_text(separator=" ", strip=True)
        start_match = re.search(
            r"Pubblicazione\s+dal\s*(\d{2}-\d{2}-\d{4})",
            card_text,
            re.I,
        )
        date_start = start_match.group(1) if start_match else ""
        date_end = ""
        if start_match:
            tail = card_text[start_match.end():]
            end_match = re.search(
                r"^\s*(?:[-–—]\s*)?al\s*(\d{2}-\d{2}-\d{4})",
                tail,
                re.I,
            )
            if end_match:
                date_end = end_match.group(1)

        tipo_match = re.search(r"Tipo:\s*([^|]+?)(?=\s+Mittente:|\s+Atto\s+n\.|$)", card_text, re.I)
        tipo = tipo_match.group(1).strip() if tipo_match else ""
        sender_match = re.search(r"Mittente:\s*(.+?)\s+Tipo:", card_text, re.I)
        act_match = re.search(r"Atto\s+n\.\s*([^\s]+)", card_text, re.I)
        register_match = re.search(r"Registro\s+generale\s+n\.\s*([^\s]+)", card_text, re.I)

        expired = None
        if date_end:
            try:
                d = datetime.strptime(date_end, "%d-%m-%Y").replace(tzinfo=timezone.utc)
                expired = d.date() < datetime.now(timezone.utc).date()
            except ValueError:
                expired = None

        items.append({
            "title": title,
            "num_riga": num_riga,
            "date": date_start,
            "date_end": date_end,
            "expired": expired,
            "tipo": tipo,
            "num_pub": num_pub,
            "sender": sender_match.group(1).strip() if sender_match else "",
            "act_number": act_match.group(1).strip() if act_match else "",
            "register_number": register_match.group(1).strip() if register_match else "",
            "allegati": [],
        })

    if invalid_candidates:
        log.warning(
            f"{invalid_candidates} card MC02 incomplete ignorate; "
            f"continuo con {len(items)} atti validi."
        )
    if mc02_candidates and not items:
        raise IncompleteAlboSnapshot(
            f"pagina con {mc02_candidates} card MC02 ma nessun atto parsabile"
        )

    log.info(f"Atti unici estratti: {len(items)}")
    return items


# ---------------------------------------------------------------------------
# Utility stato / scritture atomiche
# ---------------------------------------------------------------------------
def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _atomic_write_bytes(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def merge_item_groups(*groups: list) -> list:
    """Unisce gruppi di atti senza duplicare la stessa riga/sessione."""
    merged = []
    seen_keys = set()
    for group in groups:
        for item in group:
            key = item.get("num_riga") or item_id(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(item)
    return merged


# ---------------------------------------------------------------------------
# Formattazione e invio messaggi
# ---------------------------------------------------------------------------
def format_caption(item: dict) -> str:
    """Formatta una nuova pubblicazione o una revisione in HTML Telegram."""
    tipo = f" ({escape_html(item['tipo'])})" if item.get("tipo") else ""
    allegati = item.get("allegati", [])
    expected = int(item.get("_attachment_expected_count", len(allegati)))
    is_revision = item.get("_notification_kind") == "revision"
    if is_revision:
        lines = ["♻️ <b>Atto aggiornato in Albo Pretorio</b>\n"]
    else:
        lines = ["🏛 <b>Nuovo atto in Albo Pretorio</b>\n"]

    lines.append(f"📄 <b>{escape_html(item['title'])}</b>{tipo}")
    if item.get("num_pub"):
        lines.append(f"🔢 N° {escape_html(item['num_pub'])}")
    if item.get("date") and item.get("date_end"):
        lines.append(f"📅 Dal {escape_html(item['date'])} al {escape_html(item['date_end'])}")
    elif item.get("date"):
        lines.append(f"📅 Dal {escape_html(item['date'])} · scadenza non indicata")
    else:
        lines.append("📅 Scadenza non indicata")

    if is_revision and item.get("_revision_changes"):
        changes = ", ".join(item["_revision_changes"])
        lines.append(f"🔄 Modifiche rilevate: {escape_html(changes)}")

    if expected > 1:
        lines.append(f"📎 {expected} documenti allegati — seguono in sequenza")
    elif expected == 1:
        lines.append("📎 1 documento allegato")
    else:
        lines.append("📎 Nessun documento allegato")
    if not item.get("_attachment_fetch_ok", True):
        lines.append("⚠️ Uno o più allegati non sono stati acquisiti: il bot li ritenterà.")
    return "\n".join(lines)


def _preflight_attachments(item: dict) -> tuple[list[dict], str | None]:
    """Valida l'intero gruppo prima di inviare perfino la caption.

    Nessun gruppo parte se tutti i file attesi non sono presenti, integri e
    attribuiti allo stesso item. In particolare non si passa mai da una caption
    a un allegato mancante e poi, accidentalmente, all'atto successivo.
    """
    attachments = item.get("allegati", []) or []
    try:
        expected = int(item.get("_attachment_expected_count", len(attachments)))
    except (TypeError, ValueError):
        return [], "conteggio allegati non valido"
    if expected < 0:
        return [], "conteggio allegati negativo"
    if not item.get("_attachment_fetch_ok", True):
        return [], "acquisizione allegati non completa"
    if len(attachments) != expected:
        return [], f"allegati disponibili {len(attachments)}/{expected}"

    prepared = []
    positions = set()
    for index, attachment in enumerate(attachments, start=1):
        filename = Path(str(attachment.get("filename") or "")).name
        if not filename or filename in {".", ".."}:
            return [], f"nome allegato {index} non valido"
        try:
            position = int(attachment.get("position", index))
        except (TypeError, ValueError):
            return [], f"posizione allegato {index} non valida"
        if position < 1 or position > max(expected, 1) or position in positions:
            return [], f"sequenza allegato {index} non valida"
        positions.add(position)

        spool = attachment.get("_spool")
        content = attachment.get("content")
        if isinstance(spool, _AttachmentSpool):
            if not spool.verify():
                return [], f"file temporaneo allegato {position} assente o non integro"
            if attachment.get("size") not in (None, spool.size):
                return [], f"dimensione allegato {position} incoerente"
            if attachment.get("sha256") not in (None, spool.sha256):
                return [], f"impronta allegato {position} incoerente"
            prepared.append({**attachment, "filename": filename, "position": position})
            continue

        # Compatibilità controllata con item già costruiti in memoria. Il
        # downloader di produzione non crea più bytes e non recupera mai URL
        # Halley dopo la chiusura della sessione.
        if isinstance(content, (bytes, bytearray)) and 0 < len(content) <= MAX_ATTACHMENT_BYTES:
            digest = hashlib.sha256(content).hexdigest()
            if attachment.get("sha256") not in (None, digest):
                return [], f"impronta allegato {position} incoerente"
            prepared.append({**attachment, "filename": filename, "position": position})
            continue
        return [], f"contenuto allegato {position} non disponibile"

    return prepared, None


async def _download_and_send_docs(
    bot_or_update,
    chat_id: int | None,
    item: dict,
    is_reply: bool = False,
    report_incomplete: bool = True,
) -> bool:
    """
    Invia caption + allegati a un chat_id oppure come reply a un update.
    Restituisce True soltanto quando il messaggio e tutti gli allegati attesi
    sono stati consegnati. Un invio parziale resta quindi da ritentare.
    """
    allegati = item.get("allegati", []) or []
    try:
        expected = int(item.get("_attachment_expected_count", len(allegati)))
    except (TypeError, ValueError):
        expected = -1
    if is_reply:
        effective_chat = getattr(bot_or_update, "effective_chat", None)
        target_chat_id = getattr(effective_chat, "id", None)
        if target_chat_id is None:
            target_chat_id = getattr(getattr(bot_or_update, "message", None), "chat_id", 0)
    else:
        target_chat_id = int(chat_id or 0)

    async def send_text(text):
        if is_reply:
            await bot_or_update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await bot_or_update.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

    async def send_doc(content, filename, document_caption):
        if is_reply:
            await bot_or_update.message.reply_document(
                document=content, filename=filename,
                caption=document_caption, parse_mode=ParseMode.HTML,
            )
        else:
            await bot_or_update.send_document(
                chat_id=chat_id, document=content, filename=filename,
                caption=document_caption, parse_mode=ParseMode.HTML,
            )

    async with _chat_delivery_lock(int(target_chat_id or 0)):
        try:
            prepared, preflight_error = _preflight_attachments(item)
            all_documents_sent = preflight_error is None
            if preflight_error is not None:
                # Non inviare un sottoinsieme: al ciclo successivo il bot
                # riprova l'atto intero, evitando duplicati e documenti isolati.
                log.warning(
                    f"Allegati non completi per '{item.get('title', '')[:60]}': "
                    f"{preflight_error}; nessun documento inviato."
                )
                if report_incomplete:
                    title = escape_html(item.get("title") or "Atto Albo")
                    await send_text(
                        f"⚠️ Documenti non completi per <b>{title}</b>. "
                        "Nessun allegato è stato inviato; il bot ritenterà."
                    )
                return False

            caption = format_caption(item)
            await send_text(caption)

            for index, alleg in enumerate(prepared, start=1):
                await asyncio.sleep(0.3)
                publication = escape_html(item.get("num_pub") or item.get("title") or "Atto Albo")
                position = int(alleg.get("position", index))
                document_caption = f"📎 Allegato {position}/{expected or len(allegati)} · {publication}"
                sent = False
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        spool = alleg.get("_spool")
                        if isinstance(spool, _AttachmentSpool):
                            # Un nuovo handle a ogni retry evita offset EOF e
                            # permette di riusare lo spool per più destinatari.
                            with spool.open() as document:
                                await send_doc(document, alleg["filename"], document_caption)
                        else:
                            await send_doc(alleg["content"], alleg["filename"], document_caption)
                        sent = True
                        break
                    except Exception as error:
                        log.warning(
                            f"Invio {attempt}/{MAX_RETRIES} fallito per allegato "
                            f"'{alleg['filename']}': {_safe_error(error)}"
                        )
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(RETRY_DELAY)
                if not sent:
                    all_documents_sent = False

            if all_documents_sent:
                log.info(f"✓ Atto inviato integralmente: {item['title'][:60]}")
            else:
                log.warning(f"Invio parziale, da ritentare: {item['title'][:60]}")
            return all_documents_sent
        except Exception as e:
            log.error(f"Errore invio atto: {_safe_error(e)}")
            return False

async def send_item_to_chat(bot: Bot, chat_id: int, item: dict) -> bool:
    return await _download_and_send_docs(
        bot, chat_id, item, is_reply=False, report_incomplete=False
    )

async def reply_item(update: Update, item: dict) -> bool:
    try:
        return await _download_and_send_docs(update, None, item, is_reply=True)
    finally:
        cleanup_attachment_files(item)

async def notify(bot: Bot, item: dict, recipients: set | None = None) -> tuple[set, set]:
    """Invia un atto e restituisce (destinatari riusciti, destinatari falliti).

    La cronologia viene salvata in blocco dal ciclo chiamante: in questo modo
    una raffica di atti non genera un commit Git per ogni singolo destinatario.
    """
    successful = set()
    failed = set()
    targets = recipients if recipients is not None else get_all_recipients()
    try:
        for chat_id in targets:
            ok = await send_item_to_chat(bot, chat_id, item)
            if ok:
                successful.add(chat_id)
            else:
                failed.add(chat_id)
            await asyncio.sleep(1.0)  # evita flood verso Telegram
        return successful, failed
    finally:
        # Solo dopo tutti i destinatari: ciascuno riapre lo stesso file da zero.
        cleanup_attachment_files(item)

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
        log.info(f"✓ News inviata a un destinatario: {item['title'][:60]}")
        return True
    except Exception as e:
        log.error(f"Errore invio news: {_safe_error(e)}")
        return False

async def notify_news(bot: Bot, item: dict, recipients: set | None = None) -> tuple[set, set]:
    successful = set()
    failed = set()
    targets = recipients if recipients is not None else get_all_news_recipients()
    for chat_id in targets:
        if await send_news_to_chat(bot, chat_id, item):
            successful.add(chat_id)
        else:
            failed.add(chat_id)
        await asyncio.sleep(1.0)  # evita flood verso Telegram
    return successful, failed

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

    log.info(f"Iscrizione completa (albo: {len(subs_albo)}, news: {len(subs_news)})")

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

    log.info(f"Disiscrizione completa (albo: {len(subs_albo)}, news: {len(subs_news)})")
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
    log.info(f"Nuova iscrizione albo (totale: {len(subs)})")
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
    log.info(f"Disiscrizione albo (totale: {len(subs)})")
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
    log.info(f"Nuova iscrizione news (totale: {len(subs)})")
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
    log.info(f"Disiscrizione news (totale: {len(subs)})")
    await update.message.reply_text(
        "✅ Iscrizione News cancellata." + MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_atti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Recupero atti, dettagli e allegati...")

    async def update_progress(page, count):
        if page > 1:
            await msg.edit_text(f"⏳ Scaricando pagina {page}... ({count} atti finora)")

    # La lista e MC02/MC96-99 restano nella stessa sessione: num_riga non è
    # riutilizzabile dopo una nuova open_session.
    try:
        items = await fetch_albo_html(
            detail_selector=lambda current: current,
            on_progress=update_progress,
            raise_on_failure=True,
        )
    except IncompleteAlboSnapshot as error:
        log.warning(f"/atti: portale raggiunto ma risposta Albo incompleta: {_safe_error(error)}")
        await msg.edit_text(
            "⚠️ Il portale è raggiungibile, ma una pagina dell'Albo non è stata letta "
            "in modo affidabile. Riprova tra poco." + MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except (httpx.HTTPError, ConnectionError) as error:
        log.warning(f"/atti: errore di collegamento Halley: {_safe_error(error)}")
        await msg.edit_text(
            "❌ Problema di collegamento con il portale. Riprova tra poco." + MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception as error:
        log.error(f"/atti: errore inatteso: {_safe_error(error)}")
        await msg.edit_text(
            "❌ Errore durante la lettura dell'Albo. Il portale è raggiungibile, "
            "ma il comando non è stato completato." + MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if items is None:
        await msg.edit_text(
            "❌ Errore durante la lettura dell'Albo. Riprova tra poco." + MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if not items:
        await msg.edit_text("Nessun atto trovato al momento." + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)
        return

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
        badge    = {
            "active": "🟢 ",
            "recent": "🟡 ",
            "no_expiry": "🟠 ",
            "expired": "🔴 ",
        }.get(status, "🔴 ")
        title    = html_lib.escape(item["title"])
        albo_url = "https://www.comune.roccabascerana.av.it/EG0/EGHOMEPAGE.HBL"
        scaduto  = f' — <a href="{albo_url}">consulta l\'albo</a>' if status in ("expired", "recent") else ""
        date_str = f"\n    📅 Dal {item['date']}" if item.get("date") else ""
        if item.get("date_end"):
            date_str += f" al {item['date_end']}"
        elif status == "no_expiry":
            date_str += " · scadenza non indicata"
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
    allegati_ammessi = [i for i in visibili if item_attachment_delivery_eligible(i)]
    statuses = [item_status(i) for i in visibili]
    totale_msg = (
        f"📋 *{len(visibili)} atti* in albo\n"
        f"🟢 Attivi: *{statuses.count('active')}* · "
        f"🟡 Scaduti recenti: *{statuses.count('recent')}* · "
        f"🟠 Scadenza non indicata: *{statuses.count('no_expiry')}* · "
        f"🔴 Scaduti: *{statuses.count('expired')}*"
    )
    await update.message.reply_text(totale_msg, parse_mode=ParseMode.MARKDOWN)

    # Personalizzazione per utente: manda solo gli allegati mai visti da QUESTO utente.
    # Per gli atti senza scadenza l'invio automatico è limitato alle pubblicazioni recenti.
    chat_id      = update.effective_chat.id
    user_seen    = get_user_seen_hashes(chat_id)
    da_inviare   = [
        i for i in allegati_ammessi
        if (i.get("allegati") or not i.get("_attachment_fetch_ok", True))
        and item_id(i) not in user_seen
    ]
    gia_visti = [
        i for i in allegati_ammessi
        if (i.get("allegati") or int(i.get("_attachment_expected_count") or 0) > 0)
        and item_id(i) in user_seen
    ]

    sent_hashes = []
    failed_hashes = []
    for item in da_inviare:
        await asyncio.sleep(0.5)
        if await reply_item(update, item):
            sent_hashes.append(item_id(item))
        else:
            failed_hashes.append(item_id(item))

    # Gli atti già visti verranno scaricati di nuovo soltanto se l'utente
    # preme “Sì”: non trattenere nel frattempo file temporanei inutilizzati.
    cleanup_attachment_files(items)

    if sent_hashes:
        mark_user_seen(chat_id, sent_hashes)

    if gia_visti:
        ref = store_pending_resend(chat_id, [item_id(i) for i in gia_visti])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📎 Sì, rimandameli", callback_data=f"resend:{ref}"),
            InlineKeyboardButton("No grazie", callback_data=f"resend:no:{ref}"),
        ]])
        await update.message.reply_text(
            f"📨 Hai già ricevuto i documenti di *{len(gia_visti)}* atti attivi/recenti. "
            "Vuoi che te li rimandi?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
    else:
        ending = (
            f"⚠️ Fine provvisoria: i documenti di *{len(failed_hashes)}* atti non erano completi "
            "e verranno ritentati."
            if failed_hashes else "✅ Fine."
        )
        await update.message.reply_text(ending + MENU_TEXT, parse_mode=ParseMode.MARKDOWN)

async def cmd_atti_resend_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce la risposta al bottone 'Vuoi che te li rimandi?'."""
    query = update.callback_query
    await query.answer()
    data = query.data  # "resend:no:<ref>" oppure "resend:<ref>"
    chat_id = query.message.chat_id

    if data.startswith("resend:no"):
        parts = data.split(":", 2)
        cancelled = len(parts) == 3 and cancel_pending_resend(parts[2], chat_id)
        if cancelled:
            await query.edit_message_text(
                "👍 Ok, nessun documento rinviato." + MENU_TEXT,
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await query.edit_message_text(
                "⏰ Richiesta non valida o scaduta. Usa di nuovo /atti.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    ref = data.split(":", 1)[1]
    hashes = pop_pending_resend(ref, chat_id)
    if hashes is None:
        await query.edit_message_text(
            "⏰ Richiesta scaduta (il bot potrebbe essere stato riavviato). "
            "Usa di nuovo /atti per riprovare." + MENU_TEXT,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await query.edit_message_text("⏳ Recupero atti e allegati nella stessa sessione...")

    # Selezione e arricchimento avvengono prima di chiudere la sessione che ha
    # prodotto num_riga; non viene più aperta una seconda sessione.
    items = await fetch_albo_html(
        detail_selector=lambda current: [i for i in current if item_id(i) in hashes]
    )
    if items is None:
        await query.edit_message_text("❌ Impossibile recuperare gli atti. Riprova tra poco.")
        return

    targets = [i for i in items if item_id(i) in hashes]
    if not targets:
        await query.edit_message_text("⚠️ Atti non trovati (potrebbero essere scaduti nel frattempo).")
        return

    await query.edit_message_text(f"⏳ Invio documenti di {len(targets)} atti...")

    failed_items = []
    sent_items = []
    try:
        for item in targets:
            if item.get("allegati"):
                if await send_item_to_chat(context.bot, chat_id, item):
                    sent_items.append(item)
                else:
                    failed_items.append(item)
                await asyncio.sleep(0.5)
            elif not item.get("_attachment_fetch_ok", True):
                failed_items.append(item)
    finally:
        cleanup_attachment_files(targets)

    if failed_items:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ Reinvio completato solo in parte: {len(sent_items)} atti completi, "
                f"{len(failed_items)} da riprovare. Usa /atti tra qualche minuto."
                + MENU_TEXT
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
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
        f"{ok_albo} Albo: {result_albo.get('new', 0)} nuovi · {result_albo.get('updated', 0)} aggiornati · archivio {result_albo.get('total', 0)}\n"
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
_ALBO_CHECK_LOCK = asyncio.Lock()
_NEWS_CHECK_LOCK = asyncio.Lock()


async def run_check(bot: Bot) -> dict:
    """Serializza i controlli Albo per evitare doppioni tra loop e /controlla."""
    async with _ALBO_CHECK_LOCK:
        return await _run_check_albo(bot)


async def _run_check_albo(bot: Bot) -> dict:
    """Controlla nuovi atti, retry e revisioni di atti già notificati."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    db = load_db()
    seen = {h for h, rec in db.items() if rec.get("notified", True)}
    enrichment_items: list[dict] = []

    def select_enrichment_items(current_items: list) -> list:
        new_now = [i for i in current_items if item_id(i) not in seen]
        pending_now = [
            i for i in current_items
            if item_id(i) in seen and db.get(item_id(i), {}).get("delivery_pending", False)
        ]

        revision_due = []
        for item in current_items:
            h = item_id(item)
            rec = db.get(h, {})
            if h not in seen or rec.get("delivery_pending", False):
                continue
            if not _revision_trackable(item):
                continue
            if _revision_check_due(rec, now):
                revision_due.append(item)

        # Prima chi non è mai stato baseline-ato con fingerprint, poi chi non
        # viene ricontrollato da più tempo. Il limite evita di martellare Halley.
        revision_due.sort(
            key=lambda item: (
                0 if not db.get(item_id(item), {}).get("revision_fingerprint") else 1,
                str(db.get(item_id(item), {}).get("last_revision_check_at") or ""),
            )
        )
        revision_due = revision_due[:REVISION_RECHECK_MAX_ITEMS]

        selected = merge_item_groups(new_now, pending_now, revision_due)
        enrichment_items[:] = selected
        if selected:
            log.info(
                f"Dettagli Albo da recuperare: {len(selected)} "
                f"({len(new_now)} nuovi, {len(pending_now)} consegne da ritentare, "
                f"{len(revision_due)} controlli revisione)."
            )
        return selected

    items = await fetch_albo_html(
        detail_selector=select_enrichment_items,
        force_detail=True,
        push_cache=False,
    )
    touch_last_check()

    if items is None:
        log.error("Fetch albo fallito — skip ciclo.")
        return {"ok": False, "new": 0, "updated": 0, "total": len(seen), "failed": 0}

    # enrich_with_attachments può aver aggiornato la cache date: ripartiamo dal
    # DB più recente prima di confrontare le revisioni.
    db = load_db()
    db_changed = False
    new_items = [i for i in items if item_id(i) not in seen]
    pending_initial = []
    revision_items = []

    for item in enrichment_items:
        h = item_id(item)
        rec = db.setdefault(h, {"notified": False})
        snapshot = item_revision_snapshot(item)
        fingerprint = item_revision_fingerprint(snapshot)

        # Nuovi atti: saranno gestiti sotto come prima pubblicazione.
        if h not in seen:
            continue

        # Un retry precedente può riguardare la prima consegna oppure una
        # revisione. Le revisioni usano una delivery-key diversa dal solo hash.
        if rec.get("delivery_pending", False):
            if rec.get("pending_kind") == "revision":
                item["_notification_kind"] = "revision"
                item["_revision_hash"] = fingerprint or str(rec.get("pending_version") or "")
                item["_revision_changes"] = list(rec.get("pending_revision_changes") or ["contenuto dell'atto"])
                revision_items.append(item)
            else:
                item["_notification_kind"] = "new"
                pending_initial.append(item)
            continue

        # Se il recupero dettaglio/allegati non è completo non consideriamo il
        # controllo concluso: il medesimo atto verrà ritentato al prossimo giro.
        if not snapshot or not fingerprint:
            continue

        previous_fp = str(rec.get("revision_fingerprint") or "")
        previous_snapshot = rec.get("revision_snapshot") if isinstance(rec.get("revision_snapshot"), dict) else None

        if not previous_fp:
            # Migrazione silenziosa: gli atti già noti prima di questa funzione
            # ricevono una baseline, non una raffica di finte "revisioni".
            rec["revision_fingerprint"] = fingerprint
            rec["revision_snapshot"] = snapshot
            rec["revision"] = max(1, int(rec.get("revision") or 1))
            rec["last_revision_check_at"] = now_iso
            db_changed = True
            continue

        if fingerprint != previous_fp:
            changes = revision_changes(previous_snapshot, snapshot) or ["contenuto dell'atto"]
            item["_notification_kind"] = "revision"
            item["_revision_hash"] = fingerprint
            item["_revision_changes"] = changes
            rec["pending_kind"] = "revision"
            rec["pending_version"] = fingerprint
            rec["pending_revision_changes"] = changes
            rec["delivery_pending"] = True
            revision_items.append(item)
            db_changed = True
        else:
            rec["revision_snapshot"] = snapshot
            rec["last_revision_check_at"] = now_iso
            db_changed = True

    work_items = merge_item_groups(new_items, pending_initial, revision_items)

    recipients = get_all_recipients()
    user_seen_data = load_user_seen()
    user_seen_changed = False
    notified_count = 0
    updated_count = 0
    failed_deliveries = 0

    for item in work_items:
        h = item_id(item)
        rec = db.setdefault(h, {"notified": False})
        kind = item.get("_notification_kind") or "new"
        snapshot = item_revision_snapshot(item)
        fingerprint = item.get("_revision_hash") or item_revision_fingerprint(snapshot)
        was_notified = bool(rec.get("notified", False))
        previous_fp = str(rec.get("revision_fingerprint") or "")

        delivery_key = h
        if kind == "revision" and fingerprint:
            delivery_key = revision_delivery_key(h, fingerprint)

        already_delivered = {
            chat_id for chat_id in recipients
            if delivery_key in set(user_seen_data.get(str(chat_id), []))
        }
        targets = recipients - already_delivered

        successful, failed = await notify(bot, item, targets)

        for chat_id in successful:
            key = str(chat_id)
            delivered = set(user_seen_data.get(key, []))
            delivered.add(delivery_key)
            # Una revisione presuppone che l'atto base sia ormai noto a quella chat.
            if kind == "revision":
                delivered.add(h)
            user_seen_data[key] = sorted(delivered)
            user_seen_changed = True

        if snapshot and fingerprint:
            rec["last_revision_check_at"] = now_iso

        if kind == "revision":
            rec["notified"] = True
            if successful or not targets:
                if fingerprint and fingerprint != previous_fp:
                    rec["revision"] = max(1, int(rec.get("revision") or 1)) + 1
                if fingerprint:
                    rec["revision_fingerprint"] = fingerprint
                if snapshot:
                    rec["revision_snapshot"] = snapshot
                updated_count += 1 if fingerprint != previous_fp else 0

            rec["delivery_pending"] = bool(failed)
            if failed:
                rec["pending_kind"] = "revision"
                rec["pending_version"] = fingerprint
                rec["pending_revision_changes"] = list(item.get("_revision_changes") or ["contenuto dell'atto"])
            else:
                rec.pop("pending_kind", None)
                rec.pop("pending_version", None)
                rec.pop("pending_revision_changes", None)
        else:
            if successful or not targets:
                rec["notified"] = True
            rec["delivery_pending"] = bool(failed) if rec.get("notified") else False
            if rec.get("delivery_pending"):
                rec["pending_kind"] = "new"
                rec["pending_version"] = fingerprint
            else:
                rec.pop("pending_kind", None)
                rec.pop("pending_version", None)
                rec.pop("pending_revision_changes", None)

            # La prima versione diventa subito baseline per confronti futuri.
            if snapshot and fingerprint:
                rec["revision_fingerprint"] = fingerprint
                rec["revision_snapshot"] = snapshot
                rec["revision"] = max(1, int(rec.get("revision") or 1))
            if not was_notified and rec.get("notified"):
                notified_count += 1

        db_changed = True
        failed_deliveries += len(failed)
        if failed:
            label = "revisione" if kind == "revision" else "atto"
            log.warning(
                f"{label.capitalize()} con {len(failed)} consegne in sospeso: "
                f"{item.get('title', '?')[:80]}"
            )
        await asyncio.sleep(1.5)

    paths_to_push = []
    if user_seen_changed:
        latest_user_seen = load_user_seen()
        for key, hashes in user_seen_data.items():
            merged = set(latest_user_seen.get(key, []))
            merged.update(hashes)
            latest_user_seen[key] = sorted(merged)
        save_user_seen(latest_user_seen, push=False)
        paths_to_push.append(str(USER_SEEN_PATH))

    if db_changed:
        latest_db = load_db()
        # I record in memoria contengono stato consegne/revisioni; la cache date
        # può invece essere stata aggiornata durante l'arricchimento. La uniamo.
        cache_fields = ("date", "date_end", "expired")
        for item_hash, record in db.items():
            latest_record = latest_db.get(item_hash, {})
            merged_record = {**latest_record, **record}
            for field in cache_fields:
                if field in latest_record:
                    merged_record[field] = latest_record[field]
            latest_db[item_hash] = merged_record
        db = latest_db
        save_db(db, push=False)
        paths_to_push.append(str(DB_PATH))

    git_ok = True
    if paths_to_push:
        git_ok = git_commit_and_push(
            list(dict.fromkeys(paths_to_push)),
            message="aggiornamento stato/revisioni albo [skip ci]",
        )

    current_seen = {h for h, rec in db.items() if rec.get("notified", True)}
    if failed_deliveries:
        log.warning(f"⚠️ {failed_deliveries} consegne Albo verranno ritentate.")
    if not git_ok:
        log.error("Stato Albo aggiornato localmente ma NON persistito su Git.")
    if updated_count:
        log.info(f"♻️ {updated_count} revisioni di atti notificate.")

    cleanup_attachment_files(items)
    return {
        "ok": failed_deliveries == 0 and git_ok,
        "new": notified_count,
        "updated": updated_count,
        "total": len(current_seen),
        "failed": failed_deliveries,
    }


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
    """Serializza i controlli News per evitare doppioni tra loop e /controlla."""
    async with _NEWS_CHECK_LOCK:
        return await _run_check_news(bot)


async def _run_check_news(bot: Bot) -> dict:
    """
    Esegue un singolo ciclo di controllo News.
    Stesso schema di run_check, ma senza allegati/sessione MC02.

    Usa stop_at_known per fermare la paginazione alla prima pagina
    interamente già vista: le news più vecchie di quelle in cache non
    cambiano mai, quindi non ha senso ri-scaricare tutto l'archivio
    (25+ pagine) ad ogni ciclo di 6h.

    Le news "nuove" più vecchie di NEWS_NOTIFY_MAX_AGE_DAYS (es. dopo
    un downtime prolungato del bot) vengono segnate come viste ma SENZA
    notifica push — restano comunque visibili con /news.
    """
    news_db    = load_news_db()
    seen_news  = set(news_db.keys())
    news_items = await fetch_news_html(stop_at_known=seen_news)

    if news_items is None:
        log.error("Fetch news fallito — skip ciclo.")
        return {"ok": False, "new": 0, "total": len(seen_news)}

    new_news = [i for i in news_items if news_id(i) not in seen_news]
    pending_news = []
    for nid, rec in news_db.items():
        if rec.get("delivery_pending", False):
            pending_news.append({"id": nid, **rec})

    if new_news or pending_news:
        to_notify = [i for i in new_news if news_is_recent(i)]
        too_old   = [i for i in new_news if not news_is_recent(i)]

        if too_old:
            log.info(f"🕓 {len(too_old)} news più vecchie di {NEWS_NOTIFY_MAX_AGE_DAYS}gg — "
                      "segnate come viste senza notifica push.")

        log.info(
            f"🆕 {len(to_notify)} nuove news e {len(pending_news)} con consegne da ritentare..."
        )
        recipients = get_all_news_recipients()
        delivered_data = load_user_seen_news()
        delivered_changed = False
        db_changed = False
        notified_count = 0
        failed_deliveries = 0

        for item in to_notify + pending_news:
            nid = news_id(item)
            is_new = nid not in news_db
            already_delivered = {
                chat_id for chat_id in recipients
                if nid in set(delivered_data.get(str(chat_id), []))
            }
            targets = recipients - already_delivered
            successful, failed = await notify_news(bot, item, targets)

            for chat_id in successful:
                key = str(chat_id)
                delivered = set(delivered_data.get(key, []))
                delivered.add(nid)
                delivered_data[key] = sorted(delivered)
                delivered_changed = True

            previous_rec = news_db.get(nid, {})
            was_notified = bool(previous_rec.get("notified", not is_new))
            now_notified = was_notified or bool(successful) or not targets

            # Persistiamo ANCHE una news nuova fallita per tutti i destinatari:
            # così, dopo un riavvio, resta delivery_pending e non può essere
            # inghiottita dalla baseline iniziale.
            news_db[nid] = {
                "title": item.get("title", ""),
                "category": item.get("category", ""),
                "date": item.get("date", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
                "notified": now_notified,
                "delivery_pending": bool(failed),
            }
            db_changed = True
            if not was_notified and now_notified:
                notified_count += 1

            failed_deliveries += len(failed)
            if failed:
                log.warning(
                    f"News con {len(failed)} consegne in sospeso: "
                    f"{item.get('title', '?')[:80]}"
                )
            await asyncio.sleep(0.5)

        # Le news troppo vecchie vengono volutamente archiviate senza push.
        for item in too_old:
            nid = news_id(item)
            news_db[nid] = {
                "title": item.get("title", ""),
                "category": item.get("category", ""),
                "date": item.get("date", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
                "notified": True,
                "delivery_pending": False,
            }
            db_changed = True

        paths_to_push = []
        if delivered_changed:
            save_user_seen_news(delivered_data, push=False)
            paths_to_push.append(str(USER_SEEN_NEWS_PATH))
        if db_changed:
            save_news_db(news_db, push=False)
            paths_to_push.append(str(NEWS_DB_PATH))
        git_ok = True
        if paths_to_push:
            git_ok = git_commit_and_push(
                paths_to_push,
                message="aggiornamento consegne news [skip ci]",
            )
        if not git_ok:
            log.error("Stato News aggiornato localmente ma NON persistito su Git.")

        log.info(
            f"✅ {notified_count + len(too_old)} nuove news salvate "
            f"({notified_count} notificate, {len(too_old)} silenziose, "
            f"{failed_deliveries} consegne in sospeso)."
        )
        return {
            "ok": failed_deliveries == 0 and git_ok,
            "new": notified_count,
            "total": len(news_db),
            "failed": failed_deliveries,
        }
    else:
        log.info(f"Nessuna nuova news. (archivio: {len(seen_news)}, online ora: {len(news_items)})")
        return {"ok": True, "new": 0, "total": len(seen_news)}

# ---------------------------------------------------------------------------
# Loop principale — check immediato all'avvio, poi ogni INTERVAL_MINUTES
# ---------------------------------------------------------------------------
async def _ensure_albo_baseline() -> bool:
    """Crea la baseline solo se il DB non esiste davvero.

    Un errore di rete durante il bootstrap non deve trasformarsi in una raffica
    di notifiche storiche: in quel caso il check Albo resta sospeso e si ritenta.
    """
    if DB_PATH.exists():
        return True

    log.info("Prima esecuzione: costruisco baseline albo senza notifiche...")
    items = await fetch_albo_html()
    if items is None:
        log.error("Baseline Albo non creata: fetch fallito. Nessun atto storico verrà notificato.")
        return False

    seen = {item_id(i) for i in items}
    save_seen(seen)
    touch_last_check()
    log.info(f"Baseline albo: {len(seen)} atti salvati.")
    return True


async def _ensure_news_baseline() -> bool:
    """Come per l'Albo: baseline solo su stato realmente assente."""
    if NEWS_DB_PATH.exists():
        return True

    log.info("Prima esecuzione: costruisco baseline news senza notifiche...")
    news_items = await fetch_news_html()
    if news_items is None:
        log.error("Baseline News non creata: fetch fallito. Il check News resta sospeso.")
        return False

    seen_news = {news_id(i) for i in news_items}
    items_by_id = {news_id(i): i for i in news_items}
    save_seen_news(seen_news, items_by_id)
    log.info(f"Baseline news: {len(seen_news)} news salvate.")
    return True


async def polling_loop(app: Application):
    bot = app.bot

    async def execute_cycle(label: str):
        albo_ready = await _ensure_albo_baseline()
        news_ready = await _ensure_news_baseline()

        if albo_ready:
            result = await run_check(bot)
            seen = load_seen()
        else:
            result = {"ok": False, "new": 0, "updated": 0, "total": 0, "failed": 0}
            seen = set()

        if news_ready:
            result_news = await run_check_news(bot)
            seen_news = load_seen_news()
        else:
            result_news = {"ok": False, "new": 0, "total": 0, "failed": 0}
            seen_news = set()

        await send_heartbeat(
            bot,
            seen,
            seen_news,
            albo_ok=bool(result.get("ok")),
            news_ok=bool(result_news.get("ok")),
        )
        log.info(
            f"{label}: albo nuovi={result['new']}, aggiornati={result.get('updated', 0)}, totale={result['total']} · "
            f"news nuove={result_news['new']}, totale={result_news['total']}"
        )

    log.info("Check immediato all'avvio...")
    try:
        await execute_cycle("Check avvio")
    except Exception as e:
        log.error(f"Errore check avvio: {_safe_error(e)}")

    log.info(f"Loop polling ogni {CONFIG['INTERVAL_MINUTES']} min.")
    while True:
        await asyncio.sleep(CONFIG["INTERVAL_MINUTES"] * 60)
        try:
            await execute_cycle("Check periodico")
        except Exception as e:
            log.error(f"Errore loop: {_safe_error(e)}")

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
