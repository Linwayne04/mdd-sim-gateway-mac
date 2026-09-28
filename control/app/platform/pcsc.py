"""Platform boundary for PC/SC smart-card access.

The rest of the app talks to cards through pyscard's CardConnection. The only
platform-specific behavior needed so far is transport-protocol selection at
connect time, which lives here so existing modules stay free of sys.platform
branches (see docs/macos-port/PLAN.md §5).
"""
import sys

from smartcard.CardConnection import CardConnection


def connect(conn):
    """Connect a pyscard CardConnection with the platform-preferred protocol.

    macOS: CryptoTokenKit's CCID stack negotiates T=1 but then fails every APDU
    with SCARD_E_CARD_UNSUPPORTED (0x80100016) on some readers (observed with a
    Generic USB2.0-CRW); T=0 works. T=0 is universally implemented, so prefer it
    on darwin and fall back to pyscard's default T0|T1 mask only if the card
    refuses T=0 outright.

    Everywhere else: pyscard's default (T0|T1 bitmask, driver picks) is correct
    on Linux pcsc-lite.
    """
    if sys.platform != "darwin":
        conn.connect()
        return conn
    try:
        conn.connect(protocol=CardConnection.T0_protocol)
    except Exception:
        conn.connect()
    return conn
