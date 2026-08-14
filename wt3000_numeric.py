# =============================================================================
# Datei: wt3000_numeric.py
# Layer 3 - Messwert-Layer: Item-Tabelle spiegeln, FLOat-Block parsen,
#           Werte auf Namen zurueckmappen, Tabelle sichern/wiederherstellen.
# =============================================================================

from __future__ import annotations

import json
import logging
import math
import struct
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from wt3000_core import ProtocolError, WTSession

_log = logging.getLogger("wt3000.numeric")

# --- Sentinel-Bitmuster im FLOat-Format (Handbuch, "Numeric Data Format") ----
# ACHTUNG: Das sind gueltige, ENDLICHE IEEE-Singles um 9.9E+37. math.isnan()
# und math.isinf() greifen hier NICHT. Der Vergleich erfolgt deshalb auf dem
# rohen 4-Byte-Bitmuster, bevor ueberhaupt in float gewandelt wird.
# Nicht verwechseln mit 0x7FC00000 etc. - die gelten fuer .WTD-Dateien.
FLOAT_NO_DATA: int = 0x7E951BEE  # ASCii-Aequivalent: "NAN"  -> Item existiert nicht
FLOAT_OVERRANGE: int = 0x7E94F56A  # ASCii-Aequivalent: "INF"  -> Bereich falsch


class ValueStatus(Enum):
    """Messtechnische Bedeutung eines gelesenen Werts."""

    OK = "OK"
    NO_DATA = "NO_DATA"  # NAN: Item ist NONE oder nicht zur Messung konfiguriert
    OVERRANGE = "OVERRANGE"  # INF: Overrange, Overflow, Data over, nicht eingeschwungen


@dataclass(frozen=True)
class NumericValue:
    """Ein einzelner Messwert samt Statusbewertung."""

    value: float
    status: ValueStatus
    raw_bits: int

    @property
    def is_usable(self) -> bool:
        """True, wenn der Wert messtechnisch verwertbar ist."""
        return self.status is ValueStatus.OK

    def __str__(self) -> str:
        if self.status is ValueStatus.OK:
            return f"{self.value: .6g}"
        return f"<{self.status.value}>"


@dataclass(frozen=True)
class NumericItem:
    """Ein Eintrag der Item-Tabelle: :NUMeric:NORMal:ITEM<index>."""

    index: int  # 1..255
    function: str  # z.B. "UTHD", "FU", "PHI", "NONE"
    element: str | None = None  # "1".."4", "SIGMA", "SIGMB"
    order: str | None = None  # "TOTAL", "DC", "1".."100"

    @property
    def is_none(self) -> bool:
        """True, wenn das Item auf NONE steht."""
        return self.function.upper() == "NONE"

    @property
    def argument(self) -> str:
        """Parameterstring, wie ihn ITEM<x> als Eingabe erwartet."""
        if self.is_none:
            return "NONE"
        parts = [self.function]
        if self.element is not None:
            parts.append(self.element)
        if self.order is not None:
            parts.append(self.order)
        return ",".join(parts)

    @property
    def key(self) -> str:
        """Sprechender Name fuer das Ergebnis-Dictionary, z.B. 'UTHD1', 'PHI1_1'."""
        if self.is_none:
            return f"NONE_{self.index}"
        name = self.function
        if self.element is not None:
            name += self.element
        if self.order is not None:
            name += f"_{self.order}"
        return name

    @classmethod
    def parse(cls, index: int, token: str) -> "NumericItem":
        """Ein Token wie 'UTHD,1' oder 'PHI,1,1' in ein NumericItem wandeln."""
        parts = [p.strip() for p in token.split(",") if p.strip()]
        if not parts:
            raise ProtocolError(f"Leeres Item-Token an Position {index}")
        return cls(
            index=index,
            function=parts[0],
            element=parts[1] if len(parts) > 1 else None,
            order=parts[2] if len(parts) > 2 else None,
        )


@dataclass
class ItemTable:
    """Spiegelbild der geraeteseitigen Item-Tabelle.

    Die Reihenfolge ist die zentrale Information: :NUMeric:NORMal:VALue?
    liefert die Werte ausschliesslich positionsbezogen.
    """

    number: int
    items: list[NumericItem] = field(default_factory=list)

    # -- Erzeugen -----------------------------------------------------------

    @classmethod
    def from_response(cls, response: str) -> "ItemTable":
        """Antwort auf ':NUMeric:NORMal?' parsen (Header-Modus OFF vorausgesetzt).

        Format: '<NUMber>;<Item1>;<Item2>;...'
        """
        fields = [f.strip() for f in response.split(";") if f.strip()]
        if not fields:
            raise ProtocolError("Leere Antwort auf :NUMeric:NORMal?")
        try:
            number = int(fields[0])
        except ValueError as exc:
            raise ProtocolError(
                f"Erstes Feld ist keine Zahl: {fields[0]!r}. "
                "Sind die Header eingeschaltet (:COMMunicate:HEADer)?"
            ) from exc

        items = [NumericItem.parse(i, token) for i, token in enumerate(fields[1:], start=1)]
        if len(items) != number:
            _log.warning(
                "NUMber=%d, aber %d Items in der Antwort - Feldzuordnung pruefen",
                number,
                len(items),
            )
        return cls(number=number, items=items)

    @classmethod
    def read_from_device(cls, session: WTSession) -> "ItemTable":
        """Aktuelle Item-Tabelle vom Geraet lesen."""
        response = session.query(":NUMeric:NORMal?")
        table = cls.from_response(response)
        _log.info("Item-Tabelle gelesen: NUMber=%d, %d Items", table.number, len(table.items))
        return table

    # -- Sichern / Wiederherstellen ----------------------------------------

    def to_dict(self) -> dict:
        """Serialisierbare Darstellung fuer das JSON-Backup."""
        return {
            "number": self.number,
            "items": [
                {
                    "index": it.index,
                    "function": it.function,
                    "element": it.element,
                    "order": it.order,
                    "argument": it.argument,
                }
                for it in self.items
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ItemTable":
        """Gegenstueck zu to_dict()."""
        items = [
            NumericItem(
                index=int(d["index"]),
                function=d["function"],
                element=d.get("element"),
                order=d.get("order"),
            )
            for d in data["items"]
        ]
        return cls(number=int(data["number"]), items=items)

    def save(self, path: Path) -> None:
        """Backup als JSON auf Platte schreiben."""
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        _log.info("Item-Tabelle gesichert nach %s", path)

    @classmethod
    def load(cls, path: Path) -> "ItemTable":
        """Backup aus JSON laden."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def restore_to_device(self, session: WTSession, force: bool = False) -> int:
        """Diese Tabelle auf dem Geraet wiederherstellen.

        Es werden nur Items geschrieben, die vom Ist-Zustand abweichen -
        ausser force=True, dann werden alle Items geschrieben (nuetzlich, um
        den Schreibpfad einmal bewusst zu testen).

        Bewusst wird KEIN ':NUMeric:NORMal:CLEar ALL' gesendet: Items jenseits
        von NUMber sind im Backup nicht enthalten und bleiben so unangetastet.

        Rueckgabe: Anzahl tatsaechlich geschriebener Kommandos.
        """
        current = ItemTable.read_from_device(session)
        current_by_index = {it.index: it.argument for it in current.items}

        written = 0
        for item in self.items:
            if not force and current_by_index.get(item.index) == item.argument:
                continue
            session.write(f":NUMeric:NORMal:ITEM{item.index} {item.argument}")
            written += 1

        if force or current.number != self.number:
            session.write(f":NUMeric:NORMal:NUMber {self.number}")
            written += 1

        if written:
            session.assert_no_error("Wiederherstellung der Item-Tabelle")
            _log.info("Item-Tabelle wiederhergestellt (%d Kommandos)", written)
        else:
            _log.info("Item-Tabelle unveraendert - kein Schreibzugriff noetig")
        return written

    # -- Werte zuordnen -----------------------------------------------------

    def map_values(self, values: list[NumericValue]) -> dict[str, NumericValue]:
        """Positionsbezogene Werte auf sprechende Namen abbilden.

        Doppelte Schluessel (die Tabelle enthaelt z.B. FU,1 zweimal) bekommen
        das Suffix '#2', '#3' usw. Die geordnete Liste bleibt ueber
        zip(table.items, values) jederzeit verfuegbar.
        """
        if len(values) != len(self.items):
            _log.warning(
                "Anzahl Werte (%d) passt nicht zur Anzahl Items (%d)", len(values), len(self.items)
            )

        mapped: dict[str, NumericValue] = {}
        seen: dict[str, int] = {}
        for item, value in zip(self.items, values):
            key = item.key
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key = f"{key}#{seen[key]}"
            mapped[key] = value
        return mapped


# ---------------------------------------------------------------------------
# FLOat-Parser
# ---------------------------------------------------------------------------


def parse_float_block(payload: bytes) -> list[NumericValue]:
    """Binaeren Messwertblock (IEEE single, MSB first) in NumericValue-Liste wandeln.

    Die Sentinel-Bitmuster fuer NAN und INF werden VOR der Float-Wandlung
    erkannt, weil sie als IEEE-Zahl voellig unauffaellig aussehen.
    """
    if len(payload) % 4 != 0:
        raise ProtocolError(f"Blocklaenge {len(payload)} ist kein Vielfaches von 4 Bytes")

    values: list[NumericValue] = []
    for offset in range(0, len(payload), 4):
        chunk = payload[offset : offset + 4]
        (bits,) = struct.unpack(">I", chunk)  # MSB first
        if bits == FLOAT_NO_DATA:
            values.append(NumericValue(math.nan, ValueStatus.NO_DATA, bits))
        elif bits == FLOAT_OVERRANGE:
            values.append(NumericValue(math.inf, ValueStatus.OVERRANGE, bits))
        else:
            (number,) = struct.unpack(">f", chunk)
            values.append(NumericValue(number, ValueStatus.OK, bits))
    return values


def read_numeric_values(session: WTSession, expected_count: int | None = None) -> list[NumericValue]:
    """':NUMeric:NORMal:VALue?' im FLOat-Format lesen und parsen."""
    payload = session.query_block(":NUMeric:NORMal:VALue?")
    values = parse_float_block(payload)
    if expected_count is not None and len(values) != expected_count:
        _log.warning("Erwartet: %d Werte, erhalten: %d", expected_count, len(values))
    return values