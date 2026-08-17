# =============================================================================
# Datei: wt3000_measure.py
# Layer 3 (HOLD) + Layer 4 (Aufzeichnung) - wiederverwendbare Bausteine
# fuer die Messschleife.
#
# Aendert nichts an wt3000_core.py, wt3000_numeric.py, wt3000_itemspec.py.
# =============================================================================

from __future__ import annotations

import csv
import json
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import TextIO

from .wt3000_core import WTError, WTSession
from .wt3000_itemspec import ItemSpec
from .wt3000_numeric import ItemTable, NumericValue, ValueStatus, read_numeric_values

_log = logging.getLogger("wt3000.measure")


# ---------------------------------------------------------------------------
# Messprofile
# ---------------------------------------------------------------------------


def build_standard_profile() -> tuple[ItemSpec, ...]:
    """Standardprofil fuer die Verdrahtung V3A3,P1W2.

    Elemente 1-3 = Drehstromseite (Wiring-Unit SigmaA)
    SIGMA        = Summe der Drehstromseite
    Element 4    = separater DC-Kanal (Wiring-Unit SigmaB)

    FU wird nur fuer Element 3 gefuehrt: die Frequenzmessquelle steht laut
    ':MEASure?' auf U3/I3, deshalb liefern FU1 und FU2 strukturell NAN.
    Aendert sich die Frequenzmessquelle, ist diese Liste anzupassen.
    """
    three_phase = ("U", "I", "P", "S", "Q", "LAMBDA", "PHI")
    sum_functions = ("U", "I", "P", "S", "Q", "LAMBDA")
    dc_functions = ("U", "I", "P")  # Element 4 ist DC: S/Q/LAMBDA/PHI waeren NAN

    specs: list[ItemSpec] = []
    for element in ("1", "2", "3"):
        specs.extend(ItemSpec(f, element) for f in three_phase)
    specs.append(ItemSpec("FU", "3"))  # einzige konfigurierte Frequenzquelle
    specs.extend(ItemSpec(f, "SIGMA") for f in sum_functions)
    specs.extend(ItemSpec(f, "4") for f in dc_functions)
    return tuple(specs)


# ---------------------------------------------------------------------------
# Layer 3 - Snapshot ueber :NUMeric:HOLD
# ---------------------------------------------------------------------------


class NumericHold:
    """Context Manager fuer ':NUMeric:HOLD'.

    Ein erneutes ON bei aktivem HOLD verwirft die alten Daten und friert die
    aktuellsten ein - laut Handbuch der vorgesehene Weg fuer Dauermessungen.
    Es muss also nicht zwischendurch auf OFF geschaltet werden.

    Wichtig: bleibt HOLD nach einem Absturz aktiv, liefert das Geraet in der
    naechsten Sitzung eingefrorene Werte, waehrend die Anzeige weiterlaeuft.
    OFF wird deshalb im __exit__ garantiert gesendet.
    """

    def __init__(self, session: WTSession, enabled: bool = True) -> None:
        self._session = session
        self._enabled = enabled
        self._armed = False

    def __enter__(self) -> "NumericHold":
        if not self._enabled:
            _log.info("HOLD deaktiviert - Werte werden ungefroren gelesen")
            return self
        # Ein bereits aktives HOLD aus einem frueheren Lauf erkennen.
        state = self._session.query(":NUMeric:HOLD?").strip()
        if state == "1":
            _log.warning("HOLD war bereits aktiv (Rest eines frueheren Laufs) - wird uebernommen")
        return self

    def refresh(self) -> None:
        """Aktuellsten Datensatz einfrieren. Vor jedem VALue? aufrufen."""
        if not self._enabled:
            return
        self._session.write(":NUMeric:HOLD ON")
        self._armed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._enabled and not self._armed:
            return
        try:
            self._session.write(":NUMeric:HOLD OFF")
            _log.info("HOLD abgeschaltet")
        except WTError as error:
            _log.error("HOLD OFF fehlgeschlagen: %s - Geraet ggf. manuell pruefen", error)


# ---------------------------------------------------------------------------
# Layer 4 - CSV-Aufzeichnung
# ---------------------------------------------------------------------------


class CsvRecorder:
    """Schreibt Messwerte zeilenweise in eine CSV-Datei.

    Kodierung der Sonderfaelle so, dass gaengige Auswertewerkzeuge sie ohne
    Nacharbeit richtig einlesen:
        OK        -> Zahl
        NO_DATA   -> leere Zelle  (pandas: NaN)
        OVERRANGE -> 'INF'        (pandas: inf)
    Zusaetzlich listet die Spalte 'status_flags' alle nicht-OK-Items im
    Klartext, damit die Unterscheidung auch beim Sichten der Rohdatei erhalten
    bleibt.
    """

    def __init__(self, path: Path, column_names: list[str], delimiter: str = ",") -> None:
        self._path = path
        self._columns = column_names
        self._handle: TextIO = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle, delimiter=delimiter)
        header = ["timestamp_iso", "elapsed_s", "sample", "condition"]
        header.extend(column_names)
        header.append("status_flags")
        self._writer.writerow(header)
        self._handle.flush()
        _log.info("CSV geoeffnet: %s (%d Spalten)", path, len(header))

    @staticmethod
    def _cell(value: NumericValue) -> str:
        """Einen Messwert in die Zellendarstellung wandeln."""
        if value.status is ValueStatus.OK:
            return repr(value.value)  # volle float-Genauigkeit, Dezimalpunkt
        if value.status is ValueStatus.NO_DATA:
            return ""
        return "INF"

    def write_row(
        self,
        timestamp: datetime,
        elapsed_s: float,
        sample: int,
        condition: int | None,
        values: list[NumericValue],
    ) -> None:
        """Eine Messzeile schreiben und sofort flushen."""
        flags = [
            f"{name}={value.status.value}"
            for name, value in zip(self._columns, values)
            if value.status is not ValueStatus.OK
        ]
        row: list[str] = [
            timestamp.isoformat(timespec="milliseconds"),
            f"{elapsed_s:.3f}",
            str(sample),
            "" if condition is None else str(condition),
        ]
        row.extend(self._cell(v) for v in values)
        row.append(";".join(flags))
        self._writer.writerow(row)
        # Bei 1 Hz kostenlos; ein harter Abbruch kostet damit hoechstens
        # die letzte Zeile.
        self._handle.flush()

    def close(self) -> None:
        """Datei schliessen. Mehrfachaufruf ist unschaedlich."""
        if not self._handle.closed:
            self._handle.close()
            _log.info("CSV geschlossen: %s", self._path)

    def __enter__(self) -> "CsvRecorder":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Metadaten-Sidecar
# ---------------------------------------------------------------------------


def write_metadata(
    path: Path,
    session: WTSession,
    table: ItemTable,
    parameters: dict,
) -> None:
    """Geraetezustand und Laufparameter neben der CSV ablegen.

    Ohne diese Angaben ist eine Messreihe spaeter nicht mehr interpretierbar -
    insbesondere Bereiche und Skalierung (z.B. CT = 2000 auf Element 4).
    Alle Abfragen sind reine Queries.
    """
    queries = {
        "idn": "*IDN?",
        "communicate": ":COMMunicate?",
        "rate": ":RATE?",
        "numeric_format": ":NUMeric:FORMat?",
        "input": ":INPut?",
        "input_wiring": ":INPut:WIRing?",
        "input_module": ":INPut:MODUle?",
        "input_scaling": ":INPut:SCALing?",
        "input_filter": ":INPut:FILTer?",
        "input_cfactor": ":INPut:CFACtor?",
        "measure": ":MEASure?",
    }
    device: dict[str, str] = {}
    for key, command in queries.items():
        try:
            device[key] = session.query(command)
        except WTError as error:
            device[key] = f"<Fehler: {error}>"

    payload = {
        "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "parameters": parameters,
        "device": device,
        "item_table": table.to_dict(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _log.info("Metadaten gesichert nach %s", path)


# ---------------------------------------------------------------------------
# Messschleife
# ---------------------------------------------------------------------------


@dataclass
class LoopStatistics:
    """Auswertung der Zykluszeiten und Statusverteilung."""

    samples: int = 0
    overruns: int = 0
    cycle_times: list[float] = field(default_factory=list)
    status_counts: dict[ValueStatus, int] = field(
        default_factory=lambda: {s: 0 for s in ValueStatus}
    )

    def log_summary(self, interval_s: float) -> None:
        """Zusammenfassung ausgeben."""
        _log.info("=" * 78)
        _log.info("Samples: %d, Overruns: %d", self.samples, self.overruns)
        if self.cycle_times:
            _log.info(
                "Zykluszeit min/median/max: %.3f / %.3f / %.3f s (Soll %.3f s)",
                min(self.cycle_times),
                statistics.median(self.cycle_times),
                max(self.cycle_times),
                interval_s,
            )
        total = sum(self.status_counts.values())
        if total:
            for status, count in self.status_counts.items():
                _log.info("  %-10s %6d  (%.1f %%)", status.value, count, 100.0 * count / total)


def run_measurement_loop(
    session: WTSession,
    table: ItemTable,
    recorder: CsvRecorder,
    interval_s: float,
    max_samples: int | None,
    max_duration_s: float | None,
    use_hold: bool,
    record_condition: bool,
    log_every: int,
) -> LoopStatistics:
    """Messschleife mit driftfreier Taktung.

    Bricht sauber ab bei KeyboardInterrupt, erreichter Sampleanzahl oder
    abgelaufener Maximaldauer.
    """
    stats = LoopStatistics()
    started_monotonic = time.monotonic()
    next_tick = started_monotonic
    sample = 0

    with NumericHold(session, enabled=use_hold) as hold:
        try:
            while True:
                if max_samples is not None and sample >= max_samples:
                    _log.info("Sampleanzahl erreicht (%d)", max_samples)
                    break
                elapsed = time.monotonic() - started_monotonic
                if max_duration_s is not None and elapsed >= max_duration_s:
                    _log.info("Maximaldauer erreicht (%.1f s)", max_duration_s)
                    break

                # Auf den naechsten Takt warten.
                wait = next_tick - time.monotonic()
                if wait > 0:
                    time.sleep(wait)

                cycle_start = time.monotonic()

                # Snapshot einfrieren, dann lesen. Der Zeitstempel bezieht sich
                # auf den Moment des HOLD ON, nicht auf den Antworteingang.
                hold.refresh()
                timestamp = datetime.now(timezone.utc).astimezone()
                values = read_numeric_values(session, expected_count=len(table.items))

                condition: int | None = None
                if record_condition:
                    condition = int(session.query(":STATus:CONDition?"))

                sample += 1
                stats.samples = sample
                for value in values:
                    stats.status_counts[value.status] += 1

                recorder.write_row(
                    timestamp=timestamp,
                    elapsed_s=cycle_start - started_monotonic,
                    sample=sample,
                    condition=condition,
                    values=values,
                )

                cycle_time = time.monotonic() - cycle_start
                stats.cycle_times.append(cycle_time)

                if log_every > 0 and sample % log_every == 0:
                    _log.info(
                        "Sample %d | Zyklus %.3f s | Condition %s | %s",
                        sample,
                        cycle_time,
                        "-" if condition is None else condition,
                        _preview(table, values),
                    )

                # Naechsten Takt setzen. Bei Overrun wird der Takt neu
                # aufgesetzt, statt aufzuholen.
                next_tick += interval_s
                if next_tick < time.monotonic():
                    stats.overruns += 1
                    if stats.overruns in (1, 10, 100) or stats.overruns % 500 == 0:
                        _log.warning(
                            "Zyklus %d ueberschreitet das Intervall (%.3f s > %.3f s), "
                            "Overruns bisher: %d",
                            sample,
                            cycle_time,
                            interval_s,
                            stats.overruns,
                        )
                    next_tick = time.monotonic() + interval_s

        except KeyboardInterrupt:
            _log.info("Abbruch durch Benutzer (Strg+C) nach %d Samples", sample)

    return stats


def _preview(table: ItemTable, values: list[NumericValue], count: int = 3) -> str:
    """Kurze Vorschau der ersten Werte fuer die Logzeile."""
    parts = [
        f"{item.key}={value}" for item, value in list(zip(table.items, values))[:count]
    ]
    return " ".join(parts)