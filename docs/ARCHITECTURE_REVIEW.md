# Revisione architetturale: servizio, invarianti e guasti

Analisi iniziata il 1° ottobre e ripresa il 3 ottobre 2026. Questo documento
precede le modifiche applicative. Fotografia del codice: `8364ea9`; al riavvio
dell'analisi `origin/main` è `3efcfd1`, con sole differenze nei dati operativi.
Le sezioni sui difetti descrivono quella fotografia, non fingono che una
correzione proposta sia già distribuita. L'esito finale è registrato in fondo.

## 1. Giudizio sul sistema

Il progetto ha difese valide contro identità instabili, replay e documenti
attribuiti male. Non ha ancora un modello unico di disponibilità, progresso
e conferma delle consegne. Una run verde certifica soprattutto che il processo
è terminato secondo il contratto del workflow; non certifica il servizio.
L'incidente dello scheduling è una contraddizione tra due orologi: l'identità
storica dell'evento determina una decisione che dovrebbe dipendere dal presente.

Non occorre cambiare hosting per eliminare questo errore. GitHub Actions resta
un supervisore con avvii e durata limitati, senza garanzia di continuità. Il
runtime deve possedere la politica applicativa, separando Telegram dal polling.
File e Git restano lo store; una riscrittura contemporanea di scheduler, scraper,
identità e delivery introdurrebbe più incertezza di quella che risolve.

## 2. Ambito, prove e limiti dell'esame

Esaminati `bot.py` (4.267 righe nella fotografia iniziale), `albo_identity.py`,
`admin_status.py`, entrambi i workflow, i due requirements, README,
HARDENING, PROJECT_REVIEW, ADMIN_STATUS, SEARCH_ATTACHMENTS e tutti i file
Python sotto `tests/`. Analizzata la struttura di `data/` e la storia Actions.
I quattro file personali cifrati sono riconosciuti come Fernet, non decifrati.
Non sono stati contattati destinatari Telegram durante le verifiche.

Baseline ripetuta: **137 test, 131 superati, 6 skip legacy**. Le riproduzioni
dei guasti usano directory temporanee, HTTP simulato e Telegram simulato.
I test verdi sono evidenza di casi coperti, non una dimostrazione degli invarianti.

La fotografia del 1° ottobre conteneva 380 atti, 156 news, zero atti pending e
241 atti senza titolo. Sono conteggi storici, non misure live né iscritti reali.
`data/public/` contiene export storici ma il codice corrente non li mantiene:
presenza di un file e disponibilità di una funzionalità sono cose diverse.

## 3. Modello attuale e responsabilità

```text
GitHub cron / dispatch
  -> ammissione concurrency [1 attivo + 1 pending sostituibile]
  -> runner: checkout, dipendenze, test, scelta profilo, timeout
       -> Python / unico event loop
            +-> polling automatico -> baseline -> Albo -> News -> heartbeat
            +-> Telegram getUpdates -> claim durevole -> handler -> checkpoint
                     |                    |
                     |                    +-> /atti, /news, /cerca, /controlla
                     +-> iscrizioni / status / callback

Halley -> snapshot lista -> riconciliazione -> dettaglio/spool -> consegne
             |                 |                    |              |
             +------------ stato globale -----------+       ricevute per chat
                                      \                   /
                                  JSON locali + Git push
                                          |
                            checkout del runner successivo
```

| Componente logico | Responsabilità corretta | Accoppiamento attuale |
|---|---|---|
| Source/acquisition | Leggere uno snapshot con completezza, identità e diagnostica; possedere sessione e spool | Alcune letture aggiornano già la cache; strictness diversa tra automatico e ricerca |
| Reconciliation | Decidere baseline, nuovo, revisione, noto o anomalia senza inviare | `_run_check_albo` mescola decisioni, mutazioni, checkpoint e invio |
| Delivery engine | Per atto/versione/chat: selezione, tentativo, esito e checkpoint | Albo e News hanno cicli diversi; `notify` restituisce gli esiti dopo il fanout |
| Telegram interface | Interpretare richieste, autorizzare, presentare risultati | Handler lunghi condividono event loop e store; gli update sono seriali |
| State store | Letture validate, scritture consistenti, pubblicazione, conflitti, recupero | Funzioni globali e Git sincrono; atomicità del singolo file, non transazione remota |
| Runtime/scheduler | Politica temporale, supervisione, arresto e vita del processo | Parte della politica è congelata dal cron nel YAML; runtime non conosce giorno/notte |
| GitHub Actions | Avviare e serializzare runner, fornire secret, limitare risorse e conservare recuperi | Decide impropriamente la politica di polling usando l'evento storico |

Il polling automatico è un produttore di richieste al motore del servizio.
Telegram è un altro produttore. Sospendere il primo non richiede spegnere il secondo.

## 4. Invarianti richiesti e garanzie realistiche

1. **Tempo reale:** la politica usa un istante consapevole del fuso convertito
   in Europe/Rome; cron nominale, ritardo e dispatch non scelgono giorno/notte.
2. **Transizione:** la politica viene rivalutata durante la run. Nessun nuovo
   ciclo automatico è ammesso fuori fascia; lavoro già ammesso termina e salva.
3. **Indipendenza:** la sospensione automatica include il bootstrap delle
   baseline; non vieta comandi manuali né la ricezione degli update Telegram.
4. **Un solo writer:** un processo autorizzato scrive ogni stato/ref. La concurrency
   vale nel proprio repository/gruppo, non blocca un processo locale o un repair.
5. **Identità:** riga e URL di sessione non sono identità permanenti. Alias
   conservati, riconciliazione ambigua rifiutata, identità forte verificata.
6. **Acquisizione:** snapshot incompleto non equivale a sorgente completa o vuota;
   documenti non riconosciuti non equivalgono a zero documenti.
7. **Nuovo/baseline:** cache manuale non marca globalmente come notificato; stato
   assente/corrotto non deve trasformare lo storico in una raffica.
8. **Revisione:** variazione cosmetica non genera una revisione; variazione reale
   richiede uno snapshot attendibile e una chiave di consegna versionata.
9. **Consegna:** una ricevuta confermata e durevole esclude un reinvio automatico
   della stessa versione alla stessa chat. È un obiettivo condizionato alla
   durabilità della ricevuta, non una promessa di exactly-once su Telegram.
10. **Fallimento conservativo:** sincronizzazione non confermata ferma ulteriori
    invii del ciclo; il recupero non inventa ricevute né azzera archivi.
11. **Checkpoint:** arresti previsti sono guasti da progettare, non eccezioni rare;
    il flush dei file non recupera dati rimasti soltanto in variabili Python.
12. **Privacy:** nessun secret o identificativo personale in report pubblici;
    cifratura necessaria su Actions e nessuna chiave dentro artifact o Git.
13. **Osservabilità:** processo vivo, Telegram raggiungibile, lista acquisita,
    revisioni verificate, invii completati e push riuscito sono segnali distinti.
14. **Status:** tentativo e successo non sono sinonimi; dati ignoti non diventano
    zero; un esito precedente non prova la salute presente.
15. **Budget:** durata applicativa, setup, grazia di arresto, persistenza finale e
    artifact devono stare nel timeout. Il setup non ha durata zero.
16. **Recovery:** un artifact vecchio non può sovrascrivere ciecamente ricevute
    recenti o ripristinare utenti disiscritti successivamente.
17. **Limite dell'infrastruttura:** nessun test locale può garantire puntualità
    dei cron, disponibilità continua o esecuzione del watchdog durante un outage GitHub.

## 5. Incidente P0: teoria contro esecuzioni reali

Orari seguenti convertiti da UTC a Europe/Rome, allora UTC+2. Si usano i tempi
dello step **Esegui bot**, non `run_started_at`, che in questi dati può precedere
l'acquisizione del runner e non prova l'inizio del processo.

| Run | Cron conservato | Processo effettivo, ora italiana | Politica applicata |
|---|---|---|---|
| [36831735692](https://github.com/cramberri1/bot-albo_e_news_Rocca/actions/runs/36831735692) | `37 3 * * *` | 01/10 09:41:09–10:36:10 | 3.300 secondi, intervallo 30 min |
| [36775429302](https://github.com/cramberri1/bot-albo_e_news_Rocca/actions/runs/36775429302) | `17 18 * * *` | 01/10 00:13:17–05:56:17 | 20.580 secondi, intervallo 15 min |
| [36800191236](https://github.com/cramberri1/bot-albo_e_news_Rocca/actions/runs/36800191236) | `7 0 * * *` | 01/10 05:56:41–06:51:42 | 3.300 secondi, intervallo 30 min |
| [36682840259](https://github.com/cramberri1/bot-albo_e_news_Rocca/actions/runs/36682840259) | `37 3 * * *` | 30/09 09:17:33–10:12:35 | stesso difetto mattutino |

La prima run è stata creata alle 09:40:57: passa soltanto circa 12 secondi
tra creazione osservabile e avvio del bot. Non è dimostrato che tutte le ore di
ritardo di **quella** run siano state trascorse in concurrency: parte del ritardo
precede la creazione osservabile. La seconda invece nasce il 30/09 alle 22:50:27
e ottiene il job alle 00:13:04, dopo circa 83 minuti di attesa. La terza nasce
alle 03:15:05 e ottiene il job alle 05:56:22, dopo circa 161 minuti.

**Causa:** `github.event.schedule` identifica l'evento che ha richiesto il lavoro,
non l'orario al quale il lavoro si sta svolgendo. `Seleziona profilo` usa quella
stringa per durata e frequenza; `polling_loop` conserva poi l'intervallo fino
alla fine. Anche una run puntuale può attraversare una fascia senza adattarsi.
**Impatto:** stop di 55 minuti in mattinata e scansioni diurne a notte fonda.
**Perché success:** GNU timeout tratta la durata scelta come arresto previsto,
e il workflow converte quel caso in successo. Nessun componente viola il proprio
contratto locale: è il contratto complessivo a essere sbagliato.

### Semantica reale di GitHub

Con la configurazione attuale esistono al massimo un'esecuzione attiva e una
pending nel gruppo. Un nuovo ingresso sostituisce la precedente pending;
`cancel-in-progress: false` protegge l'attiva, non crea una coda FIFO illimitata.
Non serve che ogni trigger sia conservato: è una richiesta di avviare un worker,
non un evento comunale da consegnare. Atti e ricevute sono nello store.

GitHub documenta ritardi e possibili scarti dei job schedulati sotto carico.
L'ordine dei trigger non è un orologio applicativo. La variante recente di coda
estesa non è necessaria qui: conservare molte partenze obsolete peggiora la
staffetta senza rendere più fresche le letture.

Fonti primarie consultate: [concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[eventi schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
[sintassi dei workflow](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax),
[limiti Actions](https://docs.github.com/en/actions/reference/limits).

## 6. Politica temporale proposta e scelta dall'utente

Scelta esplicita dell'utente: **07:00 incluso – 23:00 escluso, Europe/Rome**,
polling automatico ogni 15 minuti; fuori fascia sospeso. Orari configurabili,
non dedotti dal cron. La frequenza è una pausa dopo il lavoro, non una garanzia
di appuntamenti esatti. Alle 07:00 un worker già attivo può riprendere senza
attendere un nuovo evento GitHub. Durante un ciclo lungo si rivaluta la fascia
prima di ammettere una nuova sorgente; una consegna già iniziata non viene
cancellata apposta alle 23:00. È una scelta esplicita per proteggere i checkpoint.

Proposta di supervisione: durata massima uniforme di **5 ore** per il processo,
sia schedule sia dispatch; wake orario al minuto 17 per lasciare una pending
recente. Concurrency invariata. La vita del processo protegge il budget del
runner, non definisce il giorno applicativo. Dispatch diventa avvio operativo
normale; il precedente limite di dieci minuti non era né dry-run né recovery
duratura e introduceva un'altra causa di stop breve.

Il budget di 355 minuti del job conserva spazio ampio per setup e chiusura,
con limiti espliciti ai passi. Timeout con SIGINT e grazia di 120 secondi,
salvataggio finale e artifact restano difese necessarie. Un setup fallito non
avvia un bot parzialmente configurato. Un crash precoce resta failure; non viene
mascherato come scadenza normale. Nessun calendario elimina il gap di setup o
un ritardo GitHub: il nuovo modello elimina la politica errata, non promette 24/7.

Il simulatore puro deve distinguere istante nominale, ingresso effettivo nel
gruppo, setup, intervallo Python, cleanup e rilascio del gruppo. Input: ritardi
per trigger, durata, dispatch, crash, setup e politica. Output: intervalli
Telegram, intervalli di ammissione polling, gap, sostituzioni pending e profili.
Modello su almeno 48 ore, eventi UTC e conversione Rome; DST include ore saltate
e ripetute. Nei pareggi è ammesso un ordinamento deterministico del simulatore,
che non viene presentato come garanzia di ordinamento del provider.

La proprietà chiave è: due worker che si trovano nello stesso istante e nella
stessa configurazione applicano la stessa politica, qualunque sia cron, ritardo
o ordine degli eventi. Il modello vecchio deve violarla nel caso 09:41; quello
nuovo deve soddisfarla anche attraversando 07:00 e 23:00.

## 7. Problemi, gravità e interventi

P0 indica una decisione operativa già dimostrata errata in produzione; P1 indica
perdita/replay o falsa salute riproducibili; P2 indica un limite da correggere
con un contratto più ampio; P3 riguarda manutenzione e chiarezza.

| ID | Priorità e natura | Problema, causa e scenario | Intervento e compromesso |
|---|---|---|---|
| S1 | P0, architettura | Cron notturno parte al mattino e congela durata/frequenza notturne | Politica nel runtime sull'ora reale; supervisione uniforme. Rimangono gap del provider |
| S2 | P1, contratto operativo | Test assume setup di 8 minuti; 343 min + grazia + setup/flush possono superare 355 | Vita più corta, passi limitati e margine di recupero; più staffette e gap di setup |
| D1 | P1, durabilità | News salva tutto a fine lotto: SIGINT sulla seconda news perde anche la ricevuta della prima | Checkpoint per news e stop sul push fallito; più commit, finestra per singolo destinatario ancora aperta |
| D2 | P1, coordinamento | Albo blocca per Git non sincronizzato ma News può inviare subito dopo | Stesso gate prima degli invii News; indisponibilità Git sospende consegne invece di rischiare replay |
| A1 | P1, completezza | `incomplete_pagination` è prodotto dal source ma ignorato dal selettore automatico Albo | Rifiutare ciclo troncato prima degli invii; possibili ritardi conservativi |
| A2 | P1, contratto source | MC02 con controllo documento sconosciuto può diventare zero allegati nel percorso automatico | Validare struttura/riferimenti anche lì; markup nuovo viene sospeso, non interpretato come rimozione reale |
| O1 | P1, semantica di successo | Dettaglio di atto noto fallisce, snapshot=None, ciclo ritorna ok=True | Segnalare acquisizioni selezionate incomplete e ciclo degradato; nessuna falsa prova di salute |
| D3 | P1, limite distribuito | Invio ad A riuscito, arresto durante B prima del checkpoint dell'atto | Progettare ricevute per destinatario/parte; non eliminabile completamente dopo risposta HTTP ambigua |
| P1 | P1, disponibilità/arresto | Git sincrono può impiegare più della grazia di 120s e bloccare anche il segnale nell'event loop | Store con proprietario, deadline e sincronizzazione seriale; non spostare semplicemente Git su un thread |
| T1 | P2, semantica comandi | Claim durevole, crash, handler mai eseguito; update poi scartato | Documentare at-most-once dell'invocazione; eventuale recupero mirato dei soli comandi idempotenti |
| P2 | P2, concorrenza dati | Rebase aggiorna file mentre chiamanti conservano vecchi dict; altro writer o repair può causare sovrascritture | Un writer e snapshot versionati/CAS; separare branch non basta a creare una transazione |
| D4 | P2, race manuale | /atti e automatico selezionano lo stesso atto prima del lock chat, poi lo inviano in sequenza | Selezione e conferma sotto lo stesso contratto delivery; preservare il significato del recupero manuale |
| T2 | P2, conferma anticipata | Iscrizione scritta localmente e confermata, poi push finale fallisce | Propagare risultato dello store prima della conferma; claim non deve diventare conferma dell'effetto |
| N1 | P2, assunzione source | Early-stop News e data non parsabile trattata recente; revisione di news vecchia ignorata | Politica esplicita su aggiornamenti/date ignote e limiti dopo downtime; non cambiare gli ID alla cieca |
| R1 | P2, recupero | Artifact vecchio contiene snapshot parziali e può precedere disiscrizioni o ricevute nuove | Manifest, verifica isolata, confronto con main e merge semantico; mai restore cieco |
| M1 | P2, monitoring | /status non risponde se morto; heartbeat giornaliero usa anche check incompleti | Segnali persistiti separati e watchdog indipendente dal bot, con unknown esplicito |
| C1 | P3, manutenzione | Dipendenze a intervalli larghi; CI push non include requirements-dev.txt | Versioni riproducibili e copertura del file dev; senza confonderlo con correzione dello scheduling |

Le correzioni iniziali sono S1/S2, D1/D2, A1/A2/O1 se dimostrate dai test.
D3/P1 richiedono un contratto di persistenza più ampio: non si risolvono con
un refactoring cosmetico o una promessa di atomicità. Non si introduce una
migrazione dello store in questa patch.

## 8. Albo: compromessi e confini

Identità v2 usa ente/anno/numero contestuale, con fallback normalizzato e alias.
Le chiavi canoniche storiche preservano ricevute. Fusioni per contenuto richiedono
unicità, snapshot completo e hash coerenti; zero allegati non è prova sufficiente
di equivalenza. Questa prudenza può ritardare un nuovo atto ambiguo ma impedisce
una fusione sbagliata che perderebbe notifiche o assocerebbe documenti ad altro atto.

Sconosciuti troppo vecchi, futuri o senza data diventano baseline silenziosa:
evita raffiche dopo drift/downtime ma può perdere una pubblicazione reale inserita
oggi con data vecchia. Non è possibile garantire contemporaneamente zero falsi
nuovi e nessuna perdita con metadati non affidabili. Il fusibile blocca l'intero
episodio anomalo, non lo smaltisce in piccoli lotti automaticamente.

Le revisioni richiedono snapshot completo, normalizzazione e differenze sostanziali.
Gli atti selezionati che falliscono sempre possono occupare il budget di ricontrollo
e ritardare altri atti: servono in futuro priorità con backoff e distinzione tra
ultimo tentativo e ultimo successo di dettaglio. Non si può dichiarare riuscita
una verifica mai completata. Un pending scomparso dal portale può restare irrisolto:
lo storico non è un archivio persistente dei documenti.

Gli allegati hanno streaming, hash, limiti di documento/atto/spool, preflight e
cleanup; PDF, P7M, Office, ZIP, immagini e testi non vanno ridotti al solo PDF.
Sessione Halley e num_riga devono vivere nello stesso possesso temporaneo.
Rendere strict il recupero manuale non protegge automaticamente l'acquisizione
produttiva: le due vie devono condividere il contratto di completezza.

`/atti` aggiorna cache e ricevute della chat: non brucia `notified` globale, ma
può legittimamente evitare una seconda consegna identica a quella stessa chat.
Le revision-key sono distinte. `/cerca` con allegati è volutamente readonly sullo
stato delle consegne; non sopprime future notifiche o revisioni. I token restano
monouso, legati alla chat e persi al riavvio.

## 9. News e delivery

News usa ID nell'URL, early-stop sulla prima pagina interamente nota, nessun
monitoraggio sistematico delle vecchie revisioni e date assenti/illeggibili
considerate recenti. Dopo downtime lungo, oltre 60 giorni si archivia senza push;
una data ignota può invece autorizzare molti invii. Non esiste il medesimo fusibile
Albo. Prima di convergere i motori serve decidere esplicitamente questa politica.

La prova D1 è concreta: prima news consegnata, cancellazione durante la seconda,
nessun file di archivio/ricevute ancora scritto. Un flush Git finale non può
salvare variabili locali perdute. Un checkpoint per news riduce il replay dal
lotto al singolo fanout. La finestra invio riuscito -> checkpoint rimane.

Un futuro delivery engine comune può condividere ricevute, retry e persistenza,
non deve uniformare forzatamente identità, snapshot o requisiti di allegati delle
due sorgenti. Distinguere esiti **confermato / fallito / sconosciuto**: un timeout
Telegram dopo accettazione non prova mancata consegna. Ripetere un documento in
quel caso privilegia completezza rispetto a unicità; sopprimerlo privilegerebbe
unicità rispetto a consegna. Serve una scelta dichiarata, non exactly-once.

## 10. Telegram: cosa significa esattamente il claim

Sequenza attuale: claim atomico e push -> handler -> checkpoint elaborazione ->
push -> avanzamento offset. Un claim durevole non viene rieseguito dopo crash:
**at-most-once dell'invocazione dell'handler, completamento best effort**. Non è
at-most-once degli invii di rete né exactly-once degli effetti distribuiti.

Claim -> crash -> nessun effetto perde la richiesta, che l'utente deve ripetere
con un nuovo update. Effetto -> crash dopo claim normalmente non riesegue quello
stesso handler; le notifiche automatiche hanno invece una deduplicazione separata
basata sulle ricevute. La libreria PTB può catturare l'errore del handler e chiamare
`process_error`: `last_processed_update_id` significa tentativo processato, non
necessariamente effetto riuscito. Il gestore corrente dell'errore si limita al log.

Per /help, /status e ricerca una risposta persa è recuperabile con un nuovo comando.
Per allegati e /controlla il replay cieco è rischioso. Iscrizioni/disiscrizioni sono
operazioni desiderate idempotenti: un futuro inbox pending/completed potrebbe
recuperare specificamente queste, senza riaprire i token di invio o riavviare tutti
i comandi. Non si cambia globalmente la semantica dei claim in questa patch.

## 11. Git come StateStore e recupero

Git offre copia remota, storia e audit, ma oggi svolge anche checkpoint e sincronizzazione.
`git_commit_and_push` occupa circa 190 righe, oltre a molti wrapper e al codice YAML.
Scrivere un file atomicamente non rende atomici due file, un commit, un push e
Telegram. Dopo timeout del push il remoto può avere accettato: l'esito è incerto.
Dopo invio Telegram e push fallito la ricevuta locale può andare perduta con il runner.
Se si segnasse prima una consegna mai inviata, si perderebbe invece il messaggio.

Le chiamate Git sincrone evitano un rebase simultaneo a scritture nell'event loop,
ma bloccano Telegram e la gestione dei segnali. Metterle in `to_thread` senza un
proprietario dello store introdurrebbe una race nuova. I timeout per singolo comando
non sono una deadline globale: tre push da 90s più fetch/rebase superano la grazia.

Contratto evolutivo, senza cambiare ora formato o backend:

```text
StateStore.read_snapshot() -> snapshot immutabile + schema + revision
StateStore.commit(changes, expected_revision, reason, deadline)
    -> local_durable, remote_confirmed, revision, conflict/unknown/failure
StateStore.record_delivery(source, canonical_id, version, recipient, outcome)
StateStore.claim_update(id, kind) / complete_update(id, outcome)
StateStore.flush(deadline) / health() / export_recovery_manifest()
```

Un solo proprietario serializza tutte le mutazioni e Git. La prima estrazione
delega ai formati esistenti; successivamente si eliminano dict mutabili trattenuti
attraverso una sincronizzazione. Un branch di stato con checkout separato riduce
la commistione col codice e i trigger, ma non risolve conflitti semantici e non
rende privato lo stato. Un repository privato separato cambia permessi e recovery:
è un'opzione P2 da progettare, non un prerequisito della correzione P0.

Recovery progettato: fermare writer, annotare SHA corrente, verificare manifest,
hash, schema, cifratura, run/base SHA; confrontare con stato remoto recente;
unire ricevute monotone e alias senza eliminazioni; **non unire ciecamente le
iscrizioni**, perché resusciterebbe disiscritti. Produrre dry-run senza invii e
pubblicare solo sulla revisione attesa. L'artifact di tre giorni non garantisce
upload dopo SIGKILL o hard timeout e non contiene gli esiti rimasti in memoria.

## 12. Monitoring esterno: progetto e condizioni di attivazione

Un watchdog Actions separato è indipendente dal processo bot ma dipende ancora
da GitHub. Non può garantire allarme durante un outage comune del provider.
Non deve chiamare getUpdates/deleteWebhook o modificare gli archivi applicativi.
Il solo stato `in_progress` di una run può essere setup o processo bloccato.

Protocollo proposto: il solo bot pubblica un piccolo heartbeat operativo con
run ID, timestamp, progressi event loop/Telegram, politica corrente, tentativi e
successi distinti Albo/News, errori consecutivi e successo del checkpoint remoto.
Niente chat ID, titoli, URL Halley o errori grezzi. Il watchdog legge quel segnale
e gli step Actions; non usa `last_check.txt` come successo e non confonde un
controllo senza consegne con mancata attività.

| Stato | Evidenza richiesta | Soglia iniziale proposta e falsi positivi |
|---|---|---|
| Processo non attivo | Nessuno step bot attivo + heartbeat scaduto | Due osservazioni a distanza >=10 min; tolleranza iniziale 20 min di staffetta. Ritardi GitHub restano allarmi di disponibilità, non crash certi |
| Event loop non fresco | Step attivo ma heartbeat assente | Heartbeat previsto ogni 5 min, stale dopo 15; Git lento/rete GitHub possono impedire pubblicazione: esito 'processo o persistenza non osservabile' |
| Polling fermo | Fascia automatica abilitata ma nessun nuovo tentativo | Intervallo15 + budget ciclo definito + tolleranza; finché non è fissato il budget massimo del ciclo non chiamarlo matematicamente morto |
| Halley ripetutamente fallita | Tentativi freschi, almeno3 acquisizioni fallite e nessun successo recente | Distinguere lista/dettagli/invii; non basta il vecchio bool ok. Niente alert di polling atteso fra23 e07 |
| Telegram non raggiungibile | Event loop fresco ma getUpdates fallisce ripetutamente | Non attribuire il guasto a Halley; il watchdog non può usare solo Telegram come unico canale di allarme |

Anti-spam: ledger separato dal main del bot, incidente identificato da categoria
e sorgente; allarme dopo due osservazioni concordi, un solo invio iniziale,
promemoria non più di uno ogni sei ore, recovery dopo due osservazioni sane.
Il ledger registra stato di invio confermato/sconosciuto: un timeout dell'alert
ha anch'esso ambiguità. Nessun recupero automatico di archivi o rilancio di notifiche.

**Decisione:** progettare il watchdog, non distribuirlo in questa prima patch.
Manca ancora un heartbeat remoto affidabile, il vecchio ok può nascondere
acquisizioni parziali e introdurre subito un secondo writer su main creerebbe
nuove race. Una workflow che osservasse soltanto il verde Actions ripeterebbe
il difetto concettuale appena trovato. Implementazione successiva su branch/ledger
separato dopo test di telemetry e incidenti; nessuna infrastruttura a pagamento
necessaria, ma indipendenza da GitHub non ottenibile con il solo GitHub.

## 13. Test: copertura reale e lacune

| File | Test iniziali | Categoria prevalente |
|---|---:|---|
| test_safety.py | 25 | Unit/regressione; cicli e shutdown simulati |
| test_attachment_primitives.py | 21 | Integrazione HTTP/spool simulata e regressione |
| test_search_attachments.py | 48 | Unit e integrazione handler→source→Telegram simulato |
| test_search_detail_advisory.py | 5 | Unit/regressione markup |
| test_admin_health_edges.py | 5 | Unit stato runtime |
| test_admin_status.py | 11 | Unit/integrazione locale auth, cifratura e report |
| test_admin_status_edges.py | 9 | Regres­sione schema/auth e monitor |
| test_git_persistence.py | 1 | Integrazione reale con remote bare temporaneo |
| test_incident_repair.py | 3 | Forensic su commit storici e idempotenza |
| test_public_albo_archive.py | 6 skip | Legacy API assente, non verificata |
| test_telegram_nontext.py | 1 | Integrazione simulata replay/polling |
| test_workflow.py | 2 | Controlli statici YAML |

Mancano nella baseline system test temporali di 48h, crash a metà batch News,
crash a metà fanout, latenza/fallimento Git durante invio, timeout Telegram ambiguo,
recovery da artifact con remoto più recente, race manuale/automatico e prova di
segnali OS reali. Il test SIGTERM esistente registra un callback simulato, usa
worker finti e flush istantaneo: non prova che un Git bloccato chiuda in120s.
Il test Git reale copre detached HEAD e cambiamenti remoti non conflittuali,
non merge semantico di cronologie cifrate.

Nuove prove prioritarie: ritardi0/5/30/60/180min su24/48h, pending sostituita,
arrivi durante active, setup lungo, dispatch, crash e transizioni07/23/DST;
nessun bootstrap automatico notturno; comandi manuali invariati; stop sotto
SIGINT/SIGTERM; snapshot incompleto senza notify; dettagli falliti senza falso
successo; checkpoint News conservato dopo cancellazione e push fallito che
impedisce il successivo invio. I test non usano token reali né inviano messaggi.

## 14. Revisione della revisione precedente

PROJECT_REVIEW aveva correttamente distinto archivio, osservazione, baseline,
destinatari e ricevute; riconosciuto limiti di uptime, checkpoint News, Git lento
e assenza di exactly-once. Aveva però classificato il checkpoint News come
evoluzione generica anziché riprodurre la perdita durante uno stop programmato.

La frase esplicita «GitHub può ritardare i cron» si trova in HARDENING, mentre
PROJECT_REVIEW descrive concurrency e disponibilità a finestre. Il problema non
è non avere letto un avvertimento: è non avere composto **origine dell'evento ->
attesa -> selezione profilo -> vita del processo** in un modello temporale.
Le durate venivano chiamate «di giorno» e «di notte», assumendo implicitamente
che l'evento e l'esecuzione appartenessero alla stessa fascia. Nessuna timeline
confrontava il job reale col cron. I test statici conservavano addirittura le
stringhe che codificavano l'errore. La precedente revisione era descrittiva dei
componenti, insufficiente come revisione dei loro contratti e delle interazioni.

## 15. Evoluzione graduale e cose da non cambiare

1. Estrarre politica temporale pura e simulatore; lasciare ai workflow solo
   supervisione e budget. Collegare il runtime senza cambiare riconciliazione.
2. Correggere confini di completezza e checkpoint News con fixture e crash test.
3. Formalizzare StateStore e telemetria prima di spostare Git fuori dall'event loop.
4. Estrarre acquisition senza scritture, poi reconciliation pura, poi delivery:
   un confine e un contratto verificabile alla volta, con rollback indipendente.
5. Distribuire watchdog solo dopo prove di soglie, notte, recovery e anti-spam.

Da preservare: identità v2 e alias/repair; fusibile e baseline prudente;
criptazione; formati e chiavi delle ricevute; sessione Halley con riga effimera;
preflight/spool e formati non PDF; callback monouso/chat/TTL; /cerca readonly;
claim Telegram esistente; flush e artifact; GitHub Actions come hosting.
Non svuotare archivi, non backfillare notifiche, non alzare soglie per fare
passare un collaudo, non presentare gli skip come test verdi.

## 16. Assunzioni ancora non dimostrate

- Unicità e stabilità dei numeri del portale, integrità dei metadati e copertura
  di tutti i futuri markup Halley; fixture non sostituiscono un contratto del fornitore.
- Ordine delle News adatto all'early-stop e immutabilità delle vecchie News.
- Un solo writer esterno oltre al processo sotto concurrency; nessun lock Git
  impedisce automaticamente una riparazione o una sessione locale concorrente.
- Tempo massimo di rete/Git e ciclo, tempestività del segnale OS e completamento
  dell'upload artifact durante ogni forma di cancellazione del provider.
- Disponibilità del prossimo cron e del watchdog, affidabilità dell'orologio
  del runner, futura disponibilità dei documenti e raggiungibilità delle chat.
- Esito effettivo di una richiesta Telegram o Git quando la risposta è persa.

Non si può certificare «rischio zero di notifiche massive». Si può verificare
che le patch non cancellino lo stato, non alterino identità/baseline/fusibile e
non introducano invii nei test; i limiti residui devono rimanere espliciti.

## 17. Esito dell'implementazione e delle verifiche

Da compilare dopo le patch P0/P1 e il collaudo. Questa sezione vuota segnala
esplicitamente che l'analisi è stata scritta prima dell'implementazione.
