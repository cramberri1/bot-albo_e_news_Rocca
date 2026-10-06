# 🏛 Albo Pretorio & News Bot – Comune di Roccabascerana

Bot Telegram che monitora **l'albo pretorio** e **le news**
pubblicate sul sito del Comune di Roccabascerana (portale Halley EG),
inviando una notifica ogni volta che viene pubblicato un nuovo atto
(con documento allegato) o una nuova news.

Il bot gira automaticamente su **GitHub Actions**: non serve un server,
un Raspberry Pi o un hosting dedicato — GitHub esegue il workflow secondo
lo scheduling configurato, gratuitamente, nei limiti del piano free.

> Aggiornamento 24/09/2026: identità v2, anti-raffica, deduplicazione Telegram, ricerca e nuovi orari.
> Vedi [revisione e limiti operativi](docs/HARDENING.md) per configurazione e verifiche.
> Aggiornamento 01/10/2026: [pannello amministratore `/status`](docs/ADMIN_STATUS.md)
> e [revisione concettuale del progetto](docs/PROJECT_REVIEW.md).
> Aggiornamento 04/10/2026: [revisione architetturale e incidente scheduling](docs/ARCHITECTURE_REVIEW.md).
> Aggiornamento 05/10/2026: polling automatico **07:00–20:00 Europe/Rome**;
> comandi Telegram indipendenti. La revisione del 04/10 descrive la precedente fascia 07:00–23:00.
> Corretto il riconoscimento degli RTF; ampliati i formati e i controlli di integrità
> degli allegati. Vedi [gestione e collaudo degli allegati](docs/ATTACHMENT_FORMATS.md).
> Aggiornamento 06/10/2026: [recupero limitato dopo un runner perso](docs/WORKFLOW_RECOVERY.md),
> checkpoint per destinatario e revisioni ripetute, bootstrap e shutdown verificati.

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

Gli allegati vengono scaricati e inviati come file originali, senza convertirli
in PDF: RTF, PDF, P7M/P7S, Word, Excel, PowerPoint, OpenDocument, ZIP/7Z,
immagini, CSV/TSV, TXT, XML, JSON e altri formati riconoscibili. I documenti
HTML richiedono un nome esplicito e una risposta di download; una pagina di
errore del portale non viene inoltrata come allegato. Restano i limiti di
dimensione e spazio temporaneo. Più di dieci documenti vengono inviati in
sequenza, ciascuno con numero e riferimento all'atto.

Per i file **P7M** la didascalia ricorda che serve un'app di verifica della
firma digitale per aprire il documento contenuto e verificarne la firma,
con collegamento ai [software indicati da AgID](https://www.agid.gov.it/en/node/1534).
Il bot conserva la busta originale; non dichiara verificata la firma e non
estrae automaticamente il documento firmato.

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
  invece di scaricare sempre tutte le pagine dell'archivio. Questa ottimizzazione
  presuppone un ordinamento coerente: non monitora le modifiche alle news vecchie.
  Le consegne confermate vengono salvate su Git dopo ogni news, prima della successiva.
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
| `/cerca <testo>` | Cerca negli atti conservati; 5 risultati per pagina, con recupero allegati su richiesta per il singolo atto |
| `/cerca_news <testo>` | Cerca nelle news conservate; 5 risultati per pagina |
| `/controlla` | Forza un controllo immediato di nuovi atti e nuove news, con eventuali notifiche (**solo amministratori, in privato**) |
| `/status` | Pannello con iscritti, destinatari, archivi, code, esiti e tempi dei controlli (**solo amministratori, in privato**) |

Le due sottoscrizioni (albo / news) sono indipendenti: puoi iscriverti
a una sola, a entrambe, o a nessuna. Gli amministratori (`CHAT_IDS`)
ricevono sempre entrambe le notifiche indipendentemente dall'iscrizione.

**Per l'amministratore:** apri la chat privata del bot e scrivi `/status`.
Il tuo ID utente Telegram deve essere tra quelli del secret `CHAT_IDS`.
Il riepilogo distingue le chat iscritte dai destinatari effettivi, che includono
anche gli amministratori. Mostra l'ultimo tentativo e l'ultimo successo
separatamente per Albo e News, le consegne pendenti e lo stato delle protezioni.
Non avvia controlli sul portale né cambia iscrizioni, archivi o consegne.
I dettagli sono [nella guida al pannello](docs/ADMIN_STATUS.md).

Nei risultati di `/cerca`, ogni pulsante **📎 Scarica allegati · N** corrisponde
al risultato numerato N. Il bot ritrova l'atto in una nuova sessione Halley,
verifica l'identità e scarica tutti i documenti prima di iniziare l'invio.
Una richiesta ambigua, incompleta o scaduta non invia documenti. I pulsanti
sono monouso, legati alla chat, validi 30 minuti e non sopravvivono al riavvio:
in questi casi basta ripetere `/cerca`. Cambiare pagina disattiva i pulsanti
allegati della pagina precedente. `/cerca_news` non scarica allegati.
Il recupero manuale non cambia notifiche, revisioni o cronologia `/atti`.
Vedi [dettagli e collaudo](docs/SEARCH_ATTACHMENTS.md).

---

## Esecuzione su GitHub Actions

Il workflow è definito in `.github/workflows/albo_check.yml`:

- **Richieste di avvio**: ogni ora al minuto 17, Europe/Rome. Sono richieste
  intercambiabili di avviare un worker; il cron originario non determina la politica.
- **Durata**: processo ordinario al massimo **5 ore**; l'avvio manuale può usare
  `run_seconds` tra 60 e 18000 per un collaudo breve;
  timeout job 355 minuti. Gli step hanno budget espliciti: la loro somma è 331
  minuti. `SIGINT` chiede la chiusura e restano fino a 120 secondi prima di
  `SIGKILL`, oltre agli step finali di persistenza e recupero.
  Gli errori di chiusura conservano il loro exit code. Le operazioni Git hanno
  un budget per transazione e condividono una deadline durante lo shutdown.
- **Polling automatico**: dalle **07:00 incluse alle 20:00 escluse**, Europe/Rome,
  con pausa di **15 minuti** dopo il ciclo. Il runtime rivaluta l'ora reale anche
  durante la run. Il bootstrap delle baseline è soggetto alla stessa fascia.
  Il lavoro già iniziato può concludersi e salvare dopo le 20:00.
- **Telegram**: long polling e comandi manuali restano attivi finché vive il
  processo, anche di notte. `/controlla` può quindi generare normali notifiche
  fuori fascia; `/status` mostra se il polling automatico è abilitato o sospeso.
- **Persistenza dati**: il bot stesso esegue `git commit` + `git push`
  dei file di stato quando aggiorna dati importanti (notifiche, iscritti,
  cronologia utenti, cache), non solo a fine job, così lo stato non resta
  solo nel filesystem temporaneo del runner
- **Consegne interrotte**: la ricevuta viene salvata dopo ciascun destinatario
  completato; le revisioni hanno una sequenza persistente, così anche un ritorno
  A→B→A viene notificato. Invii parziali restano da ritentare.
- **Recupero**: un workflow separato può richiedere un solo nuovo worker dopo
  una run scheduled fallita o scaduta, se non esiste già un successore.
  I dispatch di recupero non generano catene di retry e le cancellazioni manuali
  sono rispettate. Vedi [contratto e limiti](docs/WORKFLOW_RECOVERY.md).
- **Crash visibili**: il workflow non usa più `|| true` sull'esecuzione del
  bot; il timeout programmato è considerato normale, ma un crash reale fa
  fallire il job
- **Anti-sovrapposizione**: concurrency conserva una run attiva e al massimo
  una pending. Un nuovo trigger sostituisce la pending precedente senza fermare
  l'attiva (`cancel-in-progress: false`). Non è una coda FIFO di tutti gli avvii.
  La prossima run usa la politica dell'ora in cui esegue realmente il bot.
- **`permissions: contents: write`**: dichiarato esplicitamente nel
  workflow, necessario perché il bot possa fare `git push` — senza,
  alcuni repository (a seconda delle impostazioni di default) negano
  il permesso di scrittura al token automatico

GitHub può ritardare o perdere trigger: questo schema migliora la staffetta ma
**non garantisce disponibilità 24/7**. Durante setup o assenza di runner il bot
non risponde. Il verde Actions non è monitoring del servizio; il progetto del
watchdog e i limiti residui sono in [ARCHITECTURE_REVIEW](docs/ARCHITECTURE_REVIEW.md).

Configurazione applicativa (variabili ambiente; nel workflow sono esplicite):

| Variabile | Default | Significato |
|---|---|---|
| `AUTO_POLL_START` | `07:00` | Inizio incluso, Europe/Rome |
| `AUTO_POLL_END` | `20:00` | Fine esclusa, Europe/Rome |
| `AUTO_POLL_INTERVAL_MINUTES` | `15` | Pausa tra cicli automatici |

`INTERVAL_MINUTES` non seleziona più profili; usa `AUTO_POLL_INTERVAL_MINUTES`.
Il fuso IANA è obbligatorio e `tzdata` permette di verificarlo anche su Windows.

### Secrets richiesti nel repository

| Secret | Descrizione |
|---|---|
| `BOT_TOKEN` | Il token del bot, ottenuto da [@BotFather](https://t.me/BotFather) |
| `CHAT_IDS` | ID utente Telegram degli amministratori, separati da virgola; i comandi admin richiedono la loro chat privata. Eventuali ID di gruppi restano destinazioni delle notifiche, senza autorizzare comandi admin dal gruppo. |
| `STATE_ENCRYPTION_KEY` | Chiave Fernet per cifrare i file con chat_id; generarla con `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

(`GITHUB_TOKEN` è fornito automaticamente da GitHub Actions, non va creato)

### Avvio manuale

Dalla tab **Actions** del repository → workflow **Albo Pretorio Check**
→ **Run workflow**.
È un avvio produttivo con la stessa durata e politica temporale delle run
schedulate, non un dry-run di dieci minuti.

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
| `data/last_check.txt` | Timestamp dell'ultimo tentativo Albo arrivato al fetch, non prova di successo; aggiornato localmente e committato al massimo una volta al giorno |
| `data/seen_news.json` | Cache news: id, titolo, categoria, data, url |
| `data/subscribers_news.json` 🔒 | Elenco chat_id iscritti alle notifiche delle News |
| `data/user_seen_news.json` 🔒 | Cronologia per destinatario delle news già inviate |

🔒 = contiene chat_id (dato personale) ed è **cifrato** con `Fernet`
prima di ogni commit — vedi sezione [Cifratura dei dati personali](#cifratura-dei-dati-personali).

⚠️ Questi file **non vanno inseriti in `.gitignore`** — il bot deve
poterli leggere e scrivere ad ogni esecuzione per mantenere la cache e
la cronologia tra un run e l'altro.

### Archivio pubblico per il portale appalti

L'API della precedente integrazione dell'archivio pubblico è assente dal
`main` attuale. I sei test legacy restano esplicitamente saltati; questo bot
non mantiene attualmente gli export `data/public/albo-*.json` né esegue il
relativo backfill. La ricerca usa lo storico locale `data/seen_items.json`.

---

## Cifratura dei dati personali

Il repository è pubblico, quindi `subscribers.json`, `subscribers_news.json`,
`user_seen.json` e `user_seen_news.json` — i quattro file con chat_id Telegram —
vengono cifrati con una chiave simmetrica (`Fernet`) prima di ogni commit.

La chiave va impostata come secret del repository (`STATE_ENCRYPTION_KEY`).
Per generarla:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Su GitHub Actions la chiave è obbligatoria: senza di essa il bot si ferma
all'avvio. Solo in locale, senza `GITHUB_ACTIONS`, è previsto il salvataggio
in chiaro per lo sviluppo; non usarlo per committare dati personali reali.
Conserva una copia sicura della chiave: serve anche per leggere lo stato
cifrato nei ripristini.

---

## Struttura del progetto

```
.
├── .github/workflows/albo_check.yml   # Workflow GitHub Actions
├── bot.py                             # Logica del bot (albo + news)
├── admin_status.py                    # Esiti e tempi dei controlli del processo
├── requirements.txt                   # Dipendenze Python
└── data/                              # Stato generato automaticamente
    ├── seen_items.json
    ├── subscribers.json               # 🔒 cifrato
    ├── user_seen.json                 # 🔒 cifrato
    ├── last_check.txt
    ├── seen_news.json
    ├── subscribers_news.json          # 🔒 cifrato
    └── user_seen_news.json            # 🔒 cifrato
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
controllo automatico nella fascia configurata, pausa predefinita 15 minuti)
finché non viene interrotto manualmente. Anche localmente i comandi manuali
restano indipendenti dalla pausa notturna.

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
