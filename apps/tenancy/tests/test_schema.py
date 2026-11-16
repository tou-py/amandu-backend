from drf_spectacular.generators import SchemaGenerator


def _has_tenant_header(operation):
    return any(
        p['name'] == 'X-Tenant-ID' for p in operation.get('parameters', [])
    )


def test_tenant_header_only_on_tenant_scoped_endpoints():
    """The X-Tenant-ID header must be documented exactly where HasActiveMembership
    resolves a tenant through it, and nowhere else."""
    paths = SchemaGenerator().get_schema(request=None, public=True)['paths']

    assert _has_tenant_header(paths['/api/clients/']['get'])
    assert _has_tenant_header(paths['/api/appointments/']['post'])
    assert _has_tenant_header(paths['/api/auth/invitations/']['get'])

    # Public / identity-only endpoints never resolve a tenant from the header.
    assert not _has_tenant_header(paths['/api/auth/login/']['post'])
    assert not _has_tenant_header(paths['/api/auth/me/']['get'])
