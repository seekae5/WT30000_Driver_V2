# =============================================================================
# Datei: wt3000_ranging.py
# Layer 3 - Messbereiche deklarativ beschreiben, sichern, setzen, verifizieren
#           und vollstaendig zurueckstellen.
#
# Verhaeltnis zu wt3000_rangeio.py wie wt3000_itemspec.py zu wt3000_numeric.py:
# dort die SCPI-Knoten, hier der Ablauf.
#
# Dieses Modul FUEHRT VON SICH AUS NICHTS AUS. Es stellt Bausteine fuer einen
# spaeteren Import bereit. Ein aufrufendes Stufenskript entscheidet, ob und
# was veraendert wird.
#
# Typische Verwendung:
#
#     access = RangeAccess(session, allow_changes=True,
#                          sigma_members=sigma_members_from_units(units))
#     plan = RangePlan.of(
#         RangeSpec(Quantity.VOLTAGE, "SIGMA", 300.0),
#         AutoRangeSpec(Quantity.CURRENT, 4, False),
#     )
#     with applied_ranges(access, plan, backup_file=path) as report:
#         ...  # messen
#     # Ausgangszustand ist hier garantiert wiederhergestellt
# =============================================================================

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

from wt3000_common import canonical_scope  # UEBERARBEITET (RANGEIO-2): values_match
                                           # wird nicht mehr direkt gebraucht -
                                           # Bereiche vergleicht ranges_match().
from wt3000_core import WTError
# UEBERARBEITET (RANGEIO-2): RangeValue/ranges_match tragen die Eingangsart mit.
from wt3000_rangeio import Quantity, RangeAccess, RangeValue, ranges_match

_log = logging.getLogger("wt3000.ranging")

# Relative Toleranz beim Zurueckvergleichen eines Bereichswerts.
DEFAULT_TOLERANCE: float = 1e-3


# ---------------------------------------------------------------------------
# Zielzustand deklarieren
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RangeSpec:
    """Ein gewuenschter fester Messbereich.

    scope: Elementnummer, 'SIGMA', 'SIGMB' oder 'ALL'.
    Ein fester Bereich impliziert Autorange AUS - das erledigt apply_plan().

    UEBERARBEITET (RANGEIO-2): sensor=True bezeichnet den Bereich des externen
    Stromsensoreingangs. Der Wert ist dann eine SPANNUNG in Volt. Ohne dieses
    Kennzeichen liesse sich ein Sensorelement nicht widerspruchsfrei
    beschreiben - und ein Amperewert an einem Sensoreingang waere keine
    Rundungsfrage, sondern eine Fehlkonfiguration.
    """

    quantity: Quantity
    scope: str | int
    value: float
    sensor: bool = False

    @property
    def range_value(self) -> RangeValue:
        """Wert und Eingangsart als RangeValue."""
        return RangeValue(self.value, self.sensor)

    def describe(self) -> str:
        """Lesbare Kurzform fuer Protokolle."""
        return (
            f"{self.quantity.range_label} {canonical_scope(self.scope)} = "
            f"{self.range_value.describe(self.quantity)}"
        )


@dataclass(frozen=True)
class AutoRangeSpec:
    """Ein gewuenschter Autorange-Zustand.

    Bewusst eine eigene Klasse und keine Variante von RangeSpec: RANGe und
    AUTO sind zwei getrennte SCPI-Knoten, nicht zwei Schreibweisen desselben
    Werts.
    """

    quantity: Quantity
    scope: str | int
    state: bool

    def describe(self) -> str:
        """Lesbare Kurzform fuer Protokolle."""
        zustand = "EIN" if self.state else "AUS"
        return f"Autorange {self.quantity.label} {canonical_scope(self.scope)} = {zustand}"


@dataclass(frozen=True)
class RangePlan:
    """Vollstaendiger Zielzustand einer Aenderung."""

    ranges: tuple[RangeSpec, ...] = ()
    autos: tuple[AutoRangeSpec, ...] = ()

    @classmethod
    def of(cls, *specs: RangeSpec | AutoRangeSpec) -> "RangePlan":
        """Plan aus einer gemischten Spec-Liste bauen."""
        ranges = tuple(s for s in specs if isinstance(s, RangeSpec))
        autos = tuple(s for s in specs if isinstance(s, AutoRangeSpec))
        return cls(ranges=ranges, autos=autos)

    def is_empty(self) -> bool:
        """True, wenn der Plan nichts veraendern wuerde."""
        return not self.ranges and not self.autos

    def describe(self) -> list[str]:
        """Alle Vorgaben als Textzeilen."""
        return [s.describe() for s in (*self.ranges, *self.autos)]

    # -- Pruefung -----------------------------------------------------------

    def validate(self, access: RangeAccess) -> None:
        """Widerspruechliche Vorgaben abfangen, bevor etwas gesendet wird.

        Geprueft wird nach Aufloesung der Scopes, denn 'SIGMA' und '1' koennen
        dasselbe Element meinen. Ein Konflikt hier waere sonst erst am Geraet
        aufgefallen - nach der ersten Aenderung.
        """
        if self.is_empty():
            raise WTError("Leerer RangePlan")

        # UEBERARBEITET (RANGEIO-2): Der Konfliktschluessel traegt jetzt Wert UND
        # Eingangsart. 10 A direkt und 10 V am Sensor sind nicht derselbe Zustand.
        fixed: dict[tuple[Quantity, int], RangeValue] = {}
        for spec in self.ranges:
            if spec.value <= 0:
                raise WTError(f"Ungueltiger Bereichswert: {spec.describe()}")
            if spec.sensor and spec.quantity is Quantity.VOLTAGE:
                raise WTError(
                    f"Ungueltig: {spec.describe()} - einen Sensoreingang gibt es "
                    "nur fuer den Strompfad"
                )
            for element in access.expand_scope(spec.scope):
                key = (spec.quantity, element)
                if key in fixed and not ranges_match(fixed[key], spec.range_value):
                    raise WTError(
                        f"Widerspruch: {spec.quantity.label} Element {element} wird auf "
                        f"{fixed[key].describe(spec.quantity)} und auf "
                        f"{spec.range_value.describe(spec.quantity)} gesetzt"
                    )
                fixed[key] = spec.range_value

        # UEBERARBEITET (RANGEIO-2): Eingangsart gegen das Geraet pruefen, BEVOR
        # das erste Set-Kommando faellt. Elemente 1-3 haengen an externen
        # Stromsensoren; ein Amperewert wuerde dort die Sensorbeschaltung aus
        # der Konfiguration werfen. Kostet hoechstens vier Abfragen und faellt
        # sonst erst am bereits veraenderten Geraet auf.
        for (quantity, element), wanted in fixed.items():
            if quantity is not Quantity.CURRENT:
                continue
            present = access.get_range(quantity, element)
            if present.sensor != wanted.sensor:
                ist = "externer Sensoreingang" if present.sensor else "Direkteingang"
                soll = "Sensorbereich" if wanted.sensor else "Direktbereich"
                raise WTError(
                    f"Element {element} steht auf {ist}, der Plan gibt einen "
                    f"{soll} vor ({wanted.describe(quantity)}). Die Eingangsart "
                    "wird von diesem Modul bewusst nicht umgeschaltet."
                )

        auto_on: dict[tuple[Quantity, int], bool] = {}
        for spec in self.autos:
            for element in access.expand_scope(spec.scope):
                key = (spec.quantity, element)
                if key in auto_on and auto_on[key] != spec.state:
                    raise WTError(
                        f"Widerspruch: Autorange {spec.quantity.label} Element "
                        f"{element} wird ein- UND ausgeschaltet"
                    )
                auto_on[key] = spec.state
                if spec.state and key in fixed:
                    raise WTError(
                        f"Widerspruch: {spec.quantity.label} Element {element} bekommt "
                        f"einen festen Bereich UND Autorange EIN"
                    )

        _log.info("Plan geprueft: %d Bereiche, %d Autorange-Vorgaben",
                  len(self.ranges), len(self.autos))


# ---------------------------------------------------------------------------
# Ist-Zustand sichern
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ElementRangeState:
    """Bereichszustand eines einzelnen Elements.

    UEBERARBEITET (RANGEIO-2): Die Bereiche sind RangeValue statt float und
    fuehren damit die Eingangsart mit.
    """

    element: int
    voltage_range: RangeValue
    voltage_auto: bool
    current_range: RangeValue
    current_auto: bool

    def range_of(self, quantity: Quantity) -> RangeValue:
        """Bereich der gewuenschten Messgroesse inklusive Eingangsart."""
        return self.voltage_range if quantity is Quantity.VOLTAGE else self.current_range

    def value_of(self, quantity: Quantity) -> float:
        """Reiner Zahlenwert des Bereichs - ohne Aussage zur Eingangsart."""
        return self.range_of(quantity).value

    def auto_of(self, quantity: Quantity) -> bool:
        """Autorange-Zustand der gewuenschten Messgroesse."""
        return self.voltage_auto if quantity is Quantity.VOLTAGE else self.current_auto


@dataclass(frozen=True)
class RangeBackup:
    """Gesicherter Bereichszustand aller Elemente.

    Es wird bewusst der VOLLE Zustand gesichert, nicht nur die vom Plan
    beruehrten Werte: ein Abbruch mitten in der Aenderung darf das Geraet
    nicht halb verstellt zuruecklassen.
    """

    states: tuple[ElementRangeState, ...]
    captured_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    wiring: str = ""

    # -- Erfassen -----------------------------------------------------------

    @classmethod
    def capture(cls, access: RangeAccess) -> "RangeBackup":
        """Aktuellen Bereichszustand aller Elemente lesen."""
        states = tuple(
            ElementRangeState(
                element=element,
                voltage_range=access.get_range(Quantity.VOLTAGE, element),
                voltage_auto=access.get_auto(Quantity.VOLTAGE, element),
                current_range=access.get_range(Quantity.CURRENT, element),
                current_auto=access.get_auto(Quantity.CURRENT, element),
            )
            for element in access.elements
        )
        backup = cls(states=states, wiring=access.get_wiring())
        _log.info("Bereichszustand gesichert (%d Elemente, Wiring %s)",
                  len(states), backup.wiring or "unbekannt")
        return backup

    def state_of(self, element: int) -> ElementRangeState:
        """Zustand eines Elements herausgreifen."""
        for state in self.states:
            if state.element == element:
                return state
        raise WTError(f"Element {element} ist im Backup nicht enthalten")

    def log_summary(self) -> None:
        """Gesicherten Zustand tabellarisch protokollieren."""
        _log.info("%-8s %16s %6s %16s %6s", "Element", "U-Bereich", "U-Auto", "I-Bereich", "I-Auto")
        for s in self.states:
            # UEBERARBEITET (RANGEIO-2): Einheit und Eingangsart mit ausgeben -
            # '10 V (Sensor)' ist etwas anderes als '10 A'.
            _log.info(
                "%-8d %16s %6s %16s %6s",
                s.element,
                s.voltage_range.describe(Quantity.VOLTAGE),
                "EIN" if s.voltage_auto else "AUS",
                s.current_range.describe(Quantity.CURRENT),
                "EIN" if s.current_auto else "AUS",
            )

    # -- Persistenz ---------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialisierbare Form."""
        return {
            "captured_at": self.captured_at,
            "wiring": self.wiring,
            "states": [
                {
                    "element": s.element,
                    "voltage_range": s.voltage_range.value,
                    "voltage_auto": s.voltage_auto,
                    "current_range": s.current_range.value,
                    # UEBERARBEITET (RANGEIO-2): Eingangsart mitschreiben, sonst
                    # laesst sich ein Sensorbereich nicht korrekt zurueckstellen.
                    "current_sensor": s.current_range.sensor,
                    "current_auto": s.current_auto,
                }
                for s in self.states
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RangeBackup":
        """Gegenstueck zu to_dict()."""
        return cls(
            states=tuple(
                # UEBERARBEITET (RANGEIO-2): 'current_sensor' fehlt in aelteren
                # Backupdateien - dort galt implizit der Direkteingang.
                ElementRangeState(
                    element=int(d["element"]),
                    voltage_range=RangeValue(float(d["voltage_range"])),
                    voltage_auto=bool(d["voltage_auto"]),
                    current_range=RangeValue(
                        float(d["current_range"]), bool(d.get("current_sensor", False))
                    ),
                    current_auto=bool(d["current_auto"]),
                )
                for d in data["states"]
            ),
            captured_at=data.get("captured_at", ""),
            wiring=data.get("wiring", ""),
        )

    def save(self, path: Path) -> None:
        """Backup als JSON schreiben."""
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        _log.info("Bereichs-Backup gesichert nach %s", path)

    @classmethod
    def load(cls, path: Path) -> "RangeBackup":
        """Backup aus JSON laden."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # -- Vergleich ----------------------------------------------------------

    def diff(self, other: "RangeBackup", tolerance: float = DEFAULT_TOLERANCE) -> list[str]:
        """Abweichungen gegenueber einem anderen Zustand auflisten."""
        problems: list[str] = []
        for mine in self.states:
            try:
                theirs = other.state_of(mine.element)
            except WTError:
                problems.append(f"Element {mine.element} fehlt im Vergleichszustand")
                continue
            for quantity in Quantity:
                # UEBERARBEITET (RANGEIO-2): Ein Wechsel der Eingangsart ist
                # immer eine Abweichung, auch bei gleichem Zahlenwert.
                if not ranges_match(
                    mine.range_of(quantity), theirs.range_of(quantity), tolerance
                ):
                    problems.append(
                        f"Element {mine.element}: {quantity.range_label} "
                        f"{mine.range_of(quantity).describe(quantity)} -> "
                        f"{theirs.range_of(quantity).describe(quantity)}"
                    )
                if mine.auto_of(quantity) != theirs.auto_of(quantity):
                    problems.append(
                        f"Element {mine.element}: Autorange {quantity.label} "
                        f"{mine.auto_of(quantity)} -> {theirs.auto_of(quantity)}"
                    )
        return problems


# ---------------------------------------------------------------------------
# Voraussetzungen und Schreibprobe
# ---------------------------------------------------------------------------


def check_preconditions(access: RangeAccess) -> None:
    """Umfeld pruefen, bevor geschrieben wird. Veraendert nichts."""
    if not access.get_independent():
        _log.warning(
            ":INPut:INDependent ist AUS - elementweise Bereichskommandos wirken "
            "moeglicherweise gekoppelt. ZU VERIFIZIEREN am Geraet."
        )
    _log.info("Wiring: %s | Module: %s", access.get_wiring(), access.get_module())


def probe_write_capability(access: RangeAccess, backup: RangeBackup) -> None:
    """Den Schreibpfad testen, ohne etwas zu veraendern.

    Es wird der AKTUELLE Spannungsbereich des ersten Elements mit seinem
    eigenen Wert ueberschrieben. Faellt der Test durch, war es ein Nulleffekt;
    besteht er, ist bewiesen, dass die INPut-Gruppe Set-Kommandos annimmt.

    Damit ist die offene Frage geklaert, ob ':COMMunicate:REMote ON' fuer
    ':INPut' noetig ist - fuer ':NUMeric' ist es das nachweislich nicht.
    """
    element = access.elements[0]
    # UEBERARBEITET (RANGEIO-2): RangeValue statt float. Der Spannungspfad kennt
    # keinen Sensoreingang; das Kennzeichen wird trotzdem durchgereicht, damit
    # hier keine zweite Sonderregel entsteht.
    current = backup.state_of(element).range_of(Quantity.VOLTAGE)

    _log.info("Schreibprobe: Element %d wird auf seinen eigenen Wert %s gesetzt",
              element, current.describe(Quantity.VOLTAGE))
    access.set_range(Quantity.VOLTAGE, element, current)

    readback = access.get_range(Quantity.VOLTAGE, element)
    if not ranges_match(current, readback):
        raise WTError(
            f"Schreibprobe fehlgeschlagen: gesendet "
            f"{current.describe(Quantity.VOLTAGE)}, zurueckgelesen "
            f"{readback.describe(Quantity.VOLTAGE)}. Moegliche Ursache: die "
            "INPut-Gruppe verlangt "
            "':COMMunicate:REMote ON' - dann use_remote=True in WTConfig setzen."
        )
    _log.info("Schreibprobe erfolgreich - die INPut-Gruppe nimmt Set-Kommandos an")


# ---------------------------------------------------------------------------
# Anwenden und pruefen
# ---------------------------------------------------------------------------


def apply_plan(access: RangeAccess, plan: RangePlan) -> int:
    """Plan auf das Geraet schreiben. Rueckgabe: Anzahl gesendeter Kommandos.

    Reihenfolge ist nicht beliebig:
      1. Autorange AUS fuer alle Elemente, die einen festen Bereich bekommen.
         Ein fester Bereich bei aktivem Autorange waere wirkungslos, sobald
         das Geraet neu skaliert.
      2. Feste Bereiche setzen.
      3. Ausdrueckliche Autorange-Vorgaben - zuletzt, damit ein gewolltes
         Autorange EIN nicht von Schritt 1 wieder ueberschrieben wird.
    """
    plan.validate(access)
    written = 0

    for spec in plan.ranges:
        access.set_auto(spec.quantity, spec.scope, False)
        written += 1

    for spec in plan.ranges:
        # UEBERARBEITET (RANGEIO-2): Eingangsart mitgeben.
        access.set_range(spec.quantity, spec.scope, spec.range_value)
        written += 1

    for spec in plan.autos:
        access.set_auto(spec.quantity, spec.scope, spec.state)
        written += 1

    _log.info("Plan geschrieben: %d Kommandos", written)
    return written


def verify_plan(
    access: RangeAccess,
    plan: RangePlan,
    tolerance: float = DEFAULT_TOLERANCE,
    allow_snapping: bool = False,
) -> list[str]:
    """Zurueckgelesenen Zustand gegen den Plan pruefen.

    Rueckgabe: Liste der Abweichungen (leer = alles uebernommen).

    Sammelknoten (:ALL, :SIGMA, :SIGMB) sind laut Handbuch nur schreibbar,
    deshalb wird immer elementweise zurueckgelesen.

    allow_snapping=True wertet es nicht als Fehler, wenn das Geraet den
    angeforderten Wert auf eine benachbarte gueltige Stufe gelegt hat - der
    tatsaechlich gewaehlte Wert wird dann als Warnung protokolliert. Ob das
    Geraet ueberhaupt rundet oder stattdessen ablehnt, ist am WT3000 noch
    ZU VERIFIZIEREN; bis dahin ist der strenge Modus die Voreinstellung.
    """
    problems: list[str] = []

    for spec in plan.ranges:
        for element in access.expand_scope(spec.scope):
            actual = access.get_range(spec.quantity, element)
            # UEBERARBEITET (RANGEIO-2): Vergleich schliesst die Eingangsart ein.
            if ranges_match(spec.range_value, actual, tolerance):
                continue
            message = (
                f"{spec.quantity.range_label} Element {element}: angefordert "
                f"{spec.range_value.describe(spec.quantity)}, eingestellt "
                f"{actual.describe(spec.quantity)}"
            )
            if allow_snapping:
                _log.warning("Geraet hat den Wert angepasst - %s", message)
            else:
                problems.append(message)

    for spec in plan.autos:
        for element in access.expand_scope(spec.scope):
            actual = access.get_auto(spec.quantity, element)
            if actual != spec.state:
                problems.append(
                    f"Autorange {spec.quantity.label} Element {element}: angefordert "
                    f"{spec.state}, eingestellt {actual}"
                )

    if not problems:
        _log.info("Verifikation erfolgreich: Plan vollstaendig uebernommen")
    return problems


# ---------------------------------------------------------------------------
# Wiederherstellen
# ---------------------------------------------------------------------------


def restore_ranges(access: RangeAccess, backup: RangeBackup, force: bool = False) -> int:
    """Gesicherten Bereichszustand vollstaendig zurueckschreiben.

    Reihenfolge spiegelt apply_plan(): erst alles auf Autorange AUS, dann die
    festen Bereiche, dann die Elemente wieder auf Autorange EIN, die vorher
    so standen.

    force=False schreibt nur, was tatsaechlich abweicht - das spart bei
    Set-Kommandos von 100-250 ms spuerbar Zeit. force=True schreibt alles.
    """
    current = None if force else RangeBackup.capture(access)
    written = 0

    for quantity in Quantity:
        # Erst entscheiden, was ueberhaupt anzufassen ist. Die Entscheidung
        # muss VOR dem ersten Schreibkommando fallen, sonst vergleicht Schritt 3
        # gegen einen Zustand, den Schritt 1 bereits veraendert hat.
        # UEBERARBEITET (RANGEIO-2): Zielwert ist ein RangeValue - ein
        # Sensorbereich wird als 'EXTernal,<Volt>' zurueckgeschrieben und nicht
        # als Amperewert.
        plan_per_element: list[tuple[int, RangeValue, bool, bool, bool]] = []
        for state in backup.states:
            target_range = state.range_of(quantity)
            target_auto = state.auto_of(quantity)
            if current is None:
                need_range, need_auto = True, True
            else:
                now = current.state_of(state.element)
                need_range = not ranges_match(target_range, now.range_of(quantity))
                need_auto = now.auto_of(quantity) != target_auto
            plan_per_element.append(
                (state.element, target_range, target_auto, need_range, need_auto)
            )

        # Schritt 1: Autorange aus, wo der feste Bereich neu gesetzt wird.
        for element, _, _, need_range, _ in plan_per_element:
            if need_range:
                access.set_auto(quantity, element, False)
                written += 1

        # Schritt 2: feste Bereiche zurueckschreiben.
        for element, target_range, _, need_range, _ in plan_per_element:
            if need_range:
                access.set_range(quantity, element, target_range)
                written += 1

        # Schritt 3: Autorange auf den gesicherten Zustand bringen. Ein
        # gesichertes Autorange EIN muss auch dann wieder gesetzt werden, wenn
        # es urspruenglich schon EIN war - Schritt 1 hat es zwischenzeitlich
        # abgeschaltet, um den Bereich ueberhaupt setzen zu koennen.
        for element, _, target_auto, need_range, need_auto in plan_per_element:
            if need_auto or (need_range and target_auto):
                access.set_auto(quantity, element, target_auto)
                written += 1

    _log.info("Wiederherstellung: %d Kommandos gesendet", written)
    return written


# ---------------------------------------------------------------------------
# Ablauf als Kontextmanager
# ---------------------------------------------------------------------------


@dataclass
class RangeReport:
    """Ergebnis eines Durchlaufs - was gesichert, geschrieben, gefunden wurde."""

    backup: RangeBackup
    commands_written: int = 0
    problems: list[str] = field(default_factory=list)
    restore_problems: list[str] = field(default_factory=list)


@contextmanager
def applied_ranges(
    access: RangeAccess,
    plan: RangePlan,
    backup_file: Path | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
    allow_snapping: bool = False,
    force_restore: bool = False,
) -> Iterator[RangeReport]:
    """Bereiche setzen, Block ausfuehren, Ausgangszustand garantiert zurueck.

    Kapselt den try/finally-Ablauf, der in den Stufen 3 und 4 jedes Mal von
    Hand nachgebaut wurde: sichern, Schreibprobe, anwenden, verifizieren,
    Nutzblock, wiederherstellen, Gegenprobe.

    Die Wiederherstellung laeuft im finally und damit auch bei Strg+C oder
    einem Fehler im Nutzblock. Sie benutzt dieselbe Sitzung - ein zweiter
    Verbindungsaufbau waere unzuverlaessiger.
    """
    if not access.allow_changes:
        raise WTError(
            "applied_ranges() braucht ein RangeAccess mit allow_changes=True "
            "und eine WTSession mit read_only=False"
        )

    check_preconditions(access)
    backup = RangeBackup.capture(access)
    backup.log_summary()
    if backup_file is not None:
        backup.save(backup_file)

    report = RangeReport(backup=backup)

    try:
        probe_write_capability(access, backup)
        report.commands_written = apply_plan(access, plan)
        report.problems = verify_plan(access, plan, tolerance, allow_snapping)
        if report.problems:
            for problem in report.problems:
                _log.error("Verifikation: %s", problem)
            raise WTError(f"{len(report.problems)} Abweichung(en) beim Setzen der Bereiche")
        yield report

    finally:
        try:
            restore_ranges(access, backup, force=force_restore)
            report.restore_problems = backup.diff(RangeBackup.capture(access), tolerance)
            if report.restore_problems:
                for problem in report.restore_problems:
                    _log.error("Restore-Kontrolle: %s", problem)
            else:
                _log.info("Restore-Kontrolle: Ausgangszustand exakt wiederhergestellt")
        except WTError as error:
            location = backup_file if backup_file is not None else "nicht gesichert"
            _log.error("Wiederherstellung fehlgeschlagen: %s - Backup: %s", error, location)
            raise