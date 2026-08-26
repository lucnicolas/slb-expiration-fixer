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
ALLOWED_STATUSES = {"COMPLETE", "WITH_ERRORS"}  # valeurs reelles renvoyees par l'API (la doc dit "COMPLETED"/"INCOMPLETED", la realite dit "COMPLETE"/"INCOMPLETE")
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
        resp = self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        if not resp.ok:
            detail = resp.text
            try:
                body = resp.json()
                detail = f"{body.get('errorCode')}: {body.get('errorMessage')}"
            except ValueError:
                pass
            raise SlbApiError(f"{method} {path} -> HTTP {resp.status_code} ({detail})")
        return resp

    def search_open_validate_workcases(self) -> list[dict]:
        """Recupere tous les dossiers state=OPEN dont l'etape courante correspond
        exactement au libelle de validation configure."""
        results: list[dict] = []
        seen: set[str] = set()
        filter_expr = f'(state=={STATE_FILTER});(currentStepNames=="{self.step_name}")'
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

    def get_workcase_status(self, external_id: str) -> str:
        resp = self._request("GET", f"/api/v3/workcases/{external_id}")
        return resp.json()["workcase"]["status"]

    def update_expiration_date(self, external_id: str, new_expiration: datetime) -> None:
        payload = {"expirationDate": format_slb_datetime(new_expiration)}
        self._request(
            "PUT", f"/api/v2/folders/{external_id}/expirationDate", json=payload
        )


def parse_slb_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def find_candidates(client: SlbClient, workcases: list[dict]) -> list[Candidate]:
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

        c.new_expiration = c.current_expiration + timedelta(days=EXTENSION_DAYS)
        confirmed.append(c)
    return confirmed


def run(apply: bool) -> int:
    load_dotenv()
    base_url = os.environ.get("SLB_BASE_URL", "https://cd-vaucluse.slfb.itesoft.cloud")
    access_token = os.environ.get("SLB_ACCESS_TOKEN")
    step_name = os.environ.get("SLB_VALIDATE_STEP_NAME", "Valider les justificatifs")

    if not access_token:
        log.error("SLB_ACCESS_TOKEN manquant (voir .env.example).")
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

    date_candidates = find_candidates(client, workcases)
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

    mode = "APPLIQUE" if apply else "DRY-RUN (aucune ecriture)"
    log.info(
        "Resume [%s] : scannes=%d, candidats_date=%d, confirmes=%d, "
        "mis_a_jour=%d, erreurs=%d",
        mode,
        len(workcases),
        len(date_candidates),
        len(confirmed),
        updated,
        errors,
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
    return run(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
