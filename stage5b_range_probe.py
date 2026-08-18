# =============================================================================
# Datei: stage5b_range_probe.py
# Layer 4 - Stufe 5b: die offenen Fragen zur Bereichseinstellung klaeren,
#                     BEVOR jemals ein veraendernder Schreibversuch stattfindet.
#
# Voreinstellung: dieses Skript SCHREIBT NICHTS. Es oeffnet die Sitzung mit
# read_only=True und RangeAccess mit allow_changes=False - zwei unabhaengige
# Sperren, genau wie Stufe 5.
#
# Mit ENABLE_NOOP_WRITE_PROBE = True wird zusaetzlich EIN Kommando gesendet,
# das den aktuellen Spannungsbereich des ersten Elements mit seinem EIGENEN
# Wert ueberschreibt. Das ist ein Nulleffekt und beantwortet trotzdem die
# Frage, ob die INPut-Gruppe Set-Kommandos ohne ':COMMunicate:REMote ON'
# annimmt. Der Ausgangszustand wird davor gesichert und danach geprueft.
#
# Beantwortete Fragen:
#   (1) Rundet das Geraet Bereichswerte?  -> nur teilweise; siehe Hinweis unten
#   (2) Was bedeutet 'Bereich' an den externen Stromsensoren 1-3?
#   (3) Braucht ':INPut' ein ':COMMunicate:REMote ON'?
#   (4) Gibt es an Element 4 (DC) ueberhaupt einen Autorange?
# =============================================================================

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from wt3000_core import TmctlTransport, WTConfig, WTError, WTSession
from wt3000_ranging import RangeBackup, probe_write_capability
from wt3000_rangeio import Quantity, RangeAccess

# ---------------------------------------------------------------------------
# Laufparameter
# ---------------------------------------------------------------------------

# True sendet GENAU EIN Set-Kommando mit dem bereits eingestellten Wert.
# Kein Messwert und keine Eichung aendert sich dadurch. Trotzdem bewusst
# abschaltbar, damit der erste Lauf am echten Geraet rein lesend bleibt.
ENABLE_NOOP_WRITE_PROBE: bool = False

# Zielverzeichnis fuer Bericht, Backup und Protokoll.
OUTPUT_DIR: Path = Path.cwd() / "konfiguration"


def setup_logging(log_file: Path) -> None:
    """Logging auf Konsole und in eine Protokolldatei einrichten."""
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)


def report_environment(access: RangeAccess, log: logging.Logger) -> None:
    """Frage 2 und 4: Umfeld der Bereichseinstellung erfassen."""
    log.info("-" * 78)
    log.info("Umfeld")
    log.info("  Wiring:       %s", access.get_wiring())
    log.info("  Module:       %s", access.get_module())
    log.info("  INDependent:  %s", "EIN" if access.get_independent() else "AUS")
    log.info("  Peak Over:    %s", access.get_peak_over())

    # Rohabzuege. Hier steht, in welcher Einheit die Bereiche gefuehrt werden -
    # das entscheidet, ob an den Elementen 1-3 die Sensoreingangsspannung oder
    # ein Amperewert gesetzt werden muss.
    for quantity in Quantity:
        log.info("  %s-Rohabzug: %s", quantity.label, access.dump(quantity))


def report_ranges(access: RangeAccess, log: logging.Logger) -> RangeBackup:
    """Ist-Zustand aller Bereiche erfassen und protokollieren."""
    log.info("-" * 78)
    log.info("Bereichszustand")
    backup = RangeBackup.capture(access)
    backup.log_summary()

    # Frage 4: an einem DC-Kanal koennte Autorange strukturell fehlen. Wenn
    # die Abfrage sauber antwortet, existiert der Knoten - unabhaengig davon,
    # ob er dort sinnvoll ist.
    for state in backup.states:
        if state.current_auto or state.voltage_auto:
            log.info("Element %d: Autorange ist aktiv", state.element)
    return backup


def run_noop_write_probe(
    session: WTSession, backup: RangeBackup, log: logging.Logger
) -> None:
    """Frage 3: Schreibpfad testen, ohne einen Wert zu veraendern."""
    log.info("-" * 78)
    log.warning("Schreibprobe aktiviert - es wird EIN Set-Kommando gesendet")

    writable = RangeAccess(session, allow_changes=True)
    probe_write_capability(writable, backup)

    problems = backup.diff(RangeBackup.capture(writable))
    if problems:
        for problem in problems:
            log.error("Nach der Schreibprobe veraendert: %s", problem)
        raise WTError("Die Schreibprobe war kein Nulleffekt - Zustand pruefen")
    log.info("Zustand nach der Schreibprobe unveraendert")


def main() -> int:
    """Stufe 5b ausfuehren. Rueckgabewert 0 = erfolgreich."""
    config = WTConfig()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    log_file = OUTPUT_DIR / f"wt3000_stage5b_{timestamp}.txt"
    backup_file = OUTPUT_DIR / f"wt3000_ranges_{timestamp}.json"

    setup_logging(log_file)
    log = logging.getLogger("wt3000.stage5b")
    log.info("Protokolldatei: %s", log_file)
    log.info("Stufe 5b - Messbereiche erfassen (%s)",
             "mit Nulleffekt-Schreibprobe" if ENABLE_NOOP_WRITE_PROBE else "nur Lesen")

    try:
        with TmctlTransport(config) as transport:
            # Die Sitzung wird nur dann schreibfaehig geoeffnet, wenn die
            # Schreibprobe ausdruecklich verlangt ist.
            session = WTSession(transport, config, read_only=not ENABLE_NOOP_WRITE_PROBE)

            access = RangeAccess(session, allow_changes=False)
            report_environment(access, log)
            backup = report_ranges(access, log)
            backup.save(backup_file)

            if ENABLE_NOOP_WRITE_PROBE:
                run_noop_write_probe(session, backup, log)

            session.assert_no_error("Bereichserfassung")

    except WTError as error:
        log.error("Abbruch: %s", error)
        return 1

    log.info("=" * 78)
    log.info("Backup: %s", backup_file)
    log.info(
        "Offen bleibt Frage 1 (Rundung ungueltiger Werte). Sie laesst sich nur "
        "mit einer echten Aenderung klaeren und gehoert deshalb in ein eigenes, "
        "bewusst gestartetes Stufenskript."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())