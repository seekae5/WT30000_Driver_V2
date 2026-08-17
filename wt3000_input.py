# =============================================================================
# Datei: wt3000_input.py
# Layer 2 (Erweiterung) - Eingangs- und Messkonfiguration
#
# Deckt die bisher fehlenden Stellgroessen ab:
#   Verdrahtung, Spannungsbereich, Strombereich (direkt + Sensoreingang),
#   Auto-Range, Crest-Faktor, Line-/Frequenzfilter, Skalierung (State/VT/CT/
#   SFACtor/SRATio), Sync-Quelle, Update-Rate.
#
# Aendert nichts an wt3000_core.py, wt3000_numeric.py oder wt3000_itemspec.py.
#
# GRUNDREGEL DIESES MODULS
# Das Geraet ist metrologisch eingemessen. Jeder Schreibzugriff ist daher
# doppelt gesperrt: allow_changes=False (Default) und zusaetzlich eine
# Gruppensperre fuer die kritischen Bereiche (Wiring, Range, Scaling,
# CFactor). Entsperrt wird punktuell ueber den Kontextmanager unlocked().
# Jeder Set-Vorgang liest zurueck und prueft die Fehlerqueue.
# =============================================================================

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator

from wt3000_core import WTError, WTSession

_log = logging.getLogger("wt3000.input")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigLocked(WTError):
    """Ein Schreibzugriff wurde von der Sicherung dieses Moduls abgewiesen."""


class VerificationError(WTError):
    """Das Geraet hat den geschriebenen Wert nicht (oder anders) uebernommen."""


# ---------------------------------------------------------------------------
# Gruppen fuer die Schreibsperre
# ---------------------------------------------------------------------------

GROUP_WIRING: str = "WIRING"
GROUP_RANGE: str = "RANGE"
GROUP_AUTO: str = "AUTO"
GROUP_CFACTOR: str = "CFACTOR"
GROUP_FILTER: str = "FILTER"
GROUP_SCALING: str = "SCALING"
GROUP_SYNC: str = "SYNC"
GROUP_MODE: str = "MODE"
GROUP_RATE: str = "RATE"

ALL_GROUPS: frozenset[str] = frozenset(
    {
        GROUP_WIRING,
        GROUP_RANGE,
        GROUP_AUTO,
        GROUP_CFACTOR,
        GROUP_FILTER,
        GROUP_SCALING,
        GROUP_SYNC,
        GROUP_MODE,
        GROUP_RATE,
    }
)

# Diese Gruppen definieren den eingemessenen Zustand. Sie bleiben auch dann
# gesperrt, wenn allow_changes=True gesetzt wurde.
DEFAULT_PROTECTED: frozenset[str] = frozenset(
    {GROUP_WIRING, GROUP_RANGE, GROUP_SCALING, GROUP_CFACTOR}
)


# ---------------------------------------------------------------------------
# Aufzaehlungen (Parameterwerte laut IM WT3001E-17EN, Kap. 6.14 / 6.19)
# ---------------------------------------------------------------------------


class Wiring(str, Enum):
    """Verdrahtungsmuster einer Wiring-Unit."""

    P1W2 = "P1W2"  # 1P2W  - einphasig, zweileiter   -> 1 Element
    P1W3 = "P1W3"  # 1P3W  - einphasig, dreileiter   -> 2 Elemente
    P3W3 = "P3W3"  # 3P3W  - dreiphasig, dreileiter  -> 2 Elemente
    P3W4 = "P3W4"  # 3P4W  - dreiphasig, vierleiter  -> 3 Elemente
    V3A3 = "V3A3"  # 3P3W(3V3A)                      -> 3 Elemente
    NONE = "NONE"  # keine Verdrahtung               -> 0 Elemente


class SyncSource(str, Enum):
    """Synchronisationsquelle eines Elements."""

    U1 = "U1"
    U2 = "U2"
    U3 = "U3"
    U4 = "U4"
    I1 = "I1"
    I2 = "I2"
    I3 = "I3"
    I4 = "I4"
    EXTERNAL = "EXTernal"
    NONE = "NONE"


class LineFilter(str, Enum):
    """Line-Filter: AUS oder Grenzfrequenz.

    ZU VERIFIZIEREN am WT3000 (nicht WT3000E): ob alle drei Grenzfrequenzen
    angeboten werden. Bei aelterer Firmware kann der Umfang kleiner sein.
    Die Rueckleseprobe in _write_element() faengt das ab.
    """

    OFF = "OFF"
    HZ500 = "500HZ"
    KHZ5P5 = "5.5KHZ"
    KHZ50 = "50KHZ"


class MeasMode(str, Enum):
    """Messmodus fuer Spannung bzw. Strom."""

    RMS = "RMS"
    MEAN = "MEAN"
    DC = "DC"
    RMEAN = "RMEAN"


# Anzahl der Elemente, die ein Verdrahtungsmuster belegt.
PATTERN_ELEMENT_COUNT: dict[str, int] = {
    "P1W2": 1,
    "P1W3": 2,
    "P3W3": 2,
    "P3W4": 3,
    "V3A3": 3,
    "NONE": 0,
}

# Zulaessige Stellwerte. Werden vor dem Senden geprueft, damit kein
# unnoetiger Fehlereintrag im Geraet entsteht.
VOLTAGE_RANGES: dict[int, tuple[float, ...]] = {
    3: (15.0, 30.0, 60.0, 100.0, 150.0, 300.0, 600.0, 1000.0),
    6: (7.5, 15.0, 30.0, 50.0, 75.0, 150.0, 300.0, 500.0),
}

# Direkteingang, nach Elementtyp (30 A bzw. 2 A) und Crest-Faktor, in Ampere.
CURRENT_RANGES: dict[tuple[int, int], tuple[float, ...]] = {
    (30, 3): (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0),
    (30, 6): (0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0),
    (2, 3): (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0),
    (2, 6): (0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
}

# Externer Stromsensoreingang, in Volt.
SENSOR_RANGES: dict[int, tuple[float, ...]] = {
    3: (0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0),
    6: (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
}

# Datenaktualisierungsintervall in Sekunden.
UPDATE_RATES_S: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)

# Skalierungsfaktoren VT/CT/SFACtor/SRATio.
SCALING_MIN: float = 0.0001
SCALING_MAX: float = 99999.9999


# ---------------------------------------------------------------------------
# Kommandoknoten
# ---------------------------------------------------------------------------

_BASE_VOLT_RANGE = ":INPut:VOLTage:RANGe"
_BASE_VOLT_AUTO = ":INPut:VOLTage:AUTO"
_BASE_VOLT_MODE = ":INPut:VOLTage:MODE"
_BASE_CURR_RANGE = ":INPut:CURRent:RANGe"
_BASE_CURR_AUTO = ":INPut:CURRent:AUTO"
_BASE_CURR_MODE = ":INPut:CURRent:MODE"
_BASE_SRATIO = ":INPut:CURRent:SRATio"
_BASE_FILTER_LINE = ":INPut:FILTer:LINE"
_BASE_FILTER_FREQ = ":INPut:FILTer:FREQuency"
_BASE_SCAL_STATE = ":INPut:SCALing:STATe"
_BASE_SCAL_VT = ":INPut:SCALing:VT"
_BASE_SCAL_CT = ":INPut:SCALing:CT"
_BASE_SCAL_SFACTOR = ":INPut:SCALing:SFACtor"
_BASE_SYNC = ":INPut:SYNChronize"


def target_node(target: int | str) -> str:
    """Zieladressierung in den Knotennamen uebersetzen.

    Erlaubt sind 1..4, 'ALL', 'SIGMA' und 'SIGMB'. Bewusst OHNE Praefix-
    matching: 'SIGM' waere nicht entscheidbar und wuerde SigmaA und SigmaB
    stillschweigend gleichsetzen (derselbe Fehler, der in Stufe 3 im
    Itemvergleich gefunden wurde).
    """
    if isinstance(target, bool):  # bool ist Subtyp von int - hier sinnlos
        raise WTError(f"Ungueltiges Ziel: {target!r}")
    if isinstance(target, int):
        if not 1 <= target <= 4:
            raise WTError(f"Element {target} liegt ausserhalb 1..4")
        return f":ELEMent{target}"

    token = target.strip().upper()
    if token in {"ALL", "SIGMA", "SIGMB"}:
        return f":{token}"
    raise WTError(
        f"Ungueltiges Ziel {target!r}. Erlaubt: 1..4, 'ALL', 'SIGMA', 'SIGMB'."
    )


def _node(base: str, target: int | str) -> str:
    """Vollstaendigen Kommandoknoten bauen."""
    return f"{base}{target_node(target)}"


# ---------------------------------------------------------------------------
# Antwort-Parser
# ---------------------------------------------------------------------------


def strip_header(response: str) -> str:
    """Fuehrenden Kommandoheader entfernen, falls vorhanden.

    Bei ':COMMunicate:HEADer 0' (Sollzustand dieses Projekts) liefert das
    Geraet nur den Wert. Ist HEADer eingeschaltet, kommt
    ':INPUT:VOLTAGE:RANGE:ELEMENT1 1.000E+03', bei verketteten Antworten auch
    die verkuerzte Form 'ELEMENT2 OFF'.

    Regel: Geraetewerte enthalten nie ein Leerzeichen ('EXTERNAL,10.00E+00'
    ist ein Token). Also ist der Wert immer das letzte Whitespace-Token.
    """
    parts = response.strip().split()
    return parts[-1] if parts else ""


def split_multi(response: str) -> list[str]:
    """Antwort mehrerer Werte (';'-getrennt) in Einzelwerte zerlegen.

    Beispiel (HEADer ein):
    ':INPUT:FILTER:LINE:ELEMENT1 OFF;ELEMENT2 OFF' -> ['OFF', 'OFF']
    """
    return [strip_header(part) for part in response.split(";") if part.strip()]


def parse_bool(text: str) -> bool:
    """<Boolean>-Antwort auswerten. Das Geraet antwortet '1' bzw. '0'."""
    token = strip_header(text).upper()
    if token in {"1", "ON"}:
        return True
    if token in {"0", "OFF"}:
        return False
    raise WTError(f"Kein Boolescher Wert: {text!r}")


def parse_float(text: str) -> float:
    """NRf-Antwort in float wandeln (z.B. '1.000E+03', '500.0E-03')."""
    token = strip_header(text)
    try:
        return float(token)
    except ValueError as exc:
        raise WTError(f"Keine Zahl: {text!r}") from exc


def parse_current_range(text: str) -> tuple[float | None, float | None]:
    """Strombereich auswerten.

    Rueckgabe: (Direktbereich in A, Sensorbereich in V). Genau einer der
    beiden Werte ist gesetzt, der andere ist None.
    'EXTERNAL,10.00E+00' -> (None, 10.0);  '30.0E+00' -> (30.0, None)
    """
    token = strip_header(text).upper()
    if token.startswith("EXT"):
        _, _, volts = token.partition(",")
        return None, parse_float(volts)
    return parse_float(token), None


def parse_line_filter(text: str) -> str:
    """Line-Filter-Antwort normalisieren: 'OFF' oder Grenzfrequenz in Hz."""
    token = strip_header(text).upper()
    if token.startswith("OFF") or token == "0":
        return "OFF"
    return f"{parse_float(token):g}"


# ---------------------------------------------------------------------------
# Vergleichsregeln fuer die Rueckleseprobe
# ---------------------------------------------------------------------------


def _float_close(expected: float, actual: float, rel: float = 1e-3) -> bool:
    """Relativer Vergleich. Das Geraet rundet auf 4 signifikante Stellen."""
    if expected == 0.0:
        return abs(actual) <= rel
    return abs(expected - actual) <= rel * abs(expected)


def _token_match(requested: str, actual: str) -> bool:
    """Geraeteantwort in Kurzform gegen die gesendete Langform pruefen.

    VERBose ist aus, das Geraet antwortet in SCPI-Kurzform ('EXT' statt
    'EXTERNAL'). Wie in wt3000_itemspec._functions_compatible() laeuft die
    Pruefung nur in EINE Richtung: die Antwort muss Praefix der Anforderung
    sein, nicht umgekehrt.
    """
    req, act = requested.strip().upper(), actual.strip().upper()
    return req == act or req.startswith(act)


def _nearest(value: float, allowed: Iterable[float]) -> float:
    """Nachbarwert aus der Liste der zulaessigen Stellwerte."""
    return min(allowed, key=lambda candidate: abs(candidate - value))


def _check_allowed(value: float, allowed: tuple[float, ...], what: str) -> float:
    """Stellwert gegen die Liste pruefen und den exakten Listenwert liefern."""
    match = _nearest(value, allowed)
    if not _float_close(match, value, rel=1e-6):
        raise WTError(
            f"{what}: {value:g} ist kein zulaessiger Stellwert. "
            f"Erlaubt: {', '.join(f'{v:g}' for v in allowed)}"
        )
    return match


# ---------------------------------------------------------------------------
# Formatierung der Stellwerte
# ---------------------------------------------------------------------------


def format_voltage(volts: float) -> str:
    """Spannungsangabe fuer das Geraet ('1000V', '7.5V')."""
    return f"{volts:g}V"


def format_current(amps: float) -> str:
    """Stromangabe. Unter 1 A wird in mA formuliert (Schreibweise Handbuch)."""
    if amps < 1.0:
        return f"{amps * 1000.0:g}MA"
    return f"{amps:g}A"


def format_rate(seconds: float) -> str:
    """Update-Rate als Geraeteparameter ('500MS', '1S')."""
    if seconds < 1.0:
        return f"{seconds * 1000.0:g}MS"
    return f"{seconds:g}S"


# ---------------------------------------------------------------------------
# Wiring-Units
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WiringUnit:
    """Eine Wiring-Unit inkl. der von ihr belegten Elemente."""

    name: str  # 'SIGMA' oder 'SIGMB'
    pattern: str  # z.B. 'V3A3'
    elements: tuple[int, ...]  # z.B. (1, 2, 3)


def resolve_wiring_units(patterns: Iterable[str]) -> list[WiringUnit]:
    """Aus der Verdrahtungsliste die Elementzuordnung der Units ableiten.

    Die Muster werden laut Handbuch in Elementreihenfolge angegeben, also
    belegt Muster 1 die ersten n Elemente, Muster 2 die folgenden usw.
    Beispiel V3A3,P1W2 -> SigmaA = 1..3, SigmaB = 4.

    ZU VERIFIZIEREN: ob das Geraet bei mehr als zwei Units ueberhaupt noch
    SIGMA/SIGMB anbietet. Ueber zwei Units hinaus wird hier nichts benannt.
    """
    units: list[WiringUnit] = []
    next_element = 1
    for position, raw in enumerate(patterns):
        pattern = raw.strip().upper()
        count = PATTERN_ELEMENT_COUNT.get(pattern)
        if count is None:
            raise WTError(f"Unbekanntes Verdrahtungsmuster: {raw!r}")
        elements = tuple(range(next_element, next_element + count))
        next_element += count
        if count == 0:
            continue
        name = {0: "SIGMA", 1: "SIGMB"}.get(position, "")
        units.append(WiringUnit(name=name, pattern=pattern, elements=elements))
    return units


# ---------------------------------------------------------------------------
# Layer 2 - Konfigurationsobjekt
# ---------------------------------------------------------------------------


class InputConfig:
    """Lesen und (gesichertes) Schreiben der Eingangs-/Messkonfiguration.

    Lesen ist immer erlaubt. Schreiben verlangt allow_changes=True UND eine
    Gruppe, die nicht in protected_groups steht.
    """

    def __init__(
        self,
        session: WTSession,
        allow_changes: bool = False,
        protected_groups: frozenset[str] = DEFAULT_PROTECTED,
        verify: bool = True,
        check_errors: bool = True,
    ) -> None:
        self._session = session
        self._allow_changes = allow_changes
        self._protected = set(protected_groups)
        self._verify = verify
        self._check_errors = check_errors
        self._module_cache: dict[int, int] | None = None

    # -- Sperre -------------------------------------------------------------

    @property
    def protected_groups(self) -> frozenset[str]:
        """Aktuell gesperrte Gruppen."""
        return frozenset(self._protected)

    @contextmanager
    def unlocked(self, *groups: str) -> Iterator["InputConfig"]:
        """Gruppen fuer die Dauer des Blocks freigeben.

        Beispiel:
            with cfg.unlocked(GROUP_RATE):
                cfg.set_update_rate(0.5)
        """
        unknown = {g for g in groups if g not in ALL_GROUPS}
        if unknown:
            raise WTError(f"Unbekannte Gruppe(n): {sorted(unknown)}")

        previous_allow = self._allow_changes
        previous_protected = set(self._protected)
        self._allow_changes = True
        self._protected -= set(groups)
        _log.warning("Schreibzugriff freigegeben fuer: %s", ", ".join(groups))
        try:
            yield self
        finally:
            self._allow_changes = previous_allow
            self._protected = previous_protected
            _log.info("Schreibzugriff wieder gesperrt")

    def _require_writable(self, group: str) -> None:
        """Vor jedem Set-Kommando pruefen, ob geschrieben werden darf."""
        if not self._allow_changes:
            raise ConfigLocked(
                f"Schreibzugriff auf '{group}' abgelehnt: InputConfig wurde mit "
                "allow_changes=False erzeugt. Freigabe ueber unlocked()."
            )
        if group in self._protected:
            raise ConfigLocked(
                f"Gruppe '{group}' ist geschuetzt (eingemessener Zustand). "
                f"Freigabe ausdruecklich ueber: with cfg.unlocked('{group}'): ..."
            )

    # -- Basisoperationen ---------------------------------------------------

    def _query(self, node: str) -> str:
        """Query absetzen und den Header entfernen."""
        return strip_header(self._session.query(f"{node}?"))

    def _write_scalar(
        self,
        group: str,
        command: str,
        query_node: str,
        matches: Callable[[str], bool],
        label: str,
    ) -> None:
        """Geraeteweites Set-Kommando senden, zuruecklesen und pruefen.

        Set-Kommandos sind rund 50x langsamer als Queries (100-250 ms,
        gemessen in Stufe 3). Die Rueckleseprobe kostet vergleichsweise wenig
        und ist der einzige belastbare Nachweis, dass der Wert angekommen ist -
        das Geraet quittiert Set-Kommandos nicht.
        """
        self._require_writable(group)
        _log.info("SET %s", command)
        self._session.write(command)

        if self._verify:
            actual = self._query(query_node)
            if not matches(actual):
                errors = self._session.read_error_queue()
                raise VerificationError(
                    f"{label}: gesendet {command!r}, zurueckgelesen {actual!r}. "
                    f"Fehlerqueue: {errors}"
                )
            _log.info("  verifiziert: %s = %s", query_node, actual)

        if self._check_errors:
            self._session.assert_no_error(label)

    def _write_element(
        self,
        group: str,
        base: str,
        target: int | str,
        parameter: str,
        matches: Callable[[str], bool],
        label: str,
    ) -> None:
        """Elementbezogenes Set-Kommando senden und elementweise zuruecklesen.

        Ein einziger Schreibpfad fuer Element-, ALL- und SIGMA-Ziele: das
        Kommando geht in der gewuenschten Form raus, die Kontrolle laeuft
        immer ueber die Einzelabfragen der betroffenen Elemente.
        """
        self._require_writable(group)
        command = f"{_node(base, target)} {parameter}"
        _log.info("SET %s", command)
        self._session.write(command)

        if self._verify:
            self._verify_group(target, base, matches, label)

        if self._check_errors:
            self._session.assert_no_error(label)

    def _elements_of(self, target: int | str) -> tuple[int, ...]:
        """Elemente ermitteln, die von einem Ziel betroffen sind."""
        if isinstance(target, int):
            return (target,)
        token = target.strip().upper()
        if token == "ALL":
            return (1, 2, 3, 4)
        for unit in resolve_wiring_units(self.get_wiring()):
            if unit.name == token:
                return unit.elements
        raise WTError(
            f"Wiring-Unit {token} existiert bei der aktuellen Verdrahtung "
            f"{self.get_wiring()} nicht"
        )

    # =======================================================================
    # Lesen
    # =======================================================================

    def get_crest_factor(self) -> int:
        """Aktueller Crest-Faktor (3 oder 6)."""
        return int(parse_float(self._query(":INPut:CFACtor")))

    def get_wiring(self) -> tuple[str, ...]:
        """Verdrahtungsmuster aller Wiring-Units in Elementreihenfolge."""
        answer = self._query(":INPut:WIRing")
        return tuple(part.strip().upper() for part in answer.split(",") if part.strip())

    def get_wiring_units(self) -> list[WiringUnit]:
        """Wiring-Units inkl. Elementzuordnung."""
        return resolve_wiring_units(self.get_wiring())

    def get_independent(self) -> bool:
        """True, wenn die Elemente unabhaengig voneinander eingestellt werden.

        Ist das aus, wirkt eine Bereichsaenderung an einem Element auf die
        ganze Wiring-Unit. Das ist die haeufigste Ueberraschung beim
        elementweisen Setzen.
        """
        return parse_bool(self._query(":INPut:INDependent"))

    def get_module(self, element: int) -> int:
        """Elementtyp: 30 (30-A-Element), 2 (2-A-Element) oder 0 (nicht bestueckt)."""
        if self._module_cache is None:
            answer = self._query(":INPut:MODUle")
            values = [int(parse_float(v)) for v in answer.replace(";", ",").split(",")]
            self._module_cache = {i: v for i, v in enumerate(values, start=1)}
        if element not in self._module_cache:
            raise WTError(f"Kein Elementtyp fuer Element {element} bekannt")
        return self._module_cache[element]

    def get_update_rate(self) -> float:
        """Datenaktualisierungsintervall in Sekunden."""
        return parse_float(self._query(":RATE"))

    def get_voltage_range(self, element: int) -> float:
        """Spannungsmessbereich eines Elements in Volt."""
        return parse_float(self._query(_node(_BASE_VOLT_RANGE, element)))

    def get_current_range(self, element: int) -> tuple[float | None, float | None]:
        """Strommessbereich: (Direktbereich in A, Sensorbereich in V)."""
        return parse_current_range(self._query(_node(_BASE_CURR_RANGE, element)))

    def get_voltage_auto(self, element: int) -> bool:
        """Auto-Range Spannung."""
        return parse_bool(self._query(_node(_BASE_VOLT_AUTO, element)))

    def get_current_auto(self, element: int) -> bool:
        """Auto-Range Strom."""
        return parse_bool(self._query(_node(_BASE_CURR_AUTO, element)))

    def get_voltage_mode(self, element: int) -> str:
        """Messmodus Spannung (RMS/MEAN/DC/RMEAN)."""
        return self._query(_node(_BASE_VOLT_MODE, element)).upper()

    def get_current_mode(self, element: int) -> str:
        """Messmodus Strom (RMS/MEAN/DC/RMEAN)."""
        return self._query(_node(_BASE_CURR_MODE, element)).upper()

    def get_line_filter(self, element: int) -> str:
        """Line-Filter: 'OFF' oder Grenzfrequenz in Hz als Text."""
        return parse_line_filter(self._query(_node(_BASE_FILTER_LINE, element)))

    def get_frequency_filter(self, element: int) -> bool:
        """Frequenzfilter (Filter im Synchronisationspfad)."""
        return parse_bool(self._query(_node(_BASE_FILTER_FREQ, element)))

    def get_scaling_state(self, element: int) -> bool:
        """Skalierung aktiv?"""
        return parse_bool(self._query(_node(_BASE_SCAL_STATE, element)))

    def get_vt_ratio(self, element: int) -> float:
        """VT-Verhaeltnis (Spannungswandler)."""
        return parse_float(self._query(_node(_BASE_SCAL_VT, element)))

    def get_ct_ratio(self, element: int) -> float:
        """CT-Verhaeltnis (Stromwandler)."""
        return parse_float(self._query(_node(_BASE_SCAL_CT, element)))

    def get_power_factor(self, element: int) -> float:
        """Leistungsskalierungsfaktor SFACtor."""
        return parse_float(self._query(_node(_BASE_SCAL_SFACTOR, element)))

    def get_sensor_ratio(self, element: int) -> float:
        """Sensorkonstante SRATio des externen Stromsensors (mV/A)."""
        return parse_float(self._query(_node(_BASE_SRATIO, element)))

    def get_sync_source(self, element: int) -> str:
        """Synchronisationsquelle des Elements (z.B. 'I3', 'U3', 'EXT', 'NONE')."""
        return self._query(_node(_BASE_SYNC, element)).upper()

    def get_raw_input_dump(self) -> str:
        """Rohantwort von ':INPut?' - vollstaendiger Abzug als Beleg."""
        return self._session.query(":INPut?")

    # =======================================================================
    # Schreiben
    # =======================================================================

    # -- Verdrahtung --------------------------------------------------------

    def set_wiring(self, patterns: Iterable[str | Wiring]) -> None:
        """Verdrahtung setzen, Muster in Elementreihenfolge.

        Beispiel: set_wiring([Wiring.V3A3, Wiring.P1W2]) -> SigmaA = Elemente
        1..3 (3P3W/3V3A), SigmaB = Element 4 (1P2W).

        Achtung: Mit der Verdrahtung aendert sich, welche Elemente zu welcher
        Wiring-Unit gehoeren und ob SIGMA/SIGMB ueberhaupt existieren. Alle
        SIGMA/SIGMB-Kommandos danach sind neu zu bewerten.
        """
        tokens = [str(p.value if isinstance(p, Wiring) else p).strip().upper() for p in patterns]
        if not tokens:
            raise WTError("Leere Verdrahtungsangabe")
        for token in tokens:
            if token not in PATTERN_ELEMENT_COUNT:
                raise WTError(f"Unbekanntes Verdrahtungsmuster: {token!r}")

        used = sum(PATTERN_ELEMENT_COUNT[t] for t in tokens)
        if used > 4:
            raise WTError(f"Verdrahtung belegt {used} Elemente, das Geraet hat 4")

        expected = [t for t in tokens if t != "NONE"]

        def matches(actual: str) -> bool:
            got = [p.strip().upper() for p in actual.split(",") if p.strip()]
            got = [p for p in got if p != "NONE"]
            return got == expected

        self._write_scalar(
            GROUP_WIRING,
            f":INPut:WIRing {','.join(tokens)}",
            ":INPut:WIRing",
            matches,
            "Verdrahtung setzen",
        )
        self._module_cache = None

    # -- Crest-Faktor -------------------------------------------------------

    def set_crest_factor(self, factor: int) -> None:
        """Crest-Faktor auf 3 oder 6 setzen.

        Der Crest-Faktor bestimmt die zulaessigen Messbereiche. Nach einer
        Aenderung kann das Geraet bestehende Bereiche verschieben - deshalb
        immer zuerst den Crest-Faktor, dann die Bereiche setzen.
        """
        if factor not in (3, 6):
            raise WTError(f"Crest-Faktor {factor} unzulaessig, erlaubt sind 3 und 6")
        self._write_scalar(
            GROUP_CFACTOR,
            f":INPut:CFACtor {factor}",
            ":INPut:CFACtor",
            lambda actual: int(parse_float(actual)) == factor,
            "Crest-Faktor setzen",
        )

    # -- Bereiche -----------------------------------------------------------

    def set_voltage_range(self, volts: float, target: int | str = "ALL") -> None:
        """Spannungsmessbereich fest setzen (schaltet Auto-Range ab)."""
        crest = self.get_crest_factor()
        value = _check_allowed(volts, VOLTAGE_RANGES[crest], f"Spannungsbereich (CF{crest})")
        self._warn_if_not_independent(target)

        self._write_element(
            GROUP_RANGE,
            _BASE_VOLT_RANGE,
            target,
            format_voltage(value),
            lambda actual: _float_close(value, parse_float(actual)),
            "Spannungsbereich setzen",
        )

    def set_current_range(self, amps: float, target: int | str = "ALL") -> None:
        """Strommessbereich fuer den DIREKTEN Stromeingang setzen.

        Fuer Elemente mit externem Stromsensor ist stattdessen
        set_current_range_sensor() zu verwenden - ein Direktbereich wuerde die
        Sensorbeschaltung aus der Konfiguration werfen.
        """
        crest = self.get_crest_factor()
        elements = self._elements_of(target)
        modules = {self.get_module(e) for e in elements}
        if len(modules) > 1:
            raise WTError(
                "Gemischte Elementtypen im Ziel - der Direktbereich laesst sich "
                "nicht sammelweise setzen (Geraetefehler 863). Elementweise setzen."
            )
        module = modules.pop()
        if module == 0:
            raise WTError(f"Ziel {target!r} enthaelt ein nicht bestuecktes Element")

        value = _check_allowed(
            amps, CURRENT_RANGES[(module, crest)], f"Strombereich ({module} A-Element, CF{crest})"
        )
        self._warn_if_not_independent(target)

        def matches(actual: str) -> bool:
            direct, sensor = parse_current_range(actual)
            return sensor is None and direct is not None and _float_close(value, direct)

        self._write_element(
            GROUP_RANGE,
            _BASE_CURR_RANGE,
            target,
            format_current(value),
            matches,
            "Strombereich setzen",
        )

    def set_current_range_sensor(self, volts: float, target: int | str = "ALL") -> None:
        """Bereich des externen Stromsensoreingangs setzen (EXTernal,<Volt>)."""
        crest = self.get_crest_factor()
        value = _check_allowed(volts, SENSOR_RANGES[crest], f"Sensorbereich (CF{crest})")
        self._warn_if_not_independent(target)

        def matches(actual: str) -> bool:
            direct, sensor = parse_current_range(actual)
            return direct is None and sensor is not None and _float_close(value, sensor)

        self._write_element(
            GROUP_RANGE,
            _BASE_CURR_RANGE,
            target,
            f"EXTernal,{format_voltage(value)}",
            matches,
            "Sensorbereich setzen",
        )

    # -- Auto-Range ---------------------------------------------------------

    def set_voltage_auto_range(self, enabled: bool, target: int | str = "ALL") -> None:
        """Auto-Range Spannung ein-/ausschalten.

        Auto-Range ist fuer Vergleichsmessungen mit Vorsicht zu geniessen: der
        Bereichswechsel faellt mitten in ein Messintervall und macht einzelne
        Datensaetze unbrauchbar.
        """
        self._set_boolean(GROUP_AUTO, _BASE_VOLT_AUTO, enabled, target, "Auto-Range Spannung")

    def set_current_auto_range(self, enabled: bool, target: int | str = "ALL") -> None:
        """Auto-Range Strom ein-/ausschalten."""
        self._set_boolean(GROUP_AUTO, _BASE_CURR_AUTO, enabled, target, "Auto-Range Strom")

    # -- Filter -------------------------------------------------------------

    def set_line_filter(self, value: LineFilter | str | float, target: int | str = "ALL") -> None:
        """Line-Filter setzen: 'OFF' oder Grenzfrequenz.

        Zulaessig sind LineFilter.OFF, .HZ500, .KHZ5P5, .KHZ50 - alternativ
        'OFF' oder eine Frequenz in Hz (500, 5500, 50000).
        """
        parameter, expected = _line_filter_parameter(value)

        def matches(actual: str) -> bool:
            got = parse_line_filter(actual)
            if expected == "OFF":
                return got == "OFF"
            return got != "OFF" and _float_close(float(expected), float(got), rel=1e-2)

        self._write_element(
            GROUP_FILTER,
            _BASE_FILTER_LINE,
            target,
            parameter,
            matches,
            "Line-Filter setzen",
        )

    def set_frequency_filter(self, enabled: bool, target: int | str = "ALL") -> None:
        """Frequenzfilter ein-/ausschalten (stabilisiert die Synchronisation)."""
        self._set_boolean(GROUP_FILTER, _BASE_FILTER_FREQ, enabled, target, "Frequenzfilter")

    # -- Skalierung ---------------------------------------------------------

    def set_scaling_state(self, enabled: bool, target: int | str = "ALL") -> None:
        """Skalierung (VT/CT/SFACtor) ein-/ausschalten.

        Achtung: Das Abschalten aendert saemtliche Messwerte des Elements um
        den Skalierungsfaktor. In diesem Aufbau ist Skalierung ueberall aktiv.
        """
        self._set_boolean(GROUP_SCALING, _BASE_SCAL_STATE, enabled, target, "Skalierung")

    def set_vt_ratio(self, ratio: float, target: int | str = "ALL") -> None:
        """VT-Verhaeltnis setzen (Spannungswandler)."""
        self._set_scaling_factor(_BASE_SCAL_VT, ratio, target, "VT-Verhaeltnis")

    def set_ct_ratio(self, ratio: float, target: int | str = "ALL") -> None:
        """CT-Verhaeltnis setzen (Stromwandler)."""
        self._set_scaling_factor(_BASE_SCAL_CT, ratio, target, "CT-Verhaeltnis")

    def set_power_factor(self, factor: float, target: int | str = "ALL") -> None:
        """Leistungsskalierungsfaktor SFACtor setzen."""
        self._set_scaling_factor(_BASE_SCAL_SFACTOR, factor, target, "Leistungsfaktor")

    def set_sensor_ratio(self, ratio: float, target: int | str = "ALL") -> None:
        """Sensorkonstante SRATio des externen Stromsensors setzen (mV/A)."""
        self._set_scaling_factor(_BASE_SRATIO, ratio, target, "Sensorkonstante")

    # -- Sync-Quelle --------------------------------------------------------

    def set_sync_source(self, source: SyncSource | str, target: int | str = "ALL") -> None:
        """Synchronisationsquelle setzen.

        Erlaubt sind U1..U4, I1..I4, EXTernal und NONE. Die Sync-Quelle
        bestimmt das Messfenster; sie ist damit auch die Ursache Nr. 1 fuer
        unplausible Leistungswerte, wenn sie auf einen Kanal ohne Signal
        zeigt (Condition-Bit 7, PLLE).
        """
        token = str(source.value if isinstance(source, SyncSource) else source).strip()
        upper = token.upper()
        valid = {s.value.upper() for s in SyncSource}
        if upper not in valid:
            raise WTError(
                f"Ungueltige Sync-Quelle {source!r}. Erlaubt: "
                f"{', '.join(sorted(valid))}"
            )

        self._write_element(
            GROUP_SYNC,
            _BASE_SYNC,
            target,
            token,
            lambda actual: _token_match(upper, actual),
            "Sync-Quelle setzen",
        )

    # -- Messmodus ----------------------------------------------------------

    def set_voltage_mode(self, mode: MeasMode | str, target: int | str = "ALL") -> None:
        """Messmodus Spannung (RMS/MEAN/DC/RMEAN)."""
        self._set_mode(_BASE_VOLT_MODE, mode, target, "Spannungsmodus")

    def set_current_mode(self, mode: MeasMode | str, target: int | str = "ALL") -> None:
        """Messmodus Strom (RMS/MEAN/DC/RMEAN)."""
        self._set_mode(_BASE_CURR_MODE, mode, target, "Strommodus")

    # -- Update-Rate --------------------------------------------------------

    def set_update_rate(self, seconds: float) -> None:
        """Datenaktualisierungsintervall setzen.

        Zulaessig: 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20 s. Das Abtastintervall
        der Messschleife darf diesen Wert nicht unterschreiten, sonst werden
        identische Datensaetze mehrfach aufgezeichnet.
        """
        value = _check_allowed(seconds, UPDATE_RATES_S, "Update-Rate")
        self._write_scalar(
            GROUP_RATE,
            f":RATE {format_rate(value)}",
            ":RATE",
            lambda actual: _float_close(value, parse_float(actual)),
            "Update-Rate setzen",
        )

    # -- interne Helfer -----------------------------------------------------

    def _set_boolean(
        self, group: str, base: str, enabled: bool, target: int | str, label: str
    ) -> None:
        """Gemeinsamer Pfad fuer alle <Boolean>-Einstellungen."""
        self._write_element(
            group,
            base,
            target,
            "ON" if enabled else "OFF",
            lambda actual: parse_bool(actual) is enabled,
            f"{label} setzen",
        )

    def _set_scaling_factor(
        self, base: str, value: float, target: int | str, label: str
    ) -> None:
        """Gemeinsamer Pfad fuer VT, CT, SFACtor und SRATio."""
        if not SCALING_MIN <= value <= SCALING_MAX:
            raise WTError(
                f"{label}: {value} liegt ausserhalb {SCALING_MIN} .. {SCALING_MAX}"
            )
        self._write_element(
            GROUP_SCALING,
            base,
            target,
            f"{value:.4f}",
            lambda actual: _float_close(value, parse_float(actual), rel=1e-4),
            f"{label} setzen",
        )

    def _set_mode(
        self, base: str, mode: MeasMode | str, target: int | str, label: str
    ) -> None:
        """Gemeinsamer Pfad fuer VOLTage:MODE und CURRent:MODE."""
        token = str(mode.value if isinstance(mode, MeasMode) else mode).strip().upper()
        if token not in {m.value for m in MeasMode}:
            raise WTError(f"{label}: {mode!r} unzulaessig (RMS, MEAN, DC, RMEAN)")
        self._write_element(
            GROUP_MODE,
            base,
            target,
            token,
            lambda actual: _token_match(token, actual),
            f"{label} setzen",
        )

    def _verify_group(
        self, target: int | str, base: str, matches: Callable[[str], bool], label: str
    ) -> None:
        """Sammelkommandos elementweise zuruecklesen.

        ':...:ALL?' und ':...:SIGMA?' gibt es nicht - fuer die Rueckleseprobe
        muss jedes betroffene Element einzeln abgefragt werden.
        """
        for element in self._elements_of(target):
            actual = self._query(_node(base, element))
            if not matches(actual):
                errors = self._session.read_error_queue()
                raise VerificationError(
                    f"{label}: Element {element} zeigt {actual!r}. Fehlerqueue: {errors}"
                )
        _log.info("  verifiziert fuer Elemente %s", list(self._elements_of(target)))

    def _warn_if_not_independent(self, target: int | str) -> None:
        """Warnen, wenn elementweise gesetzt wird, obwohl INDependent aus ist."""
        if isinstance(target, int) and not self.get_independent():
            _log.warning(
                "':INPut:INDependent' ist AUS - die Bereichsaenderung an Element %d "
                "wirkt auf die gesamte Wiring-Unit",
                target,
            )


# ---------------------------------------------------------------------------
# Line-Filter-Parameter
# ---------------------------------------------------------------------------


def _line_filter_parameter(value: LineFilter | str | float) -> tuple[str, str]:
    """(Geraeteparameter, Erwartungswert fuer die Rueckleseprobe) bestimmen.

    Akzeptiert LineFilter, 'OFF', '500HZ' ... und reine Frequenzzahlen
    (500, 5500.0, '50000') - Letzteres, weil der Snapshot die Grenzfrequenz
    als Zahlentext fuehrt.
    """
    if isinstance(value, LineFilter):
        token = value.value
    elif isinstance(value, (int, float)):
        token = f"{float(value):g}"
    else:
        token = str(value).strip().upper()

    # Reine Frequenzangabe auf das Geraetetoken abbilden.
    try:
        hertz = float(token)
    except ValueError:
        pass
    else:
        token = {0.0: "OFF", 500.0: "500HZ", 5500.0: "5.5KHZ", 50000.0: "50KHZ"}.get(
            hertz, token
        )

    if token in {"OFF", "0"}:
        return "OFF", "OFF"
    if token in {"500HZ", "500"}:
        return "500HZ", "500"
    if token in {"5.5KHZ", "5500"}:
        return "5.5KHZ", "5500"
    if token in {"50KHZ", "50000"}:
        return "50KHZ", "50000"
    raise WTError(
        f"Line-Filter {value!r} unzulaessig. Erlaubt: OFF, 500HZ, 5.5KHZ, 50KHZ"
    )


# ---------------------------------------------------------------------------
# Snapshot: sichern, vergleichen, zurueckstellen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ElementSettings:
    """Vollstaendiger Einstellungssatz eines Eingangselements."""

    element: int
    module: int
    voltage_range: float
    voltage_auto: bool
    voltage_mode: str
    current_direct: float | None
    current_sensor: float | None
    current_auto: bool
    current_mode: str
    sensor_ratio: float
    line_filter: str
    frequency_filter: bool
    scaling: bool
    vt_ratio: float
    ct_ratio: float
    power_factor: float
    sync_source: str

    def to_dict(self) -> dict:
        """Serialisierbare Form."""
        return {
            "element": self.element,
            "module": self.module,
            "voltage_range": self.voltage_range,
            "voltage_auto": self.voltage_auto,
            "voltage_mode": self.voltage_mode,
            "current_direct": self.current_direct,
            "current_sensor": self.current_sensor,
            "current_auto": self.current_auto,
            "current_mode": self.current_mode,
            "sensor_ratio": self.sensor_ratio,
            "line_filter": self.line_filter,
            "frequency_filter": self.frequency_filter,
            "scaling": self.scaling,
            "vt_ratio": self.vt_ratio,
            "ct_ratio": self.ct_ratio,
            "power_factor": self.power_factor,
            "sync_source": self.sync_source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ElementSettings":
        """Gegenstueck zu to_dict()."""
        return cls(**data)


@dataclass(frozen=True)
class InputSnapshot:
    """Abzug der gesamten Eingangskonfiguration."""

    crest_factor: int
    wiring: tuple[str, ...]
    independent: bool
    update_rate_s: float
    elements: tuple[ElementSettings, ...]
    raw_dump: str

    # -- Erfassen -----------------------------------------------------------

    @classmethod
    def capture(cls, config: InputConfig) -> "InputSnapshot":
        """Kompletten Ist-Zustand lesen. Reine Leseoperation."""
        raw = config.get_raw_input_dump()
        elements: list[ElementSettings] = []
        for element in range(1, 5):
            module = config.get_module(element)
            if module == 0:
                continue
            direct, sensor = config.get_current_range(element)
            elements.append(
                ElementSettings(
                    element=element,
                    module=module,
                    voltage_range=config.get_voltage_range(element),
                    voltage_auto=config.get_voltage_auto(element),
                    voltage_mode=config.get_voltage_mode(element),
                    current_direct=direct,
                    current_sensor=sensor,
                    current_auto=config.get_current_auto(element),
                    current_mode=config.get_current_mode(element),
                    sensor_ratio=config.get_sensor_ratio(element),
                    line_filter=config.get_line_filter(element),
                    frequency_filter=config.get_frequency_filter(element),
                    scaling=config.get_scaling_state(element),
                    vt_ratio=config.get_vt_ratio(element),
                    ct_ratio=config.get_ct_ratio(element),
                    power_factor=config.get_power_factor(element),
                    sync_source=config.get_sync_source(element),
                )
            )

        snapshot = cls(
            crest_factor=config.get_crest_factor(),
            wiring=config.get_wiring(),
            independent=config.get_independent(),
            update_rate_s=config.get_update_rate(),
            elements=tuple(elements),
            raw_dump=raw,
        )
        _log.info(
            "Eingangskonfiguration gesichert: CF%d, Wiring %s, RATE %.3f s, %d Elemente",
            snapshot.crest_factor,
            ",".join(snapshot.wiring),
            snapshot.update_rate_s,
            len(snapshot.elements),
        )
        return snapshot

    # -- Persistenz ---------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialisierbare Form."""
        return {
            "crest_factor": self.crest_factor,
            "wiring": list(self.wiring),
            "independent": self.independent,
            "update_rate_s": self.update_rate_s,
            "elements": [e.to_dict() for e in self.elements],
            "raw_dump": self.raw_dump,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InputSnapshot":
        """Gegenstueck zu to_dict()."""
        return cls(
            crest_factor=int(data["crest_factor"]),
            wiring=tuple(data["wiring"]),
            independent=bool(data["independent"]),
            update_rate_s=float(data["update_rate_s"]),
            elements=tuple(ElementSettings.from_dict(e) for e in data["elements"]),
            raw_dump=data.get("raw_dump", ""),
        )

    def save(self, path: Path) -> None:
        """Snapshot als JSON ablegen - vor jedem Schreibzugriff Pflicht."""
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        _log.info("Eingangskonfiguration gesichert nach %s", path)

    @classmethod
    def load(cls, path: Path) -> "InputSnapshot":
        """Snapshot aus JSON laden."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # -- Vergleich ----------------------------------------------------------

    def diff(self, other: "InputSnapshot") -> list[str]:
        """Abweichungen gegenueber einem anderen Snapshot auflisten.

        self = Soll, other = Ist. Leere Liste bedeutet: identisch.
        """
        problems: list[str] = []
        if self.crest_factor != other.crest_factor:
            problems.append(f"Crest-Faktor: soll {self.crest_factor}, ist {other.crest_factor}")
        if self.wiring != other.wiring:
            problems.append(f"Wiring: soll {self.wiring}, ist {other.wiring}")
        if self.independent != other.independent:
            problems.append(f"INDependent: soll {self.independent}, ist {other.independent}")
        if not _float_close(self.update_rate_s, other.update_rate_s):
            problems.append(
                f"Update-Rate: soll {self.update_rate_s} s, ist {other.update_rate_s} s"
            )

        actual_by_element = {e.element: e for e in other.elements}
        for wanted in self.elements:
            got = actual_by_element.get(wanted.element)
            if got is None:
                problems.append(f"Element {wanted.element} fehlt im Vergleichssnapshot")
                continue
            problems.extend(_diff_element(wanted, got))
        return problems

    def log_summary(self) -> None:
        """Uebersicht ins Log schreiben."""
        _log.info("-" * 78)
        _log.info(
            "Crest-Faktor %d | Wiring %s | INDependent %s | RATE %.3f s",
            self.crest_factor,
            ",".join(self.wiring),
            "ON" if self.independent else "OFF",
            self.update_rate_s,
        )
        _log.info(
            "%-3s %-6s %-10s %-16s %-5s %-9s %-8s %-6s %-8s",
            "El",
            "Typ",
            "U-Range",
            "I-Range",
            "Auto",
            "LineFilt",
            "FreqFilt",
            "Sync",
            "Scaling",
        )
        for e in self.elements:
            current = (
                f"EXT {e.current_sensor:g} V" if e.current_sensor is not None
                else f"{e.current_direct:g} A"
            )
            auto = f"{'U' if e.voltage_auto else '-'}{'I' if e.current_auto else '-'}"
            scaling = (
                f"{'ON' if e.scaling else 'OFF'} VT{e.vt_ratio:g}/CT{e.ct_ratio:g}"
            )
            _log.info(
                "%-3d %-6s %-10s %-16s %-5s %-9s %-8s %-6s %-8s",
                e.element,
                f"{e.module}A",
                f"{e.voltage_range:g} V",
                current,
                auto,
                e.line_filter,
                "ON" if e.frequency_filter else "OFF",
                e.sync_source,
                scaling,
            )


def _diff_element(wanted: ElementSettings, got: ElementSettings) -> list[str]:
    """Abweichungen eines einzelnen Elements auflisten."""
    problems: list[str] = []
    prefix = f"Element {wanted.element}"

    def compare_float(name: str, a: float | None, b: float | None) -> None:
        if a is None and b is None:
            return
        if a is None or b is None or not _float_close(a, b, rel=1e-4):
            problems.append(f"{prefix} {name}: soll {a}, ist {b}")

    def compare_plain(name: str, a: object, b: object) -> None:
        if a != b:
            problems.append(f"{prefix} {name}: soll {a}, ist {b}")

    compare_float("U-Range", wanted.voltage_range, got.voltage_range)
    compare_plain("U-Auto", wanted.voltage_auto, got.voltage_auto)
    compare_plain("U-Mode", wanted.voltage_mode, got.voltage_mode)
    compare_float("I-Range direkt", wanted.current_direct, got.current_direct)
    compare_float("I-Range Sensor", wanted.current_sensor, got.current_sensor)
    compare_plain("I-Auto", wanted.current_auto, got.current_auto)
    compare_plain("I-Mode", wanted.current_mode, got.current_mode)
    compare_float("SRATio", wanted.sensor_ratio, got.sensor_ratio)
    compare_plain("Line-Filter", wanted.line_filter, got.line_filter)
    compare_plain("Freq-Filter", wanted.frequency_filter, got.frequency_filter)
    compare_plain("Scaling", wanted.scaling, got.scaling)
    compare_float("VT", wanted.vt_ratio, got.vt_ratio)
    compare_float("CT", wanted.ct_ratio, got.ct_ratio)
    compare_float("SFACtor", wanted.power_factor, got.power_factor)
    compare_plain("Sync", wanted.sync_source, got.sync_source)
    return problems


# ---------------------------------------------------------------------------
# Wiederherstellung
# ---------------------------------------------------------------------------


def restore_input_snapshot(config: InputConfig, snapshot: InputSnapshot) -> int:
    """Gesicherten Zustand wiederherstellen und die Anzahl der Set-Kommandos liefern.

    Es wird nur geschrieben, was tatsaechlich abweicht - Set-Kommandos kosten
    100-250 ms, ein blindes Zurueckschreiben aller Werte waere im
    Fehlerfall unnoetig lang und riskant.

    Reihenfolge: Crest-Faktor -> Wiring -> Bereiche -> Auto-Range -> Filter ->
    Skalierungsfaktoren -> Skalierung EIN/AUS -> Sync -> Rate. Crest-Faktor und
    Wiring zuerst, weil sie die zulaessigen Bereiche und die Unit-Zuordnung
    bestimmen.

    Der Aufrufer muss die betroffenen Gruppen vorher freigeben, z.B.:
        with config.unlocked(GROUP_RANGE, GROUP_FILTER, GROUP_RATE):
            restore_input_snapshot(config, backup)
    """
    current = InputSnapshot.capture(config)
    problems = snapshot.diff(current)
    if not problems:
        _log.info("Eingangskonfiguration ist bereits im Sollzustand - nichts zu tun")
        return 0

    _log.warning("Wiederherstellung noetig, %d Abweichung(en):", len(problems))
    for problem in problems:
        _log.warning("  %s", problem)

    written = 0

    if snapshot.crest_factor != current.crest_factor:
        config.set_crest_factor(snapshot.crest_factor)
        written += 1
    if snapshot.wiring != current.wiring:
        config.set_wiring(snapshot.wiring)
        written += 1

    current_by_element = {e.element: e for e in current.elements}
    for wanted in snapshot.elements:
        got = current_by_element.get(wanted.element)
        element = wanted.element

        if got is None or not _float_close(wanted.voltage_range, got.voltage_range):
            config.set_voltage_range(wanted.voltage_range, element)
            written += 1

        if wanted.current_sensor is not None:
            if got is None or got.current_sensor is None or not _float_close(
                wanted.current_sensor, got.current_sensor
            ):
                config.set_current_range_sensor(wanted.current_sensor, element)
                written += 1
        elif wanted.current_direct is not None:
            if got is None or got.current_direct is None or not _float_close(
                wanted.current_direct, got.current_direct
            ):
                config.set_current_range(wanted.current_direct, element)
                written += 1

        # Auto-Range NACH dem Bereich: das Setzen eines festen Bereichs
        # schaltet Auto-Range ab.
        if got is None or wanted.voltage_auto != got.voltage_auto:
            config.set_voltage_auto_range(wanted.voltage_auto, element)
            written += 1
        if got is None or wanted.current_auto != got.current_auto:
            config.set_current_auto_range(wanted.current_auto, element)
            written += 1

        if got is None or wanted.line_filter != got.line_filter:
            parameter = "OFF" if wanted.line_filter == "OFF" else float(wanted.line_filter)
            config.set_line_filter(parameter, element)
            written += 1
        if got is None or wanted.frequency_filter != got.frequency_filter:
            config.set_frequency_filter(wanted.frequency_filter, element)
            written += 1

        if got is None or not _float_close(wanted.sensor_ratio, got.sensor_ratio, rel=1e-4):
            config.set_sensor_ratio(wanted.sensor_ratio, element)
            written += 1
        if got is None or not _float_close(wanted.vt_ratio, got.vt_ratio, rel=1e-4):
            config.set_vt_ratio(wanted.vt_ratio, element)
            written += 1
        if got is None or not _float_close(wanted.ct_ratio, got.ct_ratio, rel=1e-4):
            config.set_ct_ratio(wanted.ct_ratio, element)
            written += 1
        if got is None or not _float_close(wanted.power_factor, got.power_factor, rel=1e-4):
            config.set_power_factor(wanted.power_factor, element)
            written += 1

        # Skalierung EIN/AUS zuletzt, damit nie ein Zwischenzustand mit
        # falschem Faktor und aktiver Skalierung entsteht.
        if got is None or wanted.scaling != got.scaling:
            config.set_scaling_state(wanted.scaling, element)
            written += 1

        if got is None or not _token_match(wanted.sync_source, got.sync_source):
            config.set_sync_source(wanted.sync_source, element)
            written += 1

    if not _float_close(snapshot.update_rate_s, current.update_rate_s):
        config.set_update_rate(snapshot.update_rate_s)
        written += 1

    remaining = snapshot.diff(InputSnapshot.capture(config))
    if remaining:
        for problem in remaining:
            _log.error("Restore-Kontrolle: %s", problem)
        raise VerificationError(
            f"Wiederherstellung unvollstaendig: {len(remaining)} Abweichung(en)"
        )

    _log.info("Eingangskonfiguration wiederhergestellt (%d Set-Kommandos)", written)
    return written