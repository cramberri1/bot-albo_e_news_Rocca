# Pannello amministratore

Nella **chat privata del bot**, invia `/status`. Il bot usa gli ID già
configurati in `CHAT_IDS`: non servono nuovi secret. L'ID dell'utente che
scrive deve essere autorizzato e coincidere con quello della chat privata.
Un gruppo configurato come destinatario continua a ricevere notifiche, ma
non autorizza i suoi membri a usare `/status` o `/controlla` nel gruppo.

`/status` risponde con un unico messaggio di testo, senza nomi, identificativi
Telegram, elenchi di utenti, token o dettagli grezzi delle eccezioni.

## Cosa mostra

| Informazione | Significato |
|---|---|
| Ora e durata del processo | Snapshot al momento della risposta, con orari esplicitamente in UTC. La durata riparte al riavvio. |
| Pausa tra cicli | Attesa configurata dopo il completamento del ciclo; la durata del controllo si aggiunge a questa pausa. |
| Polling automatico | Fascia predefinita 07:00–20:00 Europe/Rome e abilitazione attuale secondo l'ora reale. La sospensione fuori fascia non sospende Telegram o i comandi manuali. |
| Iscrizioni Albo e News | Chat presenti nei rispettivi file di iscrizione, senza aggiungere implicitamente gli amministratori. |
| Chat iscritte uniche / solo Albo / solo News / entrambe | Unione, differenze e intersezione dei due elenchi, senza duplicati. Un gruppo conta come una chat: non è un conteggio delle persone. |
| Destinatari | Unione degli iscritti e delle destinazioni `CHAT_IDS`. Gli amministratori presenti anche tra gli iscritti si contano una sola volta. Non è una verifica della raggiungibilità su Telegram. |
| Atti e news conservati | Record nello stato locale, non pubblicazioni attualmente online. Gli atti marcati `notified` includono la baseline storica silenziosa. |
| Atti non marcati come notificati | Record con `notified=False`: non sono automaticamente nuovi atti da inviare. Un flag assente nei dati legacy conta invece come baseline. |
| Consegne pendenti | Numero di record con `delivery_pending`, con distinzione delle revisioni Albo. Non è il numero di destinatari ancora da raggiungere. |
| Stato Albo e News | `non ancora verificato`, `in corso`, `completato`, `incompleto o bloccato`, `errore` oppure `interrotto`, separatamente per fonte. |
| Dettagli non verificati nel ciclo | Acquisizioni Albo selezionate ma non completate: rendono il ciclo incompleto anche se nessun invio Telegram è fallito. |
| Ultimo tentativo e successo | Dati dei controlli effettuati dal processo corrente. Solo un risultato `ok=True` aggiorna il successo; un errore conserva la data del successo precedente. |
| Durata e contatori del ciclo | Durata dell'ultimo tentativo o tempo trascorso se in corso; nuovi, revisioni e consegne fallite secondo il risultato restituito dal controllo. Contatori assenti o non validi risultano non disponibili. |
| Ultimo tentativo Albo salvato | Timestamp storico di `last_check.txt`, aggiornato dopo il fetch Albo. Non dimostra il successo e non viene aggiornato dai blocchi che avvengono prima del fetch. Nel repository può essere meno recente: il commit è limitato a uno al giorno. |
| Protezioni | Presenza della chiave di cifratura, inizializzazione identità v2, presenza di un blocco Albo salvato e limiti del ricontrollo revisioni. Non è un nuovo collaudo delle protezioni. |

Un file di iscrizioni assente equivale a nessuna iscrizione esplicita; gli
amministratori restano destinatari. Un file illeggibile, uno schema non
valido o una chiave errata producono **dati non disponibili**, senza
inventare uno zero. Un problema a un archivio non nasconde gli altri dati.

## Azioni e limiti

- Ripeti `/status` per aggiornare lo snapshot. Il comando non effettua
  richieste al portale, migrazioni, scritture di archivi, iscrizioni o
  cronologie di consegna. La normale elaborazione Telegram continua a
  salvare claim e offset degli aggiornamenti, come per gli altri comandi.
- `/controlla`, anch'esso riservato all'amministratore in privato, esegue il
  normale controllo produttivo: può inviare nuove notifiche e ritentare
  quelle pendenti. Non è un semplice aggiornamento del pannello.
- Il bot deve essere attivo per rispondere. Un comando ricevuto mentre è
  spento attende un successivo avvio secondo la gestione Telegram esistente.
  Il pannello non è un monitor esterno e non garantisce disponibilità 24/7.
- I comandi Telegram sono elaborati in sequenza: una richiesta di allegati
  o un `/controlla` precedente può ritardare la risposta. Le operazioni Git
  sincrone possono aggiungere attesa anche durante i controlli periodici.
- Gli esiti in memoria ripartono a ogni avvio. Dopo un riavvio il timestamp
  storico rimane visibile, ma lo stato è non verificato fino al primo
  controllo. I tentativi di costruzione della baseline all'avvio precedono
  questi controlli e non costituiscono un successo osservato dal pannello.
- Il codice aggiornato viene caricato dal prossimo processo: un runner già
  avviato continua con il codice che aveva caricato. Nessuna nuova notifica
  automatica è introdotta da questa funzione.

## Verifica

I test `test_admin_status.py`, `test_admin_status_edges.py` e
`test_admin_health_edges.py` usano dati sintetici e bot simulati. Coprono
autorizzazioni, conteggi senza duplicati, cifratura, dati corrotti, assenza di
scritture applicative, compatibilità legacy e transizioni dei controlli,
inclusi errori e cancellazioni. La suite completa conserva le verifiche
precedenti su identità, consegne, allegati e polling.
