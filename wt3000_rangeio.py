# =============================================================================
# Datei: wt3000_rangeio.py
# Layer 2 - Typisierter Zugriff auf die Messbereichsknoten der INPut-Gruppe.
#
#Aenderung 1: 18.08.26 -> Neu: RangeValue(value, sensor) —
#                           Bereichswert mit Eingangsart; parse_range_value() versteht EXTERNAL,<V>;
#                           ranges_match() (10 A direkt ≠ 10 V Sensor); Quantity.unit(sensor)
#                       -> get_range()/get_ranges() liefern jetzt RangeValue statt float → der Abbruch in parse_nr3() entfällt.
#                       -> set_range(..., sensor=False) bzw. set_range(..., RangeValue) sendet EXTernal,<Volt>;
#                           Sensorbereich auf dem Spannungspfad wird abgelehnt.
#
#
# Dieses Modul ist das Gegenstueck zu wt3000_numeric.py, nur fuer ':INPut'
# statt ':NUMeric'. Es kennt die SCPI-Pfade und die Antwortformate - mehr
# nicht. Kein Backup, kein Verify, keine Ablaufsteuerung; das ist Aufgabe von
# wt3000_ranging.py.
#
# ANGETASTETE KNOTEN - abschliessende Liste:
#   [:INPut]:VOLTage:RANGe{:ELEMent<x>|:SIGMA|:SIGMB|:ALL}
#   [:INPut]:VOLTage:AUTO {:ELEMent<x>|:SIGMA|:SIGMB|:ALL}
#   [:INPut]:CURRent:RANGe{:ELEMent<x>|:SIGMA|:SIGMB|:ALL}
#   [:INPut]:CURRent:AUTO {:ELEMent<x>|:SIGMA|:SIGMB|:ALL}
#
# NICHT angetastet werden - und dieses Modul besitzt dafuer auch keine
# Methode: SRATio (Stromsensorkonstante), SCALing (VT/CT/SFACtor/STATe),
# WIRing, FILTer, SYNChronize, MODUle, INDependent, NULL.
# Das ist kein Zufall: Elemente 1-3 haengen an externen Stromsensoren und
# Element 4 an CT-Ratio 2000. Wer dort schreibt, verstellt die Eichung.
# =============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass          # UEBERARBEITET (RANGEIO-2)
from enum import Enum
from typing import Final                   # UEBERARBEITET (RANGEIO-2)

from wt3000_common import (
    ALL,
    DEFAULT_ELEMENTS,
    SIGMA,
    SIGMB,
    canonical_scope,
    format_nrf,
    is_element_scope,
    parse_boolean,
    parse_nr3,
    scope_suffix,
    strip_response_header,
    values_match,                          # UEBERARBEITET (RANGEIO-2)
)
from wt3000_core import WTError, WTSession

_log = logging.getLogger("wt3000.rangeio")


class ChangesNotAllowed(WTError):
    """Am RangeAccess wurde geschrieben, ohne allow_changes=True zu setzen."""


class Quantity(Enum):
    """Messgroesse und zugehoeriger SCPI-Teilpfad."""

    VOLTAGE = ":INPut:VOLTage"
    CURRENT = ":INPut:CURRent"

    @property
    def label(self) -> str:
        """Kurzbezeichnung fuer Protokollausgaben."""
        return "Spannung" if self is Quantity.VOLTAGE else "Strom"

    @property
    def range_label(self) -> str:
        """Bezeichnung des Messbereichs fuer Protokollausgaben."""
        return "Spannungsbereich" if self is Quantity.VOLTAGE else "Strombereich"

    # UEBERARBEITET (RANGEIO-2): Die Einheit eines Strombereichs haengt von der
    # Eingangsart ab - direkt in Ampere, am externen Sensor in Volt.
    def unit(self, sensor: bool = False) -> str:
        """Einheit des Bereichswerts fuer diese Messgroesse."""
        if self is Quantity.VOLTAGE:
            return "V"
        return "V" if sensor else "A"


# ---------------------------------------------------------------------------
# UEBERARBEITET (RANGEIO-2): Bereichswert samt Eingangsart
# ---------------------------------------------------------------------------

# Kurzform, mit der das Geraet den Sensoreingang meldet ('EXT'/'EXTERNAL').
# Die Pruefung laeuft bewusst nur in EINE Richtung (Antwort beginnt mit 'EXT'):
# kein anderer Bereichswert dieser Gruppe faengt so an, ein beidseitiges
# Praefixmatching waere hier also unnoetiges Risiko.
_SENSOR_PREFIX: Final[str] = "EXT"


@dataclass(frozen=True)
class RangeValue:
    """Ein eingestellter Messbereich einschliesslich der Eingangsart.

    Elemente 1-3 dieses Aufbaus haengen an externen Stromsensoren. Fuer sie
    antwortet ':INPut:CURRent:RANGe:ELEMent<x>?' mit 'EXTERNAL,10.00E+00' -
    der Bereich ist dann die Sensoreingangsspannung in VOLT, nicht ein Strom
    in Ampere. Beide Faelle in einem blanken float zu fuehren, war die
    Ursache dafuer, dass RangeBackup.capture() auf diesem Aufbau abbrach; ein
    blindes Zurueckschreiben des Zahlenwerts in die falsche Eingangsart waere
    ausserdem eine echte Fehlkonfiguration.
    """

    value: float
    sensor: bool = False

    def unit(self, quantity: "Quantity") -> str:
        """Einheit dieses Werts fuer die angegebene Messgroesse."""
        return quantity.unit(self.sensor)

    def describe(self, quantity: "Quantity") -> str:
        """Lesbare Kurzform, z.B. '10 V (Sensor)'."""
        art = " (Sensor)" if self.sensor else ""
        return f"{self.value:g} {self.unit(quantity)}{art}"


def parse_range_value(response: str, context: str = "") -> RangeValue:
    """Antwort eines RANGe-Knotens auswerten - direkt oder Sensoreingang.

    'EXTERNAL,10.00E+00' -> RangeValue(10.0, sensor=True)
    '30.0E+00'           -> RangeValue(30.0, sensor=False)

    Geraeteunabhaengig und damit ohne Verbindung testbar.
    """
    text = strip_response_header(response)
    token = text.upper()
    if token.startswith(_SENSOR_PREFIX):
        _, separator, volts = token.partition(",")
        if not separator:
            suffix = f" ({context})" if context else ""
            raise WTError(
                f"Sensorbereich ohne Wert in der Antwort {response!r}{suffix} - "
                "erwartet 'EXTERNAL,<Volt>'"
            )
        return RangeValue(parse_nr3(volts, context), sensor=True)
    return RangeValue(parse_nr3(text, context), sensor=False)


def ranges_match(
    requested: RangeValue, actual: RangeValue, tolerance: float = 1e-3
) -> bool:
    """Zwei Bereichswerte vergleichen - Eingangsart zaehlt mit.

    10 A direkt und 10 V am Sensoreingang sind NICHT derselbe Zustand, auch
    wenn der Zahlenwert uebereinstimmt.
    """
    if requested.sensor != actual.sensor:
        return False
    return values_match(requested.value, actual.value, tolerance)


# ---------------------------------------------------------------------------
# Zugriffsklasse
# ---------------------------------------------------------------------------


class RangeAccess:
    """Lesender und schreibender Zugriff auf Messbereiche und Autorange.

    Zwei unabhaengige Schloesser schuetzen das eingemessene Geraet:
      1. WTSession(read_only=True) lehnt jedes Nicht-Query-Kommando ab.
      2. allow_changes=False lehnt jeden Schreibaufruf schon hier ab.
    Beide muessen bewusst geoeffnet werden, damit sich etwas veraendern kann.

    sigma_members bildet die Wiring-Units auf Elementnummern ab, zum Beispiel
    {'SIGMA': (1, 2, 3), 'SIGMB': (4,)} fuer die Verdrahtung V3A3,P1W2.
    Ohne diese Angabe werden SIGMA-/SIGMB-Scopes abgelehnt statt geraten -
    eine falsch geratene Zuordnung waere genau der Fehler, den die strikte
    Scope-Normalisierung verhindern soll.
    """

    def __init__(
        self,
        session: WTSession,
        allow_changes: bool = False,
        elements: tuple[int, ...] = DEFAULT_ELEMENTS,
        sigma_members: dict[str, tuple[int, ...]] | None = None,
    ) -> None:
        self._session = session
        self._allow_changes = allow_changes
        self._elements = tuple(elements)
        self._sigma_members = {
            canonical_scope(name): tuple(members)
            for name, members in (sigma_members or {}).items()
        }
        _log.debug(
            "RangeAccess: Elemente %s, Aenderungen %s",
            self._elements,
            "erlaubt" if allow_changes else "gesperrt",
        )

    # -- Eigenschaften ------------------------------------------------------

    @property
    def elements(self) -> tuple[int, ...]:
        """Vorhandene Elementnummern."""
        return self._elements

    @property
    def allow_changes(self) -> bool:
        """True, wenn dieses Objekt schreiben darf."""
        return self._allow_changes

    # -- Scope-Aufloesung ---------------------------------------------------

    def expand_scope(self, scope: str | int) -> tuple[int, ...]:
        """Scope in die Liste der betroffenen Elementnummern aufloesen.

        Wird gebraucht, weil die Sammelknoten (:ALL, :SIGMA, :SIGMB) laut
        Handbuch NUR schreibbar sind. Zurueckgelesen werden muss deshalb
        immer elementweise.
        """
        token = canonical_scope(scope)
        if token.isdigit():
            number = int(token)
            if number not in self._elements:
                raise WTError(f"Element {number} existiert nicht (vorhanden: {self._elements})")
            return (number,)
        if token == ALL:
            return self._elements
        members = self._sigma_members.get(token)
        if not members:
            raise WTError(
                f"Scope {token!r} ist nicht aufloesbar - RangeAccess wurde ohne "
                "sigma_members angelegt. Wiring-Units muessen vom Aufrufer "
                "uebergeben werden, geraten wird hier nichts."
            )
        return members

    # -- Lesen --------------------------------------------------------------

    # UEBERARBEITET (RANGEIO-2): Rueckgabe ist jetzt RangeValue statt float,
    # damit der Sensoreingang nicht mehr in parse_nr3() abstuerzt.
    def get_range(self, quantity: Quantity, element: int) -> RangeValue:
        """Eingestellten Messbereich eines Elements lesen.

        Antwortet das Element mit 'EXTERNAL,<Volt>', wird der Wert als
        Sensorbereich gekennzeichnet zurueckgegeben.
        """
        response = self._session.query(f"{quantity.value}:RANGe:ELEMent{element}?")
        return parse_range_value(response, f"{quantity.range_label} Element {element}")

    def get_auto(self, quantity: Quantity, element: int) -> bool:
        """Autorange-Zustand eines Elements lesen."""
        response = self._session.query(f"{quantity.value}:AUTO:ELEMent{element}?")
        return parse_boolean(response, f"Autorange {quantity.label} Element {element}")

    def get_ranges(self, quantity: Quantity) -> dict[int, RangeValue]:  # UEBERARBEITET (RANGEIO-2)
        """Messbereiche aller Elemente lesen."""
        return {e: self.get_range(quantity, e) for e in self._elements}

    def get_autos(self, quantity: Quantity) -> dict[int, bool]:
        """Autorange-Zustaende aller Elemente lesen."""
        return {e: self.get_auto(quantity, e) for e in self._elements}

    # -- Umfeld lesen (nur zur Diagnose, nie geschrieben) -------------------

    def get_independent(self) -> bool:
        """':INPut:INDependent' lesen.

        Steht die unabhaengige Einstellung auf OFF, wirken elementweise
        Bereichskommandos moeglicherweise gekoppelt oder werden abgelehnt.
        Vor jedem Schreibvorgang pruefen.
        """
        return parse_boolean(self._session.query(":INPut:INDependent?"), "INDependent")

    def get_wiring(self) -> str:
        """':INPut:WIRing' lesen. Nur informativ, wird nie gesetzt."""
        return strip_response_header(self._session.query(":INPut:WIRing?"))

    def get_module(self) -> str:
        """':INPut:MODUle?' lesen - Bauart der Eingangselemente."""
        return strip_response_header(self._session.query(":INPut:MODUle?"))

    def get_peak_over(self) -> str:
        """':INPut:POVer?' lesen - Peak-Over-Information je Eingang."""
        return strip_response_header(self._session.query(":INPut:POVer?"))

    def dump(self, quantity: Quantity) -> str:
        """Rohabzug aller Einstellungen einer Messgroesse (':INPut:VOLTage?')."""
        return strip_response_header(self._session.query(f"{quantity.value}?"))

    # -- Schreiben ----------------------------------------------------------

    # UEBERARBEITET (RANGEIO-2): nimmt jetzt auch einen Sensorbereich entgegen.
    def set_range(
        self,
        quantity: Quantity,
        scope: str | int,
        value: float | RangeValue,
        sensor: bool = False,
    ) -> str:
        """Messbereich setzen. Rueckgabe: das gesendete Kommando.

        Der Scope darf ein Element, eine Wiring-Unit oder ALL sein. Ob das
        Geraet einen Zwischenwert auf die naechste gueltige Stufe rundet oder
        ihn ablehnt, ist NICHT vorausgesetzt - deshalb liefert dieses Modul
        nur das Kommando zurueck und ueberlaesst die Kontrolle dem Verify in
        wt3000_ranging.py.

        sensor=True setzt den Bereich des externen Stromsensoreingangs; der
        Wert ist dann eine SPANNUNG in Volt und das Kommando lautet
        ':INPut:CURRent:RANGe:ELEMent<x> EXTernal,<Volt>'. Wird ein RangeValue
        uebergeben, bestimmt dessen Kennzeichen die Eingangsart.

        ZU VERIFIZIEREN: ob das Geraet die reine NRf-Form ('10') erwartet oder
        die Einheitenschreibweise ('10V'), die wt3000_input verwendet.
        """
        if isinstance(value, RangeValue):
            sensor = value.sensor
            value = value.value
        if sensor and quantity is Quantity.VOLTAGE:
            raise WTError(
                "Ein Sensorbereich existiert nur fuer den Stromeingang - "
                ":INPut:VOLTage:RANGe kennt kein 'EXTernal'"
            )
        parameter = f"EXTernal,{format_nrf(value)}" if sensor else format_nrf(value)
        command = f"{quantity.value}:RANGe{scope_suffix(scope)} {parameter}"
        self._write(command)
        return command

    def set_auto(self, quantity: Quantity, scope: str | int, state: bool) -> str:
        """Autorange ein- oder ausschalten. Rueckgabe: das gesendete Kommando."""
        command = f"{quantity.value}:AUTO{scope_suffix(scope)} {'ON' if state else 'OFF'}"
        self._write(command)
        return command

    # -- Intern -------------------------------------------------------------

    def _write(self, command: str) -> None:
        """Schreibkommando nach Pruefung des Schlosses absetzen."""
        if not self._allow_changes:
            raise ChangesNotAllowed(
                f"RangeAccess wurde mit allow_changes=False angelegt - "
                f"'{command}' wird nicht gesendet"
            )
        _log.info("Set: %s", command)
        self._session.write(command)


# ---------------------------------------------------------------------------
# Wiring-Units aus einem InputConfig uebernehmen
# ---------------------------------------------------------------------------


def sigma_members_from_units(units) -> dict[str, tuple[int, ...]]:
    """Ergebnis von InputConfig.get_wiring_units() in eine Scope-Abbildung wandeln.

    Erwartet Objekte mit '.name' und '.elements'. Namen werden ueber
    canonical_scope() normalisiert, also strikt und ohne Praefixmatching.
    Einheiten ohne verwertbaren Namen werden uebergangen statt geraten.
    """
    mapping: dict[str, tuple[int, ...]] = {}
    for unit in units:
        raw = getattr(unit, "name", None)
        if not raw:
            continue
        try:
            token = canonical_scope(raw)
        except WTError:
            _log.warning("Wiring-Unit %r nicht zuordenbar - uebergangen", raw)
            continue
        if token not in (SIGMA, SIGMB):
            continue
        mapping[token] = tuple(int(e) for e in unit.elements)
    _log.info("Wiring-Units uebernommen: %s", mapping or "keine")
    return mapping