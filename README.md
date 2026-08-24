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
   `COMPLETED` ou `WITH_ERRORS` sont traites (`INCOMPLETED` est ignore).
6. Pour les dossiers confirmes : nouvelle date d'expiration = date
   d'expiration actuelle + 15 jours.

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

Le script se termine avec un code de sortie non-nul si au moins une mise a
jour a echoue (utile pour la supervision d'une tache planifiee : cron,
launchd, etc. — cette tache planifiee est a mettre en place separement,
ce script ne s'auto-planifie pas).

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
