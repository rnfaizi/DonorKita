"""Pengujian aturan domain murni (tanpa database)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.domain import compatibility as C
from app.domain import eligibility as E
from app.domain import scoring as S
from app.domain import geo

NOW = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)
POLICY = E.EligibilityPolicy(interval_days=60, max_per_year=5, screening_valid_days=30)


# ---------- kompatibilitas ----------

@pytest.mark.parametrize("recipient,expected", [
    ("o", {"o"}), ("a", {"a", "o"}), ("b", {"b", "o"}), ("ab", {"a", "b", "ab", "o"}),
])
def test_prc_abo(recipient, expected):
    assert C.compatible_donor_abo(recipient, "prc") == expected


@pytest.mark.parametrize("recipient,expected", [
    ("o", {"o", "a", "b", "ab"}), ("a", {"a", "ab"}), ("b", {"b", "ab"}), ("ab", {"ab"}),
])
def test_plasma_abo_is_reversed(recipient, expected):
    assert C.compatible_donor_abo(recipient, "plasma") == expected


@pytest.mark.parametrize("component", ["whole_blood", "platelets"])
def test_whole_blood_and_platelets_need_identical_abo(component):
    for t in C.BLOOD_TYPES:
        assert C.compatible_donor_abo(t, component) == {t}


def test_rhesus_negative_recipient_only_negative_for_red_cells():
    groups = C.compatible_donor_groups("a", "negative", "prc")
    assert groups == {("a", "negative"), ("o", "negative")}
    assert C.is_compatible(("o", "negative"), ("a", "negative"), "prc")
    assert not C.is_compatible(("o", "positive"), ("a", "negative"), "prc")


def test_rhesus_positive_recipient_accepts_both():
    assert C.compatible_donor_groups("o", "positive", "prc") == {("o", "positive"), ("o", "negative")}


def test_rhesus_irrelevant_for_plasma():
    assert ("ab", "positive") in C.compatible_donor_groups("a", "negative", "plasma")


def test_universal_cases():
    # O negatif sel darah merah cocok untuk semua penerima
    for t in C.BLOOD_TYPES:
        for rh in C.RHESUS:
            assert C.is_compatible(("o", "negative"), (t, rh), "prc")
    # AB positif menerima sel darah merah dari semua
    assert len(C.compatible_donor_groups("ab", "positive", "prc")) == 8


def test_unknown_values_rejected():
    with pytest.raises(ValueError):
        C.compatible_donor_groups("x", "positive", "prc")
    with pytest.raises(ValueError):
        C.compatible_donor_groups("a", "positive", "darah")


# ---------- kelayakan ----------

def facts(**kw):
    base = dict(birth_date=date(2000, 1, 1), weight_kg=60.0, donation_dates=(),
                deferred_until=None, screening_at=NOW - timedelta(days=1), screening_flagged=False)
    base.update(kw)
    return E.DonorFacts(**base)


def test_healthy_first_time_donor_is_eligible():
    r = E.evaluate(facts(), NOW, POLICY)
    assert r.eligible and r.reasons == [] and r.next_eligible_at is None


def test_incomplete_profile():
    r = E.evaluate(facts(weight_kg=None), NOW, POLICY)
    assert not r.eligible and r.reasons == ["profile_incomplete"]


def test_age_boundaries():
    assert "under_age" in E.evaluate(facts(birth_date=date(2010, 1, 1)), NOW, POLICY).reasons
    assert E.evaluate(facts(birth_date=date(2009, 12, 1)), NOW, POLICY).eligible  # tepat 17 tahun
    first_timer_61 = facts(birth_date=date(1965, 1, 1))
    assert "over_age_review" in E.evaluate(first_timer_61, NOW, POLICY).reasons
    repeat_61 = facts(birth_date=date(1965, 1, 1), donation_dates=(NOW - timedelta(days=200),))
    assert E.evaluate(repeat_61, NOW, POLICY).eligible


def test_weight_minimum():
    assert "under_weight" in E.evaluate(facts(weight_kg=44.9), NOW, POLICY).reasons
    assert E.evaluate(facts(weight_kg=45), NOW, POLICY).eligible


def test_interval_not_met_gives_next_date():
    last = NOW - timedelta(days=30)
    r = E.evaluate(facts(donation_dates=(last,)), NOW, POLICY)
    assert r.reasons == ["interval_not_met"]
    assert r.next_eligible_at == last + timedelta(days=60)
    assert E.evaluate(facts(donation_dates=(NOW - timedelta(days=60),)), NOW, POLICY).eligible


def test_yearly_limit():
    dates = tuple(NOW - timedelta(days=d) for d in (70, 140, 210, 280, 350))
    r = E.evaluate(facts(donation_dates=dates), NOW, POLICY)
    assert "yearly_limit_reached" in r.reasons and r.donations_last_365d == 5
    assert r.next_eligible_at == NOW - timedelta(days=350) + timedelta(days=365)


def test_deferral_and_screening():
    r = E.evaluate(facts(deferred_until=NOW + timedelta(days=3)), NOW, POLICY)
    assert r.reasons == ["deferred"] and r.next_eligible_at == NOW + timedelta(days=3)
    assert E.evaluate(facts(screening_at=None), NOW, POLICY).reasons == ["screening_required"]
    assert E.evaluate(facts(screening_at=NOW - timedelta(days=31)), NOW, POLICY).reasons == ["screening_required"]
    assert E.evaluate(facts(screening_flagged=True), NOW, POLICY).reasons == ["screening_flagged"]


def test_next_date_hidden_when_reason_not_time_based():
    r = E.evaluate(facts(weight_kg=40, donation_dates=(NOW - timedelta(days=10),)), NOW, POLICY)
    assert set(r.reasons) == {"under_weight", "interval_not_met"} and r.next_eligible_at is None


# ---------- skor ----------

def test_closer_and_more_reliable_scores_higher():
    w = {"distance": 0.5, "reliability": 0.3, "exact_match": 0.1, "freshness": 0.1}
    near = S.CandidateFacts(500, 5000, 0, 0, False, 0)
    far = S.CandidateFacts(4500, 5000, 0, 0, False, 0)
    assert S.score(near, w) > S.score(far, w)
    reliable = S.CandidateFacts(2000, 5000, 4, 4, False, 0)
    flaky = S.CandidateFacts(2000, 5000, 0, 4, False, 0)
    assert S.score(reliable, w) > S.score(flaky, w)
    exact = S.CandidateFacts(2000, 5000, 0, 0, True, 0)
    assert S.score(exact, w) > S.score(S.CandidateFacts(2000, 5000, 0, 0, False, 0), w)


def test_reliability_prior():
    assert S.reliability(0, 0) == 0.5
    assert S.reliability(3, 3) == 0.8


# ---------- geo ----------

def test_haversine_and_bbox():
    d = geo.haversine_m(-6.9147, 107.6098, -6.8915, 107.6107)
    assert 2400 < d < 2700
    box = geo.parse_bbox("107.5,-7.0,107.7,-6.8")
    assert geo.in_bbox(-6.9, 107.6, box) and not geo.in_bbox(-6.5, 107.6, box)
    with pytest.raises(ValueError):
        geo.parse_bbox("1,2,3")
