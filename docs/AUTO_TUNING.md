# Ricerca forward e calibrazione

Trado usa un percorso di ricerca osservazionale: raccoglie outcome attribuiti
alla configurazione che ha generato il segnale e non promuove automaticamente
varianti nel trading operativo.

## Esperimento forward

File principali:

- `backend/app/engine/experiment.py`
- `backend/app/engine/experiment_outcomes.py`

`AppSetting("research_experiment")` identifica:

- `active_version`: versione della configurazione osservata;
- `assignment`: normalmente `champion`;
- `mode`: `frozen_observation`;
- `promotion_policy`: `manual`;
- `forward_gate_min_outcomes`: campione minimo prima di usare gli outcome
  forward nei gate empirici.
- `paper_candidate`: filtro solo paper per timeframe, direzione e, se
  configurato, `min_score_by_timeframe`. Un candidato attivo ma malformato
  blocca il segnale invece di renderlo idoneo al live.
- `forward_gates_shadow_only`: se esplicitamente attivo per una nuova versione,
  i gate Expected R e pre-TP1 restano osservativi anche dopo il campione minimo;
  il passaggio a gate bloccanti richiede una successiva versione deliberata.

Ogni segnale contiene `_experiment` nel breakdown. Quando la relativa posizione
paper si chiude, il worker crea un record immutabile in `experiment_outcomes`
con R lordo/netto, fee stimate, MAE/MFE e ordine di primo tocco tra stop e TP1.

Durante il bootstrap:

- gli overlay appresi dallo storico precedente restano visibili ma non bloccano
  nuovi segnali;
- Expected R e pre-TP1 validation operano in shadow;
- raggiunto il campione minimo, i gate leggono solo gli outcome della versione e
  dell'assignment attivi.

Endpoint read-only:

- `GET /api/analytics/experiments`
- `GET /api/analytics/expected-r-calibration`
- `GET /api/analytics/pre-tp1-validation`
- `GET /api/analytics/live-eligibility`
- `GET /api/analytics/diagnostics`

## Nessuna mutazione automatica

Il motore AutoCalibration, il runner worker e i relativi endpoint sono stati
rimossi. Equity slope, outcome profile e strategy edge sono strumenti
read-only: non cambiano pesi, soglie o sizing.

Le chiavi `auto_calibration*`, `auto_tuner_*` e i relativi DecisionLog
eventualmente presenti in database sono solo archivio storico; il runtime non
li legge e non li modifica.

## Promozione di una configurazione

La promozione è una decisione manuale:

1. raccogliere almeno il campione minimo della versione attiva;
2. verificare profit factor netto, expectancy in R e drawdown su finestra piena
   e recente;
3. controllare coerenza per timeframe, direzione e mercato;
4. modificare una sola famiglia di parametri;
5. incrementare `active_version` e raccogliere un nuovo campione forward.

## Ipotesi paper pre-registrata, attiva dal 1 ottobre 2026

Al 1 ottobre 2026 il candidato `paper-candidate-4h-long-v1` aveva 30 outcome
chiusi: -2,65 R netti, con train +0,98 R (20 trade) e holdout -3,63 R
(10 trade) nel report di eligibility. Il sottoinsieme score >= 70 aveva solo
8 trade (+1,60 R netti): e' una scoperta esplorativa sullo stesso campione,
non una conferma fuori campione.
Uno screening read-only dei 45 outcome storici long 4h trova 19 trade con
score >= 70 (+1,88 R), ma l'ultimo 30% temporale di quel gruppo conta solo
6 trade (-0,07 R). Sui 30 outcome forward v1, una permutazione esplorativa
di 20.000 riassegnazioni degli R ai punteggi dà circa p=0,17 per il miglior
contrasto tra le soglie 60, 65 e 70. Questo controllo non corregge la
correlazione tra ingressi simultanei e non trasforma la soglia in evidenza;
serve soltanto a evitare di scambiare il risultato degli 8 trade per un edge.
La versione v1 ha inoltre mescolato gate forward in shadow e attivi dopo il
trentesimo outcome: gli ingressi successivi non formano una coorte omogenea
con i precedenti.

Un controfattuale read-only sulle candele chiuse (con TP parziali gia' eseguiti
e fee round-trip stimate) suggeriva +2,26 R per un time-stop a 24 ore sui
30 trade forward, invece di -2,65 R osservati a 96 ore. Non e' stato adottato:
nel holdout storico separato di 15 trade long 4h, lo stesso proxy a 24 ore
scendeva da +1,29 R effettivi a -1,22 R. Il proxy non ricostruisce tutta la
gestione intrabar; un'uscita anticipata richiede una propria prova prospettica.

Il 1 ottobre 2026, dopo test e riavvio autorizzato, e' stata attivata la nuova
versione solo paper `paper-candidate-4h-long-score70-v2`, assignment
`candidate_4h_long_s70`: long 4h con score >= 70 e
`forward_gates_shadow_only=true`, senza cambiare rischio, sizing o live.
La versione e' partita con zero ingressi e zero outcome. Non mescolare outcome
della versione precedente. Valutare il
risultato netto dei costi in ordine di ingresso, con train/holdout temporale,
campione sufficiente su entrambi i lati e controllo per concentrazione di
mercato e drawdown. Per considerare l'edge confermato: almeno 60 outcome
utilizzabili della stessa configurazione, primi 40 ingressi come train e
successivi 20 come holdout; in entrambi profit factor netto >= 1,20 e media
netta >= +0,02 R per trade. Il drawdown e la concentrazione per mercato
restano controlli di rischio separati; se i criteri non reggono, il candidato
non e' promosso. Nessuna promozione live automatica.

Il controllo read-only `cd backend && ../venv/bin/python -m app.engine.forward_edge_verification`
fissa le prime 60 posizioni in ordine
d'ingresso e segnala subito eventuali hash o fasi dei gate mescolati, anche
prima che tutte si chiudano. Quando tutti gli outcome sono disponibili,
verifica versione/hash/assignment e calcola train e holdout al netto dei costi. Il suo
`criteria_met` indica soltanto il superamento delle soglie numeriche: serve
ancora una revisione di drawdown, concentrazione e operativita' prima di
qualsiasi decisione manuale.

Il report di live eligibility segnala e non promuove coorti che mescolano
trade generati con gate forward in shadow e gate forward attivi, anche quando
`configuration_hash` e `active_version` coincidono. Questa e' una protezione
analitica, non un cambio automatico del trading.
