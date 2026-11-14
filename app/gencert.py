"""Generate a self-signed TLS certificate for the HTTPS web interface.

    python -m app.gencert                 # a certificate for all control-PC addresses
    python -m app.gencert --host 10.0.0.10 --host restart.local

Creates two files in the certs/ folder:
    certs/server.key  — the private key (secret, keep it like a password)
    certs/server.crt  — the certificate (add it to the trusted store on engineers' machines)

The certificate is self-signed: encryption works at full strength, but on the first visit the
browser shows an "untrusted certificate" warning — that is normal for a local network.
To remove the warning, import server.crt into the trusted root store on engineers' machines.
Validity — 5 years.
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import socket
from pathlib import Path


def _local_ips() -> list[str]:
    ips = set()
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith("127."):
                    ips.add(a.address)
    except Exception:
        pass
    return sorted(ips)


def generate(hosts: list[str], out_dir: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    out_dir.mkdir(parents=True, exist_ok=True)
    key_path = out_dir / "server.key"
    crt_path = out_dir / "server.crt"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # list of names/addresses the certificate is valid for
    san: list[x509.GeneralName] = [x509.DNSName("localhost")]
    san.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    seen = {"localhost", "127.0.0.1"}
    for h in hosts + _local_ips() + [socket.gethostname()]:
        if not h or h in seen:
            continue
        seen.add(h)
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            san.append(x509.DNSName(h))

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "restart-orchestrator")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365 * 5))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        import os
        if os.name == "posix":
            os.chmod(key_path, 0o600)
    except Exception:
        pass
    return key_path, crt_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Self-signed TLS certificate for HTTPS")
    parser.add_argument("--host", action="append", default=[], help="additional IP or name (can be repeated)")
    parser.add_argument("--out", default="certs", help="output folder for the certificate (default certs/)")
    args = parser.parse_args()
    key, crt = generate(args.host, Path(args.out))
    print(f"Certificate created:\n  key:          {key}\n  certificate:  {crt}")
    print("\nRun with HTTPS:")
    print(f"  uvicorn app.main:app --host 0.0.0.0 --port 8443 "
          f"--ssl-keyfile {key} --ssl-certfile {crt} --workers 1")
    print("\nOpen:  https://<control-PC-address>:8443")
    print("The browser will show an untrusted-certificate warning — expected for self-signed.")
    print(f"To remove the warning, import {crt} into the trusted root store on engineers' machines.")


if __name__ == "__main__":
    main()
