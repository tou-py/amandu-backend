from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Membership
from apps.commons import images, storage
from apps.scheduling.models import Service
from apps.scheduling.permissions import ManagesService
from apps.tenancy.permissions import HasActiveMembership, IsTenantOwner

# Per kind: the target in the active tenant (a 404 when it is not there), and
# whether the caller may edit it, by the very rule that guards that edit. The
# size cap is images.CAPS, which KeyField reads too.
KINDS = {
    'logo': (
        lambda request, pk: request.tenant,
        lambda request, view, tenant: IsTenantOwner().has_permission(request, view),
    ),
    'photo': (
        lambda request, pk: get_object_or_404(Membership.objects.filter(tenant=request.tenant), pk=pk),
        lambda request, view, member: request.membership.may_change_photo(member),
    ),
    'service': (
        lambda request, pk: get_object_or_404(Service.objects.for_tenant(request.tenant), pk=pk),
        lambda request, view, service: ManagesService().has_object_permission(request, view, service),
    ),
}


class UploadRequestSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=sorted(KINDS))
    # Ignored for a logo: its target is the active tenant.
    target_id = serializers.CharField(required=False, allow_blank=True)
    size = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        if attrs['kind'] != 'logo' and not attrs.get('target_id'):
            raise serializers.ValidationError({'target_id': 'This field is required.'}, code='required')
        return attrs


class UploadSerializer(serializers.Serializer):
    key = serializers.CharField()
    upload_url = serializers.URLField()
    headers = serializers.DictField(child=serializers.CharField())


class UploadView(APIView):
    """
    Signs one browser upload straight to the bucket; the bytes never come here.
    The key it returns is then attached through the owner's own endpoint
    (PATCH tenant / member / service), which is where the upload is checked.

    Who may sign is who may edit the target, by the very rule that guards that
    edit: IsTenantOwner for the logo (TenantView), Membership.may_change_photo
    for a photo, ManagesService for a service (KINDS). Checked in the order
    every other endpoint answers: a target in another tenant is a 404 before
    anything else, then the 403, and only then is the size looked at.

    ponytail: a URL signed and never attached leaves an orphan object, never
    swept. Add a sweep (list the prefix, diff against the key columns) only if
    storage cost ever shows up.
    """

    permission_classes = (IsAuthenticated, HasActiveMembership)

    @extend_schema(request=UploadRequestSerializer, responses=UploadSerializer)
    def post(self, request):
        data = UploadRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        kind, size = data.validated_data['kind'], data.validated_data['size']
        find, may_edit = KINDS[kind]
        if not may_edit(request, self, find(request, data.validated_data.get('target_id'))):
            raise PermissionDenied('Your role does not allow changing this image.')
        if size > images.CAPS[kind]:
            raise serializers.ValidationError({'size': 'Over the size limit for this image.'}, code='image_too_large')
        key = images.new_key(request.tenant, kind)
        url, headers = storage.presign_put(key, size)
        return Response({'key': key, 'upload_url': url, 'headers': headers})
