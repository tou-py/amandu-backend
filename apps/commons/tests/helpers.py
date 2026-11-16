from rest_framework.test import APIClient


def api(user=None, tenant=None):
    """A client as the frontend calls: logged in as `user`, acting for `tenant`."""
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http
