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
from apps.tenancy.permissions import HasActiveMembership


class UploadRequestSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=sorted(images.CAPS))
    # Ignored for a logo: its target is the active tenant.
    target_id = serializers.CharField(required=False, allow_blank=True)
    size = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        if attrs['size'] > images.CAPS[attrs['kind']]:
            raise serializers.ValidationError({'size': 'Over the size limit for this image.'}, code='image_too_large')
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
    edit: the owner for the logo (TenantView), the member themselves or whoever
    may_manage them for a photo (MemberSerializer), ManagesService for a
    service. A target in another tenant is a 404, like everywhere else.

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
        if not self._may_edit(request, kind, data.validated_data.get('target_id')):
            raise PermissionDenied('Your role does not allow changing this image.')
        key = images.new_key(request.tenant, kind)
        url, headers = storage.presign_put(key, size)
        return Response({'key': key, 'upload_url': url, 'headers': headers})

    def _may_edit(self, request, kind, target_id):
        me = request.membership
        if kind == 'logo':
            return me.role == Membership.Role.OWNER
        if kind == 'photo':
            target = get_object_or_404(Membership.objects.filter(tenant=request.tenant), pk=target_id)
            return target.pk == me.pk or me.may_manage(target)
        service = get_object_or_404(Service.objects.for_tenant(request.tenant), pk=target_id)
        return ManagesService().has_object_permission(request, self, service)
