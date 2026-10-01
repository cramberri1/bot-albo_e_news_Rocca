# Revisione concettuale del progetto

Revisione del 30 settembre 2026, completata il 1° ottobre 2026.
Esame del codice, dei workflow e della documentazione;
non comprende lettura delle iscrizioni reali, nuovi invii Telegram o una prova
di disponibilità continuativa del servizio.

## Valutazione

Il progetto offre un servizio di notifiche comunali con uno storico di metadati
consultabile. La sua qualità dipende soprattutto dalla corretta identità degli
atti, dalla prudenza davanti a una sorgente incompleta e dalla continuità dello
stato delle consegne. Identità v2, alias storici, fusibile contro le raffiche,
controlli sugli allegati e checkpoint sono quindi parti centrali da conservare.

La principale lacuna per l'amministratore era la visibilità: il vecchio
`/status` riportava pochi totali e un timestamp che poteva essere interpretato
come prova di un controllo riuscito. L'estensione di questa revisione rende
consultabili iscrizioni, destinatari, stato dei cicli e arretrati, con nomi che
ne chiariscono il significato. Questo è un miglioramento operativo concreto;
le ulteriori modifiche architetturali elencate sotto restano proposte.

## Concetti da tenere separati

| Concetto | Significato nel progetto | Implicazione per lo stato del bot |
|---|---|---|
| Atto conservato | Record nello storico locale, con identità canonica e metadati disponibili | Il totale include anche atti non più online e cache tecniche |
| Atto osservato online | Atto presente nello snapshot di una specifica acquisizione Halley | Il totale storico non misura quanti atti siano pubblicati adesso |
| Notificato/baseline | Record che non deve essere trattato come nuova pubblicazione globale | Può essere una baseline silenziosa; non prova una consegna a ogni iscritto |
| Revisione | Modifica sostanziale accertata rispetto a uno snapshot precedente | Va distinta da un nuovo atto e da una variazione cosmetica |
| Consegna | Invio di una versione dell'atto a una specifica chat | La cronologia per destinatario serve ai retry e alla deduplicazione |
| Iscrizione | Chat presente nell'elenco Albo, News o entrambi | Si contano chat; un gruppo non equivale a una singola persona |
| Destinatario | Unione delle chat iscritte e delle destinazioni amministrative configurate | Il numero può superare quello degli iscritti; l'unione elimina i doppioni |
| Ciclo riuscito | Esecuzione terminata con `ok=True` | È un esito del ciclo, non una certificazione dell'intera storia del servizio |

La ricerca recupera documenti dal portale quando ancora disponibili e
identificabili con sufficiente certezza. Il repository conserva i metadati e
gli hash; lo spool dei documenti è temporaneo. La disponibilità futura di un
allegato storico non è quindi garantita dalla sua presenza nei risultati.

Riferimenti: `load_db`, `get_all_recipients`, `get_all_news_recipients`,
`run_check`, `run_check_news` in [bot.py](../bot.py),
[albo_identity.py](../albo_identity.py) e
[allegati da ricerca](SEARCH_ATTACHMENTS.md).

## Controllo amministrativo aggiunto

`/status`, nella chat privata con il bot, presenta aggregati relativi alle due
iscrizioni e alla loro sovrapposizione, destinatari effettivi configurati,
archivi e record con consegne pendenti. Non elenca gli identificativi Telegram.
Gli iscritti vengono letti dagli elenchi espliciti: il fallback storico che
include gli amministratori quando manca il file Albo non gonfia questo dato.
I destinatari comprendono invece correttamente le destinazioni amministrative.

Lo stato del processo distingue controlli in corso, completati, incompleti o
bloccati, errori e interruzioni. Il monitor conserva l'ultimo successo del
processo corrente anche se un tentativo successivo fallisce. Dopo un riavvio,
un successo non ancora osservato viene mostrato come non disponibile.
`last_check.txt` indica un tentativo Albo salvato: viene aggiornato anche se
l'acquisizione non riesce e non dimostra la salute della sorgente News.

Un file illeggibile produce «dati non disponibili» per la sezione interessata,
invece di inventare un conteggio zero o nascondere le altre sezioni. La presenza
di un blocco Albo registrato viene mostrata senza copiare il contenuto grezzo
della diagnostica, che potrebbe contenere dettagli della sorgente.

L'accesso a `/status` e `/controlla` richiede un amministratore riconosciuto
nella propria chat privata; un gruppo configurato come destinazione non diventa
una console amministrativa condivisa. `/status` legge lo stato disponibile;
`/controlla` avvia acquisizioni e può generare le normali notifiche agli iscritti.

Il report non modifica archivi, iscrizioni o cronologie delle consegne e non
avvia un controllo della sorgente. Il polling Telegram mantiene comunque il
normale checkpoint di presa in carico ed elaborazione dell'update, anche per
questo comando. Questa persistenza tecnica è parte della deduplicazione.

Il monitor in memoria non costituisce uno storico dei controlli tra runner e
non misura da solo la disponibilità del servizio. Inoltre, «atti con consegne
pendenti» conta record, mentre «consegne fallite nel ciclo» conta tentativi
falliti verso destinatari: le due quantità hanno unità differenti.

## Punti solidi da preservare

- **Identità indipendente dalla sessione:** `num_riga` e URL Halley non sono
  chiavi permanenti. Gli alias conservano i collegamenti con le consegne storiche.
- **Prudenza sull'acquisizione:** anomalie importanti dell'elenco sospendono gli
  invii automatici. Il recupero manuale rifiuta identità ambigue e gruppi di
  allegati incompleti prima di iniziare l'invio.
- **Separazione delle richieste manuali:** il recupero da `/cerca` non deve
  marcare notifiche automatiche come eseguite o sopprimere revisioni future.
- **Stato persistente protetto:** scritture atomiche, lettura conservativa dei
  dati corrotti, cifratura dei file con identificativi Telegram e gestione
  esplicita dei fallimenti Git riducono i danni di un'interruzione.
- **Difese verificate:** le suite coprono identità, repair, callback, replay,
  sessioni Halley, allegati, cancellazione e isolamento dello stato. Il dettaglio
  delle verifiche precedenti è in [HARDENING.md](HARDENING.md) e
  [SEARCH_ATTACHMENTS.md](SEARCH_ATTACHMENTS.md).

Queste difese riducono errori concreti ma non rendono atomico l'invio Telegram
insieme al salvataggio Git. Una risposta HTTP ambigua o un arresto nel momento
sbagliato lasciano una finestra residua di duplicazione o consegna interrotta.

## Limiti operativi accertati

**Disponibilità a finestre.** Il workflow avvia processi di durata finita:
343 minuti di giorno, 55 minuti di notte e 10 minuti con avvio manuale. Gli
avvii notturni alle 00:07 e 03:37 lasciano intervalli intenzionali senza processo
fino al successivo avvio; anche il setup occupa tempo. Il bot risponde ai comandi
solo quando il processo sta elaborando gli update. La concurrency limita la
sovrapposizione dei runner, senza trasformare questo schema in un servizio
continuamente disponibile. Fonte: [albo_check.yml](../.github/workflows/albo_check.yml).

**Latenza della console.** Gli update Telegram vengono gestiti in sequenza.
Un comando lungo può trattenere `/status` in coda; anche Git è sincrono. Inoltre
il loop attende l'intervallo configurato dopo la conclusione del ciclo: «15
minuti» indica una pausa tra cicli, non un appuntamento a precisione assoluta.
Una risposta del bot conferma che il processo ha potuto elaborare quel comando,
non che tutti gli altri componenti fossero sani in ogni momento.

**Checkpoint diversi tra Albo e News.** L'Albo pubblica lo stato delle consegne
dopo ogni atto. Le News accumulano invece gli esiti e salvano cronologia e
archivio alla fine del lotto. Un arresto durante un lotto News ha quindi una
finestra di replay più ampia. Il retry deve continuare a usare la cronologia
dei destinatari già serviti.

**Storico parziale e politica News.** La ricerca usa i metadati disponibili:
alcuni record storici non hanno titolo. L'early-stop News presuppone che una
pagina interamente nota delimiti la parte utile della scansione; non offre un
monitoraggio delle revisioni di tutte le vecchie news. Inoltre una data News
assente o illeggibile viene considerata recente (`news_is_recent`), mentre
l'Albo tratta più prudentemente gli sconosciuti con data incerta. Sono scelte
diverse da conoscere, non misure equivalenti di completezza.

**Conservazione e recupero.** Cronologie per destinatario e stato vengono
conservati in Git; cifrare protegge la lettura ma non elimina le versioni
precedenti. La disiscrizione rimuove le iscrizioni, senza rappresentare una
richiesta di cancellazione dell'intera cronologia. La chiave Fernet è necessaria
al recupero dei file cifrati. L'artifact di recupero del workflow dura tre giorni:
serve una procedura utilizzabile prima della sua scadenza.

**Ambito delle verifiche.** I sei test legacy dell'archivio pubblico rimangono
saltati perché l'API corrispondente è assente. Non sono funzionalità verificate
né una prova di pubblicazione degli export. I test simulati riproducono i casi
noti; i collaudi live documentati fotografano singole esecuzioni, senza provare
la disponibilità futura del portale o di Telegram.

## Interventi successivi, in ordine di utilità

| Priorità | Intervento proposto | Criterio concreto di completamento |
|---|---|---|
| 1 | Definire la disponibilità attesa, soprattutto per i comandi amministrativi notturni | Orari comunicati coerenti con il workflow; se serve risposta continua, valutazione di un processo persistente con le stesse garanzie sullo stato |
| 1 | Preparare e provare una procedura di recupero | Ripristino isolato da stato e chiave custoditi, senza invii reali; passaggi chiari per fallimento Git, artifact e blocco del fusibile |
| 2 | Avvicinare i checkpoint News a quelli Albo | Test di arresto dopo una news, riavvio e retry ai soli destinatari mancanti; nessuna perdita delle consegne già confermate |
| 2 | Misurare la freschezza tra esecuzioni, se serve | Piccolo stato operativo con esiti e orari separati per sorgente, scritto solo nei punti significativi; un monitor esterno è necessario per segnalare un processo completamente fermo |
| 2 | Estrarre gradualmente responsabilità da `bot.py` | Moduli per sorgenti, persistenza e consegne con contratti testati; prima un confine alla volta, mantenendo formati e comportamento |
| 2 | Esplicitare conservazione e significato dei comandi | Documentazione coerente su storico parziale, disiscrizione, cronologie e recuperabilità dei documenti; eventuali cancellazioni progettate insieme alla deduplicazione |
| 3 | Rendere più riproducibili dipendenze e manutenzione CI | Versioni riproducibili e aggiornamenti verificati; includere `requirements-dev.txt` tra i percorsi che attivano la CI su push |

Nessuna delle proposte richiede di sostituire subito Git con un database o di
riscrivere lo scraper. La separazione introdotta per lo stato amministrativo
offre già un confine piccolo e verificabile. Un'ulteriore migrazione va motivata
da un limite misurato, per esempio tempi di risposta, dimensione delle cronologie
o requisito esplicito di disponibilità continua.
