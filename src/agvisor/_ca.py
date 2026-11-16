"""inject=: a throwaway certificate authority, made on this machine for one sandbox.

The sandbox is given the CA's certificate to trust. The egress proxy is given a certificate for the injected hosts,
signed by that CA, and its key. The CA's own private key is not stored anywhere: it signs once and is dropped, so
nothing can ever be signed with it again."""
import datetime

_DAYS = 30  # a sandbox that outlives this must be made again to get new certificates


def make(hosts):
    """(ca_pem, proxy_pem): the CA certificate for the sandbox; leaf certificate + key for the proxy, valid for `hosts`."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise RuntimeError("inject= needs the cryptography package: pip install 'agvisor[inject]'") from None

    now = datetime.datetime.now(datetime.timezone.utc)
    window = dict(not_valid_before=now - datetime.timedelta(minutes=5), not_valid_after=now + datetime.timedelta(days=_DAYS))

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def build(subject, issuer, key, signer, extensions):
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(window["not_valid_before"])
             .not_valid_after(window["not_valid_after"]))
        for ext, critical in extensions:
            b = b.add_extension(ext, critical)
        return b.sign(signer, hashes.SHA256())

    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca = build(name("agvisor egress CA"), name("agvisor egress CA"), ca_key, ca_key, [
        (x509.BasicConstraints(ca=True, path_length=0), True),
        (x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=False, content_commitment=False, key_encipherment=False,
                       data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False), True),
    ])
    leaf = build(name(hosts[0]), ca.subject, leaf_key, ca_key, [
        (x509.BasicConstraints(ca=False, path_length=None), True),
        (x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), False),
        (x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False),
    ])
    pem = serialization.Encoding.PEM
    key_pem = leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return ca.public_bytes(pem).decode(), leaf.public_bytes(pem).decode() + key_pem.decode()
