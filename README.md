# Juventus Calendar

Calendario iCalendar pubblico e sottoscrivibile con tutte le partite della Prima Squadra maschile della Juventus.

- Pagina: <https://dizzle0987.github.io/juventus-calendar/>
- Feed: <https://dizzle0987.github.io/juventus-calendar/calendar.ics>
- iPhone: <webcal://dizzle0987.github.io/juventus-calendar/calendar.ics>

Il feed comprende Serie A, Coppa Italia, Supercoppa Italiana, Champions League, Europa League, Conference League, Supercoppa UEFA, Coppa del Mondo per Club FIFA, Coppa Intercontinentale FIFA, amichevoli, tournée e partite di preparazione. Serie B è monitorata come fallback; la UEFA–CONMEBOL Club Challenge viene aggiunta soltanto se una fonte strutturata pubblica una partita della Juventus in quella competizione. Include inoltre sorteggi ufficiali e pubblicazioni di calendari o tabelloni. Juventus Women, Next Gen e formazioni giovanili sono escluse automaticamente, a meno che un evento venga aggiunto intenzionalmente in `data/manual_events.json`.

## Iscrizione

Un calendario sottoscritto si aggiorna; un file importato una sola volta no. Su iPhone apri la pagina in Safari e usa **Iscriviti al calendario**. In alternativa copia il link HTTPS e scegli **Calendario → Calendari → Aggiungi calendario → Aggiungi calendario con iscrizione**.

In Google Calendar, da computer scegli **Altri calendari → + → Da URL** e incolla il link HTTPS. Il feed si sincronizzerà anche sui dispositivi Android associati. La frequenza effettiva dipende dal client e normalmente iPhone non mostra una notifica push specifica per ogni modifica dell'evento.

## Fonti e priorità

Il progetto non usa SofaScore.

1. L'endpoint JSON e la pagina ufficiale Juventus sono la fonte principale per scoprire incontri, competizione, stadio e dettagli descrittivi.
2. Gli endpoint JSON ESPN coprono Serie A, coppe, tornei UEFA/FIFA e amichevoli mancanti.
3. TheSportsDB offre un controllo strutturato aggiuntivo, utile soprattutto per amichevoli e tournée.
4. DAZN, Sky Sport/NOW, Mediaset Infinity e Prime Video vengono consultati per dichiarazioni esplicite di data, ora e copertura italiana.
5. Gazzetta dello Sport è il fallback editoriale per un orario esplicito.
6. Gli eventi manuali hanno precedenza finale.

Un palinsesto TV può soltanto arricchire una partita già scoperta: non crea incontri. Per l'orario vale la priorità broadcaster italiano → fonte editoriale → Juventus → API sportiva. Nessun dato viene inventato; se l'orario non è confermato, l'evento è giornaliero.

## Integrità degli aggiornamenti

- Gli UID derivano da stagione, squadre e competizione. Data, ora, stadio, turno e TV non ne fanno parte.
- Le varianti `Juventus`, `Juventus FC`, `Juventus Football Club` e `Juve` sono equivalenti.
- `LAST-MODIFIED` viene conservato quando un evento non cambia; `SEQUENCE` aumenta quando cambia.
- Ogni evento usa `Europe/Rome` e include un promemoria 2 ore e 30 minuti prima.
- Tutto il parsing, la fusione e la generazione avvengono prima delle scritture atomiche.
- Se nessuna fonte di scoperta restituisce partite valide, il comando fallisce senza sostituire gli ultimi output pubblicati.

## Esecuzione locale

Richiede Python 3.12 o successivo.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
pytest -q
python update_calendar.py
```

Gli output sono `calendar.ics` e `data/events.json`.

## Sorteggi, calendari e tabelloni

`data/calendar_events.json` è il fallback strutturato per date ufficiali che non sono partite. Il generatore cerca inoltre automaticamente i sorteggi nelle pagine ufficiali di Champions League, Europa League e Conference League e gli annunci Lega Serie A relativi a sorteggi, calendari e tabelloni.

Un evento con `requires_participation: true` appare soltanto se la Juventus risulta iscritta alla competizione. `participation_confirmed: true` consente di confermare manualmente la partecipazione prima che le fonti pubblichino la prima partita. Vengono accettate solo date esplicite; in assenza di un orario dichiarato l'evento resta giornaliero. Gli eventi scoperti vengono conservati anche quando la pagina ufficiale passa al sorteggio della fase successiva.

## Eventi manuali

`data/manual_events.json` contiene un esempio disabilitato. I campi obbligatori sono:

- `home_team`, `away_team`, `competition`;
- `start`, come timestamp ISO 8601 con offset oppure data `YYYY-MM-DD`.

Sono supportati `id`/`uid`, `round`, `venue`, `location`, `neutral`, `status`, `source_url`, `time_source`, `time_source_url`, `broadcast_it` e `broadcast_source_url`. Per correggere un evento automatico usa le stesse squadre e una data vicina; i valori manuali sovrascrivono quelli recuperati senza creare un duplicato.

## Automazione

`.github/workflows/update.yml` gira ogni 6 ore e supporta l'avvio manuale. Impedisce esecuzioni sovrapposte, installa le dipendenze, esegue tutti i test, genera gli output e committa soltanto se cambiano.

`.github/workflows/pages.yml` pubblica `index.html`, `calendar.ics` e `data/events.json` dal branch `main` come artifact GitHub Pages con permessi minimi e concorrenza controllata.

## Sviluppo e sicurezza

Consulta [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) e [SECURITY.md](SECURITY.md). Il progetto è distribuito con licenza MIT.
