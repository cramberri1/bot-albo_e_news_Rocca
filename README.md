# 🏛 Albo Pretorio & News Bot – Comune di Roccabascerana

Bot Telegram che monitora **l'albo pretorio** e **le news**
pubblicate sul sito del Comune di Roccabascerana (portale Halley EG),
inviando una notifica ogni volta che viene pubblicato un nuovo atto
(con documento allegato) o una nuova news.

Il bot gira automaticamente su **GitHub Actions**: non serve un server,
un Raspberry Pi o un hosting dedicato — GitHub esegue il workflow secondo
lo scheduling configurato, gratuitamente, nei limiti del piano free.

---

## Come funziona

**Albo Pretorio:**
- Ogni atto viene letto direttamente dal portale Halley EG tramite le
  chiamate `MC01` (elenco), `PMC02` (paginazione) e `MC02` (dettaglio +
  allegati), riproducendo le richieste che fa il browser.
- Il bot distingue tre stati per ogni atto:
  - 🟢 **Attivo** — pubblicazione in corso
  - 🟡 **Scaduto di recente** — scaduto da meno di 30 giorni
  - 🔴 **Scaduto** — scaduto da più tempo (mostra link all'albo, non gli allegati)
  - Gli atti pubblicati prima del 1° gennaio 2024 vengono considerati
    archivio storico e non vengono mostrati.
- Le date e gli stati vengono salvati in cache (`data/seen_items.json`) per
  evitare di richiamare il portale per gli stessi atti ogni volta. Lo stesso
  file distingue però anche il flag `notified`: un atto può essere in cache
  senza risultare già notificato, così il comando `/atti` non può "bruciare"
  future notifiche automatiche.
- Ogni utente ha una propria cronologia di atti già ricevuti
  (`data/user_seen.json`): se richiedi `/atti` più volte, il bot non ti
  rispedisce gli stessi allegati senza chiedere conferma.

**News:**
- Le news vengono lette dalla pagina pubblica `EGSCHTST6.HBL` del
  sito del Comune — niente sessione da aprire (a differenza dell'albo),
  paginazione semplice via GET (`MESSA=PAGSUCC=N`).
- Ogni news ha già nella lista titolo, categoria (Avviso/Comunicati/
  Notizia), data e descrizione breve: non serve un secondo fetch sul
  dettaglio.
- La cache (`data/seen_news.json`) usa come ID il numero progressivo
  nell'URL della news (es. `novita_166.html` → `166`).
- **Early-stop sulla paginazione**: ad ogni controllo periodico, il bot
  si ferma alla prima pagina di news già completamente note in cache
  (le news più vecchie di quelle già viste non cambiano mai), invece
  di scaricare sempre tutte le pagine dell'archivio.
- **Filtro età per le notifiche push**: le news "nuove" per il bot ma
  pubblicate da più di `NEWS_NOTIFY_MAX_AGE_DAYS` (default 60) giorni
  non generano una notifica push (es. dopo un downtime prolungato del
  bot) — vengono comunque segnate come viste e restano visibili con
  `/news`.

---

## Comandi disponibili

| Comando | Descrizione |
|---|---|
| `/start` | Messaggio di benvenuto, mostra a quali notifiche sei iscritto |
| `/abbonati` | Iscriviti a **tutte** le notifiche (albo + news) |
| `/disabbonati` | Cancella **tutte** le iscrizioni |
| `/abbonati_albo` | Iscriviti solo alle notifiche dell'Albo Pretorio |
| `/disabbonati_albo` | Cancella solo l'iscrizione all'Albo Pretorio |
| `/abbonati_news` | Iscriviti solo alle notifiche delle News |
| `/disabbonati_news` | Cancella solo l'iscrizione alle News |
| `/atti` | Mostra l'elenco completo degli atti in albo, con stato e date. Per gli atti attivi/recenti invia anche i documenti allegati (chiede conferma se già ricevuti) |
| `/news` | Mostra le ultime 10 news pubblicate sul sito del Comune |
| `/controlla` | Forza un controllo immediato di nuovi atti e nuove news (**solo amministratori**) |
| `/status` | Statistiche del bot (solo amministratori) |

Le due sottoscrizioni (albo / news) sono indipendenti: puoi iscriverti
a una sola, a entrambe, o a nessuna. Gli amministratori (`CHAT_IDS`)
ricevono sempre entrambe le notifiche indipendentemente dall'iscrizione.

---

## Esecuzione su GitHub Actions

Il workflow è definito in `.github/workflows/albo_check.yml`:

- **Scheduling GitHub Actions**: ogni 6 ore (00:00, 06:00, 12:00, 18:00 UTC)
- **Durata massima per run**: 360 minuti; il processo Python viene chiuso
  intenzionalmente dopo **357 minuti**. Il timeout usa `SIGINT` per consentire
  una chiusura più ordinata e lascia un piccolo margine allo step finale
- **Polling interno**: dentro ogni run il bot controlla Albo Pretorio e News
  ogni `INTERVAL_MINUTES` minuti (nel workflow: 15), quindi non aspetta 6 ore
  tra un controllo effettivo e l'altro
- **Persistenza dati**: il bot stesso esegue `git commit` + `git push`
  dei file di stato quando aggiorna dati importanti (notifiche, iscritti,
  cronologia utenti, cache), non solo a fine job, così lo stato non resta
  solo nel filesystem temporaneo del runner
- **Crash visibili**: il workflow non usa più `|| true` sull'esecuzione del
  bot; il timeout programmato è considerato normale, ma un crash reale fa
  fallire il job
- **Anti-sovrapposizione tra run**: il workflow usa `concurrency` con
  `cancel-in-progress: false` — se un nuovo trigger (schedulato o
  manuale via `workflow_dispatch`) arriva mentre un run precedente è
  ancora attivo, resta in coda e parte automaticamente non appena il
  primo termina, invece di girare in parallelo (che causerebbe
  conflitti di scrittura sui file di stato)
- **`permissions: contents: write`**: dichiarato esplicitamente nel
  workflow, necessario perché il bot possa fare `git push` — senza,
  alcuni repository (a seconda delle impostazioni di default) negano
  il permesso di scrittura al token automatico

### Secrets richiesti nel repository

| Secret | Descrizione |
|---|---|
| `BOT_TOKEN` | Il token del bot, ottenuto da [@BotFather](https://t.me/BotFather) |
| `CHAT_IDS` | ID Telegram degli amministratori, separati da virgola |
| `STATE_ENCRYPTION_KEY` | Chiave Fernet per cifrare i file con chat_id; generarla con `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

(`GITHUB_TOKEN` è fornito automaticamente da GitHub Actions, non va creato)

### Avvio manuale

Dalla tab **Actions** del repository → workflow **Albo Pretorio Check**
→ **Run workflow**.

---

## File generati e committati automaticamente

Tutti i file di stato vivono in `data/`, per tenere la root del repository
pulita (solo codice). Vengono creati automaticamente al primo avvio se
assenti.

| File | Descrizione |
|---|---|
| `data/seen_items.json` | Stato atti albo: hash, flag `notified`, date pubblicazione/scadenza, stato/cache tecnica |
| `data/subscribers.json` 🔒 | Elenco chat_id iscritti alle notifiche dell'Albo Pretorio |
| `data/user_seen.json` 🔒 | Cronologia per utente degli atti già inviati |
| `data/last_check.txt` | Timestamp dell'ultimo controllo; aggiornato localmente a ogni check e committato al massimo una volta al giorno |
| `data/seen_news.json` | Cache news: id, titolo, categoria, data, url |
| `data/subscribers_news.json` 🔒 | Elenco chat_id iscritti alle notifiche delle News |

🔒 = contiene chat_id (dato personale) ed è **cifrato** con `Fernet`
prima di ogni commit — vedi sezione [Cifratura dei dati personali](#cifratura-dei-dati-personali).

⚠️ Questi file **non vanno inseriti in `.gitignore`** — il bot deve
poterli leggere e scrivere ad ogni esecuzione per mantenere la cache e
la cronologia tra un run e l'altro.

---

## Cifratura dei dati personali

Il repository è pubblico (necessario per i minuti GitHub Actions gratuiti
illimitati), quindi `subscribers.json`, `subscribers_news.json` e
`user_seen.json` — gli unici file che contengono chat_id Telegram —
vengono cifrati con una chiave simmetrica (`Fernet`) prima di ogni commit.

La chiave va impostata come secret del repository (`STATE_ENCRYPTION_KEY`).
Per generarla:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Senza questa chiave impostata, il bot funziona comunque ma salva questi
tre file **in chiaro** (con un avviso nei log) — da evitare su un repository
pubblico.

---

## Struttura del progetto

```
.
├── .github/workflows/albo_check.yml   # Workflow GitHub Actions
├── bot.py                             # Logica del bot (albo + news)
├── requirements.txt                   # Dipendenze Python
└── data/                              # Stato generato automaticamente
    ├── seen_items.json
    ├── subscribers.json               # 🔒 cifrato
    ├── user_seen.json                 # 🔒 cifrato
    ├── last_check.txt
    ├── seen_news.json
    └── subscribers_news.json          # 🔒 cifrato
```

---

## Sviluppo locale (opzionale)

Per testare il bot in locale invece che su GitHub Actions:

```bash
pip install -r requirements.txt

export BOT_TOKEN="il_tuo_token"
export CHAT_IDS="il_tuo_chat_id"

python bot.py
```

In locale il bot resta in esecuzione continua (polling Telegram +
controllo periodico ogni `INTERVAL_MINUTES`, default 180) finché non
viene interrotto manualmente.

---

## Soglie configurabili

Alcuni comportamenti sono regolati da costanti in testa a `bot.py`,
modificabili direttamente nel codice (non sono variabili d'ambiente):

| Costante | Default | Effetto |
|---|---|---|
| `RECENT_EXPIRED_DAYS` | 30 | Atti scaduti da meno di N giorni: mostrati con allegati |
| `ARCHIVE_CUTOFF_DATE` | 01/01/2024 | Atti pubblicati prima di questa data: nascosti come archivio storico |
| `NEWS_LIST_LIMIT` | 10 | Numero di news mostrate con `/news` |
| `NEWS_NOTIFY_MAX_AGE_DAYS` | 60 | News "nuove" per il bot ma pubblicate da più di N giorni: niente notifica push (segnate come viste, restano visibili con `/news`) |

---

## Adattare il bot a un altro portale Halley EG

Il bot è scritto specificamente per la struttura del portale
`comune.roccabascerana.av.it` (sistema Halley EG). Per puntarlo a un
altro comune che usa lo stesso sistema, andrebbero adattati come minimo:

- `ALBO_URL` / `NEWS_URL` e il codice ente `en=` nelle richieste
- Eventuali differenze nei selettori CSS delle card (`cmp-card`,
  `calendar-date-day`, `card-wrapper`, `category-top`, ecc.), che
  possono variare leggermente tra installazioni diverse dello stesso CMS
- Per le News: verificare che la paginazione sia comunque una GET con
  querystring (`?en=...&MESSA=PAGSUCC=N`) — non garantito identico su
  ogni installazione Halley, va controllato via tab Network del browser

Non è un'operazione plug-and-play: ogni installazione Halley EG può
avere personalizzazioni minori che richiedono verifica manuale.


## Note operative della revisione post-prima esecuzione

- La baseline reale già generata è conservata: **105 atti** e **141 news**.
- Le 105 voci esistenti di `seen_items.json` sono migrate con `notified: true`,
  quindi l'aggiornamento non provoca reinvii dello storico.
- `last_check.txt` non genera più un commit ogni 15 minuti: viene persistito al
  massimo una volta al giorno, riducendo drasticamente il rumore nella history.
- La data finale di pubblicazione è ora inclusiva: un atto con scadenza oggi
  resta attivo fino alla fine della giornata.
