# Atlas

Un trader automatico su ETF, con Claude come analista e un guardiano in codice che decide cosa si può fare davvero. Gira gratis su GitHub, senza PC e senza server.

## Come funziona

Tre volte al giorno, durante la borsa di Francoforte, GitHub avvia Atlas.

1. **Dati.** Scarica i prezzi di 5 ETF in euro e calcola trend, RSI, volatilità e slancio.
2. **Guardiano.** Controlla la posizione aperta: stop loss, obiettivo, tempo massimo, freno di emergenza. Se una regola scatta, vende senza chiedere a nessuno.
3. **Macchina del tempo.** Per ogni ETF simula 5.000 futuri possibili con le stesse regole di stop e obiettivo, e rigioca le regole sui casi simili dell'ultimo anno. Il risultato va a Claude e al guardiano.
4. **Claude.** Una volta al giorno legge segnali, posizione e il suo diario, cerca al massimo 2 notizie e propone una decisione: compra, vendi o resta fermo.
5. **Esecuzione.** Il guardiano valida la proposta. Se passa, l'ordine va a Trading 212 e subito dopo viene piazzato uno stop loss sul broker, che protegge la posizione anche tra un giro e l'altro.
6. **Dashboard e notifiche.** Aggiorna la pagina web e ti scrive su Telegram.

## Le regole del guardiano

Sono in `bot/guardian.py` e `config.json`. Claude non può cambiarle.

| Regola | Valore |
|---|---|
| Capitale gestito | 100 € (il bot ignora il resto del conto) |
| Posizioni aperte | massimo 1 |
| Acquisti al giorno | massimo 1 |
| Stop loss | −5% dall'ingresso, poi segue il prezzo a −6% dal massimo |
| Obiettivo | +12% |
| Durata massima | 30 giorni |
| Filtro trend | prezzo sopra la media a 50 giorni, RSI sotto 70 |
| Fiducia minima di Claude | 65% |
| Pausa dopo uno stop | 3 giorni sullo stesso ETF |
| Freno di emergenza | sotto 80 € il bot smette di comprare |
| Macchina del tempo | rendimento atteso simulato almeno +0,5% e stop in massimo il 45% dei futuri |

## Le tre modalità

| Modalità | Quando | Soldi |
|---|---|---|
| Simulazione | nessuna chiave Trading 212 | finti, calcolati dal bot |
| Demo | chiavi del conto demo Trading 212 | finti, ma ordini veri sul conto demo |
| Live | `T212_ENV=live` **e** `LIVE_CONFIRM=SI_HO_CAPITO` | veri |

Senza entrambe le variabili, la modalità live non parte.

## Setup dal telefono

### 1. Chiave Anthropic (il cervello)
1. Vai su platform.claude.com, crea un account e aggiungi 5 $ di credito.
2. In **Settings > Limits** imposta un limite di spesa mensile, per esempio 5 $.
3. In **API Keys** crea una chiave e copiala.

Costo stimato: circa 4–5 centesimi per analisi, quindi circa 1 $ al mese con un’analisi per giorno di borsa.

### 2. Trading 212 demo (il broker)
1. Apri un conto **Invest** su Trading 212 e passa al conto **Demo** (Practice).
2. In **Impostazioni > API (Beta)** genera una chiave con i permessi: account, portfolio, metadata, history, orders read, orders execute.
3. Copia **API Key** e **API Secret**.

Puoi saltare questo passo all'inizio: Atlas parte in simulazione.

### 3. Telegram (facoltativo)
1. Scrivi a **@BotFather**, invia `/newbot` e copia il token.
2. Scrivi un messaggio qualsiasi al tuo nuovo bot.
3. Apri `https://api.telegram.org/bot<TOKEN>/getUpdates` nel browser e copia il numero dopo `"chat":{"id":`.

### 4. GitHub
1. Nel repository, apri **Settings > Secrets and variables > Actions**.
2. Scheda **Secrets**, aggiungi: `ANTHROPIC_API_KEY`, `T212_API_KEY`, `T212_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Scheda **Variables**, aggiungi `T212_ENV` = `demo`.
4. **Settings > Pages**: Source **Deploy from a branch**, branch `main`, cartella `/docs`.
5. **Actions > Atlas > Run workflow** per il primo giro.

La dashboard sarà su https://ryckdossantos.github.io/ATLAS-trade-v.1/.

## Comandi d'emergenza

| Cosa vuoi | Cosa fai |
|---|---|
| Fermare tutto subito | Variabile `ATLAS_PAUSED` = `1` |
| Ripartire | Cancella `ATLAS_PAUSED` |
| Sbloccare dopo il freno di emergenza | In `state/state.json` metti `"halted": false` |
| Ricominciare da zero | Cancella `state/state.json` |

Se la pausa scatta con una posizione aperta, lo stop loss resta attivo sul broker.

## Prima di passare ai soldi veri

- Almeno 2–3 mesi in demo.
- Atlas batte l'indice comprato e tenuto, al netto dei costi di Claude. Se non lo batte, compra l'ETF e tienilo.
- Hai verificato la policy di AIG sugli investimenti personali.
- Sai che Trading 212 è un broker estero: le plusvalenze (26%) le dichiari tu, e serve il quadro RW.
- Il repository pubblico mostra le operazioni a chiunque abbia il link. Valuta un repository privato.

Strumento sperimentale. Non è consulenza finanziaria.

## Struttura

```
bot/guardian.py   regole di rischio, senza rete, testate
bot/brain.py      Claude: ricerca notizie e decisione in formato rigido
bot/t212.py       client Trading 212
bot/market.py     dati Yahoo Finance e indicatori
bot/timemachine.py simulazione dei futuri possibili e verifica storica
bot/run.py        il giro completo
docs/index.html   dashboard
tests/            test (pytest)
```
