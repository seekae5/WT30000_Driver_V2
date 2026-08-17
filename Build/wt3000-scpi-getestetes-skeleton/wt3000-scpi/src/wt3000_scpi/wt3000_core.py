# =============================================================================
# Datei: wt3000_core.py
# Layer 0 (Transport) + Layer 1 (Session/Plumbing)
# Unveraendert gegenueber Stufe 1, ergaenzt um: write(), query_block(),
# Fehlerqueue-Pruefung, read_only als echter Schalter.
# =============================================================================

from __future__ import annotations

import ctypes as ct
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

# TMCTL-Konstante fuer Ethernet-Transport (aus tmctl.h)
TM_CTL_ETHER: int = 4

# Maximale Laenge einer Programmnachricht inkl. Terminator (Handbuch Kap. 5).
MAX_PROGRAM_MESSAGE_BYTES: int = 1024


@dataclass(frozen=True)
class WTConfig:
    """Verbindungs- und Laufzeitparameter. Hier zentral anpassen."""

    dll_path: str = r"C:\Users\Persystems\PycharmProjects\WT3000_SCPI\tmctl8020\dll\tmctl64.dll"
    ip: str = "192.168.10.20"
    user: str = "TEST"
    password: str = "1"
    # ZU VERIFIZIEREN: Einheit von TmcSetTimeout (ms angenommen).
    timeout_ms: int = 5000
    drain_timeout_ms: int = 500
    read_buffer_size: int = 64 * 1024
    # ZU VERIFIZIEREN: Ob das Geraet Set-Kommandos ueber Ethernet auch ohne
    # ':COMMunicate:REMote ON' annimmt. Falls Schreibzugriffe abgelehnt werden,
    # hier auf True setzen. Wird dann beim Beenden zwingend wieder abgeschaltet.
    use_remote: bool = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WTError(Exception):
    """Basisklasse fuer alle Fehler dieses Treibers."""


class TmctlError(WTError):
    """Eine TMCTL-Funktion hat einen Fehlercode != 0 zurueckgegeben."""

    def __init__(self, function: str, code: int, detail: str = "") -> None:
        self.function = function
        self.code = code
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"{function} fehlgeschlagen, TMCTL-Fehlercode 0x{code:08X}{suffix}")


class ProtocolError(WTError):
    """Verstoss gegen die Protokollregeln aus Kapitel 5 des Handbuchs."""


class DeviceError(WTError):
    """Das Geraet hat einen Eintrag in die Fehlerqueue gelegt."""


class ReadOnlyViolation(WTError):
    """In einer Nur-Lesen-Session wurde ein schreibendes Kommando versucht."""


# ---------------------------------------------------------------------------
# Layer 0 - Transport
# ---------------------------------------------------------------------------


class TmctlTransport:
    """Transportschicht ueber die Yokogawa-TMCTL-DLL.

    Kennt keinerlei WT3000-Kommandos, nur write/read/query/close.
    """

    def __init__(self, config: WTConfig) -> None:
        self._log = logging.getLogger("wt3000.transport")
        self._config = config
        self._device_id = ct.c_int(0)
        self._open = False

        dll_path = Path(config.dll_path)
        if not dll_path.is_file():
            raise WTError(f"TMCTL-DLL nicht gefunden: {dll_path}")

        # Abhaengige DLLs liegen ueblicherweise im selben Verzeichnis.
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(dll_path.parent))

        self._tm = ct.WinDLL(str(dll_path))
        self._declare_prototypes()
        self._initialize()

    # -- Setup --------------------------------------------------------------

    def _declare_prototypes(self) -> None:
        """Signaturen der genutzten TMCTL-Funktionen festlegen."""
        tm = self._tm
        tm.TmcInitialize.argtypes = [ct.c_int, ct.c_char_p, ct.POINTER(ct.c_int)]
        tm.TmcInitialize.restype = ct.c_int
        tm.TmcSend.argtypes = [ct.c_int, ct.c_char_p]
        tm.TmcSend.restype = ct.c_int
        tm.TmcReceive.argtypes = [ct.c_int, ct.c_char_p, ct.c_int, ct.POINTER(ct.c_int)]
        tm.TmcReceive.restype = ct.c_int
        tm.TmcSetTimeout.argtypes = [ct.c_int, ct.c_int]
        tm.TmcSetTimeout.restype = ct.c_int
        tm.TmcFinish.argtypes = [ct.c_int]
        tm.TmcFinish.restype = ct.c_int

    def _initialize(self) -> None:
        """Verbindung aufbauen. Adressstring hat das Format 'ip,user,password'."""
        cfg = self._config
        address = f"{cfg.ip},{cfg.user},{cfg.password}".encode("ascii")
        self._check(
            self._tm.TmcInitialize(TM_CTL_ETHER, address, ct.byref(self._device_id)),
            "TmcInitialize",
            f"Adresse={cfg.ip}",
        )
        self._open = True
        self._log.info("Verbindung aufgebaut, Device-ID %d", self._device_id.value)
        self.set_timeout(cfg.timeout_ms)

    @staticmethod
    def _check(rc: int, function: str, detail: str = "") -> None:
        """Jeden TMCTL-Rueckgabewert pruefen, Fehlercode hexadezimal melden."""
        if rc != 0:
            raise TmctlError(function, rc, detail)

    # -- Basisoperationen ---------------------------------------------------

    def set_timeout(self, timeout_ms: int) -> None:
        """Kommunikationstimeout setzen (Einheit ZU VERIFIZIEREN)."""
        self._check(self._tm.TmcSetTimeout(self._device_id, timeout_ms), "TmcSetTimeout")

    def write(self, command: str) -> None:
        """Programmnachricht senden.

        Es wird bewusst KEIN Terminator angehaengt: TMCTL setzt ihn selbst
        (verifiziert mit '*IDN?').
        ZU VERIFIZIEREN: Verhalten bei mit ';' verketteten Kommandos.
        """
        payload = command.encode("ascii")
        if len(payload) + 1 > MAX_PROGRAM_MESSAGE_BYTES:
            raise ProtocolError(
                f"Programmnachricht zu lang ({len(payload)} Bytes), "
                f"Limit inkl. Terminator {MAX_PROGRAM_MESSAGE_BYTES} Bytes"
            )
        self._log.debug("TX: %r", command)
        self._check(self._tm.TmcSend(self._device_id, payload), "TmcSend", command)

    def read(self) -> bytes:
        """Einen Lesevorgang ausfuehren und die Rohbytes zurueckgeben."""
        size = self._config.read_buffer_size
        buffer = ct.create_string_buffer(size)
        received = ct.c_int(0)
        self._check(
            self._tm.TmcReceive(self._device_id, buffer, size, ct.byref(received)),
            "TmcReceive",
        )
        data = buffer.raw[: received.value]
        if received.value >= size:
            self._log.warning(
                "Lesepuffer (%d Bytes) vollstaendig gefuellt - Antwort evtl. unvollstaendig", size
            )
        self._log.debug("RX: %d Bytes", len(data))
        return data

    def query(self, command: str) -> bytes:
        """Query senden und einen Lesevorgang ausfuehren."""
        self.write(command)
        return self.read()

    def close(self) -> None:
        """Verbindung schliessen. Mehrfachaufruf ist unschaedlich."""
        if not self._open:
            return
        self._open = False
        rc = self._tm.TmcFinish(self._device_id)
        if rc != 0:
            self._log.warning("TmcFinish meldete Fehlercode 0x%08X", rc)
        else:
            self._log.info("Verbindung geschlossen")

    # -- Context Manager ----------------------------------------------------

    def __enter__(self) -> "TmctlTransport":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Layer 1 - Session / Plumbing
# ---------------------------------------------------------------------------


class WTSession:
    """Protokollschicht: Query-Regeln, Blockdaten, Fehlerqueue."""

    def __init__(self, transport: TmctlTransport, config: WTConfig, read_only: bool = False) -> None:
        self._log = logging.getLogger("wt3000.session")
        self._transport = transport
        self._config = config
        self._read_only = read_only
        self._remote_active = False

    # -- Fernsteuerung ------------------------------------------------------

    def enable_remote(self) -> None:
        """Fernsteuerung einschalten (REMOTE-LED an, Tasten ausser LOCAL gesperrt)."""
        self.write(":COMMunicate:REMote ON")
        self._remote_active = True
        self._log.info("Fernsteuerung eingeschaltet")

    def disable_remote(self) -> None:
        """Fernsteuerung abschalten. Gibt das Bedienfeld frei."""
        if not self._remote_active:
            return
        try:
            self.write(":COMMunicate:REMote OFF")
            self._log.info("Fernsteuerung abgeschaltet")
        except WTError as exc:
            self._log.warning("REMote OFF fehlgeschlagen: %s", exc)
        finally:
            self._remote_active = False

    # -- Kern ---------------------------------------------------------------

    def write(self, command: str) -> None:
        """Set-Kommando senden (kein Query)."""
        self._validate(command, expect_query=False)
        self._transport.write(command)

    def query(self, command: str) -> str:
        """Genau einen Query absetzen und die Antwort als Text zurueckgeben."""
        self._validate(command, expect_query=True)
        return self.decode(self._transport.query(command))

    def query_raw(self, command: str) -> bytes:
        """Wie query(), liefert aber die unveraenderten Rohbytes."""
        self._validate(command, expect_query=True)
        return self._transport.query(command)

    def query_block(self, command: str) -> bytes:
        """Query absetzen, dessen Antwort ein <Block data> mit '#n'-Header ist.

        Liest so lange nach, bis die im Header angekuendigte Nutzlast
        vollstaendig vorliegt. Damit ist es egal, ob TmcReceive den Block in
        einem Stueck liefert oder an einem 0x0A-Byte innerhalb der Binaerdaten
        abbricht (ZU VERIFIZIEREN, welches Verhalten tatsaechlich vorliegt).
        """
        raw = self.query_raw(command)
        return self._assemble_block(raw)

    def _assemble_block(self, raw: bytes) -> bytes:
        """'#4NNNN<daten>' auswerten und die Nutzlast vollstaendig einsammeln."""
        if not raw.startswith(b"#"):
            raise ProtocolError(
                f"Kein Blockheader in der Antwort (erste Bytes: {raw[:16]!r}). "
                "Steht :NUMeric:FORMat wirklich auf FLOat?"
            )
        try:
            digit_count = int(raw[1:2])
        except ValueError as exc:
            raise ProtocolError(f"Ungueltiger Blockheader: {raw[:8]!r}") from exc
        if digit_count == 0:
            raise ProtocolError("Block mit unbestimmter Laenge ('#0') wird nicht unterstuetzt")

        header_length = 2 + digit_count
        payload_length = int(raw[2:header_length])
        payload = raw[header_length:]

        reads = 1
        while len(payload) < payload_length:
            payload += self._transport.read()
            reads += 1
            if reads > 64:
                raise ProtocolError("Blockdaten nach 64 Lesevorgaengen immer noch unvollstaendig")
        if reads > 1:
            self._log.info("Blockdaten in %d Lesevorgaengen zusammengesetzt", reads)

        return payload[:payload_length]

    def _validate(self, command: str, expect_query: bool) -> None:
        """Protokollregeln pruefen, bevor die Nachricht das Geraet erreicht."""
        stripped = command.strip()
        is_query = stripped.endswith("?")

        if self._read_only and not is_query:
            raise ReadOnlyViolation(
                f"Nur-Lesen-Session: '{command}' ist kein Query und wird nicht gesendet"
            )
        if expect_query and not is_query:
            raise ProtocolError(f"'{command}' ist kein Query, wurde aber als solcher aufgerufen")
        if not expect_query and is_query:
            raise ProtocolError(f"'{command}' ist ein Query, wurde aber als write() aufgerufen")
        # Handbuch Kap. 5: genau ein Query pro Programmnachricht.
        if stripped.count("?") > 1:
            raise ProtocolError(f"Mehr als ein Query in einer Nachricht: '{command}'")

    @staticmethod
    def decode(raw: bytes) -> str:
        """Rohbytes in Text wandeln und Terminator entfernen."""
        return raw.decode("ascii", errors="replace").strip("\r\n\0 ")

    # -- Fehlerqueue --------------------------------------------------------

    def drain_after_failure(self) -> None:
        """Nach einem fehlgeschlagenen Query eine verspaetete Antwort abraeumen."""
        try:
            self._transport.set_timeout(self._config.drain_timeout_ms)
            leftover = self._transport.read()
            if leftover:
                self._log.warning("Nachlaufende Antwort verworfen: %r", leftover[:80])
        except TmctlError:
            pass  # Erwarteter Fall: nichts mehr da.
        finally:
            self._transport.set_timeout(self._config.timeout_ms)

    def read_error_queue(self, max_entries: int = 20) -> list[str]:
        """Fehlerqueue leeren. Hinweis: :STATus:ERRor? entfernt den Eintrag."""
        entries: list[str] = []
        for _ in range(max_entries):
            answer = self.query(":STATus:ERRor?")
            entries.append(answer)
            if answer.split(",", 1)[0].strip().lstrip("+") == "0":
                break
        return entries

    def assert_no_error(self, context: str) -> None:
        """Fehlerqueue pruefen und bei Eintraegen eine DeviceError werfen."""
        entries = self.read_error_queue()
        problems = [e for e in entries if e.split(",", 1)[0].strip().lstrip("+") != "0"]
        if problems:
            raise DeviceError(f"Geraetefehler nach '{context}': {problems}")