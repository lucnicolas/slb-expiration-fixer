# slb-expiration-fixer

Correctif quotidien des dates d'expiration sur le tenant SLB `cd-vaucluse`.

## Contexte

Un bug SLB (en attente de correction R&D) fixe parfois la date d'expiration
d'un dossier a seulement 16 jours apres sa date de reception (encodee dans
son ID, ex: `FLD_20260812_2345235` -> recu le 12/08/2026), au lieu des
34-36 jours habituels. Un dossier avec une expiration trop courte risque de
passer en `EXPIRED` (etat terminal, non recuperable) avant la fin du
traitement de validation.

Ce script tourne quotidiennement pour detecter et corriger ces dossiers en
attendant le correctif R&D.

## Regle appliquee

1. Recherche des dossiers avec :
   - etat = **en cours** (`state == OPEN`)
   - etape courante = **"Valider les justificatifs"**
2. Pour chaque dossier trouve, extraction de la date de reception depuis
   l'ID (`PREFIXE_YYYYMMDD_xxxxxxx`).
3. Calcul du seuil = date de reception + 16 jours.
4. Si la date d'expiration actuelle est <= ce seuil (comparaison sur la
   date calendaire), le dossier est un candidat.
5. Verification du statut du dossier (`GET` detail) : uniquement
   `COMPLETE` ou `WITH_ERRORS` sont traites (`INCOMPLETE` est ignore — attention,
   ce sont les valeurs reellement renvoyees par l'API, qui different de la
   documentation officielle SLB, laquelle indique a tort `COMPLETED`/`INCOMPLETED`).
6. Pour les dossiers confirmes : nouvelle date d'expiration = date
   d'expiration actuelle + 15 jours.

## Alerte sur les dossiers deja EXPIRED (non bloquante)

En plus du correctif ci-dessus (qui ne peut agir que sur des dossiers encore
`OPEN`), le script signale chaque jour les dossiers passes `EXPIRED` dans les
`EXPIRED_ALERT_LOOKBACK_DAYS` derniers jours (2 par defaut) alors qu'ils
avaient deja atteint l'etape de validation avant expiration : cas anormal
identique dans son principe, mais sur un dossier dans un etat terminal que
l'API refuse de modifier (`403 FORBIDDEN_EXPIRATION_DATE_UPDATE`).

Ces dossiers doivent etre traites/remontes manuellement — ils restent
consultables dans l'historique SLB jusqu'a la purge automatique cote SLB
(observee ~5-6 mois apres expiration).

Point important verifie manuellement sur un echantillon reel : un dossier
`EXPIRED` avec `status = COMPLETE` ou `WITH_ERRORS` n'a **pas** forcement
atteint l'etape de validation - c'est aussi le cas d'un usager qui depose une
partie de ses pieces puis abandonne sans jamais soumettre son dossier (un
traitement automatique cote SLB met alors a jour le statut des pieces sans
faire avancer le dossier). Le script distingue les deux via l'historique
(`GET /api/v3/workcases/{id}/history/`), en cherchant l'evenement
`StartValidateStep` — seul signal fiable de passage reel a l'etape de
validation.

Cette alerte est **non bloquante** : elle n'affecte jamais le code de sortie
du script (`dossiers_expires_a_traiter` dans le resume final est purement
informatif), seule une erreur d'ecriture sur un dossier `OPEN` fait echouer
le script.

## Installation

```bash
cd slb-expiration-fixer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# renseigner SLB_ACCESS_TOKEN dans .env
```

## Usage

```bash
# Dry-run (par defaut) : affiche ce qui serait fait, n'ecrit rien
python fix_expiration_dates.py

# Applique reellement les corrections
python fix_expiration_dates.py --apply
```

Codes de sortie (utiles pour la supervision d'une tache planifiee : cron,
launchd, etc. — cette tache planifiee est a mettre en place separement,
ce script ne s'auto-planifie pas) :
- `0` : succes (l'alerte EXPIRED n'affecte jamais ce code, elle est purement informative)
- `1` : au moins une mise a jour a echoue
- `2` : configuration invalide (token manquant) ou echec de la recherche initiale
- `3` : erreur inattendue (bug non anticipe) — a distinguer des cas 1/2 pour le triage

## Tests

```bash
pytest test_fix_expiration_dates.py -v
```

Les tests couvrent notamment les cas limites de calcul de dates (fin de
mois, fin d'annee, annee bissextile, ID malformes) — voir le fichier de
test pour le detail.

## Limites connues

- L'API de recherche (`GET /api/v3/workcases`) ignore les parametres de
  pagination `page`/`pageSize` sur ce tenant : un seul appel renvoie
  l'integralite des dossiers correspondant au filtre. Le script gere quand
  meme une boucle de pagination defensive au cas ou ce comportement serait
  corrige cote SLB.
- Le champ `status` (complet / avec erreur) n'est disponible que via
  l'appel de detail (`GET /api/v3/workcases/{id}`), pas dans la liste de
  recherche : il n'est donc recupere que pour les dossiers deja identifies
  comme candidats par la regle de date, pour limiter le nombre d'appels API.
- `state==EXPIRED` seul, dans le filtre de recherche, declenche un bug cote
  API (`400 Cannot read properties of undefined`) : il faut toujours le
  combiner avec une 2e clause (le script utilise `currentStepNames==*`). La
  recherche EXPIRED est par ailleurs bornee cote serveur a la fenetre
  `EXPIRED_ALERT_LOOKBACK_DAYS` via une clause `expirationDate>=...`, pour
  eviter de re-telecharger l'integralite des dossiers EXPIRED du tenant
  (plusieurs milliers, en croissance constante jusqu'a purge SLB) a chaque
  execution.
- La verification de l'alerte EXPIRED fait un appel `/history/` par dossier
  recemment expire dans la fenetre consideree (pas de concurrence pour
  rester simple) : avec ~30-60 dossiers EXPIRED par jour sur ce tenant,
  compter jusqu'a ~1-2 minutes d'execution supplementaire pour cette partie.
- Les valeurs reelles de l'enum `action` de l'historique (`StartCollectStep`,
  `StartValidateStep`, `ExpiredFolder`, ...) different aussi de celles
  documentees dans `slb-openapi-api-v3.json` (nomenclature "Workcase" vs
  "Folder"/"Step" observee en reel) — meme type d'ecart doc/realite que pour
  `status`. Les constantes du script sont basees sur les valeurs reelles,
  verifiees par appels directs a l'API.
