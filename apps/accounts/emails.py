from django.conf import settings
from django.core.mail import send_mail

`
def send_invitation_email(invitation):
    """
    Deliver the accept link to the invited address. The token travels in the URL,
    so this is the channel that actually gets it to the person -- the API response
    is for the admin, not the invitee.
    """
    accept_link = f'{settings.INVITATION_ACCEPT_URL}?token={invitation.token}'
    send_mail(
        subject=f'You have been invited to {invitation.tenant.name}',
        message=(
            f'You have been invited to join {invitation.tenant.name} on Amandu.\n\n'
            f'Accept the invitation and set your password here:\n{accept_link}\n\n'
            f'This link expires on {invitation.expires_at:%Y-%m-%d}.'
        ),
        # None falls back to DEFAULT_FROM_EMAIL.
        from_email=None,
        recipient_list=[invitation.email],
    )
