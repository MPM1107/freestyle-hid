# SPDX-FileCopyrightText: © 2013 The freestyle-hid Authors
# SPDX-License-Identifier: Apache-2.0

import csv
import logging
import pathlib
import random
import re
from typing import AnyStr, Callable, Iterator, Optional, Sequence, Tuple

import construct

from ._exceptions import ChecksumError, CommandError
from ._hidwrapper import HidWrapper
from ._freestyle_encryption import SpeckEncrypt, SpeckCMAC

ABBOTT_VENDOR_ID = 0x1A61

_AUTH_ENC_MASTER_KEY = 0xdeadbeef
_AUTH_MAC_MASTER_KEY = 0xdeadbeef
_SESS_ENC_MASTER_KEY = 0xdeadbeef
_SESS_MAC_MASTER_KEY = 0xdeadbeef

_INIT_COMMAND = 0x01
_INIT_RESPONSE = 0x71

_KEEPALIVE_RESPONSE = 0x22
_UNKNOWN_MESSAGE_RESPONSE = 0x30

_ENCRYPTION_SETUP_COMMAND = 0x14
_ENCRYPTION_SETUP_RESPONSE = 0x33

_ALWAYS_UNENCRYPTED_MESSAGES = (
    _INIT_COMMAND,
    0x04,
    0x05,
    0x06,
    0x0C,
    0x0D,
    _ENCRYPTION_SETUP_COMMAND,
    0x15,
    _ENCRYPTION_SETUP_RESPONSE,
    0x34,
    0x35,
    _INIT_RESPONSE,
    _KEEPALIVE_RESPONSE,
)


def _create_matcher(
    message_type: int, content: Optional[bytes]
) -> Callable[[Tuple[int, bytes]], bool]:
    def _matcher(message: Tuple[int, bytes]) -> bool:
        return message[0] == message_type and (content is None or content == message[1])

    return _matcher


_is_init_reply = _create_matcher(_INIT_RESPONSE, b"\x01")
_is_keepalive_response = _create_matcher(_KEEPALIVE_RESPONSE, None)
_is_unknown_message_error = _create_matcher(_UNKNOWN_MESSAGE_RESPONSE, b"\x85")
_is_encryption_missing_error = _create_matcher(_ENCRYPTION_SETUP_RESPONSE, b"\x15")
_is_encryption_setup_error = _create_matcher(_ENCRYPTION_SETUP_RESPONSE, b"\x14")

_FREESTYLE_MESSAGE = construct.Struct(
    hid_report=construct.Const(0, construct.Byte),
    message_type=construct.Byte,
    command=construct.Padded(
        63,  # command can only be up to 62 bytes, but one is used for length.
        construct.Prefixed(construct.Byte, construct.GreedyBytes),
    ),
)

_TEXT_COMPLETION_RE = re.compile(b"CMD (?:OK|Fail!)")
_TEXT_REPLY_FORMAT = re.compile(
    b"^(?P<message>.*)CKSM:(?P<checksum>[0-9A-F]{8})\r\n"
    b"CMD (?P<status>OK|Fail!)\r\n$",
    re.DOTALL,
)

_MULTIRECORDS_FORMAT = re.compile(
    b"^(?P<message>.+\r\n)(?P<count>[0-9]+),(?P<checksum>[0-9A-F]{8})\r\n$", re.DOTALL
)


def _verify_checksum(message: AnyStr, expected_checksum_hex: AnyStr) -> None:
    """Calculate the simple checksum of the message and compare with expected.

    Args:
      message: (str) message to calculate the checksum of.
      expected_checksum_hex: hexadecimal string representing the checksum
        expected to match the message.

    Raises:
      InvalidChecksum: if the message checksum calculated does not match the one
        received.
    """
    expected_checksum = int(expected_checksum_hex, 16)
    if isinstance(message, bytes):
        all_bytes = (c for c in message)
    else:
        all_bytes = (ord(c) for c in message)

    calculated_checksum = sum(all_bytes)

    if expected_checksum != calculated_checksum:
        raise ChecksumError(
            f"Invalid checksum, expected {expected_checksum}, calculated {calculated_checksum}"
        )


class Session:
    def __init__(
        self,
        product_id: Optional[int],
        device_path: Optional[pathlib.Path],
        text_message_type: int,
        text_reply_message_type: int,
        encoding: str = "ascii",
    ) -> None:
        self._handle = HidWrapper.open(device_path, ABBOTT_VENDOR_ID, product_id)
        self._text_message_type = text_message_type
        self._text_reply_message_type = text_reply_message_type
        self._encoding = encoding
        self._encrypted_protocol = product_id in [0x3950]

    def encryption_handshake(self):
        self.send_command(0x05, b"")
        response = self.read_response()
        assert response[0] == 0x06
        serial = response[1][:13]

        crypt = SpeckCMAC(_AUTH_ENC_MASTER_KEY)
        auth_enc_key = crypt.derive("AuthrEnc".encode(), serial)
        auth_enc = SpeckEncrypt(auth_enc_key)
        crypt = SpeckCMAC(_AUTH_MAC_MASTER_KEY)
        auth_mac_key = crypt.derive("AuthrMAC".encode(), serial)
        auth_mac = SpeckCMAC(auth_mac_key)

        self.send_command(_ENCRYPTION_SETUP_COMMAND, b"\x11")
        response = self.read_response()
        assert response[0] == _ENCRYPTION_SETUP_RESPONSE
        assert response[1][0] == 0x16
        reader_rand = response[1][1:9]
        iv = int.from_bytes(response[1][9:16], 'big', signed=False)
        driver_rand = random.randbytes(8)
        resp_enc = auth_enc.encrypt(iv, reader_rand + driver_rand)
        resp_mac = auth_mac.sign(b"\x14\x1a\x17" + resp_enc + b"\x01")
        resp_mac = int.to_bytes(resp_mac, 8, byteorder='little', signed=False)
        self.send_command(_ENCRYPTION_SETUP_COMMAND, b"\x17" + resp_enc + b"\x01" + resp_mac)
        response = self.read_response()
        assert response[0] == _ENCRYPTION_SETUP_RESPONSE
        assert response[1][0] == 0x18
        mac = auth_mac.sign(b"\x33\x22" + response[1][:24])
        mac = int.to_bytes(mac, 8, byteorder='little', signed=False)
        assert mac == response[1][24:32]
        iv = int.from_bytes(response[1][17:24], 'big', signed=False)
        resp_dec = auth_enc.decrypt(iv, response[1][1:17])
        assert resp_dec[:8] == driver_rand
        assert resp_dec[8:] == reader_rand

        crypt = SpeckCMAC(_SESS_ENC_MASTER_KEY)
        ses_enc_key = crypt.derive("SessnEnc".encode(), serial + reader_rand + driver_rand)
        crypt = SpeckCMAC(_SESS_MAC_MASTER_KEY)
        ses_mac_key = crypt.derive("SessnMAC".encode(), serial + reader_rand + driver_rand)
        self.crypt_enc = SpeckEncrypt(ses_enc_key)
        self.crypt_mac = SpeckCMAC(ses_mac_key)
        #print("HANDSHAKE SUCCESSFUL!")

    def connect(self):
        if self._encrypted_protocol:
            self.encryption_handshake()
        """Open connection to the device, starting the knocking sequence."""
        self.send_command(_INIT_COMMAND, b"")
        response = self.read_response()
        if not _is_init_reply(response):
            raise ConnectionError(
                f"Connection error: unexpected message %{response[0]:02x}:{response[1].hex()}"
            )

    def encrypt_message(self, packet: bytes):
        output = bytearray(packet)
        # 0xFF IV is actually 0, because of some weird padding
        encrypted = self.crypt_enc.encrypt(0xFF, packet[2:57])
        output[2:57] = encrypted
        # Not giving a f**k about the IV counter for now
        output[57:61] = bytes(4)
        mac = self.crypt_mac.sign(output[1:61])
        output[61:65] = int.to_bytes(mac, 8, byteorder='little', signed=False)[4:]
        return bytes(output)

    def decrypt_message(self, packet: bytes):
        output = bytearray(packet)
        mac = self.crypt_mac.sign(packet[:60])
        mac = int.to_bytes(mac, 8, byteorder='little', signed=False)[4:]
        assert mac == packet[60:64]
        iv = int.from_bytes(packet[56:60], 'big', signed=False) << 8
        output[1:56] = self.crypt_enc.decrypt(iv, packet[1:56])
        return bytes(output)

    def send_command(self, message_type: int, command: bytes, encrypted: bool = False):
        """Send a raw command to the device.

        Args:
          message_type: The first byte sent with the report to the device.
          command: The command to send out the device.
        """

        usb_packet = _FREESTYLE_MESSAGE.build(
            {"message_type": message_type, "command": command}
        )

        if self._encrypted_protocol and message_type not in _ALWAYS_UNENCRYPTED_MESSAGES:
            usb_packet = self.encrypt_message(usb_packet)

        logging.debug(f"Sending packet: {usb_packet!r}")
        self._handle.write(usb_packet)

    def read_response(self, encrypted: bool = False) -> Tuple[int, bytes]:
        """Read the response from the device and extracts it."""
        usb_packet = self._handle.read()

        logging.debug(f"Read packet: {usb_packet!r}")

        assert usb_packet
        message_type = usb_packet[0]

        if self._encrypted_protocol and message_type not in _ALWAYS_UNENCRYPTED_MESSAGES:
            usb_packet = self.decrypt_message(usb_packet)

        message_length = usb_packet[1]
        message_end_idx = 2 + message_length
        message_content = usb_packet[2:message_end_idx]

        # hidapi module returns a list of bytes rather than a bytes object.
        message = (message_type, bytes(message_content))

        # There appears to be a stray number of 22 01 xx messages being returned
        # by some devices after commands are sent. These do not appear to have
        # meaning, so ignore them and proceed to the next. These are always sent
        # unencrypted, so we need to inspect them before we decide what the
        # message content is.
        if _is_keepalive_response(message):
            return self.read_response(encrypted=encrypted)

        if _is_unknown_message_error(message):
            raise CommandError("Invalid command")

        if _is_encryption_missing_error(message):
            raise CommandError("Device encryption not initialized.")

        if _is_encryption_setup_error(message):
            raise CommandError("Device encryption initialization failed.")

        return message

    def _send_text_command_raw(self, command: bytes) -> bytes:
        """Send a command to the device that expects a text reply."""
        self.send_command(self._text_message_type, command)

        # Reply can stretch multiple buffers
        full_content = b""
        while True:
            message_type, content = self.read_response()

            logging.debug(
                f"Received message: type {message_type:02x} content {content.hex()}"
            )

            if message_type != self._text_reply_message_type:
                raise CommandError(
                    f"Message type {message_type:02x}: content does not match expectations: {content!r}"
                )

            full_content += content

            if _TEXT_COMPLETION_RE.search(full_content):
                break

        match = _TEXT_REPLY_FORMAT.search(full_content)
        if not match:
            raise CommandError(repr(full_content))

        message = match.group("message")
        _verify_checksum(message, match.group("checksum"))

        if match.group("status") != b"OK":
            raise CommandError(repr(message) or "Command failed")

        return message

    def send_text_command(self, command: bytes) -> str:
        return self._send_text_command_raw(command).decode(self._encoding, "replace")

    def query_multirecord(self, command: bytes) -> Iterator[Sequence[str]]:
        """Queries for, and returns, "multirecords" results.

        Multirecords are used for querying events, readings, history and similar
        other data out of a FreeStyle device. These are comma-separated values,
        variable-length.

        The validation includes the general HID framing parsing, as well as
        validation of the record count, and of the embedded records checksum.

        Args:
          command: The text command to send to the device for the query.

        Returns:
          A CSV reader object that returns a record for each line in the
          reply buffer.
        """
        message = self._send_text_command_raw(command)
        logging.debug(f"Received multi-record message:\n{message!r}")
        if message == b"Log Empty\r\n":
            return iter(())

        match = _MULTIRECORDS_FORMAT.search(message)
        if not match:
            raise CommandError(repr(message))

        records_raw = match.group("message")
        _verify_checksum(records_raw, match.group("checksum"))

        # Decode here with replacement; the software does not deal with UTF-8
        # correctly, and appears to truncate incorrectly the strings.
        records_str = records_raw.decode(self._encoding, "replace")

        logging.debug(f"Received multi-record string: {records_str}")

        return csv.reader(records_str.split("\r\n"))
