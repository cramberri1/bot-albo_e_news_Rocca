# Allegati e polling: aggiornamento del 05/10/2026

Un RTF valido inizia normalmente con `{\rtf1`: il precedente controllo
considerava ogni risposta che iniziava con `{` una pagina di errore. Questo
impediva di scaricare RTF autentici. Il controllo ora distingue la firma RTF
dalle risposte HTML/JSON del portale, senza modificare i byte del documento.

## Formati e integrità

I file vengono inviati come documenti originali, senza conversione. Non c'è
una lista chiusa di estensioni consentite. Il nome originale ha precedenza;
se manca un'estensione, il bot cerca il nome nella risposta HTTP, nell'URL,
nel tipo MIME o nelle firme riconosciute. Il ripiego è `.bin`, senza
inventare un PDF. I nomi lunghi conservano l'estensione e i percorsi remoti
non diventano percorsi locali.

Sono riconosciuti RTF, PDF, contenitori P7M/P7S, formati Office e OpenDocument,
archivi, immagini, CSV/TSV, TXT, XML, JSON/GeoJSON e messaggi EML. I documenti
HTML richiedono un nome HTML esplicito e `Content-Disposition: attachment`.
Le pagine HTML inattese, i comuni errori JSON/XML e i download parziali
vengono rifiutati. Il controllo dei contenuti serve a distinguere le risposte
del portale: non è un antivirus né una validazione completa del formato.

Restano i limiti configurabili: 45 MiB per file, 200 MiB per atto e 512 MiB
per lo spazio temporaneo complessivo. Il download usa streaming, controlla
la lunghezza dichiarata quando confrontabile e registra SHA-256. Prima
dell'invio vengono verificati disponibilità e integrità di tutti gli
allegati attesi. Più di dieci allegati vengono inviati in sequenza; le
didascalie sono limitate anche quando l'oggetto dell'atto è molto lungo.

Per `.p7m` la didascalia rimanda ai
[software di verifica indicati da AgID](https://www.agid.gov.it/en/node/1534).
Nell'Albo si tratta normalmente di una busta di firma digitale: l'estensione
da sola non significa che il documento sia cifrato. Un programma appropriato
consente di verificare la firma e recuperare il documento contenuto. Il bot
conserva la busta originale e non dichiara valida una firma che non ha verificato.

## Altre correzioni

- Le iscrizioni non vengono confermate se il salvataggio persistente fallisce.
  La modifica locale resta disponibile per il tentativo successivo.
- `/atti` salva la ricevuta dopo ogni atto completo, prima di inviare il
  successivo. Un errore di persistenza sospende gli altri invii.
- Il primo `/controlla` inizializza la baseline News, se assente, evitando
  l'invio dello storico. Una prima pagina News vuota ma con paginazione
  dichiarata viene trattata come incompleta.
- La fascia automatica predefinita è **07:00 incluse–20:00 escluse,
  Europe/Rome**, con pausa di 15 minuti dopo il ciclo. Un lavoro già iniziato
  può terminare dopo le 20:00. I comandi Telegram restano indipendenti.

## Collaudo e limiti operativi

Il 05/10 sono stati scaricati dal sito pubblico del Comune i due RTF
dell'atto relativo all'aggiudicazione del servizio mensa: 327.978 byte
ciascuno, con firma RTF e verifica SHA-256 riuscita. Non sono stati inviati
messaggi Telegram durante il collaudo. I test automatici coprono anche
risposte MIME generiche, byte invariati, file testuali, errori del portale,
download incompleti, cleanup, P7M, didascalie lunghe, più di dieci allegati,
stato dei comandi e limiti orari con cambio dell'ora.

GitHub Actions esegue processi a durata limitata: il bot risponde ai comandi
solo mentre una run è in esecuzione. Un comando Telegram non avvia una run.
Il cron e la coda Actions non garantiscono disponibilità continua 24/7.
Resta possibile un reinvio se il processo si interrompe dopo una consegna
Telegram ma prima del checkpoint; gli invii parziali non vengono segnati
come completati. La persistenza Git sincrona e la chiusura dei worker
restano oggetto della revisione architetturale precedente.
