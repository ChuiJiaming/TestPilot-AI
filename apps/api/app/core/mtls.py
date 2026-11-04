"""mTLS client contexts for internal gateway calls.

httpx 0.28 no longer honours a request-level ``cert=`` argument, and on the
client it is silently ignored, which surfaces at the TLS layer as
``alert certificate required`` — the client certificate is simply never sent.
Building an explicit ``ssl.SSLContext`` works identically on 0.27 and 0.28, so
the gateway clients stay version-independent.
"""

import ssl


def client_ssl_context(cert_file: str, key_file: str, ca_file: str) -> ssl.SSLContext:
    """Trust only the configured CA and present the client certificate."""
    context = ssl.create_default_context(cafile=ca_file)
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context
