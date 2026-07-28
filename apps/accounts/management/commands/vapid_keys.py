"""
Print a fresh VAPID key pair in the exact form the settings expect.

Run once per deployment, paste the two values into the environment, and never
run it again for that deployment. RFC 8292: "Application servers need to
remember the key that was used when requesting the creation of a subscription" --
every browser stored the public half when it subscribed, so a new pair silently
orphans all of them and each device has to opt in again.

Encoding is the whole reason this exists rather than py-vapid's own CLI: the
private key travels as base64url DER (what pywebpush accepts as a string) and
the public key as the base64url raw point (what the browser accepts as
applicationServerKey). Getting those two mixed up produces a pair that looks
fine and fails at the first send.
"""
from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid02, b64urlencode


class Command(BaseCommand):
    help = 'Generate a VAPID key pair for Web Push. Run once, then store in the environment.'

    def handle(self, *args, **options):
        vapid = Vapid02()
        vapid.generate_keys()

        private_der = vapid.private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        # Uncompressed point (0x04 || X || Y) -- the only public key encoding
        # pushManager.subscribe() takes.
        public_raw = vapid.public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )

        self.stdout.write(f'VAPID_PRIVATE_KEY={b64urlencode(private_der)}')
        self.stdout.write(f'VAPID_PUBLIC_KEY={b64urlencode(public_raw)}')
        self.stdout.write('VAPID_SUBJECT=mailto:you@example.com')
        self.stdout.write(
            self.style.WARNING(
                '\nStore these once. Regenerating invalidates every existing '
                'subscription and every device has to opt in again.'
            )
        )
