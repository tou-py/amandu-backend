from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string


def send_invitation_email(invitation):
    """
    Deliver the accept link to the invited address. The token travels in the URL,
    so this is the channel that actually gets it to the person -- the API response
    is for the admin, not the invitee.
    """
    accept_link = f'{settings.INVITATION_ACCEPT_URL}?token={invitation.token}'
    context = {
        'tenant_name': invitation.tenant.name,
        'accept_link': accept_link,
        'expires_at': invitation.expires_at,
    }
    send_mail(
        subject=f'Te invitaron a {invitation.tenant.name}',
        message=(
            f'Te invitaron a sumarte a {invitation.tenant.name} en Kyo.\n\n'
            f'Aceptá la invitación y elegí tu contraseña acá:\n{accept_link}\n\n'
            f'El enlace vence el {invitation.expires_at:%d/%m/%Y}.'
        ),
        # The plain body stays as the fallback for text-only clients and spam scoring.
        html_message=render_to_string('emails/invitation.html', context),
        # None falls back to DEFAULT_FROM_EMAIL.
        from_email=None,
        recipient_list=[invitation.email],
    )
