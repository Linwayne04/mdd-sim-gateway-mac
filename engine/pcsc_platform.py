"""Platform boundary for PC/SC smart-card access (engine side).

Mirror of control/app/platform/pcsc.py — see docs/macos-port/PLAN.md §5.
The engine scripts (pin_keeper / ami_usim / swu_ike) connect to the USIM through
pyscard with the default T0|T1 mask, which is correct on Linux pcsc-lite. On
macOS, CryptoTokenKit's CCID stack negotiates T=1 but then fails every APDU with
SCARD_E_CARD_UNSUPPORTED (0x80100016) on some readers (observed with a Generic
USB2.0-CRW); T=0 works. Branch here so the scripts stay free of sys.platform
checks.
"""
import sys

from smartcard.CardConnection import CardConnection


def connect(conn):
    """Connect a pyscard CardConnection with the platform-preferred protocol.

    macOS: prefer T=0, fall back to the default T0|T1 mask only if the card
    refuses T=0 outright. Everywhere else: pyscard's default, as before.
    """
    if sys.platform != "darwin":
        conn.connect()
        return conn
    try:
        conn.connect(protocol=CardConnection.T0_protocol)
    except Exception:
        conn.connect()
    return conn
