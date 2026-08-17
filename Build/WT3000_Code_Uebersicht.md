# Überblick: WT3000-Treiber & Testautomatisierung

Stand der Durchsicht: 17.08.2026, Ordner `Communication_WT3000_Unterlagen\Code`, 9 Python-Dateien.

## Was das Programm macht

Der Code steuert ein Yokogawa-Leistungsmessgerät WT3000 über Ethernet an, und zwar nicht über eine Standard-SCPI-Socket-Verbindung, sondern über die herstellereigene **TMCTL-DLL** (`tmctl64.dll`), die per `ctypes` eingebunden wird. Zweck ist, Messkonfiguration und Messwerte automatisiert auszulesen, eine eigene Messkanal-Konfiguration ("Item-Tabelle") zu setzen und darüber Messreihen als CSV mitzuschreiben — ohne dass am Ende die werkseitig eingemessene Geräteeinstellung verändert zurückbleibt. Der durchgängige rote Faden im gesamten Code ist: **vor jedem Schreibzugriff sichern, danach exakt wiederherstellen und die Wiederherstellung verifizieren.**

## Architektur: sauberes Schichtenmodell

Der Code ist bewusst in Layer 0–4 aufgeteilt, jede Schicht kennt nur die darunterliegende, nicht die darüberliegende. Das macht das Ganze zwar auf den ersten Blick "viele Dateien für ein Messgerät", ist aber sehr wartungsfreundlich, weil jede Schicht einzeln austauschbar/testbar ist.

| Layer | Datei | Aufgabe |
|---|---|---|
| 0 – Transport | `wt3000_core.py` (`TmctlTransport`) | Rohes Senden/Empfangen über die TMCTL-DLL, Timeout, Verbindungsauf-/-abbau |
| 1 – Session/Protokoll | `wt3000_core.py` (`WTSession`) | SCPI-Regeln durchsetzen (genau ein Query pro Nachricht, Read-Only-Sperre), Blockdaten (`#n...`) zusammensetzen, Fehlerqueue auslesen |
| 2 – Geräte-Domäne | `wt3000_input.py` | Eingangs-/Messkonfiguration: Verdrahtung, Spannungs-/Strombereiche, Auto-Range, Filter, Skalierung, Sync-Quelle, Update-Rate |
| 3 – Messwert-Layer | `wt3000_numeric.py`, `wt3000_itemspec.py`, `wt3000_measure.py` (Teil) | Item-Tabelle lesen/schreiben, FLOat-Binärblock in Werte parsen, HOLD-Snapshot |
| 4 – Anwendung/Ablauf | `stage2…stage5_*.py`, `wt3000_measure.py` (Teil) | Konkrete, ausführbare Skripte, die die Schichten darunter zu einem Testablauf zusammensetzen |

Jede Moduldatei trägt im Kopf einen Kommentar, welche Schicht sie ist und was sie explizit *nicht* verändert – das ist ein durchgehendes Stilmittel im ganzen Projekt.

## Die Bausteine im Einzelnen

**`wt3000_core.py`** — das Fundament. `TmctlTransport` öffnet die Verbindung (`TmcInitialize` mit IP/User/Passwort), sendet/empfängt Rohbytes und schließt sauber wieder (Context-Manager, `with`-fähig). Darauf sitzt `WTSession`: sie erzwingt SCPI-Disziplin (Set- vs. Query-Kommando, maximal ein `?` pro Nachricht), bietet eine echte Nur-Lesen-Sperre (`read_only=True` löst `ReadOnlyViolation` aus) und kann Fernsteuerung ein-/ausschalten. Wichtig: `query_block()` sammelt Binärdaten mit `#n`-Header so lange nach, bis die im Header angekündigte Länge komplett vorliegt.

**`wt3000_numeric.py`** — bildet die geräteseitige "Item-Tabelle" ab (`ItemTable`/`NumericItem`, das ist die Liste, die festlegt, welche Messgröße an welcher Position steht). Kann diese Tabelle vom Gerät lesen, als JSON sichern/laden und punktgenau wiederherstellen (nur abweichende Einträge schreiben). Zusätzlich der Binärparser für das FLOat-Antwortformat: Auffällig und gut kommentiert ist, dass NaN/Overrange nicht als IEEE-NaN/-Inf codiert sind, sondern als spezielle, sonst gültig aussehende Bitmuster (`0x7E951BEE`/`0x7E94F56A`), die vor der Float-Umwandlung erkannt werden müssen.

**`wt3000_itemspec.py`** — Komfortschicht darüber, um eine *gewünschte* Item-Tabelle deklarativ zu bauen (`ItemSpec`), sie zu schreiben, zurückzulesen und feldweise zu vergleichen (inkl. Kurzform-/Präfix-Logik, weil das Gerät Funktionsnamen abgekürzt zurückgibt). Enthält außerdem die "Fail-Fast"-Logik: vor dem großen Schreibvorgang wird testweise nur ein Item geschrieben und geprüft, ob es ankommt.

**`wt3000_measure.py`** — der Messbetrieb: `NumericHold` friert per `:NUMeric:HOLD` einen Datensatz ein, bevor er ausgelesen wird (für konsistente Zeitstempel), `CsvRecorder` schreibt Messwerte zeilenweise in CSV (mit sauberer Kodierung von NO_DATA/OVERRANGE), `run_measurement_loop` ist die eigentliche getaktete Messschleife (driftfrei, mit Overrun-Erkennung, sauberem Strg+C-Abbruch) und `write_metadata` sichert Geräte-Metadaten neben die CSV.

**`wt3000_input.py`** — die mit Abstand größte Datei (~55 KB) und behandelt die "kritische" Gerätekonfiguration: Verdrahtung, Spannungs-/Strombereiche, Auto-Range, Crest-Faktor, Filter, Skalierung (VT/CT/SFACtor), Sync-Quelle, Update-Rate. Hier gibt es ein explizites zweistufiges Sperrkonzept: `allow_changes=False` als Standard plus eine granulare Gruppensperre (`GROUP_WIRING`, `GROUP_RANGE`, `GROUP_SCALING`, `GROUP_CFACTOR` bleiben *immer* geschützt, auch wenn `allow_changes=True` gesetzt wird), weil das Gerät „metrologisch eingemessen“ ist und diese Werte nicht versehentlich verändert werden dürfen. Enthält außerdem `InputSnapshot` zum vollständigen Erfassen/Vergleichen/Wiederherstellen der Konfiguration.

## Die Ablaufskripte (Stufe 2–5) — der eigentliche Programmablauf

Die `stageN_*.py`-Dateien sind ausführbare Testskripte, offensichtlich in aufsteigender Ausbaustufe entstanden (Stufe 1 fehlt im Ordner, wird aber in Kommentaren referenziert). Jedes folgt demselben Muster: Logging einrichten → Verbindung öffnen → Vorbedingungen prüfen (`:COMMunicate:HEADer?` muss 0 sein, `:NUMeric:FORMat?` muss FLOat sein) → Ist-Zustand sichern → eigentliche Aktion → Prüfen auf Gerätefehler → im `finally`-Block *garantiert* wiederherstellen und die Wiederherstellung erneut verifizieren.

- **`stage2_read_numeric.py`** — reine Leseübung: sichert die *vorhandene* Item-Tabelle, liest sie mehrfach aus, verändert nichts. Dient als risikoarmer erster Schritt / Rauchtest.
- **`stage3_own_itemtable.py`** — schreibt erstmals eine *eigene* Item-Tabelle (33 fest definierte Kanäle passend zur vorgefundenen Verdrahtung), verifiziert sie, liest Messwerte, stellt danach exakt den Ausgangszustand wieder her.
- **`stage4_measure.py`** — baut auf Stufe 3 auf und ergänzt eine echte, getaktete Messschleife mit CSV-Aufzeichnung, Metadaten-Sidecar und HOLD-Snapshot; konfigurierbar über Konstanten am Dateianfang (Abtastintervall, Sample-/Zeitlimit, CSV-Trenner, Ausgabeverzeichnis).
- **`stage5_input_config.py`** — bewusst der "sichere" Sonderfall: öffnet die Session mit `read_only=True` *und* `InputConfig(allow_changes=False)` (doppelte Sperre), liest nur die komplette Eingangskonfiguration aus, speichert sie als JSON-Snapshot und macht eine Gegenprobe (laden → erneut erfassen → vergleichen), um zu belegen, dass Serialisierung verlustfrei ist. Schreibt nichts am Gerät.

Man sieht daran eine klare Entwicklungslinie: von "nur lesen" (Stufe 2) über "kontrolliert schreiben mit Rückbau" (Stufe 3) zu "produktiver Messbetrieb" (Stufe 4) und schließlich "kritische Konfiguration dokumentieren, ohne sie anzufassen" (Stufe 5).

## Durchgängige Entwurfsprinzipien (fallen beim Lesen auf)

Sicherung vor jedem Schreibzugriff, Wiederherstellung im `finally`, danach erneute Verifikation — dieses Muster zieht sich durch alle vier Ablaufskripte. Schreibzugriffe sind grundsätzlich sparsam und geprüft: einzelnes Test-Item vor der vollen Tabelle, Rücklesen und Feldvergleich nach jedem größeren Schreibvorgang, Fehlerqueue-Kontrolle nach kritischen Aktionen. Konfigurierbare Parameter (IP, Timeouts, Abtastintervall, Ausgabeordner …) stehen als benannte Konstanten am jeweiligen Dateianfang, nicht verstreut im Code. Kommentare markieren offene Fragen konsequent mit „ZU VERIFIZIEREN" (z. B. Zeiteinheit von `TmcSetTimeout`, Verhalten bei verketteten Kommandos) — hilfreich, um zu sehen, was bereits am realen Gerät bestätigt wurde und was noch Annahme ist.

## Kurz zusammengefasst

Ein sauber geschichteter SCPI/TMCTL-Treiber für das WT3000, der über vier Testskripte in wachsender Ausbaustufe demonstriert: Grundkommunikation, Konfiguration eigener Messkanäle, dauerhafte Messwertaufzeichnung nach CSV und rückstandsfreies Auslesen der kritischen Eingangskonfiguration. Der Fokus liegt durchgehend auf Nichtveränderung des eingemessenen Geräts – Lesen und temporäres Schreiben sind fast immer abgesichert durch Backup → Aktion → Restore → Verifikation.
