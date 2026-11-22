"""Kebijakan kompatibilitas golongan darah (subdomain core: Pencocokan).

Aturan ini hanya MENYARING kandidat pendonor yang pantas diundang. Uji silang
(crossmatch) dan keputusan transfusi tetap dilakukan petugas UDD/BDRS.

Ringkasan aturan yang dipakai:
- prc (sel darah merah pekat): penerima O <- O; A <- A, O; B <- B, O; AB <- semua.
- whole_blood (darah lengkap) dan platelets (trombosit): ABO harus identik.
- plasma: kebalikan sel darah merah; penerima O <- semua; A <- A, AB; B <- B, AB; AB <- AB.
- Rhesus untuk komponen yang mengandung sel darah merah (whole_blood, prc, platelets):
  penerima negatif hanya dari negatif; penerima positif dari keduanya.
  Rhesus tidak membatasi plasma.
"""
from typing import FrozenSet, Set, Tuple

BLOOD_TYPES = ("a", "b", "ab", "o")
RHESUS = ("positive", "negative")
COMPONENTS = ("whole_blood", "prc", "platelets", "plasma")

BloodGroup = Tuple[str, str]  # (blood_type, rhesus), mis. ("o", "negative")

_RED_CELL_ABO = {
    "o": {"o"},
    "a": {"a", "o"},
    "b": {"b", "o"},
    "ab": {"a", "b", "ab", "o"},
}
_PLASMA_ABO = {
    "o": {"o", "a", "b", "ab"},
    "a": {"a", "ab"},
    "b": {"b", "ab"},
    "ab": {"ab"},
}
_RHESUS_MATTERS = {"whole_blood", "prc", "platelets"}


def _check(blood_type: str, rhesus: str, component: str) -> None:
    if blood_type not in BLOOD_TYPES:
        raise ValueError(f"golongan darah tidak dikenal: {blood_type}")
    if rhesus not in RHESUS:
        raise ValueError(f"rhesus tidak dikenal: {rhesus}")
    if component not in COMPONENTS:
        raise ValueError(f"komponen tidak dikenal: {component}")


def compatible_donor_abo(recipient_type: str, component: str) -> Set[str]:
    if component == "prc":
        return set(_RED_CELL_ABO[recipient_type])
    if component == "plasma":
        return set(_PLASMA_ABO[recipient_type])
    return {recipient_type}  # whole_blood, platelets: identik


def compatible_donor_rhesus(recipient_rhesus: str, component: str) -> Set[str]:
    if component in _RHESUS_MATTERS and recipient_rhesus == "negative":
        return {"negative"}
    return {"positive", "negative"}


def compatible_donor_groups(recipient_type: str, recipient_rhesus: str, component: str) -> FrozenSet[BloodGroup]:
    """Semua (golongan, rhesus) pendonor yang boleh diundang untuk penerima ini."""
    _check(recipient_type, recipient_rhesus, component)
    return frozenset((abo, rh)
                     for abo in compatible_donor_abo(recipient_type, component)
                     for rh in compatible_donor_rhesus(recipient_rhesus, component))


def is_compatible(donor: BloodGroup, recipient: BloodGroup, component: str) -> bool:
    return tuple(donor) in compatible_donor_groups(recipient[0], recipient[1], component)


def group_key(blood_type: str, rhesus: str) -> str:
    """Kunci ringkas untuk tampilan dan agregasi, mis. "o_negative"."""
    return f"{blood_type}_{rhesus}"
