# Revisione conservativa del 24 settembre 2026

## Riscontro indipendente

Base esaminata inizialmente: `d272349`, poi aggiornata prima della distribuzione.
Backup remoto: `backup/pre-hardening-20260924`.

Il commit `4718d27f0d3a5af433fbcbdc9963444a0345ea06`, parent
`b7d5e76a40133f4daa8cf1d9d137bfe0e3741297`, aggiunge 102 record:
363 -> 465. Tutti hanno una controparte precedente unica dopo la normalizzazione
dei placeholder. Per 97 la prova include SHA-256, numero e nomi hash degli
allegati, date, mittente e numeri sostanziali. I 5 senza allegati richiedono
anche la ricostruzione esatta dei due ID SHA-256; le prove sono in
`incident_identity_evidence.json` e sono verificate dallo script.

La causa nel codice è l'identità SHA-256 di titolo, numero pubblicazione e tipo
non normalizzati, senza riconciliazione secondaria né fusibile. È stato possibile
ricostruire 86 vecchi ID dalle card live aggiungendo al tipo `Pubblicazione dal ... al ...`:
quando mancava `Atto n.`, la regex del tipo inglobava il periodo di pubblicazione.
La comparsa di `Atto n. del ...` tronca invece il tipo e cambia l'ID. Inoltre
`[^\\s]+` accettava `del` e `0` come numeri. Per gli altri 16 record l'equivalenza
sostanziale è dimostrata dagli snapshot e dagli allegati; senza l'HTML storico
non è possibile attribuire a ciascuno una specifica differenza testuale.

Il commit `25d503b7deea0893a0167b97c89d87b631f3f0cd` incrementa 19 revisioni
esclusivamente per `register_number: "" -> "0"`. Non risulta alcun percorso
che colleghi il polling automatico a `cmd_atti`: la raffica deriva da
`_run_check_albo -> notify -> send_item_to_chat`, non dall'esecuzione di `/atti`.

## Identità e riparazione

- Primary ID v2: ente + anno di pubblicazione + numero contestuale normalizzato;
  fallback: data, titolo, tipo e mittente normalizzati Unicode NFKC, HTML,
  spazi e casefold. `num_riga` resta esclusivamente un riferimento di sessione.
- Le chiavi storiche rimangono le chiavi canoniche delle consegne; primary ID,
  metadati e alias vengono conservati nei record. Una corrispondenza tramite
  contenuto richiede snapshot completo e hash degli allegati identici, un solo
  candidato e nessun conflitto con numero pubblicazione/titolo disponibili.
- I record senza allegati non vengono fusi automaticamente sulla sola data.
- La prima esecuzione v2 effettua la migrazione silenziosa; le pubblicazioni
  sconosciute troppo vecchie, future o senza data sono baseline silenziose.
- `scripts/repair_incident.py` è dry-run per default; `--apply` riconcilia solo
  i 102 ID aggiunti dal commit incriminato e corregge solo i 19 incrementi
  dimostrati. È idempotente, rifiuta storie divergenti o consegne pending e
  conserva eventuali revisioni reali successive. Il report elenca ogni coppia.
- I 102 ID accidentali restano come alias. Nessun file personale è decifrato
  o riscritto dalla riparazione, nessuna chiave viene cambiata.

## Invii e revisioni

Snapshot normalizzati prima del fingerprint e del confronto. Placeholder e
variazioni cosmetiche non generano revisioni. Cambi di date, mittente, numeri
sostanziali, numero/nomi/hash degli allegati continuano a essere rilevati.
Fingerprint diverso senza modifica concreta: warning e baseline normalizzata,
nessuna notifica generica di contenuto. I retry mantengono le delivery-key
versionate e le cronologie dei destinatari già serviti.

Configurazione del fusibile:

| Variabile | Default | Effetto |
|---|---:|---|
| ALBO_MAX_NEW_PER_CYCLE | 30 | limite presunti nuovi e consegne automatiche complessive |
| ALBO_NEW_RATIO | 0.65 | quota sospetta di nuovi nell'elenco |
| ALBO_RATIO_MIN_ITEMS | 15 | applica il controllo percentuale da questa quantità |
| ALBO_MAX_OLD_CANDIDATES | 5 | blocco per molti sconosciuti storici/data ignota |
| ALBO_NOTIFY_MAX_AGE_DAYS | 30 | finestra prima pubblicazione automatica |
| REVISION_RECHECK_MAX_ITEMS | 20 | massimo revisioni da scaricare per ciclo |
| REVISION_RECHECK_MINUTES | 60 | intervallo minimo di ricontrollo |

Il fusibile interviene prima dei download e degli invii. Un solo avviso per
admin per episodio viene marcato persistentemente prima dell'invio. Una giornata
con oltre soglia richiede verifica e regolazione delle soglie: il bot non smaltisce
a rate un'anomalia massiva. Pagine illeggibili, troppe card scartate e un elenco
vuoto inatteso sospendono gli invii. Una singola card invalida rimane fail-soft.

I comandi conservano i rispettivi handler. I token resend sono monouso, legati
alla chat, con scadenza e volutamente non persistenti: dopo un riavvio non
consentono reinvii. `/cerca` e `/cerca_news` aggiungono ricerca senza distinzione
tra maiuscole/accenti, 5 risultati per pagina e callback legate alla chat per
30 minuti. Lo storico originario non conservava molti titoli: la ricerca può
usare solo metadati effettivamente disponibili, che vengono integrati dai
successivi controlli. Nessun database esterno.

## Telegram e arresto

Long polling con gestione esplicita dell'offset e `drop_pending_updates=False`.
`telegram_updates.json` contiene esclusivamente ID update, una finestra di 512
ID e timestamp/stato di elaborazione. Nessun chat ID, username o testo.
La presa in carico viene scritta atomicamente e pubblicata su Git prima
che il comando/callback produca effetti. L'offset viene avanzato dopo
elaborazione e sincronizzazione dello stato. Sono gestiti anche ID casuali
dopo oltre una settimana di inattività Telegram.

Garanzia privilegiata: nessuna riesecuzione di update già presi in carico.
Un crash tra presa in carico ed esecuzione può interrompere un comando, che
l'utente dovrà inviare nuovamente con un nuovo update. Telegram e Git non offrono
una transazione distribuita: non si promette exactly-once. Una richiesta HTTP
di invio dal risultato ambiguo o un arresto prima del checkpoint della consegna
possono ancora richiedere intervento; il fusibile limita le conseguenze.

SIGINT/SIGTERM arrestano i task, ripuliscono gli spool, fermano Application e
tentano il flush finale. Gli spool hanno anche cleanup per eccezione/cancellazione.
PDF, P7M, Office, ZIP, immagini, TXT e altri formati preesistenti restano supportati,
con streaming, SHA-256, limiti, preflight e download nella stessa sessione Halley.
Il controllo MC02 ora rifiuta dettagli vuoti/incompatibili anche per titoli brevi.

## Persistenza e prestazioni

Scritture mediante tempfile + flush/fsync + os.replace, con rifiuto di JSON
corrotto anziché reset. Cifratura Fernet invariata. Checkpoint di ogni consegna
automatica prima di proseguire; push fallito sospende il ciclo. Git mantiene
`HEAD:main`, fetch/rebase/retry; un conflitto abortisce il rebase e conserva i
commit locali. Il workflow salva un artifact di recupero per 3 giorni se fallisce.
Le operazioni Git restano sincrone per non far correre rebase e scritture di stato
in parallelo: durante un Git lento i comandi possono attendere. I retry sono limitati.

Scansione completa dell'elenco mantenuta intenzionalmente per non perdere atti
attivi vecchi, senza scadenza e revisioni. I dettagli/hash ruotano su massimo 20
record, dal meno recentemente controllato. Gli sconosciuti storici non vengono
scaricati inutilmente. Nessun early-stop aggressivo.

## Workflow

Timezone `Europe/Rome`, supportata dalla documentazione corrente:
https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule

| Avvio locale | Durata processo | Polling |
|---|---|---|
| 06:57 | 343 minuti | 15 minuti |
| 12:37 | 343 minuti | 15 minuti |
| 18:17 | 343 minuti | 15 minuti |
| 00:07 | 55 minuti | 30 minuti |
| 03:37 | 55 minuti | 30 minuti |
| workflow_dispatch | 10 minuti | 15 minuti |

Concurrency `albo-rocca-bot`, `cancel-in-progress: false`. Timeout job 355 minuti,
SIGINT alla durata prevista e fino a 120 secondi prima di SIGKILL. Il tempo
rimanente copre setup, arresto e persistenza. GitHub può ritardare i cron:
quasi continuità, non disponibilità garantita. Nessun cambio di hosting.

## Verifiche

37 test scoperti: 31 superati, 6 esplicitamente saltati. I 6 erano già tutti
in errore su main prima della patch: richiamano `PUBLIC_DATA_DIR` e API di archivio
pubblico assenti. Rimangono nel repository e si riattivano se l'API ritorna;
non sono presentati come verifiche superate e non viene introdotta una nuova
pubblicazione di dati per farli passare.

Regressioni, fixture HTML, riparazione sui commit originali, idempotenza,
revisioni reali, raffica 102/122, migrazione silenziosa, cifratura e alias,
fallimento atomico, replay Telegram/callback, isolamento paginazione/resend,
formati allegati, cleanup cancellazione, SIGTERM e YAML.
Un test Git reale con remote bare locale dimostra detached HEAD + push respinto
+ fetch/rebase + conservazione del cambiamento remoto.

Ciclo live con Telegram simulato e stato temporaneo riparato: 122 online,
122 riconosciuti, 13 controlli revisione, zero nuove notifiche, zero revisioni,
zero chiamate notify, zero byte spool residui.

Comando: `python -m unittest discover -s tests -v` dopo
`pip install -r requirements-dev.txt` e clone con storia completa.
