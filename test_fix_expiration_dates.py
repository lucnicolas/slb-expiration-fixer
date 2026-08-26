from datetime import date, datetime, timedelta, timezone

import pytest

from fix_expiration_dates import (
    EXTENSION_DAYS,
    RECEPTION_THRESHOLD_DAYS,
    Candidate,
    SlbApiError,
    _rsql_escape,
    confirm_and_compute,
    find_candidates,
    format_slb_datetime,
    parse_reception_date,
    parse_slb_datetime,
    reached_validate_step,
)


# --- parse_reception_date -----------------------------------------------


def test_parse_reception_date_fld_prefix():
    assert parse_reception_date("FLD_20260712_4284235") == date(2026, 7, 12)


def test_parse_reception_date_other_prefix():
    assert parse_reception_date("ITESOFT_20260804_2368824") == date(2026, 8, 4)


def test_parse_reception_date_malformed_returns_none():
    assert parse_reception_date("not-a-valid-id") is None
    assert parse_reception_date("FLD_2026071_123") is None  # date trop courte


def test_parse_reception_date_invalid_calendar_date_returns_none():
    # 30 fevrier n'existe pas
    assert parse_reception_date("FLD_20260230_123") is None


# --- arithmetique de dates : fin de mois / fin d'annee / bissextile -----


def test_threshold_crosses_month_end():
    reception = date(2026, 1, 20)
    threshold = reception.__class__.fromordinal(
        reception.toordinal() + RECEPTION_THRESHOLD_DAYS
    )
    assert threshold == date(2026, 2, 5)


def test_threshold_crosses_year_end():
    reception = date(2026, 12, 20)
    threshold = reception.__class__.fromordinal(
        reception.toordinal() + RECEPTION_THRESHOLD_DAYS
    )
    assert threshold == date(2027, 1, 5)


def test_threshold_handles_leap_year_february():
    # 2028 est bissextile
    reception = date(2028, 2, 20)
    threshold = reception.__class__.fromordinal(
        reception.toordinal() + RECEPTION_THRESHOLD_DAYS
    )
    assert threshold == date(2028, 3, 7)


# --- find_candidates : detection de bout en bout ------------------------


def _wc(external_id: str, expiration_iso: str) -> dict:
    return {"externalWorkcaseName": external_id, "expirationDate": expiration_iso}


def test_find_candidates_flags_folder_at_exact_threshold():
    # Cas reel observe en prod : reception 12/08 + 16j = 28/08, expiration au 28/08 -> flagge
    workcases = [_wc("FLD_20260812_2345235", "2026-08-28T21:59:59.000Z")]
    candidates = find_candidates(workcases=workcases)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.reception_date == date(2026, 8, 12)
    assert c.threshold_date == date(2026, 8, 28)


def test_find_candidates_does_not_flag_healthy_folder():
    # Cas sain observe en prod : ~34 jours d'ecart
    workcases = [_wc("FLD_20260804_2368824", "2026-09-07T21:59:59.000Z")]
    candidates = find_candidates(workcases=workcases)
    assert candidates == []


def test_find_candidates_flags_folder_past_threshold():
    workcases = [_wc("FLD_20260801_1", "2026-08-10T21:59:59.000Z")]
    candidates = find_candidates(workcases=workcases)
    assert len(candidates) == 1


def test_find_candidates_threshold_crossing_month_end():
    # reception 20/01 + 16j = 05/02 ; expiration au 05/02 -> egalite -> flagge
    workcases = [_wc("FLD_20260120_1", "2026-02-05T21:59:59.000Z")]
    candidates = find_candidates(workcases=workcases)
    assert len(candidates) == 1
    assert candidates[0].threshold_date == date(2026, 2, 5)


def test_find_candidates_threshold_crossing_year_end():
    # reception 20/12/2026 + 16j = 05/01/2027
    workcases = [_wc("FLD_20261220_1", "2027-01-05T21:59:59.000Z")]
    candidates = find_candidates(workcases=workcases)
    assert len(candidates) == 1
    assert candidates[0].threshold_date == date(2027, 1, 5)


def test_find_candidates_skips_malformed_id():
    workcases = [_wc("garbage-id", "2026-08-28T21:59:59.000Z")]
    assert find_candidates(workcases=workcases) == []


def test_new_expiration_adds_extension_days_across_month_end():
    # expiration actuelle fin de mois -> +15j doit basculer proprement au mois suivant
    current = parse_slb_datetime("2026-01-20T21:59:59.000Z")
    from datetime import timedelta

    new_expiration = current + timedelta(days=EXTENSION_DAYS)
    assert new_expiration.date() == date(2026, 2, 4)


# --- format/parse round-trip ---------------------------------------------


def test_format_slb_datetime_strips_milliseconds():
    dt = datetime(2026, 9, 12, 21, 59, 59, tzinfo=timezone.utc)
    assert format_slb_datetime(dt) == "2026-09-12T21:59:59Z"


def test_parse_slb_datetime_handles_milliseconds():
    dt = parse_slb_datetime("2026-08-28T21:59:59.000Z")
    assert dt.year == 2026 and dt.month == 8 and dt.day == 28


def test_parse_slb_datetime_returns_none_on_malformed_value():
    assert parse_slb_datetime("not-a-date") is None
    assert parse_slb_datetime(None) is None


def test_find_candidates_skips_folder_with_unreadable_expiration_date():
    workcases = [_wc("FLD_20260812_2345235", "not-a-date")]
    assert find_candidates(workcases=workcases) == []


# --- _rsql_escape ----------------------------------------------------------


def test_rsql_escape_escapes_double_quotes():
    assert _rsql_escape('Valider" les justificatifs') == 'Valider\\" les justificatifs'


def test_rsql_escape_leaves_normal_text_untouched():
    assert _rsql_escape("Valider les justificatifs") == "Valider les justificatifs"


# --- reached_validate_step : distinction "abandon usager" vs "vraie validation" ---


def test_reached_validate_step_true_when_present():
    history = [
        {"action": "CreateFolder"},
        {"action": "StartCollectStep"},
        {"action": "CompleteCollectStep"},
        {"action": "StartValidateStep"},
        {"action": "ExpiredFolder"},
    ]
    assert reached_validate_step(history) is True


def test_reached_validate_step_false_when_abandoned_by_user():
    # Cas reel observe : usager depose une partie des pieces, ne soumet jamais,
    # un traitement automatique (source API) met a jour le statut juste avant
    # expiration, mais le dossier ne quitte jamais l'etape de collecte.
    history = [
        {"action": "CreateFolder"},
        {"action": "StartCollectStep"},
        {"action": "UploadDocument", "source": "API"},
        {"action": "ExpiredFolder"},
    ]
    assert reached_validate_step(history) is False


def test_reached_validate_step_false_on_empty_history():
    assert reached_validate_step([]) is False


def test_reached_validate_step_false_when_sent_back_to_collect_after_validate():
    # Le dossier a atteint Valider, mais a ete renvoye en collecte (ex: refus
    # d'une piece par un gestionnaire) avant d'expirer : ce n'est plus,
    # au moment de l'expiration, un dossier "en attente de validation".
    history = [
        {"action": "StartCollectStep"},
        {"action": "CompleteCollectStep"},
        {"action": "StartValidateStep"},
        {"action": "RefuseFolder"},
        {"action": "StartCollectStep"},
        {"action": "ExpiredFolder"},
    ]
    assert reached_validate_step(history) is False


def _candidate(external_id, reception_date, threshold_date, current_expiration):
    return Candidate(
        external_id=external_id,
        reception_date=reception_date,
        threshold_date=threshold_date,
        current_expiration=current_expiration,
    )


class _FakeClient:
    def __init__(self, statuses=None, raise_for=()):
        self.statuses = statuses or {}
        self.raise_for = set(raise_for)

    def get_workcase_status(self, external_id):
        if external_id in self.raise_for:
            raise SlbApiError("boom")
        return self.statuses.get(external_id, "INCOMPLETE")


def test_confirm_and_compute_filters_out_of_scope_status():
    c = _candidate("FLD_20260812_1", date(2026, 8, 12), date(2026, 8, 28), parse_slb_datetime("2026-08-28T21:59:59.000Z"))
    client = _FakeClient(statuses={"FLD_20260812_1": "INCOMPLETE"})
    assert confirm_and_compute(client, [c]) == []


def test_confirm_and_compute_computes_new_expiration_for_valid_status():
    current = parse_slb_datetime("2026-08-28T21:59:59.000Z")
    c = _candidate("FLD_20260812_1", date(2026, 8, 12), date(2026, 8, 28), current)
    client = _FakeClient(statuses={"FLD_20260812_1": "WITH_ERRORS"})
    confirmed = confirm_and_compute(client, [c])
    assert len(confirmed) == 1
    assert confirmed[0].new_expiration == current + timedelta(days=EXTENSION_DAYS)


def test_confirm_and_compute_skips_candidate_on_status_fetch_error():
    c = _candidate("FLD_20260812_1", date(2026, 8, 12), date(2026, 8, 28), parse_slb_datetime("2026-08-28T21:59:59.000Z"))
    client = _FakeClient(raise_for={"FLD_20260812_1"})
    assert confirm_and_compute(client, [c]) == []


def test_confirm_and_compute_floors_new_expiration_when_still_in_the_past():
    # Le job n'a pas tourne depuis longtemps : current_expiration + 15j est
    # encore dans le passe -> doit etre remonte a une date future (garde-fou
    # contre le rejet API 403 INVALID_EXPIRATION_DATE).
    long_ago = datetime.now(timezone.utc) - timedelta(days=100)
    c = _candidate("FLD_OLD_1", date(2020, 1, 1), date(2020, 1, 17), long_ago)
    client = _FakeClient(statuses={"FLD_OLD_1": "COMPLETE"})
    confirmed = confirm_and_compute(client, [c])
    assert len(confirmed) == 1
    assert confirmed[0].new_expiration > datetime.now(timezone.utc)


def test_reached_validate_step_true_uses_last_validate_event():
    # Un aller-retour Valider -> refus -> re-Valider doit rester detecte.
    history = [
        {"action": "StartValidateStep"},
        {"action": "RefuseFolder"},
        {"action": "StartCollectStep"},
        {"action": "CompleteCollectStep"},
        {"action": "StartValidateStep"},
        {"action": "ExpiredFolder"},
    ]
    assert reached_validate_step(history) is True
