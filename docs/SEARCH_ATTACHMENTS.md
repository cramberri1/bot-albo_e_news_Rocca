# Allegati su richiesta da `/cerca`

Revisione conservativa del 27–29 settembre 2026. La ricerca già presente è
estesa; `/cerca_news`, algoritmo identità v2, safety fuse, deduplicazione degli
update, cifratura, checkpoint, repair incident, spool e workflow sono conservati.

## Ricerca e richieste temporanee

`search_records()` continua a cercare una sottostringa normalizzata senza
distinzione di maiuscole o accenti nei metadati disponibili. Non inventa i
titoli mancanti. `render_search_page()` mostra cinque risultati numerati con
titolo, data, pubblicazione e mittente; ogni risultato Albo ha il proprio
pulsante `📎 Scarica allegati · N`. Le news non hanno questi pulsanti.

`_search_sessions` conserva per 30 minuti soltanto metadati di visualizzazione,
chiave canonica, chat e scadenza, fino a 100 ricerche. I token
`search_attach:<token>` usano 18 byte casuali; la callback non contiene chat,
URL, PATH, titolo o riga Halley. `_search_attachment_requests` associa il token
a chiave canonica, chat, scadenza e ricerca di origine. Sono attivi al massimo
cinque token per ricerca; cambiare pagina rimuove quelli della pagina precedente.

Il token viene consumato prima del primo `await`. Chat errata, token sconosciuto,
scaduto o già usato non avviano il recupero. Un tentativo da un'altra chat non
consuma la richiesta del proprietario. Il polling Telegram continua a usare il
checkpoint persistente dell'`update_id` prima dell'handler. Un riavvio perde
deliberatamente le richieste in memoria e invita a ripetere `/cerca`.

La pulizia avviene nei comandi, nelle callback e nel normale polling Telegram;
lo shutdown svuota le sessioni. Non viene aggiunto alcun file di stato.

## Identificazione e sessione

`fetch_search_attachment()` è un wrapper di `fetch_albo_html()` con
`force_detail=True`, `push_cache=False`, `write_cache=False`,
`strict_attachments=True`. `load_db(migrate=False)` normalizza i vecchi formati
soltanto in memoria. I parametri predefiniti del percorso automatico restano
invariati.

Il lock `_ALBO_SESSION_LOCK` e un nuovo `httpx.AsyncClient` possiedono l'intero
percorso elenco → dettaglio → download. Si scorre l'elenco corrente e si usa
`find_existing_equivalent_item()` per primary v2 e alias legacy; si verifica
anche l'intero database per non nascondere alias ambigui. Serve un solo candidato,
con identità storica sufficiente e senza conflitti nei campi forti disponibili.
Uno snapshot parziale o illeggibile non autorizza il download.

Halley può lasciare il bottone `btSucc` anche sull'ultima pagina. La fine
dell'elenco usa i contatori positivi e coerenti `pagCorrente`/`totalePagine`
quando disponibili; senza contatori validi resta il comportamento precedente.
Una pagina ripetuta o una pagina vuota che annuncia ancora risultati viene
segnalata come incompleta e non autorizza il recupero manuale.

Solo l'item ottenuto in questa sessione fornisce `num_riga`. La riga storica non
viene copiata né riutilizzata. Il controllo MC02 manuale richiede titolo
normalizzato esatto, struttura completa e compatibilità dei metadati disponibili
con **sia la lista corrente sia lo storico**, anche quando la lista omette un
campo che invece compare nel dettaglio.
MC02 visualizza la pubblicazione come `N/AAAA`, mentre la lista usa `N`:
questa rappresentazione è accettata solo se il numero normalizzato e l'anno
della data di pubblicazione coincidono. Gli ID e i numeri persistenti non
vengono modificati.

## Acquisizione, invio e cleanup

`enrich_with_attachments()` continua a gestire MC02 e MC96/97/98/99; ogni PATH
restituito viene scaricato immediatamente con `_download_attachment_to_spool()`
prima di risolvere il documento successivo. Non esiste un secondo downloader.
Gli URL e i riferimenti Halley restano variabili della sessione; non entrano in
callback, risultati della ricerca o file di stato.

La modalità strict rifiuta riferimenti malformati/sconosciuti e pagine MC02
incomplete. Il messaggio «nessun allegato» richiede una sezione completa e
verificata senza documenti; un dettaglio vuoto o illeggibile non equivale a zero.
L'unico avviso informativo escluso dal conteggio è l'avviso Halley verificato
per il lettore Dike dei file P7M; avvisi inattesi o di errore bloccano il gruppo.

Il preflight valida l'intero gruppo, conteggio, dimensioni, hash e spool prima
del primo invio. Se manca un documento o supera i limiti, non viene inviato
alcun sottoinsieme. Si mantengono i limiti esistenti: 45 MiB per documento,
200 MiB per atto e 512 MiB complessivi di spool (configurabili).

`cmd_search_attachments()` invia soltanto alla chat richiedente con
`send_item_to_chat()` e il lock di consegna esistente. Aggiorna il messaggio di
avanzamento con esito e conteggio. Il recupero e l'invio hanno un limite di
240 secondi, inclusa l'attesa del lock Halley. `finally` e `cleanup_spool_scope`
eliminano spool completati e parziali anche su errore, timeout o cancellazione.
I nuovi log espongono eventi, conteggi, campi diagnostici e tipi di errore,
non titoli, nomi dei documenti, chat, token o URL di sessione.

## Stato e semantica

L'operazione non chiama `notify()` o il safety fuse; non cambia `notified`,
revisioni, fingerprint, snapshot, pending o altre consegne automatiche.
Non aggiorna `user_seen`: ricevere manualmente gli allegati da una ricerca non
equivale al checkpoint di `/atti` e non può sopprimere una futura revisione.
Il solo checkpoint dell'update Telegram continua a essere scritto dal polling,
come per ogni altro comando.

L'idratazione progressiva dei metadati era già presente in `remember_identity()`
durante i controlli normali. Non è stato avviato un backfill massivo.

## Verifiche e limiti

Baseline: 37 test, 31 passati, 6 skip preesistenti dell'archivio pubblico.
Suite finale locale: **112 test, 106 passati, 0 fallimenti, 6 skip invariati**.
Sono aggiunti 75 test: 48 ricerca/recupero, 21 primitive Halley, 5 avvisi MC02
e 1 polling Telegram senza testo (con più sottocasi).
Le nuove suite `test_search_attachments.py` e `test_attachment_primitives.py`
coprono pulsanti/paginazione, chat/TTL/replay, dedupe persistente, ambiguità,
alias/v2, sessione/riga nuova, MC02/MC96–99, formati PDF/P7M/DOC/DOCX/ZIP/CSV,
zero/parziali/errori/limiti, privacy, timeout e cleanup. I test completi con
HTTP e Telegram simulati verificano anche l'immutabilità byte per byte dello
stato. `test_telegram_nontext.py` riproduce e corregge separatamente il crash
del polling per messaggi senza testo, vuoti o composti da spazi.
`test_search_detail_advisory.py` impedisce che errori o documenti negli avvisi
vengano scambiati per una lista vuota.

Collaudo live del 29 settembre, su copia temporanea dello stato aggiornato e
Telegram simulato: 129 online, 375 noti, 0 candidati nuovi, 0 nuove notifiche,
20 controlli revisione, 0 revisioni, 0 pending, 0 fallimenti e 0 chiamate notify.
Ricerca di un atto noto: un risultato; recupero reale di 1/1 PDF, preflight valido,
spool interamente ripulito e tutti i file di stato del repository identici.

I 241 record privi di titolo alla prima verifica restano ricercabili solo per
i metadati realmente presenti. Un atto storico non più recuperabile online
rimane nello storico. Un cambiamento di markup non riconosciuto può causare
un rifiuto conservativo, mai un tentativo di indovinare i documenti.

Il preflight impedisce l'invio di un'acquisizione parziale; Telegram può comunque
fallire dopo avere accettato uno o più documenti. In quel caso il messaggio
avverte che alcuni documenti potrebbero essere arrivati. Restano i limiti
preesistenti dei retry HTTP e l'assenza di una transazione exactly-once fra
Telegram e Git. Il controllo live usa Telegram simulato: nessun invio reale
agli iscritti o agli amministratori durante il collaudo.
