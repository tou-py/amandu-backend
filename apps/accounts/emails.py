from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts import signup


def _send(*, to, subject, text, template, context):
    send_mail(
        subject=subject,
        message=text,
        html_message=render_to_string(f'emails/{template}.html', context),
        from_email=None,
        recipient_list=[to],
    )


def send_verification_email(user):
    tenant = user.tenants.first()
    link = f'{settings.APP_URL}/verificar?token={signup.verify_token(user)}'
    _send(
        to=user.email,
        subject='Confirmá tu correo para entrar a Kyo',
        text=f'Tocá este enlace para entrar a {tenant.name} en Kyo:\n{link}\n\nVence en 3 días.',
        template='verify',
        context={'tenant_name': tenant.name, 'link': link},
    )


def send_already_registered_email(user):
    """What a signup with a taken address gets instead of a refusal, so the
    answer never tells a stranger who has an account."""
    login_link = f'{settings.APP_URL}/login'
    reset_link = f'{settings.APP_URL}/recuperar'
    _send(
        to=user.email,
        subject='Ya tenés una cuenta en Kyo',
        text=f'Entrá con tu contraseña:\n{login_link}\n\n¿La olvidaste? Elegí una nueva:\n{reset_link}',
        template='already_registered',
        context={'login_link': login_link, 'reset_link': reset_link},
    )


def send_password_reset_email(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    link = f'{settings.APP_URL}/restablecer?uid={uid}&token={default_token_generator.make_token(user)}'
    _send(
        to=user.email,
        subject='Elegí una contraseña nueva para Kyo',
        text=f'Elegí tu contraseña nueva acá:\n{link}\n\nSi no lo pediste, ignorá este correo.',
        template='password_reset',
        context={'link': link},
    )


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
