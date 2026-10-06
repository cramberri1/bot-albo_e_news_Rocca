# Recupero limitato dopo una run interrotta

La run `37356019888` ha superato i 236 test, avviato il bot alle 18:27 UTC e
ricevuto un segnale di arresto dal runner alle 20:01 UTC. Il polling automatico
era già sospeso secondo la fascia 07:00–20:00 Europe/Rome; prima dell'arresto
non risulta un'eccezione applicativa. La perdita del runner può saltare anche
gli step finali: `always()` non può ricreare una macchina già persa.

Il timeout usa `--preserve-status`: una chiusura normale del bot resta verde,
un errore di flush conserva l'exit code di errore e un processo ucciso dopo la
grazia resta fallito. Non viene più convertito indiscriminatamente il codice
124 del timeout in successo, perché poteva nascondere un errore dopo SIGINT.

## Checkpoint e chiusura del processo

Ogni transazione Git ha un budget di 30 secondi complessivi, condiviso da
discovery, commit, push e retry. Un rebase interrotto ha fino a 3 secondi
aggiuntivi per l'abort. I processi POSIX Git hanno un gruppo separato e, al
timeout, viene terminato anche il relativo helper di rete.

Alla prima richiesta di arresto viene fissata una deadline comune: i checkpoint
dei worker possono usare i primi 40 secondi, il flush finale può usare il
budget restante entro 60 secondi. Altri segnali non estendono questi limiti.
Il drain cooperativo è di 20 secondi; un'operazione sincrona in corso resta
soggetta alla deadline Git comune. Gli errori dei worker durante il drain e
quelli del flush non vengono trasformati in una chiusura riuscita.

Albo e News salvano l'intenzione di inviare prima del primo destinatario e una
ricevuta dopo ciascun destinatario completato. Un errore di checkpoint ferma
gli invii successivi. Nessuna ricevuta completa viene assegnata a un atto
con allegati inviati solo in parte. Per le revisioni dell'Albo, una sequenza
persistente distingue anche A→B→A e rimane stabile nei retry della stessa
versione; le ricevute delle versioni precedenti restano compatibili.

Il bootstrap usa marker espliciti per distinguere una baseline confermata
dalla cache manuale. I database legacy già inizializzati restano validi.
Una baseline non confermata viene ritentata sullo stesso snapshot; un archivio
corrotto o una lettura incompleta non provoca un reset silenzioso. Anche
`/controlla` passa da questi controlli per entrambe le fonti.

## Contratto di recupero

`albo_recovery.yml` osserva la conclusione di **Albo Pretorio Check** su `main`.
Per una run **scheduled**, al primo tentativo, con esito `failure` oppure
`timed_out`, verifica lo stato attuale tramite l'API e può richiedere **un solo
nuovo worker**, di durata ordinaria 18.000 secondi.

- Le run `workflow_dispatch`, incluse quelle richieste dal recupero, non
  generano ulteriori retry. Anche le riesecuzioni della stessa run e gli esiti
  `cancelled` sono esclusi: una cancellazione deliberata resta rispettata.
- Il recupero controlla se il workflow è stato disabilitato, se l'utente ha già
  rieseguito la run, se esiste un worker attivo/in coda o una run successiva
  terminata correttamente. In questi casi non aggiunge un nuovo avvio.
- Ogni recupero porta `recovery_from_run_id` e il titolo `Albo recovery #ID`.
  Il controllo della cronologia evita una seconda richiesta per la stessa run,
  anche se il primo recupero è a sua volta fallito.
- Il workflow di recupero serializza le decisioni. La concurrency del bot
  conserva separatamente un solo writer; un cron arrivato tra controllo API e
  dispatch può comunque aggiungere o sostituire una pending. Nessun worker in
  corso viene cancellato dal recupero.
- Le richieste API hanno timeout e un limite di cronologia (300 run recenti).
  Un errore o una cronologia incompleta fa fallire il recupero. Il dispatch non
  viene ritentato dopo un timeout ambiguo: GitHub potrebbe averlo già accettato.

L'esito originale **non viene trasformato in successo**. Il riepilogo del
recupero indica una richiesta o il motivo per cui non è stata necessaria;
non è una prova che Telegram abbia risposto. La nuova run riparte dall'ultimo
stato salvato su Git, senza ripristinare automaticamente artifact o file del
runner perso. I marker di baseline fanno parte della persistenza finale e
degli artifact, insieme agli altri file di stato.

## Avvii manuali e collaudo breve

L'input facoltativo `run_seconds` accetta solo cifre, tra **60 e 18.000**, con
default **18.000**. Per collaudare avvio, shutdown e persistenza si può avviare
una run manuale di **120 secondi**, poi richiedere una run ordinaria.
La validazione avviene prima del comando di timeout; l'input non viene
interpolato in codice shell. Tutte le durate mantengono la stessa politica
07:00–20:00 e i comandi Telegram restano indipendenti durante il processo.

Una run manuale breve non rappresenta un servizio continuo e non viene
riavviata automaticamente. L'evento pubblico di una run dispatch non distingue
in modo affidabile tutte le intenzioni dell'operatore; perciò il recupero
automatico è limitato agli avvii scheduled. Per fermare stabilmente il servizio
si possono disabilitare il workflow del bot e quello di recupero.

## Sicurezza e limiti

Il workflow di recupero usa `contents: read` e `actions: write`, non il token del
bot o la chiave dello stato. Esegue soltanto codice del `main` del repository,
con checkout senza credenziali persistite, senza cache e senza scaricare o
eseguire artifact della run precedente. Lo script verifica repository, branch,
workflow e tentativo; usa URL API fissi, non URL contenuti nel webhook.

GitHub richiede che il workflow `workflow_run` sia sul branch predefinito e
avverte dei privilegi di questo evento; il checkout di codice affidabile è
quindi essenziale. Il token automatico può generare un `workflow_dispatch`;
quel dispatch richiede `actions: write`. Fonti ufficiali:
[eventi workflow_run](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
[trigger con GITHUB_TOKEN](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow),
[API dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[sicurezza dei workflow](https://docs.github.com/en/actions/reference/security/secure-use).

Il recupero dipende ancora da consegna degli eventi, disponibilità dell'API e
assegnazione di un altro runner. Non garantisce disponibilità 24/7 e non recupera
le scritture locali perse prima del push. Errori applicativi deterministici
ricevono al massimo questo tentativo, senza un ciclo infinito di riavvii.
