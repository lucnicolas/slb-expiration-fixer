#!/usr/bin/env python3
"""
Correctif quotidien des dates d'expiration SLB (tenant cd-vaucluse).

Contexte : un bug SLB (en attente de correction R&D) fixe parfois la date
d'expiration d'un dossier a seulement 16 jours apres sa date de reception
(encodee dans son ID), au lieu des ~34-36 jours habituels. Ce script detecte
les dossiers touches et repousse leur expiration de 15 jours supplementaires.

Regle complete : voir README.md.

Usage:
    python fix_expiration_dates.py            # dry-run (aucune ecriture)
    python fix_expiration_dates.py --apply    # applique reellement les corrections
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

RECEPTION_THRESHOLD_DAYS = 16
EXTENSION_DAYS = 15
STATE_FILTER = "OPEN"
EXPIRED_STATE = "EXPIRED"
ALLOWED_STATUSES = {"COMPLETE", "WITH_ERRORS"}  # valeurs reelles renvoyees par l'API (la doc dit "COMPLETED"/"INCOMPLETED", la realite dit "COMPLETE"/"INCOMPLETE")
VALIDATE_STEP_ACTION = "StartValidateStep"  # evenement d'historique marquant l'entree reelle dans l'etape de validation
EXPIRED_ALERT_LOOKBACK_DAYS = 2  # fenetre de detection quotidienne des dossiers EXPIRED a signaler (marge de securite d'un jour)
ID_PATTERN = re.compile(r"^\w+_(\d{8})_\d+$")
REQUEST_TIMEOUT = 30
SEARCH_PAGE_SIZE = 200
MAX_SEARCH_PAGES = 20  # garde-fou si la pagination cote API venait a etre corrigee

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("slb-expiration-fixer")


class SlbApiError(Exception):
    pass


@dataclass
class Candidate:
    external_id: str
    reception_date: date
    threshold_date: date
    current_expiration: datetime
    status: str | None = None
    new_expiration: datetime | None = None


@dataclass
class ExpiredAlert:
    external_id: str
    reception_date: date | None
    expiration_date: datetime
    gap_days: int | None


class SlbClient:
    def __init__(self, base_url: str, access_token: str, step_name: str):
        self.base_url = base_url.rstrip("/")
        self.step_name = step_name
        self.session = requests.Session()
        self.session.headers.update(
            {"Access-Token": access_token, "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise SlbApiError(f"{method} {path} -> erreur reseau ({exc})") from exc
        if not resp.ok:
            detail = resp.text
            try:
                body = resp.json()
                detail = f"{body.get('errorCode')}: {body.get('errorMessage')}"
            except ValueError:
                pass
            raise SlbApiError(f"{method} {path} -> HTTP {resp.status_code} ({detail})")
        return resp

    def _search_workcases(self, filter_expr: str) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        for page in range(MAX_SEARCH_PAGES):
            resp = self._request(
                "GET",
                "/api/v3/workcases",
                params={
                    "filter": filter_expr,
                    "page": page,
                    "pageSize": SEARCH_PAGE_SIZE,
                },
            )
            batch = resp.json()
            new_items = [w for w in batch if w.get("externalWorkcaseName") not in seen]
            for w in new_items:
                seen.add(w["externalWorkcaseName"])
            results.extend(new_items)
            if len(batch) < SEARCH_PAGE_SIZE or not new_items:
                break
        else:
            log.warning(
                "Arret de la pagination apres %d pages (garde-fou MAX_SEARCH_PAGES) - "
                "verifier si le volume de dossiers a fortement augmente.",
                MAX_SEARCH_PAGES,
            )
        return results

    def search_open_validate_workcases(self) -> list[dict]:
        """Recupere tous les dossiers state=OPEN dont l'etape courante correspond
        exactement au libelle de validation configure."""
        step_name = _rsql_escape(self.step_name)
        filter_expr = f'(state=={STATE_FILTER});(currentStepNames=="{step_name}")'
        return self._search_workcases(filter_expr)

    def search_expired_workcases(self, expired_since: datetime | None = None) -> list[dict]:
        """Recupere les dossiers state=EXPIRED, optionnellement bornes aux
        expirations survenues a partir de `expired_since` (borne server-side,
        pour eviter de re-telecharger l'integralite de l'historique EXPIRED du
        tenant a chaque appel - celui-ci ne fait que croitre jusqu'a purge SLB).

        Note : `state==EXPIRED` seul declenche un bug cote API SLB (400 "Cannot
        read properties of undefined"). Une 2e clause de filtre est necessaire ;
        `currentStepNames==*` matche tout (y compris vide) et evite le bug."""
        clauses = [f"(state=={EXPIRED_STATE})", "(currentStepNames==*)"]
        if expired_since is not None:
            clauses.append(f"(expirationDate>={format_slb_datetime(expired_since)})")
        return self._search_workcases(";".join(clauses))

    def get_workcase_status(self, external_id: str) -> str:
        resp = self._request("GET", f"/api/v3/workcases/{external_id}")
        return resp.json()["workcase"]["status"]

    def get_workcase_history(self, external_id: str) -> list[dict]:
        resp = self._request("GET", f"/api/v3/workcases/{external_id}/history/")
        return resp.json()

    def update_expiration_date(self, external_id: str, new_expiration: datetime) -> None:
        payload = {"expirationDate": format_slb_datetime(new_expiration)}
        self._request(
            "PUT", f"/api/v2/folders/{external_id}/expirationDate", json=payload
        )


def _rsql_escape(value: str) -> str:
    """Echappe les guillemets doubles pour une insertion sure dans une valeur
    de filtre RSQL entre guillemets (ex: currentStepNames=="...")."""
    return value.replace('"', '\\"')


def parse_slb_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def format_slb_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_reception_date(external_id: str) -> date | None:
    match = ID_PATTERN.match(external_id)
    if not match:
        return None
    raw = match.group(1)
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def find_candidates(workcases: list[dict]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for wc in workcases:
        external_id = wc["externalWorkcaseName"]
        reception_date = parse_reception_date(external_id)
        if reception_date is None:
            log.warning(
                "ID '%s' ne correspond pas au format attendu (PREFIXE_YYYYMMDD_xxx), "
                "dossier ignore.",
                external_id,
            )
            continue

        threshold_date = reception_date + timedelta(days=RECEPTION_THRESHOLD_DAYS)
        current_expiration = parse_slb_datetime(wc["expirationDate"])
        if current_expiration is None:
            log.warning(
                "%s : date d'expiration '%s' illisible, dossier ignore.",
                external_id,
                wc.get("expirationDate"),
            )
            continue

        if current_expiration.date() <= threshold_date:
            candidates.append(
                Candidate(
                    external_id=external_id,
                    reception_date=reception_date,
                    threshold_date=threshold_date,
                    current_expiration=current_expiration,
                )
            )
    return candidates


REGRESSION_ACTIONS = {"StartCollectStep", "RefuseFolder", "ReopenWorkcase"}


def reached_validate_step(history_events: list[dict]) -> bool:
    """True si le dossier a reellement atteint l'etape de validation et y est
    reste jusqu'a expiration (dernier evenement StartValidateStep de
    l'historique non suivi d'un retour en arriere - refus, reouverture, ou
    nouveau passage en collecte).

    Ce signal est necessaire car, une fois un dossier EXPIRED, son champ
    `currentStepNames` est vide et son `status` (COMPLETE/WITH_ERRORS) peut
    aussi bien correspondre a un dossier abandonne par l'usager avant
    soumission finale (documents deposes et notes automatiquement, mais
    jamais soumis) qu'a un dossier reellement passe en validation.
    L'historique SLB est retourne par ordre chronologique."""
    last_validate_index = None
    for i, ev in enumerate(history_events):
        if ev.get("action") == VALIDATE_STEP_ACTION:
            last_validate_index = i

    if last_validate_index is None:
        return False

    return not any(
        ev.get("action") in REGRESSION_ACTIONS
        for ev in history_events[last_validate_index + 1 :]
    )


def find_expired_validate_alerts(
    client: SlbClient, lookback_days: int
) -> list[ExpiredAlert]:
    """Dossiers passes EXPIRED recemment qui avaient deja atteint l'etape de
    validation - cas anormal a traiter manuellement (etat terminal, non
    corrigeable via l'API)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    workcases = client.search_expired_workcases(expired_since=cutoff)

    alerts: list[ExpiredAlert] = []
    for wc in workcases:
        expiration_date = parse_slb_datetime(wc["expirationDate"])
        if expiration_date is None or expiration_date < cutoff:
            continue

        external_id = wc["externalWorkcaseName"]
        try:
            history = client.get_workcase_history(external_id)
        except SlbApiError as exc:
            log.warning(
                "%s : impossible de recuperer l'historique pour l'alerte EXPIRED "
                "(%s), ignore.",
                external_id,
                exc,
            )
            continue

        if not reached_validate_step(history):
            continue

        reception_date = parse_reception_date(external_id)
        gap_days = (
            (expiration_date.date() - reception_date).days if reception_date else None
        )
        alerts.append(ExpiredAlert(external_id, reception_date, expiration_date, gap_days))

    return alerts


def confirm_and_compute(client: SlbClient, candidates: list[Candidate]) -> list[Candidate]:
    confirmed = []
    for c in candidates:
        try:
            c.status = client.get_workcase_status(c.external_id)
        except SlbApiError as exc:
            log.error("Echec de recuperation du statut pour %s : %s", c.external_id, exc)
            continue

        if c.status not in ALLOWED_STATUSES:
            log.info(
                "%s : date d'expiration trop courte (seuil %s) mais statut '%s' "
                "hors perimetre (%s), ignore.",
                c.external_id,
                c.threshold_date,
                c.status,
                sorted(ALLOWED_STATUSES),
            )
            continue

        computed_expiration = c.current_expiration + timedelta(days=EXTENSION_DAYS)
        # Garde-fou : si le job n'a pas tourne depuis longtemps, current_expiration
        # peut deja etre dans le passe, et +15 jours pourrait l'y laisser -> l'API
        # rejette (403 INVALID_EXPIRATION_DATE, date doit etre >= aujourd'hui).
        floor = datetime.now(timezone.utc) + timedelta(days=1)
        if computed_expiration < floor:
            log.warning(
                "%s : expiration actuelle (%s) + %d jours retombe encore dans le "
                "passe, nouvelle date forcee a %s au lieu de %s.",
                c.external_id,
                c.current_expiration.isoformat(),
                EXTENSION_DAYS,
                floor.isoformat(),
                computed_expiration.isoformat(),
            )
            computed_expiration = floor
        c.new_expiration = computed_expiration
        confirmed.append(c)
    return confirmed


def run(apply: bool) -> int:
    load_dotenv()
    base_url = os.environ.get("SLB_BASE_URL", "https://cd-vaucluse.slfb.itesoft.cloud")
    access_token = (os.environ.get("SLB_ACCESS_TOKEN") or "").strip()
    step_name = os.environ.get("SLB_VALIDATE_STEP_NAME", "Valider les justificatifs")

    if not access_token:
        log.error("SLB_ACCESS_TOKEN manquant ou vide (voir .env.example).")
        return 2

    client = SlbClient(base_url, access_token, step_name)

    log.info(
        "Recherche des dossiers state=%s / etape='%s'...", STATE_FILTER, step_name
    )
    try:
        workcases = client.search_open_validate_workcases()
    except SlbApiError as exc:
        log.error("Echec de la recherche des dossiers : %s", exc)
        return 2

    log.info("%d dossier(s) trouve(s) a l'etape de validation.", len(workcases))

    date_candidates = find_candidates(workcases)
    log.info(
        "%d dossier(s) avec une expiration <= reception + %d jours.",
        len(date_candidates),
        RECEPTION_THRESHOLD_DAYS,
    )

    confirmed = confirm_and_compute(client, date_candidates)
    log.info(
        "%d dossier(s) confirmes (statut dans %s) a corriger.",
        len(confirmed),
        sorted(ALLOWED_STATUSES),
    )

    updated = 0
    errors = 0
    for c in confirmed:
        log.info(
            "%s : reception=%s seuil=%s expiration_actuelle=%s statut=%s -> "
            "nouvelle_expiration=%s",
            c.external_id,
            c.reception_date,
            c.threshold_date,
            c.current_expiration.isoformat(),
            c.status,
            c.new_expiration.isoformat(),
        )
        if not apply:
            continue
        try:
            client.update_expiration_date(c.external_id, c.new_expiration)
            log.info("%s : date d'expiration mise a jour avec succes.", c.external_id)
            updated += 1
        except SlbApiError as exc:
            log.error("%s : echec de la mise a jour : %s", c.external_id, exc)
            errors += 1

    log.info(
        "Recherche des dossiers EXPIRED recemment expires (fenetre %d jours) ayant "
        "atteint l'etape de validation avant expiration (alerte non bloquante)...",
        EXPIRED_ALERT_LOOKBACK_DAYS,
    )
    try:
        expired_alerts = find_expired_validate_alerts(client, EXPIRED_ALERT_LOOKBACK_DAYS)
    except SlbApiError as exc:
        log.warning(
            "Echec de la recherche des dossiers EXPIRED (alerte non bloquante, ignoree) : %s",
            exc,
        )
        expired_alerts = []

    if expired_alerts:
        details = ", ".join(
            f"{a.external_id} (ecart={a.gap_days}j)" if a.gap_days is not None else a.external_id
            for a in expired_alerts
        )
        log.warning(
            "ALERTE : %d dossier(s) sont passes EXPIRED alors qu'ils avaient deja "
            "atteint l'etape '%s' (fenetre %d jours) - etat terminal, NON corrigeable "
            "via l'API, a traiter/faire remonter manuellement (ils restent consultables "
            "dans l'historique SLB jusqu'a purge automatique) : %s",
            len(expired_alerts),
            step_name,
            EXPIRED_ALERT_LOOKBACK_DAYS,
            details,
        )
    else:
        log.info(
            "Aucun dossier EXPIRED n'a atteint l'etape de validation sur la fenetre consideree."
        )

    mode = "APPLIQUE" if apply else "DRY-RUN (aucune ecriture)"
    log.info(
        "Resume [%s] : scannes=%d, candidats_date=%d, confirmes=%d, "
        "mis_a_jour=%d, erreurs=%d, dossiers_expires_a_traiter=%d",
        mode,
        len(workcases),
        len(date_candidates),
        len(confirmed),
        updated,
        errors,
        len(expired_alerts),
    )
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Applique reellement les corrections (par defaut : dry-run).",
    )
    args = parser.parse_args()
    try:
        return run(apply=args.apply)
    except Exception:
        # Filet de securite : un crash inattendu (bug non anticipe) doit rester
        # distinguable, dans la supervision cron, d'un simple echec de mise a
        # jour (code 1) ou d'un probleme de configuration/recherche (code 2).
        log.exception("Erreur inattendue, arret du script.")
        return 3


if __name__ == "__main__":
    sys.exit(main())
