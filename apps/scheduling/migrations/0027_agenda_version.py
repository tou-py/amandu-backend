import django.db.models.deletion
from django.db import migrations, models

# Every table the agenda reads, directly or through billing. Most carry
# tenant_id; the two link tables reach it through their parent.
TABLES = (
    'tb_appointment', 'tb_appointment_client', 'tb_cash_entry', 'tb_client',
    'tb_membership', 'tb_plan', 'tb_plan_categories', 'tb_service', 'tb_subscription',
)

BUMP = """
CREATE FUNCTION kyo_bump_agenda() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    changed record;
    tenant bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN changed := OLD; ELSE changed := NEW; END IF;
    IF TG_TABLE_NAME = 'tb_appointment_client' THEN
        SELECT tenant_id INTO tenant FROM tb_appointment WHERE id = changed.appointment_id;
    ELSIF TG_TABLE_NAME = 'tb_plan_categories' THEN
        SELECT tenant_id INTO tenant FROM tb_plan WHERE id = changed.plan_id;
    ELSE
        tenant := changed.tenant_id;
    END IF;
    -- Through tb_tenant, so a row going down with its business (the cascade
    -- runs before these deferred triggers) does not resurrect the counter.
    INSERT INTO tb_agenda_version (tenant_id, version)
        SELECT id, 1 FROM tb_tenant WHERE id = tenant
        ON CONFLICT (tenant_id) DO UPDATE SET version = tb_agenda_version.version + 1;
    RETURN NULL;
END $$;
"""

TRIGGERS = ''.join(
    f'CREATE CONSTRAINT TRIGGER kyo_agenda_changed AFTER INSERT OR UPDATE OR DELETE ON {table} '
    f'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION kyo_bump_agenda();\n'
    for table in TABLES
)
DROP = ''.join(f'DROP TRIGGER kyo_agenda_changed ON {table};\n' for table in TABLES)


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0026_service_carousel_images'),
        ('tenancy', '0016_tenant_addon_caja'),
        ('accounting', '0007_caja_and_salidas'),
        ('accounts', '0018_customuser_terms'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgendaVersion',
            fields=[
                ('tenant', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, serialize=False, to='tenancy.tenant')),
                ('version', models.BigIntegerField(default=0)),
            ],
            options={
                'db_table': 'tb_agenda_version',
            },
        ),
        migrations.RunSQL(BUMP + TRIGGERS, DROP + 'DROP FUNCTION kyo_bump_agenda();'),
    ]
