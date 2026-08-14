# =============================================================================
# Datei: wt3000_itemspec.py
# Layer 3 (Erweiterung) - Eigene Item-Tabellen deklarieren, schreiben,
# verifizieren und vollstaendig zurueckstellen.
#
# Aendert nichts an wt3000_core.py oder wt3000_numeric.py.
# =============================================================================

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from wt3000_core import DeviceError, WTError, WTSession
from wt3000_numeric import ItemTable, NumericItem

_log = logging.getLogger("wt3000.itemspec")

# Order-Werte, die als gleichwertig zu "kein Order angegeben" gelten.
# Das Geraet ergaenzt bei manchen Funktionen TOTal von sich aus.
_DEFAULT_ORDERS: frozenset[str] = frozenset({"TOTAL", "TOT"})


@dataclass(frozen=True)
class ItemSpec:
    """Ein gewuenschtes Item der Zieltabelle.

    <Element> weggelassen -> Geraet setzt Element 1.
    <Order> weggelassen   -> Geraet setzt TOTal.
    """

    function: str
    element: str | None = None
    order: str | None = None
    # True, wenn die Funktion auf dem Original-WT3000 nicht gesichert ist.
    verify: bool = False

    @property
    def argument(self) -> str:
        """Parameterstring fuer ':NUMeric:NORMal:ITEM<x> <argument>'."""
        parts = [self.function]
        if self.element is not None:
            parts.append(self.element)
        if self.order is not None:
            parts.append(self.order)
        return ",".join(parts)


# ---------------------------------------------------------------------------
# Tabelle bauen
# ---------------------------------------------------------------------------


def build_item_table(specs: tuple[ItemSpec, ...] | list[ItemSpec]) -> ItemTable:
    """Aus einer Spec-Liste die Ziel-ItemTable erzeugen (Index ab 1)."""
    if not specs:
        raise WTError("Leere Zieltabelle")
    if len(specs) > 255:
        raise WTError(f"{len(specs)} Items - das Geraet unterstuetzt maximal 255")

    items = [
        NumericItem(index=i, function=s.function, element=s.element, order=s.order)
        for i, s in enumerate(specs, start=1)
    ]
    return ItemTable(number=len(items), items=items)


# ---------------------------------------------------------------------------
# Vergleichsregeln
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Vergleichsregeln
# ---------------------------------------------------------------------------


def _functions_compatible(requested: str, actual: str) -> bool:
    """Kurzform des Geraets gegen die gesendete Form pruefen.

    VERBose ist aus, das Geraet antwortet in Kurzform ('LAMB' statt 'LAMBDA',
    verifiziert in Stufe 3). SCPI-Regel: die Kurzform ist ein Praefix der
    Langform.

    Die Pruefung laeuft bewusst nur in EINE Richtung - die Antwort muss ein
    Praefix der Anforderung sein. Beidseitiges Praefixmatching wuerde
    'gesendet U, zurueckgelesen UTHD' faelschlich als Treffer werten.
    """
    req, act = requested.upper(), actual.upper()
    return req == act or req.startswith(act)


def _canonical_element(element: str | None) -> str:
    """Elementangabe auf ein eindeutiges Token normalisieren.

    Hier ist KEIN Praefixmatching erlaubt: 'SIGMB'.startswith('SIGM') ist wahr,
    eine Praefixregel wuerde also die Wiring-Units SigmaA und SigmaB
    gleichsetzen. Bei der Verdrahtung V3A3,P1W2 (SigmaA = Elemente 1-3,
    SigmaB = Element 4) waere das ein stiller Vertauscher.
    """
    if element is None:
        return "1"  # Geraet setzt Element 1, wenn <Element> weggelassen wird
    token = element.strip().upper()
    if token == "SIGMB":
        return "SIGMB"
    if token in {"SIGMA", "SIGM"}:
        return "SIGMA"
    return token


def _elements_compatible(requested: str | None, actual: str | None) -> bool:
    """Elemente nach Normalisierung exakt vergleichen."""
    return _canonical_element(requested) == _canonical_element(actual)


def _orders_compatible(requested: str | None, actual: str | None) -> bool:
    """Fehlenden Order und TOTal/TOT als gleichwertig behandeln.

    Verifiziert in Stufe 3: gesendet 'U,1' -> zurueckgelesen 'U,1,TOT'.
    """
    a = (requested or "TOTAL").upper()
    b = (actual or "TOTAL").upper()
    if a == b:
        return True
    return a in _DEFAULT_ORDERS and b in _DEFAULT_ORDERS


def items_match(requested: NumericItem, actual: NumericItem) -> bool:
    """Pruefen, ob das Geraet das angeforderte Item uebernommen hat."""
    return (
        _functions_compatible(requested.function, actual.function)
        and _elements_compatible(requested.element, actual.element)
        and _orders_compatible(requested.order, actual.order)
    )

# ---------------------------------------------------------------------------
# Backup jenseits von NUMber
# ---------------------------------------------------------------------------


def probe_extra_items(session: WTSession, first_index: int, last_index: int) -> list[NumericItem]:
    """Items oberhalb von NUMber einzeln abfragen und sichern.

    ':NUMeric:NORMal?' gibt nur Items bis NUMber aus. Wenn die Zieltabelle
    laenger ist als die gesicherte, muessen die dahinterliegenden Items
    einzeln gelesen werden, bevor sie ueberschrieben werden.
    """
    if last_index < first_index:
        return []

    _log.info("Sichere Items %d..%d einzeln (jenseits von NUMber)", first_index, last_index)
    tail: list[NumericItem] = []
    for index in range(first_index, last_index + 1):
        response = session.query(f":NUMeric:NORMal:ITEM{index}?")
        tail.append(NumericItem.parse(index, response))
    return tail


# ---------------------------------------------------------------------------
# Backup-Bundle (Tabelle + Tail) auf Platte
# ---------------------------------------------------------------------------


def save_backup_bundle(path: Path, table: ItemTable, tail: list[NumericItem]) -> None:
    """Gesicherte Tabelle inkl. Tail als JSON schreiben."""
    bundle = {
        "table": table.to_dict(),
        "tail": [
            {
                "index": it.index,
                "function": it.function,
                "element": it.element,
                "order": it.order,
                "argument": it.argument,
            }
            for it in tail
        ],
    }
    path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    _log.info("Backup-Bundle gesichert nach %s (%d Items + %d Tail)", path, len(table.items), len(tail))


def load_backup_bundle(path: Path) -> tuple[ItemTable, list[NumericItem]]:
    """Gegenstueck zu save_backup_bundle()."""
    bundle = json.loads(path.read_text(encoding="utf-8"))
    table = ItemTable.from_dict(bundle["table"])
    tail = [
        NumericItem(
            index=int(d["index"]),
            function=d["function"],
            element=d.get("element"),
            order=d.get("order"),
        )
        for d in bundle.get("tail", [])
    ]
    return table, tail


# ---------------------------------------------------------------------------
# Schreiben, pruefen, zuruecksetzen
# ---------------------------------------------------------------------------


def probe_write_capability(session: WTSession, target: ItemTable, backup: ItemTable) -> None:
    """Genau EIN Item schreiben und zuruecklesen, bevor die ganze Tabelle geht.

    Faellt der Test durch, ist nur ein einziges Item veraendert - der Restore
    im aufrufenden finally raeumt das auf.
    """
    backup_by_index = {it.index: it for it in backup.items}

    candidate: NumericItem | None = None
    for item in target.items:
        existing = backup_by_index.get(item.index)
        if existing is None or existing.argument != item.argument:
            candidate = item
            break

    if candidate is None:
        _log.info("Zieltabelle entspricht bereits dem Ist-Zustand - Write-Probe uebersprungen")
        return

    command = f":NUMeric:NORMal:ITEM{candidate.index} {candidate.argument}"
    _log.info("Write-Probe: %s", command)
    session.write(command)

    response = session.query(f":NUMeric:NORMal:ITEM{candidate.index}?")
    actual = NumericItem.parse(candidate.index, response)

    if not items_match(candidate, actual):
        errors = session.read_error_queue()
        raise DeviceError(
            f"Write-Probe fehlgeschlagen. Gesendet: {candidate.argument!r}, "
            f"zurueckgelesen: {actual.argument!r}. Fehlerqueue: {errors}. "
            "Moegliche Ursache: Set-Kommandos werden ohne ':COMMunicate:REMote ON' "
            "abgelehnt - dann use_remote=True in WTConfig setzen."
        )

    session.assert_no_error("Write-Probe")
    _log.info("Write-Probe erfolgreich (zurueckgelesen: %s)", actual.argument)


def apply_item_table(session: WTSession, target: ItemTable) -> None:
    """Zieltabelle schreiben. NUMber wird IMMER mitgeschrieben.

    Das Vergessen von NUMber ist der haeufigste Fehler: VALue? liefert dann
    weiterhin nur die alte Anzahl Werte.
    """
    for item in target.items:
        session.write(f":NUMeric:NORMal:ITEM{item.index} {item.argument}")

    session.write(f":NUMeric:NORMal:NUMber {target.number}")
    session.assert_no_error("Schreiben der Item-Tabelle")
    _log.info("Item-Tabelle geschrieben: %d Items, NUMber=%d", len(target.items), target.number)


def verify_item_table(session: WTSession, target: ItemTable) -> list[str]:
    """Ist-Tabelle zurueckliefern und mit der Anforderung vergleichen.

    Rueckgabe: Liste der Abweichungen (leer = alles in Ordnung).
    """
    actual = ItemTable.read_from_device(session)
    problems: list[str] = []

    if actual.number != target.number:
        problems.append(f"NUMber ist {actual.number}, erwartet {target.number}")

    actual_by_index = {it.index: it for it in actual.items}
    for wanted in target.items:
        got = actual_by_index.get(wanted.index)
        if got is None:
            problems.append(f"ITEM{wanted.index} fehlt in der Antwort")
        elif not items_match(wanted, got):
            problems.append(
                f"ITEM{wanted.index}: gesendet {wanted.argument!r}, "
                f"zurueckgelesen {got.argument!r}"
            )

    if not problems:
        _log.info("Verifikation erfolgreich: alle %d Items uebernommen", len(target.items))
    return problems


def restore_item_table(
    session: WTSession,
    backup: ItemTable,
    tail: list[NumericItem],
    force: bool = False,
) -> int:
    """Gesicherten Zustand vollstaendig wiederherstellen.

    Schreibt Items 1..NUMber aus dem Backup, danach den gesicherten Tail,
    danach NUMber. Kein CLEar - es wird nichts geloescht, was nicht vorher
    gesichert wurde.
    """
    written = backup.restore_to_device(session, force=force)

    for item in tail:
        session.write(f":NUMeric:NORMal:ITEM{item.index} {item.argument}")
        written += 1

    if tail:
        # NUMber nach dem Tail nochmals setzen, damit der Ausgabeumfang stimmt.
        session.write(f":NUMeric:NORMal:NUMber {backup.number}")
        written += 1
        session.assert_no_error("Wiederherstellung des Tails")

    return written