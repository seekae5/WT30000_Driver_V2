# =============================================================================
# Datei: stage5_input_config.py
# Layer 4 - Stufe 5: Eingangskonfiguration erfassen und dokumentieren.
#
# Dieses Skript SCHREIBT NICHTS. Es oeffnet die Sitzung mit read_only=True und
# das Konfigurationsobjekt mit allow_changes=False - zwei unabhaengige Sperren.
# Zweck: den eingemessenen Ist-Zustand als JSON sichern, bevor jemals ein
# Schreibversuch am realen Geraet stattfindet.
# =============================================================================

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from wt3000_core import TmctlTransport, WTConfig, WTError, WTSession
from wt3000_input import InputConfig, InputSnapshot

# Zielverzeichnis fuer Snapshot und Protokoll.
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


def main() -> int:
    """Stufe 5 ausfuehren. Rueckgabewert 0 = erfolgreich."""
    config = WTConfig()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    log_file = OUTPUT_DIR / f"wt3000_stage5_{timestamp}.txt"
    snapshot_file = OUTPUT_DIR / f"wt3000_inputconfig_{timestamp}.json"
    dump_file = OUTPUT_DIR / f"wt3000_inputdump_{timestamp}.txt"

    setup_logging(log_file)
    log = logging.getLogger("wt3000.stage5")
    log.info("Protokolldatei: %s", log_file)
    log.info("Stufe 5 - Eingangskonfiguration erfassen (nur Lesen)")

    try:
        with TmctlTransport(config) as transport:
            # read_only=True: jedes Nicht-Query-Kommando wirft ReadOnlyViolation.
            # Deshalb wird hier auch KEIN ':COMMunicate:REMote ON' gesendet.
            session = WTSession(transport, config, read_only=True)
            if config.use_remote:
                log.warning(
                    "use_remote=True wird in Stufe 5 ignoriert - "
                    "REMote ON waere ein Schreibkommando"
                )

            cfg = InputConfig(session, allow_changes=False)

            snapshot = InputSnapshot.capture(cfg)
            snapshot.log_summary()

            log.info("Wiring-Units:")
            for unit in cfg.get_wiring_units():
                log.info(
                    "  %-6s %-5s -> Elemente %s",
                    unit.name or "(unbenannt)",
                    unit.pattern,
                    ", ".join(str(e) for e in unit.elements),
                )

            snapshot.save(snapshot_file)
            dump_file.write_text(snapshot.raw_dump, encoding="utf-8")
            log.info("Rohabzug von ':INPut?' gesichert nach %s", dump_file)

            # Gegenprobe: laden, erneut erfassen, vergleichen. Damit ist
            # nachgewiesen, dass Serialisierung und Parser verlustfrei sind.
            reloaded = InputSnapshot.load(snapshot_file)
            problems = reloaded.diff(InputSnapshot.capture(cfg))
            if problems:
                for problem in problems:
                    log.error("Gegenprobe: %s", problem)
                raise WTError("Snapshot und Geraetezustand weichen voneinander ab")
            log.info("Gegenprobe erfolgreich: Snapshot bildet den Ist-Zustand exakt ab")

            session.assert_no_error("Konfigurationserfassung")

    except WTError as error:
        log.error("Abbruch: %s", error)
        return 1

    log.info("=" * 78)
    log.info("Snapshot:  %s", snapshot_file)
    log.info("Rohabzug:  %s", dump_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())